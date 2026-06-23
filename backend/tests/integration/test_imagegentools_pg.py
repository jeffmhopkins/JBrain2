"""Image-gen tool handlers against real Postgres + FakeImageGen (no ComfyUI, no network).

`generate_image` inserts a row + blob and returns a `generated_image` view; `edit_image`
resolves its source BY a prior generated id AND BY a chat attachment id, recording the
source sha; a missing / doubled / unknown source is a clean error string with NO row; and an
unconfigured ComfyUI drops both tools from the built registry (graceful degrade).
"""

import hashlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.contracts import ViewPayload
from jbrain.agent.imagegentools import build_image_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.image_gen.fake import FakeImageGen
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter, resolve_tasks
from jbrain.models.images import GeneratedImageRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeComfyUiGateway, FakeLocalGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class MemBlobStore:
    """A minimal in-memory BlobStore for tests: content-addressed put/get. The handlers
    only put/get; path_for/exists/usage round out the protocol but are unused here."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self._blobs[digest] = data
        return digest

    async def get(self, sha256: str) -> bytes:
        try:
            return self._blobs[sha256]
        except KeyError as exc:
            # Match the real BlobStore contract: an absent blob raises FileNotFoundError.
            raise FileNotFoundError(sha256) from exc

    def path_for(self, sha256: str) -> Path:
        return Path(sha256)

    async def exists(self, sha256: str) -> bool:
        return sha256 in self._blobs

    def usage(self) -> tuple[int, int]:
        return len(self._blobs), sum(len(b) for b in self._blobs.values())


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    # The database is module-scoped (one DB per module), so wipe the tables these tests
    # touch before each test — counts/single-row assertions assume a clean slate. The owner
    # holds DELETE on generated_images (immutable, but deletable); attachments cascade off
    # their sessions. Done as the owner under RLS, the same firewall the code runs under.
    await service.rotate_owner_key(SqlAuthRepo(m))
    async with scoped_session(m, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")
    async with scoped_session(m, owner) as s:
        await s.execute(text("DELETE FROM app.generated_images"))
        await s.execute(text("DELETE FROM app.turn_attachments"))
        await s.execute(text("DELETE FROM app.agent_sessions"))
    yield m
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _ctx(owner: SessionContext, session_id: str | None = None) -> ToolContext:
    """A jerv-style tool context: owner identity, EMPTY read scopes (jerv reads no
    knowledge base), carrying the chat session id so edit_image can widen for attachments."""
    return ToolContext(
        session=read_context(owner.principal_id, ()), scopes=(), agent_session_id=session_id
    )


def _router(answer: str = "an analysis of the image") -> LlmRouter:
    """A real router over a canned FakeLlmClient — analyze_image's `agent.vision` call
    routes to the default (xai) client and gets back `answer`. `llm` is exposed on the
    router (`._clients['xai']`) so a test can assert the vision call carried the image."""
    return LlmRouter({"xai": FakeLlmClient(responses=[answer])}, resolve_tasks({}))


async def _handlers(
    maker: async_sessionmaker,
    owner: SessionContext,
    fake: FakeImageGen,
    comfy: FakeComfyUiGateway | None = None,
    router: LlmRouter | None = None,
    provisioned: Sequence[str] = (
        "qwen-image",
        "qwen-image-lightning",
        "qwen-image-edit",
        "qwen-image-edit-lightning",
    ),
):
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    return build_image_handlers(
        fake,
        MemBlobStore(),
        GeneratedImageRepo(),
        attachments,
        maker,
        FakeLocalGateway(),
        comfy or FakeComfyUiGateway(),
        router or _router(),
        provisioned,
    )


async def test_generate_inserts_row_and_returns_view(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    out = await handlers["generate_image"](
        {"prompt": "a red bicycle", "aspect": "portrait"}, _ctx(owner)
    )

    assert isinstance(out, ToolOutput)
    assert isinstance(out.view, ViewPayload)
    assert out.view.view == "generated_image"
    assert out.view.surface == "inline"
    data = out.view.data
    assert data["kind"] == "generate"
    assert data["prompt"] == "a red bicycle"
    assert (data["width"], data["height"]) == (768, 1024)  # portrait preset
    assert data["model"] == "qwen-image-2512"
    assert isinstance(data["image_id"], str)  # uuid stringified, JSON-safe
    assert "image_id" in data and "/api/" not in str(data)  # data-only, no url
    # The seed rides the view (the card shows it; the PWA carries it to the next turn).
    assert isinstance(data["seed"], int)
    # The prose names the new id + seed so the model can edit/reproduce it.
    assert f"source_image_id {data['image_id']}" in out
    assert f"seed {data['seed']}" in out

    # The row + blob really landed: one owner row whose blob is in the store.
    async with scoped_session(maker, owner) as s:
        row = (await s.execute(text("SELECT count(*), max(seed) FROM app.generated_images"))).one()
    assert row[0] == 1
    assert fake.last_gen is not None and fake.last_gen.seed == row[1]  # resolved seed recorded
    assert data["seed"] == row[1]  # the view's seed is the resolved/recorded one


async def test_generate_plumbs_steps_and_negative_prompt_into_the_spec(
    maker: async_sessionmaker,
) -> None:
    """A quality `steps` value rides the spec (clamped to the band) and a negative prompt does
    too; both flow from the tool arguments through to the image model."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    await handlers["generate_image"](
        {"prompt": "a fox", "steps": 35, "negative_prompt": "blurry, text"}, _ctx(owner)
    )
    assert fake.last_gen is not None
    assert fake.last_gen.steps == 35  # an in-band quality steps passes through
    assert fake.last_gen.negative_prompt == "blurry, text"

    # Absent steps → the 20-step default; absent negative prompt → empty.
    await handlers["generate_image"]({"prompt": "a fox"}, _ctx(owner))
    assert fake.last_gen.steps == 20 and fake.last_gen.negative_prompt == ""


