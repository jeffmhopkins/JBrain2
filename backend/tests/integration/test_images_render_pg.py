"""The direct render API (Wave L3) against real Postgres + FakeImageGen (no ComfyUI, no
network). generate inserts a row + blob and returns the summary; edit by an uploaded source
AND by source_image_id records the source sha; the gallery list is owner-scoped (RLS hides a
foreign principal's rows); the MAX_EDIT_IMAGES cap holds; and a bad aspect/resolution is a 400
with no row. The owner-only firewall + the insert/list-via-API paths are exercised end to end.

Fully synchronous (TestClient + asyncio.run for direct-DB assertions): a sync TestClient call
inside an async test would block the running loop the client's portal also drives."""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.image_gen.fake import FakeImageGen
from jbrain.image_gen.render import ImageRenderService
from jbrain.main import create_app
from jbrain.models.images import GeneratedImageRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeComfyUiGateway, FakeLocalGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_T = TypeVar("_T")


def _run(coro: Awaitable[_T]) -> _T:
    """Drive an async helper to completion from a sync test (no running loop here)."""
    return asyncio.run(coro)  # type: ignore[arg-type]


class MemBlobStore:
    """In-memory content-addressed BlobStore — shared between the render service and the app's
    serve path so a generated/uploaded blob resolves by id."""

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


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


