"""Flashing a room-endpoint panel: the owner-facing API, with the sidecar faked.

What matters here is not that the happy path works — it is the set of ways the surface
must NOT behave, because the thing on the other end is a device in a child's bedroom with
no cable attached to it. A panel that takes a bad flash, or that is handed a manifest it
cannot authenticate against, is recovered by walking to it with a screwdriver.
"""

import asyncio
import io
import zipfile
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


def _artifact(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"\x00" * 64)
    return buf.getvalue()


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
    def test_a_box_without_the_profile_says_so_rather_than_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flasher is an opt-in compose profile. "Not enabled" is a configuration
        answer and must not arrive as a 500 the owner has to interpret."""
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
        assert "profile" in resp.json()["detail"]

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


class TestFirmwareUpload:
    def test_an_artifact_missing_an_image_is_refused_by_name(
        self, client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]]
    ) -> None:
        """Half a firmware set writes half a board. The error names both what is missing
        and what the zip did contain, because the owner is holding a file they downloaded
        from somewhere and needs to know whether they picked the wrong one."""
        c, _store, _blobs, _sent = client
        zip_bytes = _artifact(["jbrain-endpoint.bin"])
        resp = c.post(
            "/api/endpoint/firmware",
            params={"version": "0.1.0"},
            files={"artifact": ("a.zip", zip_bytes, "application/zip")},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "bootloader.bin" in detail and "partition-table.bin" in detail

    def test_a_directory_prefix_in_the_zip_still_matches(
        self, client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]]
    ) -> None:
        """A zip downloaded from the Actions UI can carry a folder; the workflow publishes
        the files flat. Matching on the base name means both work."""
        c, store, _blobs, _sent = client
        zip_bytes = _artifact(
            [
                "out/bootloader.bin",
                "out/partition-table.bin",
                "out/jbrain-endpoint.bin",
            ]
        )
        resp = c.post(
            "/api/endpoint/firmware",
            params={"version": "0.2.0"},
            files={"artifact": ("a.zip", zip_bytes, "application/zip")},
        )
        assert resp.status_code == 200, resp.text
        assert store.rows["endpoint_firmware"]["version"] == "0.2.0"

    def test_something_that_is_not_a_zip_is_rejected(
        self, client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]]
    ) -> None:
        c, _store, _blobs, _sent = client
        resp = c.post(
            "/api/endpoint/firmware",
            params={"version": "0.1.0"},
            files={"artifact": ("a.zip", b"not a zip at all", "application/zip")},
        )
        assert resp.status_code == 400


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


class TestFlash:
    def test_flashing_before_any_firmware_is_uploaded_is_refused(
        self, client: tuple[TestClient, FakeStore, FakeBlobs, list[Any]]
    ) -> None:
        c, _store, _blobs, _sent = client
        resp = c.post(
            "/api/endpoint/flash",
            json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
        )
        assert resp.status_code == 409

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
