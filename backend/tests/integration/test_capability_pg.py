"""Capability-token (debug console) auth + the read-only-SQL guarantee against
real Postgres (migration 0083).

Proves the SQL filtering the in-memory fake stands in for: a minted token
authenticates kind-isolated, honours expiry + revocation, stamps last_used_at —
and that the read-only transaction the /api/debug/sql route opens truly blocks
writes while allowing reads, even under an owner RLS context.
"""

import io
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api import debug
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import InvalidCredentials, PrincipalInfo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.types import LlmResult, LlmUsage
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_mint_authenticate_is_kind_isolated(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    key, record = await auth_service.mint_capability(repo, "claude", ttl_hours=24)

    principal = await auth_service.authenticate_capability(repo, key)
    assert principal is not None
    assert principal.id == record.id and principal.kind == "capability_token"

    # The capability key authenticates ONLY on its own path: presented to owner
    # login or the device path it is rejected (kind filter + a distinct key hash).
    with pytest.raises(InvalidCredentials):
        await auth_service.login(repo, key, "x")
    assert await auth_service.authenticate_device(repo, key) is None
    # And an owner key never resolves on the capability path.
    owner_key = await auth_service.rotate_owner_key(repo)
    assert await auth_service.authenticate_capability(repo, owner_key) is None


async def test_expiry_and_revocation_fail_closed(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    expired_key, _ = await auth_service.mint_capability(repo, "old", ttl_hours=-1)
    assert await auth_service.authenticate_capability(repo, expired_key) is None

    live_key, live = await auth_service.mint_capability(repo, "live", ttl_hours=24)
    assert await auth_service.authenticate_capability(repo, live_key) is not None
    # last_used_at is stamped on the successful auth.
    listed = {t.id: t for t in await repo.list_capabilities()}
    assert listed[live.id].last_used_at is not None

    assert await repo.revoke_capability(live.id) is True
    assert await auth_service.authenticate_capability(repo, live_key) is None
    assert await repo.revoke_capability(live.id) is False


async def test_has_active_capability_tracks_the_live_set(maker: async_sessionmaker) -> None:
    # The signal that switches on the wall's verbose TTS trace: True iff SOME token is live,
    # with the same fail-closed liveness as auth (expiry, revoke, suspend all drop it).
    repo = SqlAuthRepo(maker)
    # Sibling tests share this DB and leave live tokens behind, so start from a known-empty
    # active set by revoking any that are still live.
    for t in await repo.list_capabilities():
        await repo.revoke_capability(t.id)
    assert await repo.has_active_capability() is False  # no live token

    await auth_service.mint_capability(repo, "expired", ttl_hours=-1)
    assert await repo.has_active_capability() is False  # an expired token doesn't count

    _, live = await auth_service.mint_capability(repo, "live", ttl_hours=24)
    assert await repo.has_active_capability() is True

    assert await repo.suspend_capability(live.id) is True
    assert await repo.has_active_capability() is False  # suspended -> not active
    assert await repo.resume_capability(live.id) is True
    assert await repo.has_active_capability() is True

    assert await repo.revoke_capability(live.id) is True
    assert await repo.has_active_capability() is False  # last live token gone


async def test_suspend_and_resume_fail_closed_then_restore(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    key, record = await auth_service.mint_capability(repo, "pausable", ttl_hours=24)
    assert await auth_service.authenticate_capability(repo, key) is not None

    # Suspend freezes auth against real Postgres (the suspended_at filter), and the
    # owner list surfaces the stamp; a second suspend reports no row changed.
    assert await repo.suspend_capability(record.id) is True
    assert await auth_service.authenticate_capability(repo, key) is None
    listed = {t.id: t for t in await repo.list_capabilities()}
    assert listed[record.id].suspended_at is not None
    assert await repo.suspend_capability(record.id) is False

    # Resume clears the stamp; the same key authenticates again.
    assert await repo.resume_capability(record.id) is True
    assert await auth_service.authenticate_capability(repo, key) is not None
    assert await repo.resume_capability(record.id) is False

    # A revoked token can be neither suspended nor resumed (stays dead).
    assert await repo.revoke_capability(record.id) is True
    assert await repo.suspend_capability(record.id) is False
    assert await repo.resume_capability(record.id) is False


async def test_read_only_transaction_allows_reads_blocks_writes(maker: async_sessionmaker) -> None:
    # A read works under the read-only owner context the debug SQL route uses.
    async with scoped_session(maker, _DEBUG_OWNER) as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = (await session.execute(text("SELECT code FROM app.domains ORDER BY code"))).all()
    assert ("general",) in rows

    # A write in the same read-only transaction is rejected by the engine — the
    # guarantee the route leans on so full owner read can never become a write.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, _DEBUG_OWNER) as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("UPDATE app.domains SET name = name"))


def _request(maker: async_sessionmaker) -> SimpleNamespace:
    """A minimal stand-in for the FastAPI Request the debug SQL route reads
    app.state off of — enough to drive run_sql directly against real Postgres. The
    `state` namespace is where the route stashes its activity-feed detail (the real
    Request always has one; this stub supplies it)."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)),
        state=SimpleNamespace(),
    )


async def test_run_sql_route_reads_and_coerces_types(maker: async_sessionmaker) -> None:
    # A real owner-context read through the route: uuid + timestamptz exercise the
    # JSON coercion, and an owner-only table proves full read (no domain firewall).
    _, record = await auth_service.mint_capability(maker_repo := SqlAuthRepo(maker), "x", 24)
    principal = PrincipalInfo(id=record.id, kind="capability_token", label="x")
    out = await debug.run_sql(
        debug.SqlRequest(sql="SELECT id, kind, created_at FROM app.principals", max_rows=10),
        _request(maker),  # type: ignore[arg-type]
        principal,
    )
    assert out.columns == ["id", "kind", "created_at"]
    assert out.row_count >= 1
    # uuid + datetime came back as JSON-safe strings, not raw objects.
    first = out.rows[0]
    assert isinstance(first[0], str) and isinstance(first[2], str)
    assert "capability_token" in {row[1] for row in out.rows}
    assert maker_repo is not None


async def test_run_sql_route_blocks_a_write(maker: async_sessionmaker) -> None:
    principal = PrincipalInfo(id="x", kind="capability_token", label="x")
    with pytest.raises(HTTPException) as exc:
        await debug.run_sql(
            debug.SqlRequest(sql="UPDATE app.domains SET name = name"),
            _request(maker),  # type: ignore[arg-type]
            principal,
        )
    assert exc.value.status_code == 400


async def test_run_sql_route_maps_a_sql_error_to_400(maker: async_sessionmaker) -> None:
    # A statement that passes the read-only guard but errors at execution (unknown
    # table) becomes a clean 400 rather than a 500.
    principal = PrincipalInfo(id="x", kind="capability_token", label="x")
    with pytest.raises(HTTPException) as exc:
        await debug.run_sql(
            debug.SqlRequest(sql="SELECT * FROM app.does_not_exist"),
            _request(maker),  # type: ignore[arg-type]
            principal,
        )
    assert exc.value.status_code == 400


# --- vision route (drive vision.* over an on-box attachment) ----------------


class _FakeVisionRouter:
    """The two router methods the vision route touches; echoes the user prompt so
    the test can confirm the call reached the adapter."""

    async def effective_spec(self, task: str, strength: str | None = None) -> tuple[str, str]:
        return ("local", "qwen3-vl-30b")

    async def complete(self, task: str, **kw: object) -> LlmResult:
        return LlmResult(
            text=f"caption:{kw['user_text']}",
            parsed=None,
            usage=LlmUsage(input_tokens=3, output_tokens=5),
        )


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (120, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _vision_request(maker: async_sessionmaker, blobs: FsBlobStore) -> SimpleNamespace:
    """The Request stand-in the vision route reads app.state off of: the session
    maker (attachment lookup), the LLM router (egress), and the blob store (bytes)."""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_maker=maker, llm_router=_FakeVisionRouter(), blob_store=blobs
            )
        ),
        state=SimpleNamespace(),
    )


async def test_vision_route_reads_a_real_attachment_and_runs(
    maker: async_sessionmaker, tmp_path: object
) -> None:
    # Seed a note + attachment + blob, then drive the route exactly as the console
    # does: it looks the attachment up under the read-only owner context and runs
    # the routed vision task. Proves the real-Postgres round-trip end to end.
    blobs = FsBlobStore(tmp_path)  # type: ignore[arg-type]
    png = _png_bytes()
    sha = await blobs.put(png)
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"vis-{uuid4()}", domain="general", destination=None, body="pic"
    )
    att = await SqlNotesRepo(maker).add_attachment(
        OWNER,
        note_id=note.id,
        sha256=sha,
        filename="me.png",
        media_type="image/png",
        size_bytes=len(png),
    )
    assert att is not None
    principal = PrincipalInfo(id="x", kind="capability_token", label="x")
    out = await debug.vision(
        debug.VisionRequest(attachment_id=UUID(att.id), task="vision.caption"),
        _vision_request(maker, blobs),  # type: ignore[arg-type]
        principal,
    )
    assert out.text.startswith("caption:") and out.filename == "me.png"
    assert out.task == "vision.caption" and out.model == "qwen3-vl-30b"


async def test_vision_route_404s_on_a_missing_attachment(
    maker: async_sessionmaker, tmp_path: object
) -> None:
    principal = PrincipalInfo(id="x", kind="capability_token", label="x")
    with pytest.raises(HTTPException) as exc:
        await debug.vision(
            debug.VisionRequest(attachment_id=uuid4(), task="vision.caption"),
            _vision_request(maker, FsBlobStore(tmp_path)),  # type: ignore[arg-type]
            principal,
        )
    assert exc.value.status_code == 404