async def test_generate_fast_uses_the_qwen_lightning_model_at_four_steps(
    maker: async_sessionmaker,
) -> None:
    """speed: fast records the Qwen Lightning model on the row at a fixed 4 steps, while the
    default stays the full Qwen model on the quality band at the 20-step default."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    out = await handlers["generate_image"](
        {"prompt": "a quick sketch", "speed": "fast"}, _ctx(owner)
    )
    assert isinstance(out, ToolOutput) and isinstance(out.view, ViewPayload)
    assert out.view.data["model"] == "qwen-image-lightning"
    assert fake.last_gen is not None and fake.last_gen.model == "qwen-image-lightning"
    assert fake.last_gen.steps == 4  # the Lightning path is a fixed 4 steps

    # The row really recorded the fast model; the default request stays on quality Qwen.
    async with scoped_session(maker, owner) as s:
        model = (await s.execute(text("SELECT model FROM app.generated_images"))).scalar()
    assert model == "qwen-image-lightning"

    await handlers["generate_image"]({"prompt": "a finished piece"}, _ctx(owner))
    assert fake.last_gen.model == "qwen-image-2512" and fake.last_gen.steps == 20


async def test_generate_fast_when_lightning_not_installed_is_a_clean_actionable_error(
    maker: async_sessionmaker,
) -> None:
    """speed: fast on a box that provisioned only quality Qwen returns an actionable message
    (and the setup command) before any spend — no row, no render, no opaque ComfyUI error."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake, provisioned=("qwen-image",))

    out = await handlers["generate_image"](
        {"prompt": "a quick sketch", "speed": "fast"}, _ctx(owner)
    )
    assert isinstance(out, str)
    assert "comfyui-setup.sh qwen-image-lightning" in out
    assert fake.last_gen is None  # bailed before driving the model
    async with scoped_session(maker, owner) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 0  # no row written


async def test_generate_dreamshaper_uses_the_sdxl_model_at_its_fixed_steps(
    maker: async_sessionmaker,
) -> None:
    """speed: dreamshaper records the SDXL model on the row at its fixed 6-step sweet spot,
    when that (separately-provisioned) model is installed."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake, provisioned=("qwen-image", "dreamshaper"))

    out = await handlers["generate_image"](
        {"prompt": "just show me something", "speed": "dreamshaper"}, _ctx(owner)
    )
    assert isinstance(out, ToolOutput) and isinstance(out.view, ViewPayload)
    assert out.view.data["model"] == "dreamshaper"
    assert fake.last_gen is not None and fake.last_gen.model == "dreamshaper"
    assert fake.last_gen.steps == 6  # DreamShaper's fixed sweet spot, not the quality band


async def test_generate_dreamshaper_when_not_installed_is_a_clean_actionable_error(
    maker: async_sessionmaker,
) -> None:
    """speed: dreamshaper without that model provisioned bails with its own setup id (not the
    Lightning one) before any spend."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake, provisioned=("qwen-image",))

    out = await handlers["generate_image"](
        {"prompt": "a quick sketch", "speed": "dreamshaper"}, _ctx(owner)
    )
    assert isinstance(out, str)
    assert "comfyui-setup.sh dreamshaper" in out
    assert fake.last_gen is None  # bailed before driving the model


