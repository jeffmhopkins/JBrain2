"""Flashing a room-endpoint panel: the owner-facing API, with the sidecar faked.

What matters here is not that the happy path works — it is the set of ways the surface
must NOT behave, because the thing on the other end is a device in a child's bedroom with
no cable attached to it. A panel that takes a bad flash, or that is handed a manifest it
cannot authenticate against, is recovered by walking to it with a screwdriver.

The firmware itself comes off the box's own checkout (`firmware/dist/`, mounted read-only),
so the fixture here builds a real one on disk rather than faking a store: the failure modes
worth pinning — a missing image, an image that does not match its checksum — are properties
of files, and stubbing the reads away would pin nothing.
"""

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import endpoint as endpoint_api
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeDeviceRepo

SIDECAR = "http://endpoint:8000"

ARTIFACT_NAMES = ("bootloader.bin", "partition-table.bin", "jbrain-endpoint.bin")
APP_IMAGE = "jbrain-endpoint.bin"
VERSION = "9.9.9"


def _write_firmware(root: Path, *, version: str = VERSION, sums: bool = True) -> Path:
    """A stand-in for the mounted checkout: a version file and a signed set of images."""
    dist = root / "dist"
    dist.mkdir(parents=True)
    (root / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    lines = []
    for i, name in enumerate(ARTIFACT_NAMES):
        data = bytes([i + 1]) * 64
        (dist / name).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    if sums:
        (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, Path, list[Any]]]:
    firmware = _write_firmware(tmp_path / "firmware")
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        endpoint_url=SIDECAR,
        firmware_dir=str(firmware),
    )
    app = create_app(settings)
    sent: list[Any] = []

    # No LAN certificate in a unit test; the "" branch is the tunnel-only box.
    monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "")

    with TestClient(app) as c:
        app.state.auth_repo = FakeAuthRepo()
        app.state.device_repo = FakeDeviceRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"}).status_code
            == 204
        )
        yield c, firmware, sent


class FakeStream:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def aiter_bytes(self) -> Any:
        yield b"OK\n"


def _fake_sidecar(monkeypatch: pytest.MonkeyPatch, sent: list[Any]) -> None:
    def fake_stream(_self: Any, _method: str, _url: str, json: dict[str, Any]) -> FakeStream:
        sent.append(json)
        return FakeStream(json)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)


def _stub_flash(
    c: TestClient, monkeypatch: pytest.MonkeyPatch, sent: list[Any], *, lan_addr: str
) -> None:
    """Run one flash against a faked sidecar, capturing the NVS payload it would write."""
    # TestClient.app is typed as a bare ASGI callable, which has no `.state`.
    cast(FastAPI, c.app).state.settings.lan_addr = lan_addr
    _fake_sidecar(monkeypatch, sent)
    resp = c.post(
        "/api/endpoint/flash",
        json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
    )
    assert resp.status_code == 200, resp.text


PANEL_KEY = "panel-key-for-the-unit-under-test"


def _provision_panel(c: TestClient) -> str:
    """Give this box a panel identity and return the key that panel holds.

    Registered straight on the auth repo, which is how `test_owntracks_api` and
    `test_mqtt_api` do it: production keeps devices and principals in ONE table, and the
    unit fakes split them, so a `provision_device` call against `FakeDeviceRepo` leaves
    nothing for `authenticate_device` to find. The real flash path that mints this is
    covered by `TestFlash`; what is under test here is whether the credential it hands
    over opens the door it was minted for.
    """
    asyncio.run(
        cast(FastAPI, c.app).state.auth_repo.create_principal(
            "device_key", keys.hash_key(PANEL_KEY), "panel Elora"
        )
    )
    return PANEL_KEY


