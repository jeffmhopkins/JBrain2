"""Image-gen tool handlers against real Postgres + FakeImageGen (no ComfyUI, no network).

`generate_image` inserts a row + blob and returns a `generated_image` view; `edit_image`
resolves its source BY a prior generated id AND BY a chat attachment id, recording the
source sha; a missing / doubled / unknown source is a clean error string with NO row; and an
unconfigured ComfyUI drops both tools from the built registry (graceful degrade).
"""

import hashlib
from collections.abc import AsyncIterator
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
from jbrain.models.images import GeneratedImageRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeLocalGateway

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


async def _handlers(maker: async_sessionmaker, owner: SessionContext, fake: FakeImageGen):
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    return build_image_handlers(
        fake, MemBlobStore(), GeneratedImageRepo(), attachments, maker, FakeLocalGateway()
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

    # The row + blob really landed: one owner row whose blob is in the store.
    async with scoped_session(maker, owner) as s:
        row = (await s.execute(text("SELECT count(*), max(seed) FROM app.generated_images"))).one()
    assert row[0] == 1
    assert fake.last_gen is not None and fake.last_gen.seed == row[1]  # resolved seed recorded


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


async def test_edit_by_attachment_id_records_source(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    fake = FakeImageGen()
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    blobs = MemBlobStore()
    handlers = build_image_handlers(
        fake, blobs, GeneratedImageRepo(), attachments, maker, FakeLocalGateway()
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
        fake, blobs, GeneratedImageRepo(), attachments, maker, FakeLocalGateway()
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
