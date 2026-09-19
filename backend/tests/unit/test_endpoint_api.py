"""Flashing a room-endpoint panel: the owner-facing API, with the sidecar faked.

What matters here is not that the happy path works — it is the set of ways the surface
must NOT behave, because the thing on the other end is a device in a child's bedroom with
no cable attached to it. A panel that takes a bad flash, or that is handed a manifest it
cannot authenticate against, is recovered by walking to it with a screwdriver.
"""

import asyncio
import hashlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain.api import endpoint as endpoint_api
from jbrain.api.notes import get_blob_store
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeDeviceRepo

SIDECAR = "http://endpoint:8000"

ARTIFACT_NAMES = ("bootloader.bin", "partition-table.bin", "jbrain-endpoint.bin")


def _release_stub(release: dict[str, Any] | None):
    async def _stub(_settings: Any) -> dict[str, Any] | None:
        return release

    return _stub


def _asset_stub(sums: str, blob: bytes):
    """Serve SHA256SUMS as text and every other asset as the same bytes."""

    async def _get(_self: Any, url: str, **_: Any) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url.endswith("SHA256SUMS"):
            return httpx.Response(200, text=sums, request=request)
        return httpx.Response(200, content=blob, request=request)

    return _get


class FakeStore:
    """Stands in for `app.settings`, whose owner-only RLS this surface inherits."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    async def get(self, _ctx: Any, key: str, default: Any = None) -> Any:
        return self.rows.get(key, default)

    async def upsert(self, _ctx: Any, key: str, value: Any) -> None:
        self.rows[key] = value


class FakeBlobs:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        sha = f"sha{len(self.blobs)}"
        self.blobs[sha] = data
        return sha

    async def get(self, sha: str) -> bytes:
        return self.blobs[sha]


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FakeStore, FakeBlobs, list[Any]]]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        endpoint_url=SIDECAR,
    )
    app = create_app(settings)
    store = FakeStore()
    blobs = FakeBlobs()
    sent: list[Any] = []

    monkeypatch.setattr(endpoint_api, "_store", lambda _request: store)
    # No LAN certificate in a unit test; the "" branch is the tunnel-only box.
    monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "")

    with TestClient(app) as c:
        app.state.auth_repo = FakeAuthRepo()
        app.state.device_repo = FakeDeviceRepo()
        # Override the dependency FUNCTION rather than reaching into the Annotated alias:
        # `BlobStoreDep.__metadata__` is an implementation detail of typing, and pyright
        # is right to refuse it.
        app.dependency_overrides[get_blob_store] = lambda: blobs
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"}).status_code
            == 204
        )
        yield c, store, blobs, sent


class TestPorts:
    def test_a_blanked_url_says_which_setting_rather_than_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flasher is stock stack, so an empty URL means someone deliberately blanked
        it. That is a configuration answer and must not arrive as a 500 the owner has to
        interpret — it must name the setting."""
        settings = Settings(
            secure_cookies=False,
            database_url="postgresql+asyncpg://nobody@localhost:1/none",
            endpoint_url="",
        )
        app = create_app(settings)
        with TestClient(app) as c:
            app.state.auth_repo = FakeAuthRepo()
            key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
            c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
            resp = c.get("/api/endpoint/ports")
        assert resp.status_code == 503
        assert "JBRAIN_ENDPOINT_URL" in resp.json()["detail"]

    def test_no_ports_is_success_not_an_error(
        self,
        client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "Nothing is plugged in" is the answer to a real question. Returning it as a
        failure would make it indistinguishable from the flasher being broken — and those
        have completely different fixes."""
        c, _store, _blobs, _sent = client

        async def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
            return httpx.Response(200, json={"ports": []}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        resp = c.get("/api/endpoint/ports")
        assert resp.status_code == 200
        assert resp.json()["ports"] == []


class TestManifest:
    def test_absent_firmware_is_a_404_rather_than_an_empty_version(
        self, client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]]
    ) -> None:
        """A panel comparing its version against "" would flash-loop."""
        c, _store, _blobs, _sent = client
        assert c.get("/api/endpoint/firmware").status_code == 404

    def test_the_manifest_carries_a_version_and_a_url_and_nothing_else(
        self, client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]]
    ) -> None:
        """This is the one route a panel in a child's room can call. It must not become a
        place where anything about this box leaks to a device on the LAN."""
        c, store, _blobs, _sent = client
        store.rows["endpoint_firmware"] = {"version": "0.3.0", "images": {}}
        body = c.get("/api/endpoint/firmware").json()
        assert set(body) == {"version", "url"}
        assert body["version"] == "0.3.0"


class TestSync:
    """Fetching the firmware from the public release, which is what removes the errand."""

    def test_a_checksum_mismatch_stores_nothing_at_all(
        self,
        client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """These bytes become a bootloader on a device with no cable attached to it. A
        half-stored set is worse than no set, so one bad asset refuses the whole sync."""
        c, store, _blobs, _sent = client
        good = b"\x01" * 16
        release = {
            "tag_name": "firmware-v9.9.9",
            "assets": [
                {"name": n, "browser_download_url": f"https://example/{n}"}
                for n in (*ARTIFACT_NAMES, "SHA256SUMS")
            ],
        }
        monkeypatch.setattr(endpoint_api, "_latest_release", _release_stub(release))

        sums = "\n".join(
            f"{'0' * 64}  ./{name}" for name in ARTIFACT_NAMES
        )  # deliberately wrong digests
        monkeypatch.setattr(httpx.AsyncClient, "get", _asset_stub(sums, good))

        resp = c.post("/api/endpoint/firmware/sync")
        assert resp.status_code == 502
        assert "checksum" in resp.json()["detail"]
        assert "endpoint_firmware" not in store.rows

    def test_a_matching_checksum_stores_the_release_version(
        self,
        client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, store, _blobs, _sent = client
        good = b"\x01" * 16
        digest = hashlib.sha256(good).hexdigest()
        release = {
            "tag_name": "firmware-v1.2.3",
            "assets": [
                {"name": n, "browser_download_url": f"https://example/{n}"}
                for n in (*ARTIFACT_NAMES, "SHA256SUMS")
            ],
        }
        monkeypatch.setattr(endpoint_api, "_latest_release", _release_stub(release))
        sums = "\n".join(f"{digest}  ./{name}" for name in ARTIFACT_NAMES)
        monkeypatch.setattr(httpx.AsyncClient, "get", _asset_stub(sums, good))

        resp = c.post("/api/endpoint/firmware/sync")
        assert resp.status_code == 200, resp.text
        assert store.rows["endpoint_firmware"]["version"] == "1.2.3"

    def test_an_unreachable_source_names_both_things_it_could_be(
        self,
        client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "Nothing to fetch" has two causes with different fixes — no network, or no
        release cut yet — and the owner is holding a board, so the message names both."""
        c, _store, _blobs, _sent = client
        monkeypatch.setattr(endpoint_api, "_latest_release", _release_stub(None))

        resp = c.post("/api/endpoint/firmware/sync")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "github.com" in detail and "version.txt" in detail


class TestFlash:
    def test_flashing_with_no_firmware_and_no_release_is_refused(
        self,
        client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _store, _blobs, _sent = client
        # With no stored firmware AND no release reachable, the flash refuses rather than
        # writing a partial board — and says both halves of why.
        monkeypatch.setattr(endpoint_api, "_latest_release", _release_stub(None))
        resp = c.post(
            "/api/endpoint/flash",
            json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
        )
        assert resp.status_code == 409
        assert "none could be fetched" in resp.json()["detail"]

    def test_the_panel_is_given_a_fresh_device_key_and_the_api_url(
        self,
        client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The published firmware is generic; everything that makes a unit itself is
        written at flash time. The token is minted per flash and never stored here, which
        is what makes re-flashing a panel revoke the identity it had."""
        c, store, blobs, sent = client
        sha = asyncio.run(blobs.put(b"\x00" * 32))
        store.rows["endpoint_firmware"] = {"version": "0.4.0", "images": {"0x0": sha}}

        class FakeStream:
            def __init__(self, payload: dict[str, Any]) -> None:
                sent.append(payload)

            async def __aenter__(self) -> "FakeStream":
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def aiter_bytes(self) -> Any:
                yield b"OK\n"

        def fake_stream(self: Any, _method: str, _url: str, json: dict[str, Any]) -> FakeStream:
            return FakeStream(json)

        monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
        resp = c.post(
            "/api/endpoint/flash",
            json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw", "name": "left"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.text.strip() == "OK"

        (payload,) = sent
        nvs = payload["nvs"]
        assert nvs["ssid"] == "net"
        assert nvs["token"], "a panel with no credential cannot reach the manifest"
        assert nvs["api"].endswith("/api")
        assert nvs["name"] == "left"
        # No LAN certificate on this box, so the key is omitted rather than sent empty:
        # an empty PEM in NVS would fail inside mbedTLS with an unhelpful error.
        assert "ca" not in nvs