class TestPorts:
    def test_a_blanked_url_says_which_setting_rather_than_failing(self) -> None:
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
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "Nothing is plugged in" is the answer to a real question. Returning it as a
        failure would make it indistinguishable from the flasher being broken — and those
        have completely different fixes."""
        c, _fw, _sent = client

        async def fake_get(self: Any, url: str, **_: Any) -> httpx.Response:
            return httpx.Response(200, json={"ports": []}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        resp = c.get("/api/endpoint/ports")
        assert resp.status_code == 200
        assert resp.json()["ports"] == []


class TestManifest:
    def test_absent_firmware_is_not_reported_as_an_empty_version(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """A panel comparing its version against "" would flash-loop."""
        c, fw, _sent = client
        (fw / "version.txt").unlink()
        resp = c.get("/api/endpoint/firmware")
        assert resp.status_code == 503
        # CLAUDE.md #10: the owner has no terminal, so the fix named has to be one they
        # can actually carry out.
        assert "Update" in resp.json()["detail"]

    def test_the_manifest_carries_a_version_and_a_url_and_nothing_else(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """This is the one route a panel in a child's room can call. It must not become a
        place where anything about this box leaks to a device on the LAN."""
        c, _fw, _sent = client
        body = c.get("/api/endpoint/firmware").json()
        assert set(body) == {"version", "url"}
        assert body["version"] == VERSION

    def test_the_url_the_manifest_advertises_actually_serves_the_app_image(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """The manifest advertised `/endpoint/firmware/bin` while nothing served it, so
        every OTA a panel attempted would have 404'd — invisible only because no panel had
        yet got far enough to attempt one. The two must be checked together."""
        c, fw, _sent = client
        url = c.get("/api/endpoint/firmware").json()["url"]
        assert url.endswith("/endpoint/firmware/bin")

        resp = c.get("/api/endpoint/firmware/bin")
        assert resp.status_code == 200, resp.text
        assert resp.content == (fw / "dist" / APP_IMAGE).read_bytes()


class TestAPanelCanActuallyAuthenticate:
    """The one thing a panel on a bedroom wall does, and it 401'd on every attempt.

    The two firmware routes were written against `PrincipalDep` and then checked
    `principal.kind` for `device_key`. `PrincipalDep` reads the owner SESSION COOKIE, so a
    bearer key never reached that check — it was dead code guarding a door that was already
    shut. Nothing noticed because no panel had booted far enough to knock, and reaching this
    route is both the panel's OTA path and the health signal its rollback gate waits on.

    These tests are written against the credential a real panel holds: its own `device_key`,
    presented exactly the way `firmware/main/ota.c` presents it.
    """

    def test_a_panel_reaches_the_manifest_with_its_own_device_key(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()  # a panel has no owner session; only its own key

        resp = c.get("/api/endpoint/firmware", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == VERSION

    def test_a_panel_can_download_the_image_the_manifest_points_at(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """The OTA itself. A manifest it can read pointing at an image it cannot is no
        better than a manifest it cannot read."""
        c, fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.get("/api/endpoint/firmware/bin", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200, resp.text
        assert resp.content == (fw / "dist" / APP_IMAGE).read_bytes()

    def test_no_credential_is_still_refused(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        c.cookies.clear()
        assert c.get("/api/endpoint/firmware").status_code == 401
        assert c.get("/api/endpoint/firmware/bin").status_code == 401

    def test_a_garbage_bearer_token_is_refused(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        c.cookies.clear()
        resp = c.get("/api/endpoint/firmware", headers={"Authorization": "Bearer not-a-real-key"})
        assert resp.status_code == 401

    def test_a_panel_key_opens_nothing_else(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """The reason this is its own dependency rather than a widening of the owner one.

        Widening `current_principal` to read bearer tokens would have handed a device key
        every cookie-gated route in the app. A panel is a device in a child's bedroom; the
        blast radius of one being lifted off a wall has to stay at "it can ask what firmware
        to run"."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()
        auth = {"Authorization": f"Bearer {key}"}

        assert c.get("/api/endpoint/ports", headers=auth).status_code == 401
        assert (
            c.post(
                "/api/endpoint/flash",
                headers=auth,
                json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
            ).status_code
            == 401
        )


