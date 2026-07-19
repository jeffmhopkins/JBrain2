"""`compare_images` against real Postgres (migrations 0077 + 0139): resolve N chat
images by id (owner-only), run one faked vision compare, and ALWAYS persist + show a
`provenance='compare'` side-by-side the owner can verify. Error paths (fewer than two
images, a foreign id, an empty prompt, too many) too.

The vision model is faked; the source resolution, stitch, persistence, and RLS are
exercised for real."""

import io
from collections.abc import AsyncIterator

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.chat_images import PROVENANCE_FRAME, persist_chat_image
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.visiontools import MAX_COMPARE_IMAGES, build_compare_handlers
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.images import GeneratedImageRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SESSION = "11111111-1111-1111-1111-111111111111"


def _png(w: int = 64, h: int = 48, color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        key = f"sha-{len(self.data)}-{len(data)}"
        self.data[key] = data
        return key

    async def get(self, sha256: str) -> bytes:
        try:
            return self.data[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc


class FakeAttachments:
    async def session_read_context(self, ctx, agent_session_id):  # pragma: no cover - unused here
        return None

    async def get(self, ctx, attachment_id):  # pragma: no cover - unused here
        return None


def _router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter({"xai": fake}, {"agent.vision": ("xai", "grok-4.3")})


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _store_image(maker, owner, blobs, repo, data: bytes) -> str:
    row = await persist_chat_image(
        maker,
        owner,
        blobs,
        repo,
        data=data,
        provenance=PROVENANCE_FRAME,
        model="ffmpeg",
        prompt="f",
    )
    return str(row.id)


def _ctx(owner: SessionContext) -> ToolContext:
    return ToolContext(session=owner, scopes=(), agent_session_id=SESSION)


async def test_compare_persists_and_shows_a_side_by_side(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs, repo = FakeBlobs(), GeneratedImageRepo()
    id_a = await _store_image(maker, owner, blobs, repo, _png(60, 40, (200, 30, 30)))
    id_b = await _store_image(maker, owner, blobs, repo, _png(80, 40, (30, 30, 200)))
    fake = FakeLlmClient(["Image 1 is a Metropolis; image 2 is a Metropolix — different panels."])
    handlers = build_compare_handlers(_router(fake), blobs, repo, FakeAttachments(), maker)

    out = await handlers["compare_images"](
        {"prompt": "same module?", "image_ids": [id_a, id_b]}, _ctx(owner)
    )

    assert isinstance(out, ToolOutput)
    assert "Metropolix" in out  # the vision model's comparison text
    # The owner sees a side-by-side, stamped 'compare', hidden from the gallery.
    assert out.view is not None and out.view.data["provenance"] == "compare"
    stitch_id = str(out.view.data["image_id"])
    async with scoped_session(maker, owner) as session:
        row = await repo.get(session, stitch_id)
        gallery = await repo.list(session, limit=1000)
    assert row is not None and row.provenance == "compare"
    assert row.width == 60 + 80 + 8  # the two frames composed at shared height + gap
    assert stitch_id not in {str(r.id) for r in gallery}


async def test_show_false_suppresses_the_side_by_side(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs, repo = FakeBlobs(), GeneratedImageRepo()
    ids = [await _store_image(maker, owner, blobs, repo, _png()) for _ in range(2)]
    handlers = build_compare_handlers(
        _router(FakeLlmClient(["similar"])), blobs, repo, FakeAttachments(), maker
    )
    out = await handlers["compare_images"](
        {"prompt": "?", "image_ids": ids, "show": False}, _ctx(owner)
    )
    assert isinstance(out, ToolOutput) and out.view is None  # text only, no card


async def test_error_paths(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs, repo = FakeBlobs(), GeneratedImageRepo()
    one = await _store_image(maker, owner, blobs, repo, _png())
    handlers = build_compare_handlers(
        _router(FakeLlmClient(["x"])), blobs, repo, FakeAttachments(), maker
    )

    # Empty prompt, and fewer than two images → clean rule messages, no vision call.
    assert "needs a prompt" in await handlers["compare_images"]({"image_ids": [one]}, _ctx(owner))
    assert "at least two" in await handlers["compare_images"](
        {"prompt": "?", "image_ids": [one]}, _ctx(owner)
    )
    # Too many images is refused up front.
    many = [one] * (MAX_COMPARE_IMAGES + 1)
    assert "at most" in await handlers["compare_images"](
        {"prompt": "?", "image_ids": many}, _ctx(owner)
    )
    # A foreign / unknown id is a clean miss, nothing stored.
    bad = await handlers["compare_images"](
        {"prompt": "?", "image_ids": [one, "cccccccc-cccc-cccc-cccc-cccccccccccc"]}, _ctx(owner)
    )
    assert "No generated image" in bad