# Each direct-DB assertion opens (and disposes) its OWN engine inside one asyncio.run, because
# asyncpg connections are bound to the loop that created them — a shared engine reused across
# separate asyncio.run() calls would raise "attached to a different loop".
async def _count(url: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        m = async_sessionmaker(engine, expire_on_commit=False)
        async with scoped_session(m, await _owner_ctx(m)) as s:
            res = await s.execute(text("SELECT count(*) FROM app.generated_images"))
            return res.scalar() or 0
    finally:
        await engine.dispose()


async def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        m = async_sessionmaker(engine, expire_on_commit=False)
        async with scoped_session(m, await _owner_ctx(m)) as s:
            return (await s.execute(text(sql), params)).scalar()
    finally:
        await engine.dispose()


async def _rotate_owner_key(url: str) -> str:
    """Rotate the (global) owner key on a FRESH engine and return it. Driven from the test's
    asyncio.run loop — never `app.state.auth_repo`, whose engine is bound to the lifespan
    thread's loop (using it here would deadlock the asyncpg connection across loops)."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        return await service.rotate_owner_key(SqlAuthRepo(async_sessionmaker(engine)))
    finally:
        await engine.dispose()


@pytest.fixture
def db_url(database_url: str) -> str:  # noqa: F811
    """Clean the shared table and return the URL the sync helpers open per-call engines on."""

    async def _clean() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            m = async_sessionmaker(engine, expire_on_commit=False)
            await service.rotate_owner_key(SqlAuthRepo(m))
            owner = await _owner_ctx(m)
            async with scoped_session(m, owner) as s:
                await s.execute(text("DELETE FROM app.generated_images"))
        finally:
            await engine.dispose()

    _run(_clean())
    return database_url


@pytest.fixture
def app_client(
    db_url: str,
) -> Iterator[tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore]]:
    """An app with image hosting configured (so the render router mounts), then its render
    service swapped for one over FakeImageGen + an in-memory blob store sharing the app's serve
    path. A fresh owner key is rotated here and logged in via the cookie session."""
    fake = FakeImageGen()
    blobs = MemBlobStore()
    settings = Settings(
        secure_cookies=False, database_url=db_url, comfyui_url="http://comfyui:8188"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # The lifespan built the real maker; drive the render through the fake (no ComfyUI),
        # sharing one in-memory blob store so a render's bytes serve by id.
        app.state.blob_store = blobs
        app.state.generated_image_repo = GeneratedImageRepo()
        app.state.image_render = ImageRenderService(
            fake,
            blobs,
            app.state.generated_image_repo,
            app.state.session_maker,
            FakeLocalGateway(),
            FakeComfyUiGateway(),
            provisioned_models=(
                "qwen-image",
                "qwen-image-lightning",
                "qwen-image-edit",
                "qwen-image-edit-lightning",
            ),
        )
        owner_key = _run(_rotate_owner_key(db_url))
        login = client.post(
            "/api/auth/session", json={"owner_key": owner_key, "device_label": "it"}
        )
        assert login.status_code == 204
        yield client, app, fake, blobs


def _gen_body(**over: object) -> dict:
    body = {
        "prompt": "a red bicycle",
        "speed": "quality",
        "aspect": "square",
        "resolution": "medium",
        "steps": 20,
        "seed": None,
        "negativePrompt": "",
    }
    body.update(over)
    return body


def test_generate_inserts_row_and_returns_summary(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    client, _app, fake, blobs = app_client
    resp = client.post("/api/images/generate", json=_gen_body(aspect="portrait"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "generate"
    assert (body["width"], body["height"]) == (768, 1024)  # portrait preset
    assert body["model"] == "qwen-image-2512"
    assert isinstance(body["seed"], int)
    assert set(body) == {"id", "kind", "prompt", "width", "height", "model", "seed", "created_at"}

    # One row landed and its blob is in the shared store (the serve-by-id route is the
    # pre-existing Wave G2 FileResponse path, exercised against the real FS blob store elsewhere).
    assert _run(_count(db_url)) == 1
    assert fake.last_gen is not None
    blob_sha = _run(
        _scalar(
            db_url,
            "SELECT blob_sha256 FROM app.generated_images WHERE id = cast(:i AS uuid)",
            i=body["id"],
        )
    )
    assert isinstance(blob_sha, str) and _run(blobs.get(blob_sha))[:8] == b"\x89PNG\r\n\x1a\n"


def test_generate_bad_aspect_is_400_no_row(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    client, _app, fake, _ = app_client
    resp = client.post("/api/images/generate", json=_gen_body(aspect="hexagon"))
    assert resp.status_code == 400 and "aspect" in resp.json()["detail"]
    assert fake.last_gen is None
    assert _run(_count(db_url)) == 0


def test_generate_bad_resolution_is_400(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
) -> None:
    client, _app, _fake, _ = app_client
    resp = client.post("/api/images/generate", json=_gen_body(resolution="enormous"))
    assert resp.status_code == 400 and "resolution" in resp.json()["detail"]


def test_edit_by_uploaded_source_records_source_sha(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    client, _app, fake, _ = app_client
    upload = b"\x89PNG\r\n\x1a\nuploaded-source"
    spec = {"prompt": "make it night", "resolution": "medium", "negativePrompt": ""}
    resp = client.post(
        "/api/images/edit",
        data={"spec": json.dumps(spec)},
        files={"source": ("in.png", upload, "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "edit"
    assert fake.last_source == upload  # the uploaded bytes drove the edit

    expected_sha = hashlib.sha256(upload).hexdigest()
    recorded = _run(
        _scalar(db_url, "SELECT source_sha256 FROM app.generated_images WHERE kind='edit'")
    )
    assert recorded == expected_sha  # the uploaded source's sha is recorded on the edit row


def test_edit_by_source_image_id_records_source_sha(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    client, _app, fake, _ = app_client
    gen = client.post("/api/images/generate", json=_gen_body()).json()
    source_sha = _run(
        _scalar(
            db_url,
            "SELECT blob_sha256 FROM app.generated_images WHERE id = cast(:i AS uuid)",
            i=gen["id"],
        )
    )

    spec = {"prompt": "make it blue", "resolution": "medium", "sourceImageId": gen["id"]}
    resp = client.post("/api/images/edit", data={"spec": json.dumps(spec)})
    assert resp.status_code == 200 and resp.json()["kind"] == "edit"
    assert fake.last_edit is not None

    recorded = _run(
        _scalar(db_url, "SELECT source_sha256 FROM app.generated_images WHERE kind='edit'")
    )
    assert recorded == source_sha


def test_edit_requires_exactly_one_source(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
) -> None:
    client, _app, fake, _ = app_client
    spec = {"prompt": "x", "resolution": "medium"}
    resp = client.post("/api/images/edit", data={"spec": json.dumps(spec)})
    assert resp.status_code == 400 and "exactly one source" in resp.json()["detail"]
    assert fake.last_edit is None

    both = client.post(
        "/api/images/edit",
        data={"spec": json.dumps({"prompt": "x", "resolution": "medium", "sourceImageId": "a"})},
        files={"source": ("in.png", b"\x89PNG\r\n\x1a\nx", "image/png")},
    )
    assert both.status_code == 400


def test_edit_references_within_cap_and_rejects_excess(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
) -> None:
    client, _app, fake, _ = app_client
    src = b"\x89PNG\r\n\x1a\nsrc"
    ref1 = b"\x89PNG\r\n\x1a\nref1"
    ref2 = b"\x89PNG\r\n\x1a\nref2"
    spec = json.dumps({"prompt": "combine", "resolution": "medium"})

    ok = client.post(
        "/api/images/edit",
        data={"spec": spec},
        files=[
            ("source", ("s.png", src, "image/png")),
            ("references", ("r1.png", ref1, "image/png")),
            ("references", ("r2.png", ref2, "image/png")),
        ],
    )
    assert ok.status_code == 200
    assert fake.last_sources[0] == src and len(fake.last_sources) == 3  # primary first

    too_many = client.post(
        "/api/images/edit",
        data={"spec": spec},
        files=[
            ("source", ("s.png", src, "image/png")),
            ("references", ("r1.png", ref1, "image/png")),
            ("references", ("r2.png", ref2, "image/png")),
            ("references", ("r3.png", b"\x89PNG\r\n\x1a\nref3", "image/png")),
        ],
    )
    assert too_many.status_code == 400 and "at most" in too_many.json()["detail"]


def test_edit_rejects_non_image_upload(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
) -> None:
    client, _app, fake, _ = app_client
    resp = client.post(
        "/api/images/edit",
        data={"spec": json.dumps({"prompt": "x", "resolution": "medium"})},
        files={"source": ("evil.txt", b"not an image at all", "text/plain")},
    )
    assert resp.status_code == 400 and "unsupported image type" in resp.json()["detail"]
    assert fake.last_edit is None


def test_list_is_owner_scoped_newest_first(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    client, _app, _fake, _ = app_client
    first = client.post("/api/images/generate", json=_gen_body(prompt="first")).json()
    second = client.post("/api/images/generate", json=_gen_body(prompt="second")).json()

    listed = client.get("/api/images/generated")
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["id"] for r in rows] == [second["id"], first["id"]]  # newest-first
    assert {r["prompt"] for r in rows} == {"first", "second"}

    # A NON-OWNER principal sees zero rows — RLS hides the owner's artifacts (the same firewall
    # the route enforces via OwnerDep; here we drive repo.list on a capability-scoped session).
    async def _non_owner_view() -> list:
        engine = create_async_engine(db_url, poolclass=NullPool)
        try:
            m = async_sessionmaker(engine, expire_on_commit=False)
            non_owner = SessionContext(
                principal_kind="capability_token", domain_scopes=("general",)
            )
            async with scoped_session(m, non_owner) as s:
                return await GeneratedImageRepo().list(s, limit=100)
        finally:
            await engine.dispose()

    assert _run(_non_owner_view()) == []


def test_delete_removes_own_row_then_list_and_serve_miss(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    client, _app, _fake, _blobs = app_client
    gen = client.post("/api/images/generate", json=_gen_body()).json()

    deleted = client.delete(f"/api/images/generated/{gen['id']}")
    assert deleted.status_code == 204
    assert _run(_count(db_url)) == 0

    # The row is gone from the gallery and the serve-by-id route 404s (no oracle).
    assert client.get("/api/images/generated").json() == []
    assert client.get(f"/api/images/generated/{gen['id']}").status_code == 404


def test_delete_keeps_the_blob(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    """Blobs are content-addressed/keep-all (possibly shared), so a row delete never touches the
    store — only the row vanishes."""
    client, _app, _fake, blobs = app_client
    gen = client.post("/api/images/generate", json=_gen_body()).json()
    blob_sha = _run(
        _scalar(
            db_url,
            "SELECT blob_sha256 FROM app.generated_images WHERE id = cast(:i AS uuid)",
            i=gen["id"],
        )
    )
    before = blobs.usage()

    assert client.delete(f"/api/images/generated/{gen['id']}").status_code == 204
    assert blobs.usage() == before  # nothing removed from the store
    assert isinstance(blob_sha, str) and _run(blobs.exists(blob_sha))


def test_delete_unknown_id_is_404(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
) -> None:
    client, _app, _fake, _ = app_client
    assert client.delete(f"/api/images/generated/{uuid.uuid4()}").status_code == 404


def test_delete_on_non_owner_session_removes_nothing(
    app_client: tuple[TestClient, FastAPI, FakeImageGen, MemBlobStore],
    db_url: str,
) -> None:
    """RLS-level guard for repo.delete: the owner's row is invisible to a capability-scoped
    session (the owner-only firewall), so its DELETE matches nothing — the row survives. The HTTP
    route additionally 403s a non-owner at OwnerDep before reaching the repo, but the firewall
    holds even if a session somehow reached it."""
    client, _app, _fake, _ = app_client
    gen = client.post("/api/images/generate", json=_gen_body(prompt="owner's render")).json()

    async def _non_owner_delete() -> bool:
        engine = create_async_engine(db_url, poolclass=NullPool)
        try:
            m = async_sessionmaker(engine, expire_on_commit=False)
            non_owner = SessionContext(
                principal_kind="capability_token", domain_scopes=("general",)
            )
            async with scoped_session(m, non_owner) as s:
                return await GeneratedImageRepo().delete(s, gen["id"])
        finally:
            await engine.dispose()

    assert _run(_non_owner_delete()) is False  # nothing visible → nothing removed
    assert _run(_count(db_url)) == 1  # the owner's row survives
