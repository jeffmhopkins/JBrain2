"""Fish-id tool handler against real Postgres + FakeFishIdentifier (no fishial service,
no network).

`identify_fish` resolves its photo BY a chat attachment id AND BY a prior generated id,
runs the (faked) classifier, returns a `fish_identification` view, and FREES the model
after every call (load → use → unload); an empty result is a clean "no fish" message
with no view; a service error is a clean message that still frees; and neither/both/
unknown/non-uuid sources are clean errors that never reach the model.
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
from jbrain.agent.fishtools import build_fish_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.fish_id.catalog import CATALOG
from jbrain.fish_id.client import FishIdError, FishResult
from jbrain.fish_id.fake import FakeFishIdentifier
from jbrain.models.images import GeneratedImageRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeFishIdGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class MemBlobStore:
    """A minimal in-memory BlobStore: content-addressed put/get; the rest rounds out
    the protocol but is unused here."""

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
            raise FileNotFoundError(sha256) from exc

    def path_for(self, sha256: str) -> Path:
        return Path(sha256)

    async def exists(self, sha256: str) -> bool:
        return sha256 in self._blobs

    def usage(self) -> tuple[int, int]:
        return len(self._blobs), sum(len(b) for b in self._blobs.values())


class _BoomIdentifier(FakeFishIdentifier):
    """A FishIdentifier that fails the way an unreachable service does."""

    async def identify(self, image: bytes, top_k: int = 5) -> FishResult:
        raise FishIdError("connect to fish-id:8200 refused")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
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
    """A jerv-style tool context: owner identity, EMPTY read scopes, carrying the chat
    session id so the resolver can widen for attachments."""
    return ToolContext(
        session=read_context(owner.principal_id, ()), scopes=(), agent_session_id=session_id
    )


def _handlers(
    maker: async_sessionmaker,
    identifier: FakeFishIdentifier,
    gateway: FakeFishIdGateway,
    blobs: MemBlobStore | None = None,
):
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    return build_fish_handlers(
        identifier,
        gateway,
        blobs or MemBlobStore(),
        GeneratedImageRepo(),
        attachments,
        maker,
        CATALOG[0],
    )


async def _attach(maker: async_sessionmaker, owner: SessionContext, blobs: MemBlobStore):
    """A jerv chat session with one image attachment (stamped 'general'); returns
    (session_id, attachment_id, sha)."""
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    att_ctx = await attachments.session_read_context(owner, info.id)
    assert att_ctx is not None
    sha = await blobs.put(b"\x89PNG\r\n\x1a\nfish-photo")
    att = await attachments.add(
        att_ctx,
        info.id,
        sha256=sha,
        filename="fish.png",
        media_type="image/png",
        size_bytes=10,
        domain_code="general",
    )
    return info.id, att.id, sha


async def test_identify_by_attachment_returns_view_and_frees(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs = MemBlobStore()
    identifier, gateway = FakeFishIdentifier(), FakeFishIdGateway()
    handlers = _handlers(maker, identifier, gateway, blobs)
    session_id, att_id, _ = await _attach(maker, owner, blobs)

    out = await handlers["identify_fish"](
        {"source_attachment_id": att_id, "top_k": 3}, _ctx(owner, session_id)
    )

    assert isinstance(out, ToolOutput) and isinstance(out.view, ViewPayload)
    assert out.view.view == "fish_identification"
    data = out.view.data
    assert data["thumb_id"] == att_id and data["thumb_kind"] == "attachment"
    assert data["top"]["species"] == "Zebrasoma flavescens"
    assert data["arch"] == CATALOG[0].arch
    assert "Zebrasoma flavescens" in out  # the prose names the top match for the model
    assert identifier.last_image == b"\x89PNG\r\n\x1a\nfish-photo"  # the photo's bytes drove it
    assert identifier.last_top_k == 3
    assert gateway.frees == 1  # load → use → unload: freed after the identification


async def test_identify_by_generated_image_id(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs = MemBlobStore()
    handlers = _handlers(maker, FakeFishIdentifier(), FakeFishIdGateway(), blobs)
    # Seed a generated image row whose blob is in the store.
    sha = await blobs.put(b"\x89PNG\r\n\x1a\ngenerated-fish")
    async with scoped_session(maker, owner) as s:
        row = await GeneratedImageRepo().insert(
            s,
            blob_sha256=sha,
            kind="generate",
            model="qwen-image-2512",
            prompt="a fish",
            source_sha256=None,
            width=64,
            height=64,
            steps=20,
            seed=1,
        )

    out = await handlers["identify_fish"]({"source_image_id": str(row.id)}, _ctx(owner))
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["thumb_kind"] == "image"


async def test_no_fish_is_a_clean_message_no_view(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs = MemBlobStore()
    identifier = FakeFishIdentifier(FishResult(()))  # the model found nothing
    gateway = FakeFishIdGateway()
    handlers = _handlers(maker, identifier, gateway, blobs)
    session_id, att_id, _ = await _attach(maker, owner, blobs)

    out = await handlers["identify_fish"]({"source_attachment_id": att_id}, _ctx(owner, session_id))
    assert isinstance(out, str) and not isinstance(out, ToolOutput)  # no view
    assert "couldn't find a fish" in out.lower()
    assert gateway.frees == 1  # still unloaded


async def test_service_error_is_clean_and_still_frees(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs = MemBlobStore()
    gateway = FakeFishIdGateway()
    handlers = _handlers(maker, _BoomIdentifier(), gateway, blobs)
    session_id, att_id, _ = await _attach(maker, owner, blobs)

    out = await handlers["identify_fish"]({"source_attachment_id": att_id}, _ctx(owner, session_id))
    assert isinstance(out, str) and not isinstance(out, ToolOutput)
    assert "host" not in out and "8200" not in out  # never leak the loopback host:port
    assert "didn't respond" in out
    assert gateway.frees == 1  # the unload tail runs even on a service error


@pytest.mark.parametrize(
    "args",
    [
        {},  # neither source
        {"source_image_id": "a", "source_attachment_id": "b"},  # both
        {"source_image_id": "00000000-0000-0000-0000-000000000000"},  # unknown id
    ],
)
async def test_bad_source_is_clean_error_and_no_inference(
    maker: async_sessionmaker, args: dict
) -> None:
    owner = await _owner(maker)
    identifier, gateway = FakeFishIdentifier(), FakeFishIdGateway()
    handlers = _handlers(maker, identifier, gateway)

    out = await handlers["identify_fish"](args, _ctx(owner))
    assert isinstance(out, str) and not isinstance(out, ToolOutput)
    assert identifier.calls == 0  # the model was never run
    assert gateway.frees == 0  # nothing loaded → nothing to free


async def test_non_uuid_source_is_a_clean_miss(maker: async_sessionmaker) -> None:
    """A model guessing a non-uuid id under a REAL session reads as a clean miss, never a
    raw DB DataError, and the classifier never runs."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    identifier, gateway = FakeFishIdentifier(), FakeFishIdGateway()
    handlers = _handlers(maker, identifier, gateway)

    out = await handlers["identify_fish"]({"source_attachment_id": "latest"}, _ctx(owner, info.id))
    assert out == "No attached image with that id is in this chat."
    assert identifier.calls == 0
