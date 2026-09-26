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

import array
import asyncio
import base64
import hashlib
import json
import re
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

ARTIFACT_NAMES = (
    "bootloader.bin",
    "partition-table.bin",
    "ota_data_initial.bin",
    "jbrain-endpoint.bin",
    "srmodels.bin",
)
APP_IMAGE = "jbrain-endpoint.bin"
MODEL_IMAGE = "srmodels.bin"
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

    def test_a_panel_cannot_write_the_served_version_back(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """`fw_version` shares the model the owner's PUT takes, so it is worth pinning that it
        is read-only in effect: the UPDATE names six columns and ignores everything else. A
        panel that could set this could talk its siblings into installing anything."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.put(
            "/api/endpoint/settings",
            headers={"Authorization": f"Bearer {key}"},
            json={"brightness": 10, "fw_version": "9.9.9"},
        )
        # Owner-only route; a panel key is not an owner session.
        assert resp.status_code in (401, 403), resp.text

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
        # 0xF000 is `otadata`, and it is in the set because a USB flash writes `factory`
        # while a panel that has ever been OTA'd is pointed at an OTA slot — so without it
        # the panel boots the image this flash was meant to replace. The app partitions
        # were resized on 2026-09-21, which turned that from stale into a slot at the wrong
        # offset entirely.
        assert offsets == [0x0, 0x8000, 0xF000, 0x20000, 0xAA0000]

    def test_the_speech_models_are_written_because_ota_can_never_deliver_them(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`esp_https_ota` writes app slots. The models live in the `model` data partition,
        so this USB flash is their ONLY route onto a panel — and the panels are in the
        twins' rooms, which makes a missed image a physical trip rather than a re-run."""
        c, fw, sent = client
        _stub_flash(c, monkeypatch, sent, lan_addr="")

        by_offset = {img["offset"]: img["b64"] for img in sent[-1]["images"]}
        assert "0xaa0000" in by_offset, "the model partition was not written"
        assert base64.b64decode(by_offset["0xaa0000"]) == (fw / "dist" / MODEL_IMAGE).read_bytes()

    def test_missing_models_refuse_rather_than_flashing_a_panel_that_cannot_listen(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Half a flash is the failure this whole set exists to avoid: a panel written
        without its models needs the cable again to get them, and the cable is the thing
        the design spends everything to avoid."""
        c, fw, sent = client
        (fw / "dist" / MODEL_IMAGE).unlink()
        _fake_sidecar(monkeypatch, sent)

        resp = c.post(
            "/api/endpoint/flash",
            json={"port": "/dev/ttyACM0", "ssid": "net", "password": "pw"},
        )
        assert resp.status_code == 503
        assert not sent, "nothing may reach the board once an image is missing"

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


class TestAPanelCanReportItsOwnState:
    """Telemetry, and why the route exists at all.

    The display fault took six firmware releases partly because a panel could only be
    questioned two ways and both were broken: the controller's registers read back zeros that
    look exactly like a diagnosis, and opening the USB console resets the chip, so every log
    captured was of a freshly-rebooted panel rather than of the fault. This is the channel
    that does neither — and it is what lets a panel move to a plain USB charger without
    becoming undiagnosable, which is the whole premise (CLAUDE.md #10).
    """

    def test_a_panel_reports_with_its_own_device_key(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()  # a panel has no owner session; only its own key

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.14", "uptime_ms": 61000},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_an_unauthenticated_panel_cannot_report(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """The route takes whatever it is handed and writes it to the owner's log, so an
        open one would be a way for anything on the LAN to put text there."""
        c, _fw, _sent = client
        c.cookies.clear()

        resp = c.post("/api/endpoint/telemetry", json={"version": "x", "uptime_ms": 1})
        assert resp.status_code == 401

    def test_everything_that_used_to_need_a_cable_gets_through(
        self, client: tuple[TestClient, Path, list[Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A panel's worst-case report, with every diagnostic populated at once.

        These readings were all computed on the device and written to an ESP_LOG, which on
        this panel means written to nowhere — it has been on a plain charger in a bedroom
        since the day it went in. Each one is here because it would have named a real fault
        that was otherwise invisible: the largest free INTERNAL block (which `free_heap`
        cannot distinguish from a fragmented one, and which has explained the blit fault
        twice), a codec that REFUSED a level the owner set from the box, the Wi-Fi reason
        code, an update that can never install, and which of the three callers of
        `esp_restart` it was.

        The whole body has to arrive, because a field the box rejects is a field that reads
        as absent — and the panel cannot tell the difference.
        """
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={
                "version": "0.2.82",
                "uptime_ms": 900000,
                "int_largest": 9728,
                "levels": "90!/36",
                "blit_fail_total": 249,
                "blit_recov": 3,
                "meter_fail": 5100,
                "wifi_reason": 201,
                "wifi_drops": 7,
                "ota_err": "ESP_ERR_OTA_VALIDATE_FAILED",
                "ota_tries": 4,
                "restart_why": "blit-heal",
                "heard": [["jump up", 19, 1], ["burp", 0, 0]],
            },
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text
        out = capsys.readouterr().out
        for evidence in (
            '"int_largest": 9728',
            '"levels": "90!/36"',
            '"blit_fail_total": 249',
            '"meter_fail": 5100',
            '"wifi_reason": 201',
            '"ota_err": "ESP_ERR_OTA_VALIDATE_FAILED"',
            '"restart_why": "blit-heal"',
            '"jump up"',
        ):
            assert evidence in out, f"{evidence} did not reach the log"

    def test_a_healthy_panel_does_not_fill_the_log_with_empty_keys(
        self, client: tuple[TestClient, Path, list[Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fifteen minutes apart, forever. A key that is present and empty on every report
        from a panel with nothing wrong is how a log stops being read, and the counters
        already say when to go looking."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()
        c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.82", "uptime_ms": 900000, "int_largest": 81920},
            headers={"Authorization": f"Bearer {key}"},
        )
        out = capsys.readouterr().out
        assert "ota_err" not in out
        assert "heard" not in out
        # The always-present ones stay present: a zero here is a reading, not an absence.
        assert '"int_largest": 81920' in out
        assert '"wifi_drops": 0' in out

    def test_the_panel_s_whole_payload_fits_the_buffer_it_is_built_in(self) -> None:
        """The firmware builds this body with snprintf into a fixed `char body[N]`, and an
        overrun does not crash — it TRUNCATES, which until today produced JSON with no
        closing brace, a 422 from this route, and (because `ota_report` returned ESP_OK for
        any status it received) a panel that then cleared its crash ring on the strength of a
        report the box had thrown away. So the buffer must fit the worst case with room, and
        that worst case must be computed from the real declaration rather than assumed."""
        main_c = (Path(__file__).resolve().parents[3] / "firmware" / "main" / "main.c").read_text()
        m = re.search(r"char body\[(\d+)\];", main_c)
        assert m is not None, "the telemetry buffer declaration moved"
        cap = int(m.group(1))

        worst = {
            "version": "0.2.82",
            "uptime_ms": 4294967295,
            "reset_reason": "poweron_reset(1)",
            "free_heap": 4294967295,
            "free_psram": 4294967295,
            "mic_peak": 32767,
            "accel": [-32768, -32768, -32768],
            "stack_free": 999999,
            "crash_phase": -1,
            "alc": "ff-ff no-readback",
            "blit_ok": 999999999,
            "blit_fail": 999999999,
            "boot_btn": 999,
            "vocab_ok": 999,
            "vocab_bad": 999,
            "int_largest": 4294967295,
            "levels": "100!/100!",
            "blit_fail_total": 999999999,
            "blit_recov": 999999,
            "meter_fail": 999999999,
            "wifi_reason": 999,
            "wifi_drops": 999999,
            "ota_err": "ESP_ERR_OTA_VALIDATE_FAILED",
            "ota_tries": 999999,
            "restart_why": "blit-heal",
            "tap": [999, 999, 9],
            "screen": "awake",
            "pmu_history": ["0123456789abcdef0123456789a"] * 8,
            "vocab_refused": ["make a rude noise"] * 6,
            "heard": [["make a rude noise", 100, 1]] * 3,
        }
        wire = json.dumps(worst, separators=(",", ":"))
        assert len(wire) < cap, f"worst case {len(wire)} B does not fit {cap} B"
        # And every key really is one this route accepts — a field the panel spends bytes on
        # and the box silently drops is worse than one it never sent.
        assert set(worst) <= set(endpoint_api.TelemetryIn.model_fields)

    def test_a_sleeping_screen_is_distinguishable_from_a_dead_one(
        self, client: tuple[TestClient, Path, list[Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The screen sleep stops blitting on purpose, so a panel asleep on a bedside table
        reports exactly what a panel with a stalled render task reports: blit counters that
        have stopped moving. That fault cost 0.2.44 a photograph from the owner to diagnose,
        and the owner has no terminal to take a second one with — so the stage has to be in
        the report, not inferred from it."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={
                "version": "0.2.92",
                "uptime_ms": 36000000,
                "blit_ok": 41233,
                "blit_fail": 0,
                "screen": "dark",
            },
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text
        assert '"screen": "dark"' in capsys.readouterr().out

    def test_the_panel_actually_sends_the_screen_stage(self) -> None:
        """The other half of the same fault, and the one that has already happened once: the
        panel spent bytes on `tap` for months while pydantic dropped every one of them,
        because nothing checked that the two ends agreed. Read the key out of the firmware
        rather than trusting that it is still there."""
        main_c = (Path(__file__).resolve().parents[3] / "firmware" / "main" / "main.c").read_text()
        assert '\\"screen\\":' in main_c, "the panel stopped reporting its screen stage"
        assert "display_screen()" in main_c, "the stage is no longer read from the display"
        assert "screen" in endpoint_api.TelemetryIn.model_fields

    def test_the_pmu_history_survives_the_round_trip(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """The samples are the evidence. Anything that reshapes them on the way through is a
        second thing to be wrong about while chasing the first."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        history = ["00 01 4a 03 bf 00", "00 01 4a 03 bd 00"]
        resp = c.post(
            "/api/endpoint/telemetry",
            json={
                "version": "0.2.14",
                "uptime_ms": 1,
                "reset_reason": "sw",
                "pmu_history": history,
            },
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_a_cold_boot_reports_an_empty_history_rather_than_failing(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """Nothing survives a power cycle, and "we were not looking" has to be reportable —
        it is a different fact from "the PMU was fine"."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.14", "uptime_ms": 900, "reset_reason": "power"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_the_microphone_level_is_reported(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """A microphone has no symptom, and the panel is across the house. The meter drawn on
        the glass answers "is it working" for whoever is standing there; this answers it for
        whoever is not."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.15", "uptime_ms": 1000, "mic_peak": 9123},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_a_panel_that_omits_the_microphone_level_still_reports(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """Older firmware must keep reporting. A telemetry route that 422s on a field a
        deployed panel does not send would silence exactly the panel needing attention."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.14", "uptime_ms": 1000},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_the_accelerometer_is_reported_raw(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """Raw counts, because which axis points where on this board is undocumented. A
        firmware that reported "upright" would be asserting the very thing being measured."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.17", "uptime_ms": 1, "accel": [120, -8180, 240]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_the_render_task_stack_headroom_is_reported(
        self, client: tuple[TestClient, Path, list[Any]]
    ) -> None:
        """A panel panicked in the field and the render task's stack was the suspect. A
        shrinking headroom is a panic that has not happened yet; it has to be visible from the
        box, because the panel is on a charger in another room and its console resets it."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.24", "uptime_ms": 1, "stack_free": 1180},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text

    def test_the_crash_phase_is_reported(self, client: tuple[TestClient, Path, list[Any]]) -> None:
        """A panic's backtrace goes to a console the panel does not have and which resets it
        on open. The breadcrumb is what reaches the box instead."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        c.cookies.clear()

        resp = c.post(
            "/api/endpoint/telemetry",
            json={"version": "0.2.25", "uptime_ms": 1, "crash_phase": 10},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 204, resp.text


class _FakeLlm:
    """An LLM adapter that answers, or explodes on demand."""

    def __init__(self, text: str = "Hello! What are you playing?", boom: bool = False) -> None:
        self.text = text
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    async def complete(self, task: str, **kw: Any) -> Any:
        self.calls.append({"task": task, **kw})
        if self.boom:
            raise RuntimeError("no model loaded")
        return type("R", (), {"text": self.text, "parsed": None})()


def _wav_bytes(pcm: bytes, rate: int) -> bytes:
    import struct

    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


class TestConverse:
    """Press-and-hold conversation: audio in, audio out, and never a silent toy.

    The failure modes are the point. A pet in a child's bedroom that returns a 500 when a
    container is restarting is indistinguishable from a pet that is broken, so the one thing
    this route must never do is go quiet because a model was slow.
    """

    def _wire(
        self,
        c: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        *,
        heard: str = "what is your name",
        llm: "_FakeLlm | None" = None,
        tts_rate: int = 24000,
        tts_long_ms: int = 0,
        tts_pad_ms: int = 0,
    ) -> _FakeLlm:
        app = cast(FastAPI, c.app)
        app.state.settings.whisper_url = "http://tts-stt:8080/v1"
        app.state.settings.brain_tts_url = "http://tts-stt:8801"
        model = llm or _FakeLlm()
        app.state.llm_router = model

        async def fake_transcribe(self: Any, audio: bytes, **kw: Any) -> Any:
            return type("T", (), {"text": heard})()

        monkeypatch.setattr(endpoint_api.WhisperCppClient, "transcribe", fake_transcribe)

        class FakeHttp:
            async def __aenter__(self) -> "FakeHttp":
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def get(self, url: str, **kw: Any) -> Any:
                if tts_long_ms:
                    # A reply of a stated length, optionally with Kokoro's padding either
                    # side of it. A tone rather than a constant: the trim looks for peak
                    # structure, and a DC block has none.
                    import math

                    voice = array.array("h")
                    for _ in range(tts_pad_ms * tts_rate // 1000):
                        voice.append(0)
                    for i in range(tts_long_ms * tts_rate // 1000):
                        voice.append(int(11000 * math.sin(i * 0.15)))
                    for _ in range(tts_pad_ms * tts_rate // 1000):
                        voice.append(0)
                    pcm = voice.tobytes()
                else:
                    pcm = b"\x10\x00" * 480  # a fifth of a second of something
                # `request=` because `raise_for_status` refuses to judge a response that was
                # never sent — a detached Response raises RuntimeError, not HTTPStatusError.
                return httpx.Response(
                    200,
                    content=_wav_bytes(pcm, tts_rate),
                    request=httpx.Request("GET", url),
                )

        monkeypatch.setattr(endpoint_api.httpx, "AsyncClient", lambda **kw: FakeHttp())
        return model

    def test_a_turn_returns_playable_audio(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        model = self._wire(c, monkeypatch)
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 1600,
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200, r.text
        # Resampled 24 kHz -> 16 kHz, so two thirds of the samples, and an even byte count
        # because a half sample is a click.
        assert len(r.content) % 2 == 0
        assert 0 < len(r.content) < 480 * 2
        assert model.calls and model.calls[0]["task"] == "pet.turn"

    def test_a_dead_model_still_speaks(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason this mirrors the wall's `/internal/pet/say`."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        self._wire(c, monkeypatch, llm=_FakeLlm(boom=True))
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 1600,
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200, r.text
        assert len(r.content) > 0

    def test_silence_is_not_an_error(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An accidental hold uploads a room with nobody in it; that is not a failure face."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        self._wire(c, monkeypatch, heard="   ")
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 1600,
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 204

    def test_a_credential_is_required(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE COOKIE HAS TO GO FIRST, and the first version of this test did not do it.

        `PanelDep` accepts the owner's session cookie OR a panel's device key — deliberately,
        so the owner can drive a panel route from the PWA. The fixture logs in as the owner,
        so asserting a bogus bearer is rejected proved nothing: the cookie was answering.
        """
        c, _fw, _sent = client
        _provision_panel(c)
        self._wire(c, monkeypatch)
        c.cookies.clear()
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 16,
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        assert r.status_code == 401

    def test_a_long_hold_is_truncated_not_refused(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A panel held face-down in a bag must not be able to upload a minute of a room."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        self._wire(c, monkeypatch)
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * (endpoint_api.PANEL_AUDIO_MAX),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200, r.text

    def test_the_wav_walk_finds_the_data_chunk(self) -> None:
        """A fixed 44-byte skip is the bug that ships as a burst of noise before every reply:
        a WAV may carry LIST/INFO chunks before `data`, and Kokoro's layout is not promised."""
        import struct

        pcm = b"\x11\x22" * 100
        junk = b"LIST" + struct.pack("<I", 4) + b"INFO"
        wav = _wav_bytes(pcm, 24000)
        spliced = wav[:36] + junk + wav[36:]
        got, rate = endpoint_api._pcm_from_wav(spliced)
        assert got == pcm
        assert rate == 24000

    def test_resampling_keeps_the_rate_it_is_given(self) -> None:
        pcm = b"\x10\x00" * 300
        assert endpoint_api._to_panel_rate(pcm, endpoint_api.PANEL_RATE) == pcm
        out = endpoint_api._to_panel_rate(pcm, 48000)
        assert 0 < len(out) < len(pcm)
        assert len(out) % 2 == 0


class TestTrimToSpeech:
    """Finding the sentence inside the hold.

    Measured on the box 2026-09-22: every turn in the log reported `audio_ctx` 390-450 — the
    six-second cap — whatever was actually said, because the panel uploads its three-second
    lead-in and its 900 ms hush along with the speech. Whisper is charged for all of it, and
    with the LLM off the contended slot that padding became the largest term in a turn.

    The gate is relative to the room rather than a number, so these build clips the way a
    bedroom does: a noise floor everywhere, speech somewhere in the middle.
    """

    RATE = endpoint_api.PANEL_RATE

    def _clip(
        self, *, lead_ms: int, speech_ms: int, tail_ms: int, noise: int = 120, level: int = 9000
    ) -> bytes:
        import math

        out = array.array("h")
        for ms, amp in ((lead_ms, 0), (speech_ms, level), (tail_ms, 0)):
            for i in range(ms * self.RATE // 1000):
                # A tone, not a constant: a DC block has no peak structure and would let a
                # broken frame loop pass. The noise is deterministic for the same reason.
                voice = int(amp * math.sin(i * 0.2)) if amp else 0
                out.append(voice + (noise if i % 3 else -noise))
        return out.tobytes()

    def _ms(self, pcm: bytes) -> int:
        return len(pcm) * 1000 // (self.RATE * 2)

    def test_the_room_goes_and_the_sentence_stays(self) -> None:
        """The real shape: 3 s of waiting, 1.5 s of a child, 900 ms of hush."""
        clip = self._clip(lead_ms=3000, speech_ms=1500, tail_ms=900)
        assert self._ms(clip) == 5400
        got = endpoint_api._trim_to_speech(clip)
        # The speech plus its two margins, and nothing else: 200 before, 400 after.
        assert 2050 <= self._ms(got) <= 2250, self._ms(got)

    def test_the_margins_are_actually_there(self) -> None:
        """A window sized to a clip that clipped a word is a wrong answer read aloud, so the
        trim must keep room on both sides — and more after than before, because a four-year-old
        trailing off is the clip this would lose."""
        clip = self._clip(lead_ms=2000, speech_ms=1000, tail_ms=2000)
        got = endpoint_api._trim_to_speech(clip)
        kept = self._ms(got)
        assert kept > 1000 + 200 + 400 - 40, kept  # a frame of slack at each edge
        assert kept < 1000 + 200 + 400 + 60, kept

    def test_a_clip_with_no_speech_in_it_is_handed_over_whole(self) -> None:
        """Silence is `204` downstream — a judgement whisper makes, not this. Returning an
        empty clip here would turn a quiet room into a 400 from the WAV writer instead."""
        clip = self._clip(lead_ms=2000, speech_ms=0, tail_ms=2000)
        assert endpoint_api._trim_to_speech(clip) == clip

    def test_a_loud_room_does_not_deafen_a_quiet_child(self) -> None:
        """The reason the gate is not a constant. A fixed threshold tuned for a still bedroom
        finds speech everywhere once a fan is on, and trims nothing."""
        clip = self._clip(lead_ms=3000, speech_ms=1500, tail_ms=900, noise=1400, level=9000)
        got = endpoint_api._trim_to_speech(clip)
        assert self._ms(got) < 2600, self._ms(got)

    def test_a_slam_does_not_move_the_noise_floor(self) -> None:
        """Why the floor is a 10th percentile and not an average or a minimum: a single loud
        transient must leave the gate exactly where it was, so the child is still found."""
        quiet = self._clip(lead_ms=3000, speech_ms=1500, tail_ms=900)
        samples = array.array("h")
        samples.frombytes(quiet)
        for i in range(300):  # ~19 ms of door, in the lead-in, far louder than the voice
            samples[16000 + i] = 32000
        slammed = endpoint_api._trim_to_speech(samples.tobytes())
        # The door is at 1.0 s and the child starts at 3.0 s, so the kept span now reaches
        # back to the door — but it still runs all the way through the speech, which is the
        # part a gate raised by the transient would have lost.
        assert self._ms(slammed) > self._ms(endpoint_api._trim_to_speech(quiet))
        assert self._ms(slammed) < 4400, self._ms(slammed)

    def test_a_single_word_still_gets_a_window_whisper_can_place(self) -> None:
        """ "No!" is a real turn, and 150 ms of it is a clip whisper guesses at. The two
        margins are what put a floor under it — and the hold around it still has to go."""
        clip = self._clip(lead_ms=2000, speech_ms=150, tail_ms=2000)
        got = endpoint_api._trim_to_speech(clip)
        assert 600 <= self._ms(got) <= 800, self._ms(got)

    def test_a_clip_too_short_to_read_is_left_alone(self) -> None:
        got = endpoint_api._trim_to_speech(b"\x00\x01" * 200)
        assert got == b"\x00\x01" * 200

    def test_an_odd_byte_count_does_not_raise(self) -> None:
        """The panel streams raw s16 with no framing; a cut connection can land mid-sample."""
        clip = self._clip(lead_ms=500, speech_ms=500, tail_ms=500) + b"\x7f"
        assert len(endpoint_api._trim_to_speech(clip)) % 2 == 0

    def test_the_window_follows_the_speech_not_the_hold(self) -> None:
        """The whole point, end to end: the same sentence held for six seconds and for three
        must cost whisper the same, because it is the same sentence."""
        long_hold = endpoint_api._trim_to_speech(
            self._clip(lead_ms=3500, speech_ms=1500, tail_ms=1000)
        )
        short_hold = endpoint_api._trim_to_speech(
            self._clip(lead_ms=400, speech_ms=1500, tail_ms=900)
        )
        assert abs(self._ms(long_hold) - self._ms(short_hold)) < 60


class TestPanelMemory:
    """The conversation, for as long as it is one.

    The owner, on the pet: *"it also asks to play a game a lot"*. Part of that was the prompt,
    but the route also sent one utterance and nothing else — so every turn of a six-turn
    hands-free conversation arrived as the first thing anyone had ever said, and a model with
    no idea it asked about a game last time asks about a game again.
    """

    @pytest.fixture(autouse=True)
    def _clean(self) -> Iterator[None]:
        endpoint_api._panel_memory.clear()
        yield
        endpoint_api._panel_memory.clear()

    def test_the_second_turn_knows_about_the_first(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        model = TestConverse()._wire(c, monkeypatch, heard="I ate toast")
        for _ in range(2):
            c.post(
                "/api/endpoint/converse",
                content=b"\x00\x01" * 1600,
                headers={"Authorization": f"Bearer {key}"},
            )
        assert len(model.calls) == 2
        # The first turn cannot know anything; the second must carry both halves of it.
        assert "I ate toast" not in model.calls[0]["system"]
        assert "I ate toast" in model.calls[1]["system"]
        assert model.calls[1]["system"].count(_FakeLlm().text) >= 1

    def test_a_babble_is_not_remembered(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead model makes the toy apologise for itself. Feeding that back as something it
        said teaches the next turn to apologise too."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        model = TestConverse()._wire(c, monkeypatch, llm=_FakeLlm(boom=True), heard="hello")
        for _ in range(2):
            c.post(
                "/api/endpoint/converse",
                content=b"\x00\x01" * 1600,
                headers={"Authorization": f"Bearer {key}"},
            )
        assert len(model.calls) == 2
        assert not any(b in model.calls[1]["system"] for b in endpoint_api.PANEL_BABBLE)
        assert "hello" not in model.calls[1]["system"]

    def test_one_panel_cannot_read_another_panel_s_conversation(self) -> None:
        endpoint_api._panel_remember("panel-a", 100.0, "my chickens", "which one is yours?")
        assert endpoint_api._panel_history("panel-b", 100.0) == []

    def test_a_conversation_that_stopped_is_over(self) -> None:
        """The same child after tea is starting again, not on turn seven."""
        endpoint_api._panel_remember("panel-a", 100.0, "my chickens", "which one is yours?")
        still_going = 100.0 + endpoint_api._PANEL_MEMORY_TTL_S - 1
        assert endpoint_api._panel_history("panel-a", still_going)
        assert (
            endpoint_api._panel_history("panel-a", 100.0 + endpoint_api._PANEL_MEMORY_TTL_S + 1)
            == []
        )

    def test_a_long_conversation_keeps_only_the_recent_end_of_it(self) -> None:
        """A bounded prompt and a bounded dict. The panel caps itself at six hands-free turns,
        but a child pressing the face can go all afternoon."""
        for i in range(20):
            endpoint_api._panel_remember("panel-a", 100.0 + i, f"thing {i}", f"reply {i}")
        turns = endpoint_api._panel_history("panel-a", 120.0)
        assert len(turns) == endpoint_api._PANEL_TURNS_KEPT
        assert turns[-1] == ("thing 19", "reply 19")

    def test_the_dead_conversations_do_not_pile_up(self) -> None:
        """The dict is swept on read, so an expired panel is not a leak waiting for a restart."""
        for i in range(50):
            endpoint_api._panel_remember(f"panel-{i}", 100.0, "hi", "hello")
        endpoint_api._panel_history("panel-0", 100.0 + endpoint_api._PANEL_MEMORY_TTL_S + 1)
        assert endpoint_api._panel_memory == {}

    def test_the_prompt_does_not_offer_what_the_panel_cannot_do(self) -> None:
        """The owner: the pet *"could be more of a conversationalist talking about what the
        kid is doing or what the kid is eating or what the kid did today... talk about his
        toys"*. A screen with a speaker cannot fetch a ball, and offering to is a promise a
        four-year-old will hold it to.

        This used to assert the literal words "cannot play games", and that line moved on
        purpose: a voice CAN play a guessing game, and the blanket ban was over-broad in the
        same way "cannot do anything" was — see `TestPanelPrompt`, where refusing to tell a
        joke came from exactly that clause. So what is pinned now is the RULE rather than the
        sentence."""
        prompt = endpoint_api.PANEL_CONVERSATION_PROMPT.lower()
        assert "never offer" in prompt, "the no-broken-promises rule is gone"
        for impossible in ("no body", "look at things", "go anywhere"):
            assert impossible in prompt, f"the prompt no longer rules out {impossible!r}"
        for subject in ("what they ate", "what they did today", "their toys"):
            assert subject in prompt


class TestReplyCeiling:
    """What the panel can actually play, and the fact that it never said so.

    `firmware/main/talk.c` reads the reply into a fixed PSRAM buffer and `audio_play`
    truncates to the same ceiling — silently, with nothing on screen and nothing in a log.
    The owner: *"sometimes when the robot is talking back on a longer reply I get cut off."*
    Measured in the box log the same afternoon: replies of 221,012 and 261,290 bytes against
    the 192,000 the panel then held, so the 261 KB one lost its last 2.2 seconds mid-word.
    """

    def test_the_ceiling_is_the_panel_s_buffer(self) -> None:
        """Both ends say ten seconds. If this ever drifts, a reply is cut off in a bedroom
        and nothing anywhere says why — which is exactly how it was found."""
        firmware = Path(__file__).resolve().parents[3] / "firmware" / "main" / "talk.c"
        text = firmware.read_text()
        assert "#define REPLY_MAX_BYTES (16000 * 2 * 10)" in text
        assert endpoint_api.PANEL_REPLY_MAX == 16000 * 2 * 10

    def test_a_reply_over_the_ceiling_is_cut_but_never_quietly(
        self,
        client: tuple[TestClient, Path, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch, tts_long_ms=14000)
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 1600,
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200, r.text
        assert len(r.content) == endpoint_api.PANEL_REPLY_MAX
        # structlog renders to stdout, not through the logging module, so `caplog` sees
        # nothing here and a test written against it would pass on a silent truncation —
        # which is the one thing this is checking cannot happen.
        out = capsys.readouterr().out
        assert "converse_reply_truncated" in out
        assert '"lost_ms": 4000' in out

    def test_a_reply_inside_the_ceiling_is_left_whole(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch, tts_long_ms=2000)
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 1600,
            headers={"Authorization": f"Bearer {key}"},
        )
        assert 0 < len(r.content) < endpoint_api.PANEL_REPLY_MAX

    def test_the_recording_window_outlasts_the_silence_it_waits_for(self) -> None:
        """The cut-off bug, as an invariant.

        The panel opens the microphone, waits `LISTEN_LEAD_MS` for a child to start, records,
        and needs `LISTEN_HUSH_MS` of quiet to decide they finished — all inside
        `CAPTURE_MAX_MS`. At 3000 + 900 the old six-second cap left 2.1 s for the sentence
        itself, so a child who took a moment to start and paused once in the middle ran out of
        recording before the hush could fire. The owner: *"the babies keep getting cut off
        because they're a little bit slow."*

        There must be room for the waiting AND a real sentence, and the box must accept
        whatever the panel is willing to send, or the tail is cut off at the other end
        instead.
        """
        main = Path(__file__).resolve().parents[3] / "firmware" / "main"

        def const(path: str, name: str) -> int:
            m = re.search(rf"^#define {name} (\d+)$", (main / path).read_text(), re.M)
            assert m is not None, f"{name} not found in {path}"
            return int(m.group(1))

        lead = const("display.c", "LISTEN_LEAD_MS")
        hush = const("display.c", "LISTEN_HUSH_MS")
        cap = const("audio.c", "CAPTURE_MAX_MS")
        assert hush >= 1500, "a four-year-old pauses mid-sentence for longer than an adult"
        # Four seconds of actual sentence left over, which is a long one at this age.
        assert cap - lead - hush >= 4000, (lead, hush, cap)
        assert endpoint_api.PANEL_RATE * 2 * cap // 1000 <= endpoint_api.PANEL_AUDIO_MAX

    def test_the_reply_does_not_start_with_a_beat_of_nothing(
        self, client: tuple[TestClient, Path, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kokoro pads, and the panel plays what it is handed from the first sample — so the
        padding is dead air before every single reply, and it counts against the ceiling."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch, tts_long_ms=1000, tts_pad_ms=2000)
        r = c.post(
            "/api/endpoint/converse",
            content=b"\x00\x01" * 1600,
            headers={"Authorization": f"Bearer {key}"},
        )
        spoken_ms = len(r.content) * 1000 // (endpoint_api.PANEL_RATE * 2)
        # 1 s of speech plus the 30/120 ms margins, not 1 s of speech plus 4 s of padding.
        assert 1000 <= spoken_ms <= 1400, spoken_ms


class TestWakePrefix:
    """The pet's name, taken off the front of what it heard."""

    def test_the_name_comes_off_the_front_of_a_question(self) -> None:
        # The owner: "if I say hey fish and then proceed with asking it something, it
        # shouldn't be transcribed hey fish at the beginning." Whisper punctuates and
        # capitalises as it likes, so the shapes matter more than the exact string.
        # Exact expectations per input: an `or` across two acceptable answers would pass on a
        # stripper that mangled the case or ate a word, which is the failure worth catching.
        cases = {
            "Hey fish, what do dogs eat?": "what do dogs eat?",
            "hey fish what do dogs eat?": "what do dogs eat?",
            "Hey, Fish! What do dogs eat?": "What do dogs eat?",
            "  hey  fish  -  what do dogs eat?": "what do dogs eat?",
            "Hey fishy, what do dogs eat?": "what do dogs eat?",
        }
        for said, want in cases.items():
            assert endpoint_api._strip_wake_prefix(said) == want, said

    def test_a_wake_on_its_own_leaves_nothing_to_answer(self) -> None:
        # Which the caller already handles: empty means "say that again", not an error. That
        # is the right answer to an accidental wake and a better one than a reply about fish.
        assert endpoint_api._strip_wake_prefix("Hey fish.") == ""
        assert endpoint_api._strip_wake_prefix("hey fish") == ""

    def test_the_name_is_only_stripped_as_a_prefix(self) -> None:
        # In the middle of a sentence it is a child talking ABOUT the pet, and deleting it
        # would change what they said.
        assert endpoint_api._strip_wake_prefix("I told hey fish a joke") == "I told hey fish a joke"
        assert endpoint_api._strip_wake_prefix("what is a fish") == "what is a fish"

    def test_it_does_not_eat_a_word_that_merely_starts_with_the_name(self) -> None:
        # The trailing \b in the pattern is the whole reason this passes.
        assert endpoint_api._strip_wake_prefix("hey fisherman") == "hey fisherman"

    def test_an_ordinary_question_is_untouched(self) -> None:
        assert endpoint_api._strip_wake_prefix("what do dogs eat?") == "what do dogs eat?"
        assert endpoint_api._strip_wake_prefix("") == ""

    def test_it_strips_the_phrase_the_firmware_actually_listens_for(self) -> None:
        """THE COUPLING, PINNED — and it has just caught its first real break.

        The wake phrase used to be a string literal in the firmware's vocabulary table and the
        pattern here was a copy of it. It is now `s_listen`, a buffer the box can rewrite at
        runtime (migration 0212), because the owner has to be able to rename the pet without a
        cable. This still reads the firmware rather than restating the name, for exactly the
        original reason: a change to the default that did not reach the box would leave it
        stripping a name the panel no longer answers to, and the symptom is the one the owner
        reported returning.
        """
        vocab = Path(__file__).resolve().parents[3] / "firmware" / "main" / "vocab.c"
        text = vocab.read_text()
        match = re.search(r'static char s_listen\[\d+\] = "([^"]+)";', text)
        assert match is not None, "no s_listen default in vocab.c"
        phrase = match.group(1)
        assert (
            endpoint_api._strip_wake_prefix(f"{phrase} what do dogs eat?") == "what do dogs eat?"
        ), f"the firmware ships listening for {phrase!r} and the box does not strip it"

    def test_the_listen_entry_still_points_at_the_settable_buffer(self) -> None:
        """The other half of the same coupling. `vocab_set_name` rewrites `s_listen`, and it only
        reaches MultiNet because the `VOCAB_LISTEN` row points AT that buffer rather than holding
        a literal of its own. Re-inlining the phrase would leave renaming silently ineffective on
        the panel while every test above still passed.

        BOTH BUFFERS NOW, for the same reason twice over: the row carries the phrase and its
        PHONEMES, and MultiNet7 matches the phonemes. A row holding a literal pronunciation
        would leave the panel listening for the sound of whatever name the firmware was built
        with, however the box renamed it — the same silence, one level further down.
        """
        vocab = Path(__file__).resolve().parents[3] / "firmware" / "main" / "vocab.c"
        assert (
            re.search(r"\{s_listen,\s*s_listen_phon,\s*VOCAB_LISTEN", vocab.read_text()) is not None
        ), "the listen row no longer points at both buffers — renaming the pet would do nothing"

    def test_a_renamed_pet_has_ITS_name_stripped(self) -> None:
        """THE BUG THIS FEATURE WOULD HAVE SHIPPED WITH. Once the pet can be renamed, a box that
        went on stripping `fish` would send `hey pip what do dogs eat?` to the model — the wake
        word reaching the pet as part of the question, which is the exact complaint the stripper
        exists to answer."""
        assert endpoint_api._strip_wake_prefix("hey pip what do dogs eat?", "Pip") == (
            "what do dogs eat?"
        )
        assert endpoint_api._strip_wake_prefix("Hey, Pip! What do dogs eat?", "Pip") == (
            "What do dogs eat?"
        )
        # Still only a prefix, and still not eating a longer word that starts with the name.
        assert endpoint_api._strip_wake_prefix("I told hey pip a joke", "Pip") == (
            "I told hey pip a joke"
        )
        assert endpoint_api._strip_wake_prefix("hey pippin", "Pip") == "hey pippin"

    def test_the_shipped_name_is_used_when_no_rename_has_happened(self) -> None:
        """Empty means "whatever the firmware shipped with", so the default keeps its observed
        Whisper spellings — which a renamed pet cannot have, because nobody knows in advance how
        Whisper will spell a name it has never been given."""
        assert endpoint_api._strip_wake_prefix("hey fishy what do dogs eat?", "") == (
            "what do dogs eat?"
        )


class TestPanelPrompt:
    """What the pet is allowed to be."""

    def test_the_pet_is_allowed_to_tell_a_joke(self) -> None:
        """The owner: it "was like refusing to tell me a joke saying it was a robot and
        couldn't do that."

        The cause was this prompt, and the clause was added on purpose: to stop the pet
        OFFERING games it cannot play, because an offer it cannot keep is a promise broken and
        a four-year-old holds you to it. But it was written as "you cannot play games, look at
        things, go anywhere or DO ANYTHING, so never offer to" — and "cannot do anything" is a
        blanket refusal. Telling a joke is doing something.

        Both halves are pinned here, because the obvious repair is to delete the constraint and
        that would bring back the broken promises instead. The pet must still be told never to
        offer what it cannot keep, AND must not be told it can do nothing."""
        prompt = endpoint_api.PANEL_CONVERSATION_PROMPT.lower()
        assert "or do anything" not in prompt, (
            "the blanket refusal is back: a pet told it cannot DO ANYTHING refuses jokes"
        )
        assert "joke" in prompt, "nothing tells the pet that jokes are allowed"
        assert "never offer" in prompt, (
            "the no-broken-promises rule was deleted rather than narrowed"
        )

    def test_the_prompt_uses_the_techniques_that_actually_grow_language(self) -> None:
        """Not hand-tuned any more. The strategies here are the named ones from the early
        language-development literature, because "be warm and curious" is a vibe and these are
        instructions a 4B model can follow:

        - FOLLOW THE CHILD'S LEAD (serve-and-return): stay on their subject.
        - LINGUISTIC EXPANSION: affirm, restate in fuller words, add one idea. This is the
          highest-value one, and it doubles as the rule against correcting a four-year-old —
          you never say "wrong", you say it back properly.
        - ONE OPEN-ENDED PROMPT: a question they can answer yes-or-no ends the conversation,
          and a string of them is a quiz rather than a talk.

        Plus one the literature does not cover and the logs did: the transcriber mangles small
        children ("tell us a joke" arrived as "There is a joke. There is a joke."), so the pet
        has to interpret charitably rather than bouncing "say that again" back at them."""
        prompt = endpoint_api.PANEL_CONVERSATION_PROMPT.lower()
        assert "follow their lead" in prompt
        assert "never correct them" in prompt, "the pet may end up correcting a four-year-old"
        assert "open question" in prompt
        assert "one question per reply" in prompt
        assert "guess" in prompt, "nothing tells the pet what to do with a mangled transcript"