class TestReadingTheBoxsOwnRoot:
    """Which file the LAN root is read from, and why there are two.

    Caddy mints its internal CA as root, into a directory that also holds the CA PRIVATE
    key, so that directory is not world-traversable. This api runs as uid 1000 and cannot
    get to it — measured on the live box as `[Errno 13] Permission denied`. The failure was
    silent because "" is also what a box with no LAN site returns, so every panel was quietly
    handed the public hostname and routed through Cloudflare from three metres away.
    """

    def test_the_published_root_is_preferred(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        published = tmp_path / "lan-root.crt"
        published.write_text("-----BEGIN CERTIFICATE-----\npublished\n", encoding="utf-8")
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PUBLISHED", str(published))
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PATH", str(tmp_path / "nope.crt"))
        assert "published" in endpoint_api._lan_ca()

    def test_the_original_path_still_works_on_a_box_that_can_read_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A proxy image that predates the publisher must not lose a LAN site it already
        had — the fallback is what makes this deployable without ordering the two."""
        original = tmp_path / "root.crt"
        original.write_text("-----BEGIN CERTIFICATE-----\noriginal\n", encoding="utf-8")
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PUBLISHED", str(tmp_path / "absent.crt"))
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PATH", str(original))
        assert "original" in endpoint_api._lan_ca()

    def test_a_half_written_file_is_not_treated_as_a_certificate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The publisher copies to a temp name and renames, but a truncated or empty file
        must never become the root a panel pins: it would fail every handshake forever, on
        a device with no cable attached to it."""
        published = tmp_path / "lan-root.crt"
        published.write_text("", encoding="utf-8")
        original = tmp_path / "root.crt"
        original.write_text("-----BEGIN CERTIFICATE-----\noriginal\n", encoding="utf-8")
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PUBLISHED", str(published))
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PATH", str(original))
        assert "original" in endpoint_api._lan_ca()

    def test_neither_present_is_a_tunnel_only_box(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PUBLISHED", str(tmp_path / "a.crt"))
        monkeypatch.setattr(endpoint_api, "CADDY_ROOT_PATH", str(tmp_path / "b.crt"))
        assert endpoint_api._lan_ca() == ""


class TestTheSchemeHandedToAPanel:
    """Never `http://`, and this is the test that would have saved a whole evening.

    Caddy runs in Cloudflare Tunnel mode, so its own site address is plain HTTP and TLS
    terminates at the edge; uvicorn is not told to trust `X-Forwarded-Proto` from a
    container address either. `request.base_url` therefore reports `http`, honestly and
    uselessly, and that value went two places that both matter:

    - into the manifest as the image URL, where `esp_https_ota` refuses it outright, so
      every OTA failed instantly and silently ON THE PANEL — a 200 in the access log and
      no download, forever
    - into a panel's NVS at flash time as the address it calls home on, so a unit polled
      the box over http with its bearer token in the clear

    The old tests asserted the url merely ended in `/endpoint/firmware/bin`, which was
    true of the broken value. A suffix is not an address.
    """

    def test_the_manifest_never_offers_an_image_over_plain_http(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        url = c.get("/api/endpoint/firmware").json()["url"]
        assert url.startswith("https://"), url

    def test_a_panel_is_never_told_to_call_home_over_plain_http(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The flash-time half. A token on the wire in the clear is worse than a failed
        update, because nothing about it looks wrong afterwards."""
        c, _fw, sent = client
        monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "")
        _stub_flash(c, monkeypatch, sent, lan_addr="")
        assert sent[-1]["nvs"]["api"].startswith("https://")

    def test_a_proxy_declaring_plain_http_is_not_believed(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """The SECOND version of this bug, which survived a deploy.

        The first fix honoured `X-Forwarded-Proto`, reasoning that a proxy saying so is
        telling the truth about itself. It is — about its own hop. In tunnel mode Caddy
        takes the request from `cloudflared` over plain HTTP and accurately declares
        `http`, while the panel's connection to the edge was TLS the whole time.
        Believing it re-emitted the broken url verbatim and the OTA stayed broken.
        """
        c, _fw, _sent = client
        url = c.get("/api/endpoint/firmware", headers={"X-Forwarded-Proto": "http"}).json()["url"]
        assert url.startswith("https://"), url

    def test_a_proxy_declaring_https_also_gets_https(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        url = c.get("/api/endpoint/firmware", headers={"X-Forwarded-Proto": "https"}).json()["url"]
        assert url.startswith("https://")

    def test_the_host_the_panel_was_reached_at_is_preserved(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """Only the scheme is corrected. Rewriting the host would send a panel somewhere
        nobody asked for."""
        c, _fw, _sent = client
        url = c.get("/api/endpoint/firmware", headers={"Host": "box.example"}).json()["url"]
        assert url.startswith("https://box.example/"), url


class TestPanelAddress:
    """Where a panel is told to find the box, and which certificate it is given.

    These two must travel together. Handing a panel a PUBLIC hostname alongside the box's
    INTERNAL root fails TLS on every request forever — with no symptom at all, because the
    panel joins Wi-Fi perfectly and simply never gets an answer. That combination shipped
    once and these tests exist so it cannot ship twice.
    """

    def test_a_lan_box_sends_its_own_address_and_its_own_root(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A panel is on the same LAN as the box, so that — not the owner's browser
        address — is where it should look. Pinning one root also beats trusting ~150."""
        c, _fw, sent = client
        monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "-----BEGIN CERTIFICATE-----\nx\n")
        _stub_flash(c, monkeypatch, sent, lan_addr="https://jbrain.local")

        nvs = sent[-1]["nvs"]
        assert nvs["api"] == "https://jbrain.local/api"
        assert nvs["ca"].startswith("-----BEGIN CERTIFICATE-----")

    def test_a_box_with_no_lan_site_sends_no_root_at_all(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failure this pins: a public URL plus an internal root. The firmware then
        trusts ONLY that root, so every handshake against a publicly-signed certificate
        fails and the panel is mute. No root means it falls back to the public bundle."""
        c, _fw, sent = client
        monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "")
        _stub_flash(c, monkeypatch, sent, lan_addr="https://jbrain.local")

        nvs = sent[-1]["nvs"]
        assert "ca" not in nvs, "an internal root beside a public URL is worse than none"
        assert nvs["api"].endswith("/api")

    def test_a_readable_root_without_a_lan_address_is_not_used(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same pairing from the other side: a root is only right for the address it
        was minted for, so an unconfigured LAN name means the root goes unused."""
        c, _fw, sent = client
        monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "-----BEGIN CERTIFICATE-----\nx\n")
        _stub_flash(c, monkeypatch, sent, lan_addr="")

        assert "ca" not in sent[-1]["nvs"]


class TestFirmwareFromTheCheckout:
    """The images come off this box's own checkout — no release, no CDN, no credential.

    The path this replaced reached api.github.com, github.com and a signed CDN host before
    it could flash a board plugged into the box's own USB port, and the first real flash
    died on a DNS lookup inside it. These tests pin the property that removed: a flash
    touches no network at all beyond the sidecar on the internal network.
    """

    def test_a_flash_needs_no_outbound_request_of_any_kind(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, sent = client

        async def forbidden(_self: Any, url: str, **_: Any) -> httpx.Response:
            raise AssertionError(f"a flash must not fetch anything: {url}")

        monkeypatch.setattr(httpx.AsyncClient, "get", forbidden)
        _stub_flash(c, monkeypatch, sent, lan_addr="")
        assert sent, "the flash never reached the sidecar"

    def test_every_image_is_sent_in_flash_order(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Offsets ascending, and the whole set: a board written out of order or missing
        its partition table is recovered with a cable, which is the thing to avoid."""
        c, _fw, sent = client
        _stub_flash(c, monkeypatch, sent, lan_addr="")

        offsets = [int(img["offset"], 16) for img in sent[-1]["images"]]
        assert offsets == sorted(offsets)
        assert offsets == [0x0, 0x8000, 0x20000]

    def test_a_missing_image_refuses_and_names_a_fix_the_owner_can_run(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The owner has no terminal (CLAUDE.md #10), so "rebuild the firmware" is not an
        answer they can act on. `Ops -> Update` is."""
        c, fw, sent = client
        (fw / "dist" / APP_IMAGE).unlink()
        _fake_sidecar(monkeypatch, sent)

        resp = c.post(
            "/api/endpoint/flash",
            json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
        )
        assert resp.status_code == 503
        assert "Update" in resp.json()["detail"]
        assert not sent, "nothing may reach the board once an image is missing"

    def test_an_image_that_does_not_match_its_checksum_is_never_written(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A half-finished update leaves a truncated image beside a correct SHA256SUMS.
        Flashing it would brick a panel that has no cable attached once it is in a bedroom,
        so the whole set is refused rather than partially written."""
        c, fw, sent = client
        (fw / "dist" / "bootloader.bin").write_bytes(b"\xff" * 8)
        _fake_sidecar(monkeypatch, sent)

        resp = c.post(
            "/api/endpoint/flash",
            json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
        )
        assert resp.status_code == 500
        assert "SHA256SUMS" in resp.json()["detail"]
        assert not sent

    def test_the_version_is_whatever_the_checkout_says_right_now(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """Nothing caches it, so an `Ops -> Update` that lands a new firmware is live for
        the very next flash with nothing to invalidate and no button to press."""
        c, fw, _sent = client
        assert c.get("/api/endpoint/firmware").json()["version"] == VERSION
        (fw / "version.txt").write_text("1.2.3\n", encoding="utf-8")
        assert c.get("/api/endpoint/firmware").json()["version"] == "1.2.3"


class TestTheConsoleMonitor:
    """The only view from the PANEL's side, which is why it exists at all.

    A panel that polled the manifest and then quietly did not update looked, from the
    box, exactly like one that was correctly up to date: a 200 and nothing else. The
    reason was a log line on the panel that nobody could read.
    """

    def test_the_owner_sees_the_panel_console_streamed_through(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, _sent = client
        asked: list[Any] = []

        # Named apart from the module-level `FakeStream`: a nested class does not shadow
        # it for a STRING annotation, which resolves at module scope — so `-> "FakeStream"`
        # here would silently mean the other one, and pyright is right to say so.
        class FakeConsole:
            def __init__(self, params: dict[str, str]) -> None:
                asked.append(params)

            async def __aenter__(self) -> "FakeConsole":
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def aiter_bytes(self) -> Any:
                yield b"-- restarting the panel --\n"
                yield b"I (612) jbrain: up to date at 0.2.1\n"

        def fake_stream(_self: Any, _m: str, _u: str, params: dict[str, str]) -> FakeConsole:
            return FakeConsole(params)

        monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
        resp = c.get("/api/endpoint/monitor", params={"port": "/dev/ttyACM0", "reset": "true"})
        assert resp.status_code == 200, resp.text
        assert "up to date at 0.2.1" in resp.text
        assert asked[-1]["port"] == "/dev/ttyACM0"
        assert asked[-1]["reset"] == "1"

    def test_a_panel_cannot_watch_anything(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """A panel's key opens the manifest and the image. A console — the owner's window
        onto every unit plugged into this box — is not part of that bargain."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()
        resp = c.get(
            "/api/endpoint/monitor",
            params={"port": "/dev/ttyACM0"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 401


class TestRememberingTheNetwork:
    """The one secret this surface stores, and it stores it only when asked.

    A panel ends up on a bedroom wall; when one stops working the fix is a re-flash, and
    the owner has no terminal (CLAUDE.md #10). Making that possible without them standing
    at the PWA means the box keeps the Wi-Fi password — so starting to keep it has to be
    something they did, not something that began happening.
    """

    def test_a_flash_stores_nothing_by_default(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, sent = client
        stored: dict[str, Any] = {}
        monkeypatch.setattr(
            endpoint_api.SqlSettingsStore,
            "upsert",
            lambda _s, _c, k, v: stored.update({k: v}),
        )
        _stub_flash(c, monkeypatch, sent, lan_addr="")
        assert stored == {}, "the password must not be kept unless the owner asked"

    def test_remembering_keeps_the_network_for_a_later_reflash(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, sent = client
        stored: dict[str, Any] = {}

        async def fake_upsert(_s: Any, _c: Any, key: str, value: Any) -> None:
            stored[key] = value

        monkeypatch.setattr(endpoint_api.SqlSettingsStore, "upsert", fake_upsert)
        _fake_sidecar(monkeypatch, sent)
        resp = c.post(
            "/api/endpoint/flash",
            json={
                "port": "/dev/ttyACM0",
                "ssid": "bleepbloop",
                "password": "hunter2",
                "remember": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert stored[endpoint_api.WIFI_KEY] == {"ssid": "bleepbloop", "password": "hunter2"}


class TestTheSharedFlashBuilder:
    """One assembly for both surfaces, so the PWA and the debug console cannot drift.

    A second copy would be a second place to forget the CA pairing rule, and the failure
    there is a panel that joins Wi-Fi perfectly and is then silent forever.
    """

    def test_it_mints_a_new_identity_every_time(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        app = cast(FastAPI, c.app)

        async def build() -> dict[str, Any]:
            from starlette.requests import Request as StarletteRequest

            scope = {
                "type": "http",
                "app": app,
                "headers": [(b"host", b"box.example")],
                "scheme": "http",
                "server": ("box.example", 80),
                "path": "/",
                "method": "GET",
                "query_string": b"",
                "root_path": "",
            }
            return await endpoint_api.build_flash(
                StarletteRequest(scope),  # type: ignore[arg-type]
                app.state.settings,
                app.state.device_repo,
                SessionContext(principal_kind="owner"),
                port="/dev/ttyACM0",
                ssid="net",
                password="pw",
                name="Elora",
                erase=False,
            )

        first = asyncio.run(build())
        second = asyncio.run(build())
        assert first["nvs"]["token"] != second["nvs"]["token"], (
            "a re-flash must revoke the identity the panel had"
        )
        assert first["nvs"]["api"].startswith("https://")


class TestFlash:
    def test_the_panel_is_given_a_fresh_device_key_and_the_api_url(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The built firmware is generic; everything that makes a unit itself is written at
        flash time. The token is minted per flash and never stored here, which is what
        makes re-flashing a panel revoke the identity it had."""
        c, _fw, sent = client
        _fake_sidecar(monkeypatch, sent)
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
