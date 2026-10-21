"""JShare over HTTP: the owner's routes and the public `/s/<slug>/` reads.

No database: `jshare.store` is swapped for an in-memory table, and the blobs are a real
`FsBlobStore` in a temp dir, so what is stored and served is the real bytes. RLS itself is
`tests/integration/test_jshare_rls.py`.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jshare, jshare_public
from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.jshare import store
from jbrain.jshare.content import SLUG_RE
from jbrain.locations.ratelimit import TokenBucket
from jbrain.storage import FsBlobStore
from tests.unit.fakes import FakeSettingsStore

OWNER = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")
NON_OWNER = PrincipalInfo(id="cap1", kind="capability_token", label="token")


class _Table:
    def __init__(self) -> None:
        self.rows: dict[str, store.ShareItem] = {}
        self.views: list[str] = []
        self.referenced: set[str] = set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def add(_maker, _ctx, **kw) -> store.ShareItem:  # noqa: ANN001, ANN003
            item = store.ShareItem(
                id=f"00000000-0000-0000-0000-{len(self.rows) + 1:012d}",
                created_at=datetime(2026, 10, 18, tzinfo=UTC),
                last_viewed_at=None,
                view_count=0,
                **kw,
            )
            self.rows[item.id] = item
            return item

        async def list_items(_maker, _ctx) -> list[store.ShareItem]:  # noqa: ANN001
            return list(reversed(self.rows.values()))

        async def rename(_maker, _ctx, item_id: str, title: str):  # noqa: ANN001, ANN202
            if item_id not in self.rows:
                return None
            self.rows[item_id] = replace(self.rows[item_id], title=title)
            return self.rows[item_id]

        async def delete(_maker, _ctx, item_id: str):  # noqa: ANN001, ANN202
            return self.rows.pop(item_id, None)

        async def blob_in_use(_maker, _ctx, sha: str) -> bool:  # noqa: ANN001
            return sha in self.referenced or any(r.blob_sha256 == sha for r in self.rows.values())

        async def fetch_public(_maker, slug: str):  # noqa: ANN001, ANN202
            return next((r for r in self.rows.values() if r.slug == slug), None)

        async def count_view(_maker, item_id: str) -> None:  # noqa: ANN001
            self.views.append(item_id)

        for fn in (add, list_items, rename, delete, blob_in_use, fetch_public, count_view):
            monkeypatch.setattr(store, fn.__name__, fn)


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> _Table:
    t = _Table()
    t.install(monkeypatch)
    return t


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path / "blobs")


def _app(
    blobs: FsBlobStore, principal: PrincipalInfo = OWNER, *, public_base_url: str = ""
) -> FastAPI:
    app = FastAPI()
    app.include_router(jshare.router, prefix="/api")
    app.include_router(jshare_public.router)
    app.state.session_maker = object()
    app.state.blob_store = blobs
    app.state.settings = Settings(
        database_url="postgresql+asyncpg://x@localhost/none", public_base_url=public_base_url
    )
    app.state.jshare_rate_limiter = TokenBucket(capacity=400, refill_per_sec=40.0)
    app.dependency_overrides[current_principal] = lambda: principal
    return app


def _post(client: TestClient, name: str, data: bytes, title: str = ""):  # noqa: ANN202
    return client.post("/api/jshare/items", params={"filename": name, "title": title}, content=data)


def _upload(client: TestClient, name: str, data: bytes, title: str = "") -> dict:
    resp = _post(client, name, data, title)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _site(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- the owner's routes -------------------------------------------------------------------


def test_every_owner_route_refuses_a_non_owner(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs, NON_OWNER))
    assert client.get("/api/jshare/items").status_code == 403
    assert _post(client, "a.png", b"x").status_code == 403
    assert client.patch("/api/jshare/items/x", json={"title": "t"}).status_code == 403
    assert client.delete("/api/jshare/items/x").status_code == 403
    assert client.get("/api/jshare/links").status_code == 403
    assert client.delete("/api/jshare/links/chat/x").status_code == 403
    assert table.rows == {}


def test_upload_stores_the_bytes_and_returns_a_public_link(
    blobs: FsBlobStore, table: _Table
) -> None:
    client = TestClient(_app(blobs, public_base_url="https://brain.example.com/"))
    item = _upload(client, "Holiday.JPG", b"jpeg bytes", title="  Beach  ")
    assert item["kind"] == "image"
    assert item["media_type"] == "image/jpeg"
    assert item["title"] == "Beach"
    assert item["size_bytes"] == len(b"jpeg bytes")
    assert SLUG_RE.fullmatch(item["slug"])
    assert item["url"] == f"https://brain.example.com/s/{item['slug']}/"
    sha = next(iter(table.rows.values())).blob_sha256
    assert blobs.path_for(sha).read_bytes() == b"jpeg bytes"
    assert [i["id"] for i in client.get("/api/jshare/items").json()] == [item["id"]]


def test_without_a_public_base_the_pwa_names_the_host(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "a.png", b"x")
    assert item["url"] == ""
    assert item["title"] == "a.png"


def test_a_broken_zip_is_refused_and_nothing_is_stored(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    resp = _post(client, "site.zip", b"not a zip")
    assert resp.status_code == 422
    assert "could not be opened" in resp.json()["detail"]
    assert table.rows == {}
    assert blobs.usage() == (0, 0)


def test_an_oversized_upload_is_refused(
    blobs: FsBlobStore, table: _Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jshare, "MAX_UPLOAD_BYTES", 4)
    client = TestClient(_app(blobs))
    resp = _post(client, "a.mp4", b"12345")
    assert resp.status_code == 413
    assert table.rows == {}


def test_a_full_disk_refuses_before_storing(
    blobs: FsBlobStore, table: _Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FsBlobStore, "free_bytes", lambda _self: 10)
    client = TestClient(_app(blobs))
    assert _post(client, "a.mp4", b"x").status_code == 507
    assert table.rows == {}


def test_rename(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "a.png", b"x")
    resp = client.patch(f"/api/jshare/items/{item['id']}", json={"title": " New "})
    assert resp.json()["title"] == "New"
    assert client.patch(f"/api/jshare/items/{item['id']}", json={"title": "  "}).status_code == 422
    assert client.patch("/api/jshare/items/nope", json={"title": "t"}).status_code == 404


def test_delete_unpublishes_and_frees_an_unshared_blob(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "a.png", b"only me")
    sha = table.rows[item["id"]].blob_sha256
    assert client.delete(f"/api/jshare/items/{item['id']}").status_code == 204
    assert not blobs.path_for(sha).exists()
    assert client.get(f"/s/{item['slug']}/").status_code == 404
    assert client.delete(f"/api/jshare/items/{item['id']}").status_code == 404


def test_delete_keeps_bytes_something_else_holds(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    first = _upload(client, "a.png", b"same bytes")
    second = _upload(client, "b.png", b"same bytes")
    sha = table.rows[first["id"]].blob_sha256
    client.delete(f"/api/jshare/items/{first['id']}")
    assert blobs.path_for(sha).exists()  # the second item still holds them
    table.referenced.add(sha)  # and, say, a note attachment
    client.delete(f"/api/jshare/items/{second['id']}")
    assert blobs.path_for(sha).exists()


# --- the public reads ---------------------------------------------------------------------


def _assert_sandboxed(resp) -> None:  # noqa: ANN001
    csp = resp.headers["content-security-policy"]
    assert csp.startswith("sandbox ")
    assert "allow-same-origin" not in csp
    assert "allow-modals" not in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in resp.headers["x-robots-tag"]
    assert resp.headers["cache-control"] == "private, no-cache"


def test_an_html_page_is_served_sandboxed(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "proto.html", b"<script>document.cookie</script>")
    resp = client.get(f"/s/{item['slug']}/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.content == b"<script>document.cookie</script>"
    _assert_sandboxed(resp)
    assert table.views == [item["id"]]


def test_the_bare_slug_redirects_into_its_folder(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "a.png", b"x")
    resp = client.get(f"/s/{item['slug']}", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == f"/s/{item['slug']}/"


def test_unknown_and_malformed_slugs_404_sandboxed(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    resp = client.get(f"/s/{'a' * 26}/")
    assert resp.status_code == 404
    _assert_sandboxed(resp)
    assert client.get("/s/NOT-A-SLUG/").status_code == 404
    assert client.get("/s/NOT-A-SLUG", follow_redirects=False).status_code == 404


def test_video_supports_range_requests(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "clip.mp4", bytes(range(100)))
    resp = client.get(f"/s/{item['slug']}/", headers={"Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.content == bytes(range(10, 20))
    _assert_sandboxed(resp)


def test_a_plain_file_downloads_and_only_its_own_name_resolves(
    blobs: FsBlobStore, table: _Table
) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "notes.docx", b"PK")
    resp = client.get(f"/s/{item['slug']}/notes.docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")
    _assert_sandboxed(resp)
    assert client.get(f"/s/{item['slug']}/other.docx").status_code == 404


def _assert_pdf_inline(resp) -> None:  # noqa: ANN001
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline")
    # The viewers refuse a sandboxed PDF; everything else the public side promises holds.
    assert "content-security-policy" not in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "private, no-cache"


def test_a_pdf_opens_in_the_tab(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "notes.pdf", b"%PDF")
    assert item["kind"] == "file"
    assert item["media_type"] == "application/pdf"
    _assert_pdf_inline(client.get(f"/s/{item['slug']}/"))
    _assert_pdf_inline(client.get(f"/s/{item['slug']}/notes.pdf"))


def test_a_pdf_shared_as_octet_stream_still_opens_in_the_tab(
    blobs: FsBlobStore, table: _Table
) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "OLD.PDF", b"%PDF")
    table.rows[item["id"]] = replace(table.rows[item["id"]], media_type="application/octet-stream")
    _assert_pdf_inline(client.get(f"/s/{item['slug']}/"))


def test_download_asks_for_the_file(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    pdf = _upload(client, "notes.pdf", b"%PDF")
    resp = client.get(f"/s/{pdf['slug']}/", params={"download": "1"})
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("attachment")
    page = _upload(client, "proto.html", b"<p>hi</p>")
    resp = client.get(f"/s/{page['slug']}/", params={"download": "1"})
    assert resp.headers["content-disposition"].startswith("attachment")
    _assert_sandboxed(resp)


def test_a_zipped_site_serves_its_files(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    zipped = _site(
        {
            "dist/index.html": b"<link href=app.css>",
            "dist/app.css": b"body{}",
            "dist/docs/index.html": b"docs",
            "secret.txt": b"outside the root",
        }
    )
    item = _upload(client, "site.zip", zipped)
    assert item["kind"] == "site"
    base = f"/s/{item['slug']}/"

    home = client.get(base)
    assert home.content == b"<link href=app.css>"
    assert home.headers["content-type"].startswith("text/html")
    _assert_sandboxed(home)
    assert home.headers["access-control-allow-origin"] == "*"

    css = client.get(base + "app.css")
    assert css.headers["content-type"].startswith("text/css")
    assert (
        client.get(base + "app.css", headers={"If-None-Match": css.headers["etag"]}).status_code
        == 304
    )

    folder = client.get(base + "docs", follow_redirects=False)
    assert folder.status_code == 308
    assert folder.headers["location"] == base + "docs/"
    assert client.get(base + "docs/").content == b"docs"

    assert client.get(base + "missing.js").status_code == 404
    assert client.get(base + "../secret.txt").status_code == 404
    assert client.get(base + "%2e%2e/secret.txt").status_code == 404
    # Only the page load counts as a view, not each of its files.
    assert table.views == [item["id"]]


def test_big_site_files_stream(
    blobs: FsBlobStore, table: _Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jshare_public, "_BUFFER_LIMIT", 4)
    client = TestClient(_app(blobs))
    item = _upload(client, "s.zip", _site({"index.html": b"x", "v.webm": b"0123456789"}))
    resp = client.get(f"/s/{item['slug']}/v.webm")
    assert resp.content == b"0123456789"
    head = client.head(f"/s/{item['slug']}/v.webm")
    assert head.headers["content-length"] == "10"
    assert resp.headers["content-type"] == "video/webm"


def test_a_missing_blob_is_a_404_not_a_500(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "a.png", b"x")
    blobs.path_for(table.rows[item["id"]].blob_sha256).unlink()
    assert client.get(f"/s/{item['slug']}/").status_code == 404


def test_public_reads_are_rate_limited(blobs: FsBlobStore, table: _Table) -> None:
    app = _app(blobs)
    app.state.jshare_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0001)
    client = TestClient(app)
    slug = "a" * 26
    assert client.get(f"/s/{slug}/").status_code == 404
    assert client.get(f"/s/{slug}/").status_code == 429


def test_a_thumbnail_is_not_a_view(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "a.png", b"x")
    client.get(f"/s/{item['slug']}/", headers={"Sec-Fetch-Dest": "image"})
    assert table.views == []
    client.get(f"/s/{item['slug']}/", headers={"Sec-Fetch-Dest": "document"})
    assert table.views == [item["id"]]


def test_bytes_past_the_cap_abort_the_stream_and_leave_nothing(
    blobs: FsBlobStore, table: _Table, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A client that understates its Content-Length is cut off by the bytes that arrive.
    monkeypatch.setattr(jshare, "MAX_UPLOAD_BYTES", 8)

    def body():  # noqa: ANN202
        yield b"1234"
        yield b"5678"
        yield b"9"

    client = TestClient(_app(blobs))
    resp = client.post(
        "/api/jshare/items",
        params={"filename": "a.mp4"},
        content=body(),
        headers={"Content-Length": "4"},
    )
    assert resp.status_code == 413
    assert table.rows == {}
    assert list((tmp_path / "blobs").glob("**/*")) == [] or blobs.usage() == (0, 0)
    assert not list((tmp_path / "blobs").glob(".incoming-*"))


def test_an_empty_file_is_fine_but_a_missing_length_is_not(
    blobs: FsBlobStore, table: _Table
) -> None:
    client = TestClient(_app(blobs))
    assert _upload(client, "empty.txt", b"")["size_bytes"] == 0

    def body():  # noqa: ANN202
        yield b"x"

    resp = client.post("/api/jshare/items", params={"filename": "a.png"}, content=body())
    assert resp.status_code == 411


def test_a_failed_insert_frees_the_stored_bytes(
    blobs: FsBlobStore, table: _Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: object, **_k: object) -> store.ShareItem:
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "add", boom)
    client = TestClient(_app(blobs), raise_server_exceptions=False)
    assert _post(client, "a.png", b"x").status_code == 500
    assert blobs.usage() == (0, 0)


def test_head_answers_without_counting(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "clip.mp4", b"0123456789")
    resp = client.head(f"/s/{item['slug']}/")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "10"
    _assert_sandboxed(resp)
    assert client.head(f"/s/{item['slug']}", follow_redirects=False).status_code == 308
    assert table.views == []


def test_a_folder_redirect_keeps_its_query(blobs: FsBlobStore, table: _Table) -> None:
    client = TestClient(_app(blobs))
    item = _upload(client, "s.zip", _site({"index.html": b"", "docs/index.html": b""}))
    resp = client.get(f"/s/{item['slug']}/docs?tab=2", follow_redirects=False)
    assert resp.headers["location"] == f"/s/{item['slug']}/docs/?tab=2"
    resp = client.get(f"/s/{item['slug']}?tab=2", follow_redirects=False)
    assert resp.headers["location"] == f"/s/{item['slug']}/?tab=2"


def test_the_public_domain_names_links_when_no_base_url_is_set(
    blobs: FsBlobStore, table: _Table
) -> None:
    app = _app(blobs)
    app.state.settings = Settings(
        database_url="postgresql+asyncpg://x@localhost/none", public_domain="brain.example.com"
    )
    item = _upload(TestClient(app), "a.png", b"x")
    assert item["url"] == f"https://brain.example.com/s/{item['slug']}/"


class TestTheAllLinksList:
    """`/api/jshare/links` reads every feature's links through `links.py`; these pin the route
    and the one-kind-failing rule. The readers themselves run against Postgres in
    `tests/integration/test_jshare_links_pg.py`."""

    def _client(self, blobs: FsBlobStore) -> TestClient:
        app = _app(blobs)
        app.state.auth_repo = object()
        app.state.intake_repo = object()
        return TestClient(app)

    def test_lists_every_kind_and_names_the_ones_that_failed(
        self, blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jbrain.jshare import links

        when = datetime(2026, 10, 18, tzinfo=UTC)

        async def chats(_src, _ctx):  # noqa: ANN001, ANN202
            return [links.SharedLink("chat", "c1", "Trip plans", when, None, None, 3)]

        async def broken(_src, _ctx):  # noqa: ANN001, ANN202
            raise RuntimeError("table missing")

        async def empty(_src, _ctx):  # noqa: ANN001, ANN202
            return []

        readers = dict.fromkeys(links.KINDS, empty)
        readers["chat"] = chats
        readers["minecraft"] = broken
        monkeypatch.setattr(links, "_READERS", readers)

        resp = self._client(blobs).get("/api/jshare/links")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [(x["kind"], x["title"], x["view_count"]) for x in body["links"]] == [
            ("chat", "Trip plans", 3)
        ]
        assert body["unavailable"] == ["minecraft"]

    def test_revoke_is_a_204_and_an_unknown_link_a_404(
        self, blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jbrain.jshare import links

        seen: list[tuple[str, str]] = []

        async def revoke(_src, _ctx, kind: str, link_id: str) -> bool:  # noqa: ANN001
            seen.append((kind, link_id))
            return link_id == "live"

        monkeypatch.setattr(links, "revoke", revoke)
        client = self._client(blobs)
        assert client.delete("/api/jshare/links/research/live").status_code == 204
        assert client.delete("/api/jshare/links/research/gone").status_code == 404
        assert seen == [("research", "live"), ("research", "gone")]


def test_the_owners_public_url_names_the_link(blobs: FsBlobStore, table: _Table) -> None:
    # Set in Settings → Devices (OWNER_TUNNEL_PLAN wave A5c), it wins over the env's.
    app = _app(blobs, public_base_url="https://old.example.com")
    app.state.settings_store = FakeSettingsStore(
        values={"public_base_url": "https://brain.example.net"}
    )
    item = _upload(TestClient(app), "a.png", b"png")
    assert item["url"] == f"https://brain.example.net/s/{item['slug']}/"