async def test_generate_frees_comfyui_after_the_render(maker: async_sessionmaker) -> None:
    # After the image is in hand, ComfyUI's resident model is unloaded so its ~39 GB
    # returns to the unified pool (for the reply's LLM reload / a follow-up edit).
    owner = await _owner(maker)
    comfy = FakeComfyUiGateway()
    handlers = await _handlers(maker, owner, FakeImageGen(), comfy)

    out = await handlers["generate_image"]({"prompt": "a kite"}, _ctx(owner))

    assert isinstance(out, ToolOutput)
    assert comfy.frees == [(True, True)]  # unload_models + free_memory


async def test_edit_records_the_actual_output_dims_not_the_preset(
    maker: async_sessionmaker,
) -> None:
    # An edit scales the source to a megapixel budget preserving its aspect, so the
    # output differs from the square preset. The row/view must carry the REAL dims
    # (read from the PNG) — otherwise the before/after frame letterboxes with a band.
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)
    gen = await handlers["generate_image"]({"prompt": "a tree"}, _ctx(owner))
    assert isinstance(gen, ToolOutput) and gen.view is not None
    source_id = gen.view.data["image_id"]

    # The edit's output is a non-square 1264x948, not the default square preset.
    fake.out_dims = (1264, 948)
    out = await handlers["edit_image"](
        {"prompt": "make it night", "source_image_id": source_id}, _ctx(owner)
    )

    assert isinstance(out, ToolOutput) and out.view is not None
    assert (out.view.data["width"], out.view.data["height"]) == (1264, 948)


async def test_generate_resolution_scales_the_rendered_dims(maker: async_sessionmaker) -> None:
    # `small` renders below the native default — the lighter latent that buys
    # unified-memory headroom — and the chosen dims reach both the spec and the row.
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    out = await handlers["generate_image"](
        {"prompt": "a teapot", "aspect": "square", "resolution": "small"}, _ctx(owner)
    )

    assert isinstance(out, ToolOutput) and out.view is not None
    assert (out.view.data["width"], out.view.data["height"]) == (768, 768)
    assert fake.last_gen is not None
    assert (fake.last_gen.width, fake.last_gen.height) == (768, 768)


async def test_generate_rejects_an_unknown_resolution(maker: async_sessionmaker) -> None:
    # A bad resolution is a clean tool-error string — no row, no spend.
    owner = await _owner(maker)
    handlers = await _handlers(maker, owner, FakeImageGen())

    out = await handlers["generate_image"](
        {"prompt": "a teapot", "resolution": "enormous"}, _ctx(owner)
    )

    assert out == "resolution must be one of: small, medium, large."
    async with scoped_session(maker, owner) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 0


