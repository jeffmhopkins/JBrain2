"""`fetch_image` against real Postgres (migrations 0077 + 0139): a fetched image
(fetcher faked) is validated and persisted as a `provenance='web_fetch'` row that
analyze_image/compare_images resolve by id and the gallery hides; a non-image body is
refused with nothing stored; `show=false` suppresses the card; the origin URL rides
back as a WebSource citation.

The fetcher is faked (no network); the validation + persistence + RLS are exercised for
real, since that is the part real Postgres validates. The redirect-safe byte path itself
is unit-tested in tests/unit/test_web.py."""

import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.fetchtools import build_fetch_image_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.images import GeneratedImageRepo
from jbrain.web.fetch import WebFetchError
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SESSION = "11111111-1111-1111-1111-111111111111"


def _png(w: int = 96, h: int = 72) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class FakeFetcher:
    """Stands in for WebFetcher: fetch_bytes returns canned (content_type, body) or raises."""

    def __init__(self, *, body: bytes = b"", content_type: str = "", error: str = "") -> None:
        self._body = body
        self._ct = content_type
        self._error = error
        self.calls: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int = 10_000_000) -> tuple[str, bytes]:
        self.calls.append(url)
        if self._error:
            raise WebFetchError(self._error)
        return self._ct, self._body


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        key = f"sha-{len(self.data)}-{len(data)}"
        self.data[key] = data
        return key


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


def _handlers(maker: async_sessionmaker, fetcher: Any):
    # The fakes stand in for WebFetcher / BlobStore (the repo test pattern); typed Any at
    # the seam so pyright accepts the structural stand-ins.
    blobs: Any = FakeBlobs()
    return build_fetch_image_handlers(fetcher, blobs, GeneratedImageRepo(), maker)


def _ctx(owner: SessionContext) -> ToolContext:
    return ToolContext(session=owner, scopes=(), agent_session_id=SESSION)


async def test_fetches_and_persists_web_fetch_row(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    fetcher = FakeFetcher(body=_png(96, 72), content_type="image/png")
    out = await _handlers(maker, fetcher)["fetch_image"](
        {"url": "https://intellijel.com/metropolis.png"}, _ctx(owner)
    )

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["provenance"] == "web_fetch" and out.view.data["width"] == 96
    image_id = str(out.view.data["image_id"])
    assert f"source_image_id {image_id}" in out
    # The origin URL is a real citation, not model-authored prose.
    assert [s.url for s in out.web_sources] == ["https://intellijel.com/metropolis.png"]

    async with scoped_session(maker, owner) as session:
        row = await GeneratedImageRepo().get(session, image_id)
        gallery = await GeneratedImageRepo().list(session, limit=1000)
    assert row is not None and row.provenance == "web_fetch"
    assert image_id not in {str(r.id) for r in gallery}  # hidden from the gallery


async def test_non_image_body_is_refused_and_nothing_is_stored(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    # An HTML error page served with a lying content-type must NOT be stored as "an image".
    fetcher = FakeFetcher(
        body=b"<!doctype html><title>404 Not Found</title>", content_type="image/png"
    )

    async with scoped_session(maker, owner) as session:
        before = (
            await session.execute(text("SELECT count(*) FROM app.generated_images"))
        ).scalar() or 0

    out = await _handlers(maker, fetcher)["fetch_image"]({"url": "https://x/404"}, _ctx(owner))
    assert isinstance(out, str) and "didn't return an image" in out

    async with scoped_session(maker, owner) as session:
        after = (
            await session.execute(text("SELECT count(*) FROM app.generated_images"))
        ).scalar() or 0
    assert after == before  # nothing persisted


async def test_show_false_and_fetch_error_are_clean(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    ok = FakeFetcher(body=_png(), content_type="image/png")
    out = await _handlers(maker, ok)["fetch_image"](
        {"url": "https://x/a.png", "show": False}, _ctx(owner)
    )
    assert isinstance(out, ToolOutput) and out.view is None and "image_id" in out

    boom = FakeFetcher(error="that URL could not be fetched right now")
    err = await _handlers(maker, boom)["fetch_image"]({"url": "https://x/nope"}, _ctx(owner))
    assert isinstance(err, str) and "could not be fetched" in err

    empty = await _handlers(maker, ok)["fetch_image"]({"url": ""}, _ctx(owner))
    assert "needs a url" in empty