async def test_edit_by_generated_id_records_source(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    gen = await handlers["generate_image"]({"prompt": "a cat"}, _ctx(owner))
    assert isinstance(gen, ToolOutput) and gen.view is not None
    source_id = gen.view.data["image_id"]
    # The first generation's stored blob is the edit's source.
    async with scoped_session(maker, owner) as s:
        source_sha = (
            await s.execute(
                text("SELECT blob_sha256 FROM app.generated_images WHERE id = cast(:i AS uuid)"),
                {"i": source_id},
            )
        ).scalar()

    out = await handlers["edit_image"](
        {"prompt": "make it blue", "source_image_id": source_id}, _ctx(owner)
    )
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["kind"] == "edit"
    assert fake.last_edit is not None  # the image model was driven with the source bytes

    async with scoped_session(maker, owner) as s:
        edit_source = (
            await s.execute(
                text("SELECT source_sha256 FROM app.generated_images WHERE kind = 'edit'")
            )
        ).scalar()
    assert edit_source == source_sha  # the source blob's sha is recorded on the edit row


async def test_edit_fast_uses_the_lightning_model_at_four_steps(maker: async_sessionmaker) -> None:
    """speed: fast on edit records the Qwen-Edit Lightning model at a fixed 4 steps; the default
    stays the full edit model on the quality band at the 20-step default."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    gen = await handlers["generate_image"]({"prompt": "a cat"}, _ctx(owner))
    assert isinstance(gen, ToolOutput) and gen.view is not None
    source_id = gen.view.data["image_id"]

    out = await handlers["edit_image"](
        {"prompt": "make it blue", "source_image_id": source_id, "speed": "fast"}, _ctx(owner)
    )
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["model"] == "qwen-image-edit-lightning"
    assert fake.last_edit is not None
    assert fake.last_edit.model == "qwen-image-edit-lightning" and fake.last_edit.steps == 4

    # The quality default stays on the full edit model at the 20-step default.
    await handlers["edit_image"](
        {"prompt": "and add a hat", "source_image_id": source_id}, _ctx(owner)
    )
    assert fake.last_edit.model == "qwen-image-edit" and fake.last_edit.steps == 20


async def test_edit_fast_when_lightning_not_installed_is_a_clean_actionable_error(
    maker: async_sessionmaker,
) -> None:
    """speed: fast on edit, on a box without the Lightning edit model, bails with an actionable
    message before any render — same guard as the generate fast path."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake, provisioned=("qwen-image", "qwen-image-edit"))

    gen = await handlers["generate_image"]({"prompt": "a cat"}, _ctx(owner))
    assert isinstance(gen, ToolOutput) and gen.view is not None
    out = await handlers["edit_image"](
        {"prompt": "make it blue", "source_image_id": gen.view.data["image_id"], "speed": "fast"},
        _ctx(owner),
    )
    assert isinstance(out, str)
    assert "comfyui-setup.sh qwen-image-edit-lightning" in out
    assert fake.last_edit is None  # bailed before driving the model


async def test_edit_with_reference_images_passes_every_source(maker: async_sessionmaker) -> None:
    """A multi-image edit resolves the primary plus a generated AND an attached reference,
    and drives the model with all three image blobs in order (primary first)."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    blobs = MemBlobStore()
    handlers = build_image_handlers(
        fake,
        blobs,
        GeneratedImageRepo(),
        attachments,
        maker,
        FakeLocalGateway(),
        FakeComfyUiGateway(),
        _router(),
    )
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")

    # The primary + one generated reference: generate two images first.
    primary = await handlers["generate_image"]({"prompt": "a room"}, _ctx(owner, info.id))
    ref_gen = await handlers["generate_image"]({"prompt": "a lamp"}, _ctx(owner, info.id))
    assert isinstance(primary, ToolOutput) and isinstance(ref_gen, ToolOutput)
    assert primary.view is not None and ref_gen.view is not None
    primary_id, ref_gen_id = primary.view.data["image_id"], ref_gen.view.data["image_id"]

    # …and one attached reference.
    att_ctx = await attachments.session_read_context(owner, info.id)
    assert att_ctx is not None
    att_sha = await blobs.put(b"\x89PNG\r\n\x1a\nattached-ref")
    att = await attachments.add(
        att_ctx,
        info.id,
        sha256=att_sha,
        filename="ref.png",
        media_type="image/png",
        size_bytes=10,
        domain_code="general",
    )

    out = await handlers["edit_image"](
        {
            "prompt": "place the lamp in the room",
            "source_image_id": primary_id,
            "reference_image_ids": [ref_gen_id],
            "reference_attachment_ids": [att.id],
        },
        _ctx(owner, info.id),
    )
    assert isinstance(out, ToolOutput) and out.view is not None
    # Three blobs reached the adapter, primary first, then the references in order.
    assert len(fake.last_sources) == 3
    assert fake.last_sources[0] == fake.last_source  # primary stays first
    assert fake.last_sources[2] == b"\x89PNG\r\n\x1a\nattached-ref"  # the attached ref


async def test_edit_rejects_too_many_reference_images(maker: async_sessionmaker) -> None:
    """More than 2 references (3 images max) is a clean error before any spend — no render."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    handlers = await _handlers(maker, owner, fake)

    primary = await handlers["generate_image"]({"prompt": "a room"}, _ctx(owner, info.id))
    assert isinstance(primary, ToolOutput) and primary.view is not None

    out = await handlers["edit_image"](
        {
            "prompt": "combine",
            "source_image_id": primary.view.data["image_id"],
            "reference_image_ids": ["a", "b", "c"],
        },
        _ctx(owner, info.id),
    )
    assert isinstance(out, str) and "at most 3 images" in out
    assert fake.last_edit is None  # rejected before the render


async def test_edit_bad_reference_id_is_a_clean_error(maker: async_sessionmaker) -> None:
    """A reference that doesn't resolve (a non-uuid guess) is a clean miss, not a DB error,
    and the edit never runs even though the primary was valid."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    handlers = await _handlers(maker, owner, fake)

    primary = await handlers["generate_image"]({"prompt": "a room"}, _ctx(owner, info.id))
    assert isinstance(primary, ToolOutput) and primary.view is not None

    out = await handlers["edit_image"](
        {
            "prompt": "combine",
            "source_image_id": primary.view.data["image_id"],
            "reference_attachment_ids": ["latest"],
        },
        _ctx(owner, info.id),
    )
    assert out == "No attached image with that id is in this chat."
    assert fake.last_edit is None  # a bad reference stops the edit before the render


async def test_edit_by_attachment_id_records_source(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    blobs = MemBlobStore()
    handlers = build_image_handlers(
        fake,
        blobs,
        GeneratedImageRepo(),
        attachments,
        maker,
        FakeLocalGateway(),
        FakeComfyUiGateway(),
        _router(),
    )

    # A jerv chat session (empty scopes) with one image attachment (stamped 'general').
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    att_ctx = await attachments.session_read_context(owner, info.id)
    assert att_ctx is not None
    source_sha = await blobs.put(b"\x89PNG\r\n\x1a\nattached-bytes")
    att = await attachments.add(
        att_ctx,
        info.id,
        sha256=source_sha,
        filename="photo.png",
        media_type="image/png",
        size_bytes=10,
        domain_code="general",
    )

    out = await handlers["edit_image"](
        {"prompt": "add a hat", "source_attachment_id": att.id}, _ctx(owner, info.id)
    )
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["kind"] == "edit"
    assert fake.last_source == b"\x89PNG\r\n\x1a\nattached-bytes"  # the attachment's bytes drove it

    async with scoped_session(maker, owner) as s:
        edit_source = (
            await s.execute(
                text("SELECT source_sha256 FROM app.generated_images WHERE kind = 'edit'")
            )
        ).scalar()
    assert edit_source == source_sha


async def test_edit_orphan_source_blob_is_clean_error(maker: async_sessionmaker) -> None:
    """A generated row that outlives its blob (orphan) must yield a clean tool-error string,
    never a raw FileNotFoundError exposing the blob path to the model."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    blobs = MemBlobStore()
    handlers = build_image_handlers(
        fake,
        blobs,
        GeneratedImageRepo(),
        attachments,
        maker,
        FakeLocalGateway(),
        FakeComfyUiGateway(),
        _router(),
    )

    gen = await handlers["generate_image"]({"prompt": "a cat"}, _ctx(owner))
    assert isinstance(gen, ToolOutput) and gen.view is not None
    source_id = gen.view.data["image_id"]
    blobs._blobs.clear()  # evict the blob; the row now points at nothing

    out = await handlers["edit_image"](
        {"prompt": "make it blue", "source_image_id": source_id}, _ctx(owner)
    )
    assert isinstance(out, str) and not isinstance(out, ToolOutput)
    assert "no longer available" in out.lower()  # clean message, no path/stack leaked
    assert fake.last_edit is None  # the image model was never driven


@pytest.mark.parametrize(
    "args",
    [
        {"prompt": "x"},  # neither source
        {"prompt": "x", "source_image_id": "a", "source_attachment_id": "b"},  # both
        {"prompt": "x", "source_image_id": "00000000-0000-0000-0000-000000000000"},  # unknown id
    ],
)
async def test_edit_bad_source_is_clean_error_and_no_row(
    maker: async_sessionmaker, args: dict
) -> None:
    owner = await _owner(maker)
    fake = FakeImageGen()
    handlers = await _handlers(maker, owner, fake)

    out = await handlers["edit_image"](args, _ctx(owner))
    assert isinstance(out, str)
    assert not isinstance(out, ToolOutput)  # a plain error string, no view
    assert fake.last_edit is None  # the image model was never driven

    async with scoped_session(maker, owner) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 0  # nothing recorded on a refused edit


async def test_analyze_by_generated_id_returns_vision_answer(maker: async_sessionmaker) -> None:
    """analyze_image resolves a prior generated image by id, sends its bytes to the vision
    route, and returns the model's text — read-only: a plain string, no view, no new row."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    router = _router("a red bicycle leaning on a wall")
    handlers = await _handlers(maker, owner, fake, router=router)

    gen = await handlers["generate_image"]({"prompt": "a red bicycle"}, _ctx(owner))
    assert isinstance(gen, ToolOutput) and gen.view is not None
    source_id = gen.view.data["image_id"]

    out = await handlers["analyze_image"](
        {"prompt": "what is in this image?", "source_image_id": source_id}, _ctx(owner)
    )
    assert out == "a red bicycle leaning on a wall"
    assert not isinstance(out, ToolOutput)  # a read — no inline view
    # The vision route saw the question and the image bytes (one LlmImage).
    call = router._clients["xai"].calls[0]  # type: ignore[attr-defined]
    assert call["user_text"] == "what is in this image?"
    assert len(call["images"]) == 1

    async with scoped_session(maker, owner) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 1  # only the generate row — analyze inserts nothing


async def test_analyze_by_attachment_id_returns_vision_answer(maker: async_sessionmaker) -> None:
    """analyze_image reads a chat attachment by id under the widened attachment context —
    the same RLS path edit_image uses — and answers from its bytes."""
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    blobs = MemBlobStore()
    handlers = build_image_handlers(
        fake,
        blobs,
        GeneratedImageRepo(),
        attachments,
        maker,
        FakeLocalGateway(),
        FakeComfyUiGateway(),
        _router("a sign that reads OPEN"),
    )

    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    att_ctx = await attachments.session_read_context(owner, info.id)
    assert att_ctx is not None
    sha = await blobs.put(b"\x89PNG\r\n\x1a\nattached-bytes")
    att = await attachments.add(
        att_ctx,
        info.id,
        sha256=sha,
        filename="photo.png",
        media_type="image/png",
        size_bytes=10,
        domain_code="general",
    )

    out = await handlers["analyze_image"](
        {"prompt": "read the sign", "source_attachment_id": att.id}, _ctx(owner, info.id)
    )
    assert out == "a sign that reads OPEN"


async def test_analyze_needs_a_prompt_and_one_source(maker: async_sessionmaker) -> None:
    """A missing prompt or a bad source (neither/both) is a clean error string naming
    analyze_image, and never reaches the vision model."""
    owner = await _owner(maker)
    router = _router()
    handlers = await _handlers(maker, owner, FakeImageGen(), router=router)

    no_prompt = await handlers["analyze_image"]({"source_image_id": "x"}, _ctx(owner))
    assert isinstance(no_prompt, str) and "prompt" in no_prompt.lower()

    both = await handlers["analyze_image"](
        {"prompt": "what is this", "source_image_id": "a", "source_attachment_id": "b"},
        _ctx(owner),
    )
    assert isinstance(both, str) and "analyze_image" in both

    assert router._clients["xai"].calls == []  # type: ignore[attr-defined] - never spent


async def test_non_uuid_source_id_is_a_clean_miss_not_a_db_error(maker: async_sessionmaker) -> None:
    """A model guessing a non-uuid source id ("latest") under a REAL session must read as a
    clean miss, never a raw DB DataError — for both analyze_image and edit_image."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    router = _router()
    handlers = await _handlers(maker, owner, FakeImageGen(), router=router)
    ctx = _ctx(owner, info.id)  # a real session — the path that reaches the attachment query

    analyzed = await handlers["analyze_image"](
        {"prompt": "is the person female?", "source_attachment_id": "latest"}, ctx
    )
    assert analyzed == "No attached image with that id is in this chat."
    assert router._clients["xai"].calls == []  # type: ignore[attr-defined] - never spent

    edited = await handlers["edit_image"](
        {"prompt": "make it night", "source_attachment_id": "latest"}, ctx
    )
    assert edited == "No attached image with that id is in this chat."

    # A non-uuid generated id is the same clean miss (no DB argument error).
    bad_gen = await handlers["analyze_image"]({"prompt": "describe", "source_image_id": "x"}, ctx)
    assert bad_gen == "No generated image with that id is in this chat."
