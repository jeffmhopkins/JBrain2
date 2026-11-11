"""Flashing a GrowBot from the PWA (api/growbot_flash.py), the status card, revoke and the LAN
root download (api/lan_ca.py).

The endpoint sidecar and the growbot sidecar are httpx.MockTransports scripted per test; the
repo is an in-memory stand-in with the real one's rules (the SQL and its RLS run against real
Postgres in tests/integration/test_growbot_rls.py). The job runs as a real asyncio task on the
TestClient's loop and is polled the way the PWA polls it.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import secrets as pysecrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import endpoint as endpoint_api
from jbrain.api import growbot, growbot_flash, lan_ca
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.growbot import provision
from jbrain.growbot.repo import RobotInfo
from jbrain.homenet import adguard
from jbrain.settings_store import ENDPOINT_WIFI_KEY
from jbrain.storage import FsInternalSecrets
from tests.unit.test_growbot_provision import wifi_store

OWNER = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")
OWNER_CTX = SessionContext(principal_id=OWNER.id, principal_kind="owner")
DEBUG_CTX = SessionContext(principal_id="debug-console", principal_kind="owner")
BOX = "192.168.1.20"
UF2 = growbot_flash.UF2_NAME
NOW = dt.datetime(2026, 11, 10, tzinfo=dt.UTC)


def _ca() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "box root")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(NOW)
        .not_valid_after(NOW + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


CA = _ca()


# -- fakes -----------------------------------------------------------------------------------


@dataclass
class FakeRepo:
    robots: dict[str, RobotInfo] = field(default_factory=dict)
    keys: dict[str, str | None] = field(default_factory=dict)
    contexts: list[SessionContext] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    fail_put: bool = False

    def add(self, code: str, name: str) -> None:
        self.robots[code] = RobotInfo(code, NOW, None, name)
        self.keys[code] = "old-key-" + code

    async def put_robot(self, ctx: SessionContext, code: str, name: str | None = None) -> str:
        self.contexts.append(ctx)
        if self.fail_put:
            raise RuntimeError("db down")
        key = pysecrets.token_urlsafe(32)
        old = self.robots.get(code)
        if old is None:
            self.robots[code] = RobotInfo(code, NOW, None, name or "")
        else:
            self.robots[code] = replace(
                old, rotated_at=NOW, revoked_at=None, name=old.name if name is None else name
            )
        self.keys[code] = key
        return key

    async def list_robots(self, ctx: SessionContext) -> list[RobotInfo]:
        self.contexts.append(ctx)
        return [self.robots[c] for c in sorted(self.robots)]

    async def revoke_robot(self, ctx: SessionContext, code: str) -> RobotInfo | None:
        self.contexts.append(ctx)
        if code not in self.robots:
            return None
        self.keys[code] = None
        self.revoked.append(code)
        self.robots[code] = replace(self.robots[code], revoked_at=NOW)
        return self.robots[code]

    async def delete_robot(self, ctx: SessionContext, code: str) -> bool:
        self.keys.pop(code, None)
        return self.robots.pop(code, None) is not None

    def seen(self, code: str) -> None:
        self.robots[code] = replace(self.robots[code], last_seen_at=dt.datetime.now(dt.UTC))


@dataclass
class FakeStore:
    rows: dict[str, Any] = field(default_factory=dict)

    async def get(self, ctx: SessionContext, key: str, default: Any = None) -> Any:
        return self.rows.get(key, default)

    async def upsert(self, ctx: SessionContext, key: str, value: Any) -> None:
        self.rows[key] = value


def _tag(line: str) -> str:
    """The sidecar's own line tags: `@ ` for its marks, `| ` for any other text."""
    if line.startswith(("@ ", "| ")):
        return line
    mark = line.startswith(("FAILED: ", "port=", "OK ")) or line in ("OK", "copied", "key written")
    return ("@ " if mark else "| ") + line


def _stream(*lines: str) -> httpx.Response:
    return httpx.Response(200, content="".join(f"{_tag(line)}\n" for line in lines).encode())


@dataclass
class Sidecar:
    """The endpoint sidecar, scripted: each route's answer is a callable of the request
    body, so a test can also act (stamp last_seen) when the robot "joins"."""

    repo: FakeRepo
    ports: dict[str, Any] = field(default_factory=dict)
    code: str = "gb-abc123"
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    load: Callable[[dict[str, Any]], httpx.Response] | None = None
    identify: Callable[[dict[str, Any]], httpx.Response] | None = None
    provision: Callable[[dict[str, Any]], httpx.Response] | None = None
    rekey: Callable[[dict[str, Any]], httpx.Response] | None = None

    def __post_init__(self) -> None:
        self.boot_only()
        self.load = self.load or (lambda b: _stream("picotool load", "OK port=/dev/ttyACM0"))
        self.identify = self.identify or (
            lambda b: httpx.Response(
                200, json={"code": self.code, "micropython": "1.28.0", "machine": "RP2350"}
            )
        )
        self.provision = self.provision or self.joins
        self.rekey = self.rekey or self.rekey_joins

    def boot_only(self) -> None:
        self.ports = {
            "ports": [],
            "pico_boot": [
                {"bus": 1, "address": 9, "pid": "000f", "serial": "S1", "label": "RP2350 boot"}
            ],
        }

    def micropython(self) -> None:
        self.ports = {
            "ports": [
                {
                    "device": "/dev/ttyACM0",
                    "label": "Pico MicroPython",
                    "is_espressif": False,
                    "is_pico": True,
                    "vid": "2e8a",
                    "pid": "0005",
                },
                {
                    "device": "/dev/ttyACM1",
                    "label": "panel",
                    "is_espressif": True,
                    "is_pico": False,
                    "vid": "303a",
                    "pid": "1001",
                },
            ],
            "pico_boot": [],
        }

    def joins(self, body: dict[str, Any]) -> httpx.Response:
        self.repo.seen(self.code)
        return _stream(
            "restarting the robot", "port=/dev/ttyACM2", "copied", "WIFI_OK 10.0.0.9", "OK"
        )

    def rekey_joins(self, body: dict[str, Any]) -> httpx.Response:
        self.repo.seen(self.code)
        return _stream("key written", "WIFI_OK 10.0.0.9", "handshake: OK", "OK")

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/ports":
            return httpx.Response(200, json=self.ports)
        route = {
            "/pico/load": self.load,
            "/pico/identify": self.identify,
            "/pico/provision": self.provision,
            "/pico/rekey": self.rekey,
        }.get(path)
        assert route is not None, path
        return route(body)

    def called(self, path: str) -> list[dict[str, Any]]:
        return [b for p, b in self.calls if p == path]


@dataclass
class Relay:
    """The growbot sidecar's /internal routes."""

    connected: list[str] = field(default_factory=list)
    down: bool = False
    drops: list[str] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization", ""))
        if self.down:
            raise httpx.ConnectError("down")
        if request.url.path == "/internal/robots":
            return httpx.Response(
                200, json={"robots": [{"code": c, "since": "x"} for c in self.connected]}
            )
        self.drops.append(json.loads(request.content)["code"])
        return httpx.Response(200, json={"dropped": True})


def _firmware(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    files = {UF2: b"UF2\n" * 100}
    for src, _ in provision.FILE_MAP:
        files[src] = f"# {src}\n".encode()
    for name, data in files.items():
        (root / name).write_bytes(data)
    (root / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items())
    )
    return root


@dataclass
class Rig:
    app: FastAPI
    repo: FakeRepo
    store: FakeStore
    sidecar: Sidecar
    relay: Relay
    settings: SimpleNamespace
    box_host: list[str | None]
    ca: list[str]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    repo = FakeRepo()
    store = FakeStore()
    sidecar = Sidecar(repo)
    relay = Relay()
    settings = SimpleNamespace(
        endpoint_url="http://endpoint:8000",
        lan_addr="https://jbrain.local",
        jcode_preview_base_host="",
        site_addr="http://example.com",
        growbot_firmware_dir=str(_firmware(tmp_path / "fw")),
        growbot_url="http://growbot:8810",
        adguard_url="http://adguard",
    )
    app = FastAPI()
    app.include_router(growbot.router, prefix="/api")
    app.include_router(growbot_flash.router, prefix="/api")
    app.include_router(growbot_flash.debug_router, prefix="/api")
    app.include_router(lan_ca.router, prefix="/api")
    app.state.settings = settings
    app.state.growbot_repo = repo
    (tmp_path / "secrets").mkdir()
    secrets = FsInternalSecrets(tmp_path / "secrets")
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    app.dependency_overrides[owner_only] = lambda: OWNER
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    box_host: list[str | None] = [BOX]
    ca = [CA]

    async def adguard_host(request: Any, settings: Any) -> str | None:
        return box_host[0]

    monkeypatch.setattr(growbot_flash, "_transport", httpx.MockTransport(sidecar.handler))
    monkeypatch.setattr(growbot, "_transport", httpx.MockTransport(relay.handler))
    monkeypatch.setattr(growbot_flash, "_adguard_host", adguard_host)
    monkeypatch.setattr(growbot_flash, "_wifi_store", lambda request: store)
    monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: ca[0])
    monkeypatch.setattr(growbot_flash, "JOIN_POLL_S", 0.01)
    yield Rig(app, repo, store, sidecar, relay, settings, box_host, ca)


NEW = {
    "mode": "flash",
    "target": "new",
    "name": "Quorra",
    "networks": [{"ssid": "home", "password": "correct horse battery"}],
}


def _wait(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get("/api/growbot/flash").json()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("the job never finished")


def _flash(
    rig: Rig, body: dict[str, Any] | None = None, path: str = "/api/growbot/flash"
) -> dict[str, Any]:
    with TestClient(rig.app) as client:
        r = client.post(path, json=body or NEW)
        assert r.status_code == 202, r.text
        assert r.json()["job"]["state"] == "running"
        return _wait(client)


def _refused(rig: Rig, body: dict[str, Any]) -> httpx.Response:
    with TestClient(rig.app) as client:
        return client.post("/api/growbot/flash", json=body)


# -- a whole flash ----------------------------------------------------------------------------


def test_a_blank_board_is_loaded_copied_and_joins(rig: Rig) -> None:
    job = _flash(rig)
    assert job["state"] == "ok", job
    assert job["phases"] == {"load": "done", "copy": "done", "join": "done"}
    assert job["code"] == "gb-abc123" and job["name"] == "Quorra"
    assert job["port"] == "/dev/ttyACM2" and job["box_host"] == BOX
    assert job["fail"] is None and job["finished_at"]
    assert set(job) == {
        "id", "mode", "state", "name", "code", "port", "box_host", "phases", "log", "fail",
        "started_at", "finished_at",
    }  # fmt: skip

    (load,) = rig.sidecar.called("/pico/load")
    assert load["port"] is None
    assert load["sha256"] == hashlib.sha256(base64.b64decode(load["uf2_b64"])).hexdigest()

    (prov,) = rig.sidecar.called("/pico/provision")
    assert prov["port"] == "/dev/ttyACM0" and prov["watch_s"] == growbot_flash.WATCH_S
    assert [f["name"] for f in prov["files"]][-1] == "main.py"
    assert {f["name"] for f in prov["files"]} == {d for _, d in provision.FILE_MAP}
    text = base64.b64decode(prov["secrets_b64"]).decode()
    assert wifi_store.parse_config(text)[1] == [("home", "correct horse battery")]
    key = rig.repo.keys["gb-abc123"]
    assert key and f'GROWBOT_KEY = "{key}"' in text and f'GROWBOT_HOST = "{BOX}"' in text
    assert 'GROWBOT_SNI = "jbrain.local"' in text
    assert rig.repo.robots["gb-abc123"].name == "Quorra"
    # The key and the password never reach the log.
    assert key not in json.dumps(job) and "correct horse" not in json.dumps(job)
    assert rig.repo.contexts and set(rig.repo.contexts) == {OWNER_CTX}


def test_micropython_already_on_it_skips_the_load(rig: Rig) -> None:
    rig.sidecar.micropython()
    job = _flash(rig)
    assert job["state"] == "ok"
    assert job["phases"]["load"] == "skip"
    assert rig.sidecar.called("/pico/load") == []
    assert rig.sidecar.called("/pico/identify") == [{"port": "/dev/ttyACM0"}]


def test_reinstall_loads_over_a_running_board(rig: Rig) -> None:
    rig.sidecar.micropython()
    job = _flash(rig, {**NEW, "reinstall": True})
    assert job["phases"]["load"] == "done"
    assert rig.sidecar.called("/pico/load")[0]["port"] == "/dev/ttyACM0"


def test_a_new_robot_whose_code_exists_reflashes_that_row(rig: Rig) -> None:
    rig.repo.add("gb-abc123", "Tamsin")
    job = _flash(rig, {**NEW, "name": "Quorra"})
    assert job["state"] == "ok"
    assert any("already paired as Tamsin" in line for line in job["log"])
    assert rig.repo.robots["gb-abc123"].name == "Quorra"


def test_reflashing_a_named_robot_keeps_its_name(rig: Rig) -> None:
    rig.repo.add("gb-abc123", "Tamsin")
    job = _flash(rig, {**NEW, "target": "gb-abc123", "name": None})
    assert job["state"] == "ok" and job["name"] == "Tamsin"
    assert rig.repo.robots["gb-abc123"].name == "Tamsin"


def test_the_wrong_board_is_refused_before_any_key_changes(rig: Rig) -> None:
    rig.repo.add("gb-zzz999", "Tamsin")
    job = _flash(rig, {**NEW, "target": "gb-zzz999"})
    assert job["state"] == "fail" and job["fail"]["code"] == "wrong_robot"
    assert job["phases"] == {"load": "done", "copy": "fail", "join": "wait"}
    assert rig.repo.keys == {"gb-zzz999": "old-key-gb-zzz999"}
    assert "gb-abc123" not in rig.repo.robots
    assert rig.sidecar.called("/pico/provision") == []


@pytest.mark.parametrize(
    ("code", "phases"),
    [
        ("not_bootsel", {"load": "fail", "copy": "wait", "join": "wait"}),
        ("load_failed", {"load": "fail", "copy": "wait", "join": "wait"}),
        ("usb_gone", {"load": "fail", "copy": "wait", "join": "wait"}),
        ("something_new", {"load": "fail", "copy": "wait", "join": "wait"}),
    ],
)
def test_a_load_failure_ends_the_job_and_mints_nothing(
    rig: Rig, code: str, phases: dict[str, str]
) -> None:
    rig.sidecar.load = lambda b: _stream("ERROR: no device", f"FAILED: {code}: no RP2350")
    job = _flash(rig)
    expected = code if code in growbot_flash.FAIL_CODES else "internal"
    assert job["fail"] == {"code": expected, "message": growbot_flash.RECOVERY[expected]}
    assert job["phases"] == phases
    assert rig.repo.robots == {}


def test_a_load_that_names_no_port_is_usb_gone(rig: Rig) -> None:
    rig.sidecar.load = lambda b: _stream("OK")
    assert _flash(rig)["fail"]["code"] == "usb_gone"


def test_wifi_that_does_not_join_fails_the_join_phase(rig: Rig) -> None:
    rig.sidecar.provision = lambda b: _stream(
        "copied", "WIFI_FAIL", "FAILED: wifi_failed: it could not join any of its networks"
    )
    job = _flash(rig)
    assert job["fail"]["code"] == "wifi_failed"
    assert job["phases"] == {"load": "done", "copy": "done", "join": "fail"}


def test_a_copy_that_fails_fails_the_copy_phase(rig: Rig) -> None:
    rig.sidecar.provision = lambda b: _stream("FAILED: copy_failed: mpremote exited 1")
    job = _flash(rig)
    assert job["fail"]["code"] == "copy_failed"
    assert job["phases"] == {"load": "done", "copy": "fail", "join": "wait"}


def test_joined_wifi_but_never_on_the_relay_is_box_unreached(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(growbot_flash, "JOIN_WAIT_S", 0.1)
    rig.sidecar.provision = lambda b: _stream("copied", "WIFI_OK 10.0.0.9", "OK")
    job = _flash(rig)
    assert job["fail"]["code"] == "box_unreached"
    assert job["phases"]["join"] == "fail"


def test_a_last_seen_from_before_the_flash_is_not_a_join(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(growbot_flash, "JOIN_WAIT_S", 0.1)
    rig.repo.add("gb-abc123", "Tamsin")
    rig.repo.robots["gb-abc123"] = replace(rig.repo.robots["gb-abc123"], last_seen_at=NOW)
    rig.sidecar.provision = lambda b: _stream("copied", "WIFI_OK 10.0.0.9", "OK")
    assert _flash(rig)["fail"]["code"] == "box_unreached"


def test_secrets_echoed_by_the_sidecar_are_redacted(rig: Rig) -> None:
    def echo(body: dict[str, Any]) -> httpx.Response:
        rig.repo.seen("gb-abc123")
        text = base64.b64decode(body["secrets_b64"]).decode()
        return _stream(*text.splitlines(), "copied", "OK")

    job = _flash(rig)
    rig.sidecar.provision = echo
    job = _flash(rig)
    dumped = json.dumps(job)
    assert "correct horse battery" not in dumped
    key = rig.repo.keys["gb-abc123"]
    assert key and key not in dumped
    assert "[redacted]" in dumped


def test_the_log_is_bounded(rig: Rig) -> None:
    def chatty(body: dict[str, Any]) -> httpx.Response:
        rig.repo.seen("gb-abc123")
        return _stream(*(f"line {i}" for i in range(1000)), "copied", "OK")

    rig.sidecar.provision = chatty
    job = _flash(rig)
    assert len(job["log"]) == growbot_flash.LOG_MAX
    assert job["log"][-1].startswith("OK")


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (
            lambda b: httpx.Response(502, json={"error": "usb_gone: x", "code": "usb_gone"}),
            "usb_gone",
        ),
        (lambda b: httpx.Response(502, json={"error": "?", "code": "made_up"}), "internal"),
        (lambda b: httpx.Response(502, json=["no"]), "usb_gone"),
        (lambda b: httpx.Response(200, text="not json"), "usb_gone"),
        (lambda b: httpx.Response(200, json={"code": "nope"}), "internal"),
    ],
)
def test_an_identify_failure_mints_nothing(
    rig: Rig, answer: Callable[[dict[str, Any]], httpx.Response], code: str
) -> None:
    rig.sidecar.identify = answer
    job = _flash(rig)
    assert job["fail"]["code"] == code
    assert rig.repo.robots == {}


def test_a_sidecar_that_refuses_or_vanishes_ends_the_job(rig: Rig) -> None:
    rig.sidecar.load = lambda b: httpx.Response(400, json={"error": "bad"})
    assert _flash(rig)["fail"]["code"] == "internal"

    def gone(body: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadError("gone")

    rig.sidecar.load = gone
    assert _flash(rig)["fail"]["code"] == "usb_gone"
    rig.sidecar.load = lambda b: _stream("picotool load")
    assert _flash(rig)["fail"]["code"] == "internal"


def test_a_crash_ends_the_job_as_internal(rig: Rig) -> None:
    rig.repo.fail_put = True
    job = _flash(rig)
    assert job["state"] == "fail" and job["fail"]["code"] == "internal"
    assert any("RuntimeError" in line for line in job["log"])


def test_a_render_refusal_ends_the_job(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any) -> str:
        raise provision.ProvisionError("too large")

    monkeypatch.setattr(provision, "render_secrets", refuse)
    assert _flash(rig)["fail"]["code"] == "internal"


def test_a_rekey_swaps_the_key_and_rejoins(rig: Rig) -> None:
    rig.repo.add("gb-abc123", "Tamsin")
    rig.sidecar.micropython()
    job = _flash(rig, {"mode": "rekey", "target": "gb-abc123"})
    assert job["state"] == "ok" and job["mode"] == "rekey"
    assert job["phases"] == {"load": "skip", "copy": "done", "join": "done"}
    (call,) = rig.sidecar.called("/pico/rekey")
    assert call == {"port": "/dev/ttyACM0", "key": rig.repo.keys["gb-abc123"]}
    assert rig.sidecar.called("/pico/provision") == [] and rig.sidecar.called("/pico/load") == []
    assert call["key"] not in json.dumps(job)
    assert rig.repo.robots["gb-abc123"].name == "Tamsin"


def test_a_rekey_of_the_wrong_board_keeps_the_old_key(rig: Rig) -> None:
    rig.repo.add("gb-zzz999", "Tamsin")
    rig.sidecar.micropython()
    job = _flash(rig, {"mode": "rekey", "target": "gb-zzz999"})
    assert job["fail"]["code"] == "wrong_robot"
    assert rig.repo.keys["gb-zzz999"] == "old-key-gb-zzz999"


def test_the_debug_token_flashes_as_the_debug_owner(rig: Rig) -> None:
    with TestClient(rig.app) as client:
        r = client.post("/api/debug/growbot/flash", json=NEW)
        assert r.status_code == 202
        deadline = time.monotonic() + 10
        while client.get("/api/debug/growbot/flash").json()["job"]["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert client.get("/api/debug/growbot/flash").json()["job"]["state"] == "ok"
    assert set(rig.repo.contexts) == {DEBUG_CTX}


def test_a_finished_job_is_kept_until_the_next(rig: Rig) -> None:
    with TestClient(rig.app) as client:
        assert client.get("/api/growbot/flash").json() == {"job": None}
        client.post("/api/growbot/flash", json=NEW)
        first = _wait(client)
        assert client.get("/api/growbot/flash").json()["job"]["id"] == first["id"]
        client.post("/api/growbot/flash", json=NEW)
        assert _wait(client)["id"] != first["id"]


# -- refusals before a board is touched -----------------------------------------------------


def test_no_flasher_is_503(rig: Rig) -> None:
    rig.settings.endpoint_url = ""
    r = _refused(rig, NEW)
    assert r.status_code == 503 and "JBRAIN_ENDPOINT_URL" in r.json()["detail"]


@pytest.mark.parametrize("broken", [UF2, "relay_chip.py"])
def test_missing_or_mismatched_firmware_is_503(rig: Rig, broken: str) -> None:
    fw = Path(rig.settings.growbot_firmware_dir)
    (fw / broken).write_bytes(b"tampered")
    r = _refused(rig, NEW)
    assert r.status_code == 503 and "Ops -> Update" in r.json()["detail"]
    (fw / broken).unlink()
    assert _refused(rig, NEW).status_code == 503


def test_no_sums_file_is_503(rig: Rig) -> None:
    (Path(rig.settings.growbot_firmware_dir) / "SHA256SUMS").unlink()
    assert _refused(rig, NEW).status_code == 503


def test_a_running_job_is_409(rig: Rig) -> None:
    rig.app.state.growbot_flash = growbot_flash.FlashState(
        job=growbot_flash.Job("j", "flash", "x", BOX, NOW)
    )
    r = _refused(rig, NEW)
    assert r.status_code == 409 and "right now" in r.json()["detail"]


def test_no_lan_site_is_409(rig: Rig) -> None:
    rig.settings.lan_addr = ""
    assert _refused(rig, NEW).status_code == 409


def test_no_readable_ca_never_flashes(rig: Rig) -> None:
    rig.ca[0] = ""
    r = _refused(rig, NEW)
    assert r.status_code == 409 and "unverified" in r.json()["detail"]
    assert rig.sidecar.calls == []


def test_an_unknown_box_ip_is_409_unless_the_owner_types_it(rig: Rig) -> None:
    rig.box_host[0] = None
    r = _refused(rig, NEW)
    assert r.status_code == 409 and "LAN address" in r.json()["detail"]
    assert _refused(rig, {**NEW, "box_host": "10.1.2.3"}).status_code == 202
    for bad in ("8.8.8.8", "jbrain.local", "172.17.0.1"):
        assert _refused(rig, {**NEW, "box_host": bad}).status_code == 422


def test_nothing_on_usb_is_409(rig: Rig) -> None:
    rig.sidecar.ports = {"ports": [], "pico_boot": []}
    r = _refused(rig, NEW)
    assert r.status_code == 409 and "plugged" in r.json()["detail"]


def test_an_rp2040_is_409_with_its_own_sentence(rig: Rig) -> None:
    rig.sidecar.ports = {"ports": [], "pico_boot": [{"pid": "0003", "label": "RP2040"}]}
    r = _refused(rig, NEW)
    assert r.status_code == 409 and "Pico 2 W" in r.json()["detail"]


def test_two_picos_is_409(rig: Rig) -> None:
    rig.sidecar.micropython()
    rig.sidecar.ports["pico_boot"] = [{"pid": "000f", "label": "boot"}]
    r = _refused(rig, NEW)
    assert r.status_code == 409 and "More than one" in r.json()["detail"]


def test_a_flasher_that_does_not_answer_is_503(rig: Rig) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    rig.sidecar.handler = down  # type: ignore[method-assign]
    growbot_flash._transport = httpx.MockTransport(down)
    assert _refused(rig, NEW).status_code == 503


def test_a_rekey_needs_micropython_on_the_board(rig: Rig) -> None:
    rig.repo.add("gb-abc123", "Tamsin")
    r = _refused(rig, {"mode": "rekey", "target": "gb-abc123"})
    assert r.status_code == 409 and "new robot" in r.json()["detail"]


def test_an_unknown_target_is_404(rig: Rig) -> None:
    assert _refused(rig, {**NEW, "target": "gb-nope"}).status_code == 404
    rig.sidecar.micropython()
    assert _refused(rig, {"mode": "rekey", "target": "gb-nope"}).status_code == 404


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"name": None}, "name"),
        ({"name": "   "}, "name"),
        ({"networks": []}, "at least one"),
        ({"networks": None}, "at least one"),
        ({"networks": [{"ssid": "cabin"}]}, "does not remember"),
        ({"networks": [{"ssid": "home", "password": "short"}]}, "8 to 63"),
        (
            {"networks": [{"ssid": "home", "password": "password1"}] * 2},
            "twice",
        ),
    ],
)
def test_bad_requests_are_422_with_a_sentence(rig: Rig, change: dict[str, Any], match: str) -> None:
    r = _refused(rig, {**NEW, **change})
    assert r.status_code == 422 and match in r.json()["detail"], r.text
    assert rig.repo.robots == {}


def test_the_body_is_held_to_its_shape(rig: Rig) -> None:
    for bad in (
        {**NEW, "mode": "erase"},
        {**NEW, "target": "../x"},
        {**NEW, "name": "x" * 41},
        {**NEW, "networks": [{"ssid": "n", "password": "password"}] * 9},
    ):
        assert _refused(rig, bad).status_code == 422


# -- remembered Wi-Fi -----------------------------------------------------------------------


def test_remember_keeps_the_network_and_fills_the_panels_pair(rig: Rig) -> None:
    body = {
        **NEW,
        "networks": [
            {"ssid": "home", "password": "correct horse battery", "remember": True},
            {"ssid": "hotspot", "password": "hotspot-pass"},
        ],
    }
    assert _flash(rig, body)["state"] == "ok"
    row = rig.store.rows[ENDPOINT_WIFI_KEY]
    assert row == {
        "ssid": "home",
        "password": "correct horse battery",
        "networks": [{"ssid": "home", "password": "correct horse battery"}],
    }


def test_a_remembered_network_needs_no_password(rig: Rig) -> None:
    rig.store.rows[ENDPOINT_WIFI_KEY] = {"ssid": "panel-net", "password": "panel-password"}
    body = {**NEW, "networks": [{"ssid": "panel-net"}]}
    assert _flash(rig, body)["state"] == "ok"
    text = base64.b64decode(rig.sidecar.called("/pico/provision")[0]["secrets_b64"]).decode()
    assert wifi_store.parse_config(text)[1] == [("panel-net", "panel-password")]
    # Using a remembered network writes nothing.
    assert rig.store.rows[ENDPOINT_WIFI_KEY] == {"ssid": "panel-net", "password": "panel-password"}


def test_remembering_updates_and_keeps_the_panels_pair() -> None:
    row = {"ssid": "panel", "password": "p1", "networks": [{"ssid": "a", "password": "x"}]}
    out = growbot_flash._remember(row, [("a", "new-pass"), ("b", "bpass")])
    assert out["ssid"] == "panel" and out["password"] == "p1"
    assert out["networks"] == [
        {"ssid": "a", "password": "new-pass"},
        {"ssid": "b", "password": "bpass"},
    ]
    assert growbot_flash._remember(None, [])["networks"] == []
    assert growbot_flash._remembered(out) == [("a", "new-pass"), ("b", "bpass"), ("panel", "p1")]
    assert growbot_flash._remembered("nope") == []
    many = growbot_flash._remember(None, [(f"n{i}", "password") for i in range(10)])
    assert len(many["networks"]) == provision.MAX_NETWORKS


# -- setup ----------------------------------------------------------------------------------


def test_setup_says_what_a_flash_needs_and_never_a_password(rig: Rig) -> None:
    rig.store.rows[ENDPOINT_WIFI_KEY] = {
        "ssid": "panel",
        "password": "secret-panel",
        "networks": [{"ssid": "home", "password": "secret-home"}],
    }
    rig.sidecar.micropython()
    rig.sidecar.ports["pico_boot"] = [{"pid": "0003", "label": "RP2040 (not supported)"}]
    with TestClient(rig.app) as client:
        out = client.get("/api/growbot/flash/setup").json()
    assert "secret" not in json.dumps(out)
    assert out == {
        "flasher": True,
        "usb": [
            {"kind": "micropython", "port": "/dev/ttyACM0", "label": "Pico MicroPython"},
            {"kind": "boot", "port": None, "label": "RP2040 (not supported)"},
        ],
        "networks": [{"ssid": "home", "remembered": True}, {"ssid": "panel", "remembered": True}],
        "box_host": BOX,
        "lan": True,
        "ca": True,
        "micropython": {"version": "1.28.0", "ready": True},
        "firmware_ready": True,
    }


def test_setup_with_nothing_ready(rig: Rig) -> None:
    rig.settings.lan_addr = ""
    rig.ca[0] = ""
    rig.box_host[0] = None
    (Path(rig.settings.growbot_firmware_dir) / "SHA256SUMS").unlink()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    growbot_flash._transport = httpx.MockTransport(down)
    with TestClient(rig.app) as client:
        out = client.get("/api/growbot/flash/setup").json()
    assert out["flasher"] is False and out["usb"] == [] and out["networks"] == []
    assert (out["lan"], out["ca"], out["box_host"]) == (False, False, None)
    assert out["micropython"]["ready"] is False and out["firmware_ready"] is False
    rig.settings.endpoint_url = ""
    with TestClient(rig.app) as client:
        assert client.get("/api/growbot/flash/setup").json()["flasher"] is False


def test_the_box_host_comes_from_adguard(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.undo()
    (tmp_path / "internal").mkdir()
    secrets = FsInternalSecrets(tmp_path / "internal")
    secrets.ensure("adguard")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(internal_secrets=secrets)))
    answers: list[httpx.Response] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/control/status"
        return answers.pop(0)

    monkeypatch.setattr(adguard, "_transport", httpx.MockTransport(handler))
    import asyncio

    answers.append(httpx.Response(200, json={"dns_addresses": ["172.18.0.1", "192.168.4.2"]}))
    assert asyncio.run(growbot_flash._adguard_host(request, rig.settings)) == "192.168.4.2"  # type: ignore[arg-type]
    answers.append(httpx.Response(500, text="no"))
    assert asyncio.run(growbot_flash._adguard_host(request, rig.settings)) is None  # type: ignore[arg-type]
    answers.append(httpx.Response(200, json=["odd"]))
    assert asyncio.run(growbot_flash._adguard_host(request, rig.settings)) is None  # type: ignore[arg-type]


# -- status, robots and revoke --------------------------------------------------------------


def test_status_reports_the_relay_and_each_robot(rig: Rig) -> None:
    rig.repo.add("gb-a1", "Quorra")
    rig.repo.add("gb-b2", "Tamsin")
    rig.relay.connected = ["gb-b2", "gb-stranger"]
    with TestClient(rig.app) as client:
        out = client.get("/api/growbot/status").json()
        robots = client.get("/api/growbot/robots").json()
    assert out["service"] == "up" and out["connected"] == 1
    assert [(r["code"], r["name"], r["connected"]) for r in out["robots"]] == [
        ("gb-a1", "Quorra", False),
        ("gb-b2", "Tamsin", True),
    ]
    assert set(out["robots"][0]) == {
        "code", "name", "created_at", "rotated_at", "revoked_at", "last_seen_at", "connected",
    }  # fmt: skip
    assert robots == out["robots"]
    token = rig.app.state.growbot_secrets.read("growbot").decode()
    assert set(rig.relay.auth) == {f"Bearer {token}"}


def test_status_says_whether_a_persona_opens_on_the_away_name(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The PWA skips the box-certificate sheet for the away name, which has a public one.
    with TestClient(rig.app) as client:
        assert client.get("/api/growbot/status").json()["away"] is False
        rig.settings.jcode_preview_base_host = "example.com"
        assert client.get("/api/growbot/status").json()["away"] is True
        monkeypatch.setattr(growbot.client_ip, "ingress", lambda _conn: "lan")
        assert client.get("/api/growbot/status").json()["away"] is False


@pytest.mark.parametrize(
    "answer",
    [
        None,
        httpx.Response(401, json={}),
        httpx.Response(200, text="nope"),
        httpx.Response(200, json={"robots": "x"}),
        httpx.Response(200, json=[]),
    ],
)
def test_a_relay_that_is_down_or_odd_reads_as_down(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | None
) -> None:
    rig.repo.add("gb-a1", "Quorra")
    rig.relay.down = answer is None
    if answer is not None:
        monkeypatch.setattr(growbot, "_transport", httpx.MockTransport(lambda r: answer))
    with TestClient(rig.app) as client:
        out = client.get("/api/growbot/status").json()
    assert out["service"] == "down" and out["connected"] == 0
    assert out["robots"][0]["connected"] is False


def test_no_token_or_no_url_reads_as_down(rig: Rig, tmp_path: Path) -> None:
    rig.app.state.growbot_secrets = FsInternalSecrets(tmp_path / "empty")
    with TestClient(rig.app) as client:
        assert client.get("/api/growbot/status").json()["service"] == "down"
    assert rig.relay.auth == []


def test_revoke_nulls_the_key_and_drops_the_robot(rig: Rig) -> None:
    rig.repo.add("gb-a1", "Quorra")
    rig.relay.connected = ["gb-a1"]
    with TestClient(rig.app) as client:
        out = client.post("/api/growbot/robots/gb-a1/revoke").json()
        assert out["code"] == "gb-a1" and out["revoked_at"] and out["connected"] is False
        assert client.post("/api/growbot/robots/gb-nope/revoke").status_code == 404
        assert client.post("/api/growbot/robots/bad..code/revoke").status_code == 422
    assert rig.repo.keys["gb-a1"] is None
    assert rig.relay.drops == ["gb-a1"]


def test_revoke_still_revokes_when_the_relay_is_down(rig: Rig) -> None:
    rig.repo.add("gb-a1", "Quorra")
    rig.relay.down = True
    with TestClient(rig.app) as client:
        assert client.post("/api/growbot/robots/gb-a1/revoke").status_code == 200
    assert rig.repo.keys["gb-a1"] is None


def test_the_debug_token_revokes_as_the_debug_owner(rig: Rig) -> None:
    rig.repo.add("gb-a1", "Quorra")
    with TestClient(rig.app) as client:
        assert client.post("/api/debug/growbot/robots/gb-a1/revoke").status_code == 200
        assert client.post("/api/debug/growbot/robots/bad..x/revoke").status_code == 422
    assert rig.repo.contexts == [DEBUG_CTX]


# -- who may call ---------------------------------------------------------------------------


def test_every_owner_route_refuses_a_non_owner(rig: Rig) -> None:
    def refuse() -> None:
        raise HTTPException(status_code=403, detail="owner access required")

    rig.app.dependency_overrides[owner_only] = refuse
    with TestClient(rig.app) as client:
        assert client.get("/api/growbot/status").status_code == 403
        assert client.get("/api/growbot/flash/setup").status_code == 403
        assert client.post("/api/growbot/flash", json=NEW).status_code == 403
        assert client.get("/api/growbot/flash").status_code == 403
        assert client.post("/api/growbot/robots/gb-a1/revoke").status_code == 403
        assert client.get("/api/lan/root.crt").status_code == 403
    assert rig.sidecar.calls == [] and rig.repo.contexts == []


def test_every_debug_route_refuses_without_a_token(rig: Rig) -> None:
    def refuse() -> None:
        raise HTTPException(status_code=401, detail="unauthorized")

    rig.app.dependency_overrides[current_debug_principal] = refuse
    with TestClient(rig.app) as client:
        assert client.post("/api/debug/growbot/flash", json=NEW).status_code == 401
        assert client.get("/api/debug/growbot/flash").status_code == 401
        assert client.post("/api/debug/growbot/robots/gb-a1/revoke").status_code == 401
    assert rig.sidecar.calls == [] and rig.repo.contexts == []


# -- the LAN root download ------------------------------------------------------------------


def test_the_lan_root_downloads_as_a_certificate(rig: Rig) -> None:
    with TestClient(rig.app) as client:
        r = client.get("/api/lan/root.crt")
    assert r.status_code == 200 and r.text == CA
    assert r.headers["content-type"] == "application/x-x509-ca-cert"
    assert r.headers["content-disposition"] == 'attachment; filename="jbrain-box-root.crt"'
    rig.ca[0] = ""
    with TestClient(rig.app) as client:
        assert client.get("/api/lan/root.crt").status_code == 404


def test_a_failure_outside_any_phase_lands_on_copy() -> None:
    job = growbot_flash.Job("j", "flash", "x", BOX, NOW)
    assert job.running_phase() == "copy"


def test_the_wifi_store_is_the_settings_row_the_panels_use() -> None:
    from jbrain.settings_store import SqlSettingsStore

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=object())))
    assert isinstance(growbot_flash._wifi_store(request), SqlSettingsStore)  # type: ignore[arg-type]


# -- review fixes: races, marks, drops, the CA, the clock -----------------------------------


def test_two_starts_at_once_flash_one_robot(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The slot is taken before _plan's first await, so a start racing through the checks
    behind another is refused rather than flashing the same board twice."""

    async def slow_host(request: Any, settings: Any) -> str | None:
        await asyncio.sleep(0.3)
        return BOX

    monkeypatch.setattr(growbot_flash, "_adguard_host", slow_host)
    statuses: list[int] = []
    with TestClient(rig.app) as client:

        def start() -> None:
            statuses.append(client.post("/api/growbot/flash", json=NEW).status_code)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert sorted(statuses) == [202, 409]
        assert _wait(client)["state"] == "ok"
    assert len(rig.sidecar.called("/pico/provision")) == 1


def test_a_refused_start_releases_the_slot(rig: Rig) -> None:
    with TestClient(rig.app) as client:
        assert client.post("/api/growbot/flash", json={**NEW, "name": " "}).status_code == 422
        assert client.post("/api/growbot/flash", json=NEW).status_code == 202
        assert _wait(client)["state"] == "ok"
    assert rig.app.state.growbot_flash.planning is None


def test_an_unreadable_ca_is_refused_before_any_key_is_minted(rig: Rig) -> None:
    rig.ca[0] = "-----BEGIN CERTIFICATE-----\nnot base64 !!\n-----END CERTIFICATE-----\n"
    resp = _refused(rig, NEW)
    assert resp.status_code == 409 and "certificate cannot be read" in resp.json()["detail"]
    assert rig.repo.keys == {} and rig.sidecar.calls == []


def test_console_text_that_looks_like_a_mark_is_only_logged(rig: Rig) -> None:
    """The robot's console can print `OK` or `port=…`; only the sidecar's `@ ` lines act."""

    def noisy(body: dict[str, Any]) -> httpx.Response:
        rig.repo.seen(rig.sidecar.code)
        return _stream(
            "| OK", "| port=/dev/sda", "| FAILED: wifi_failed: fake", "copied", "WIFI_OK", "OK"
        )

    rig.sidecar.provision = noisy
    job = _flash(rig)
    assert job["state"] == "ok" and job["port"] == "/dev/ttyACM0"
    assert "port=/dev/sda" in job["log"] and "OK" in job["log"]


@pytest.mark.parametrize("code", ["clock_unset", "needs_reflash"])
def test_the_new_failure_codes_carry_their_recovery(rig: Rig, code: str) -> None:
    rig.sidecar.provision = lambda b: _stream("copied", f"FAILED: {code}: detail")
    job = _flash(rig)
    assert job["fail"] == {"code": code, "message": growbot_flash.RECOVERY[code]}


def test_a_failed_reflash_after_the_mint_drops_the_old_socket(rig: Rig) -> None:
    rig.repo.add("gb-abc123", "Tamsin")
    rig.sidecar.provision = lambda b: _stream("copied", "FAILED: wifi_failed: no networks")
    job = _flash(rig, {**NEW, "target": "gb-abc123", "name": None})
    assert job["fail"]["code"] == "wifi_failed"
    assert rig.relay.drops == ["gb-abc123"]


def test_a_failure_before_the_mint_drops_nothing(rig: Rig) -> None:
    rig.repo.add("gb-abc123", "Tamsin")
    rig.sidecar.load = lambda b: _stream("FAILED: not_bootsel: no RP2350")
    assert _flash(rig, {**NEW, "target": "gb-abc123"})["fail"]["code"] == "not_bootsel"
    assert rig.relay.drops == []


def test_a_drop_that_raises_is_only_logged() -> None:
    async def broken(code: str) -> bool:
        raise RuntimeError("relay gone")

    plan = SimpleNamespace(minted="gb-abc123", drop=broken)
    asyncio.run(growbot_flash._drop_minted(plan))  # type: ignore[arg-type]


def test_minting_or_forgetting_outside_a_flash_drops_the_robot(rig: Rig) -> None:
    with TestClient(rig.app) as client:
        assert client.post("/api/growbot/robots", json={"code": "gb-a1b2c3"}).status_code == 200
        assert client.delete("/api/growbot/robots/gb-a1b2c3").status_code == 204
    assert rig.relay.drops == ["gb-a1b2c3", "gb-a1b2c3"]


def test_remember_updates_the_panels_pair_for_the_same_network() -> None:
    row = {"ssid": "home", "password": "old", "networks": [{"ssid": "home", "password": "old"}]}
    out = growbot_flash._remember(row, [("home", "new-password")])
    assert out == {
        "ssid": "home",
        "password": "new-password",
        "networks": [{"ssid": "home", "password": "new-password"}],
    }
    other = growbot_flash._remember(row, [("hotspot", "hotspot-pass")])
    assert other["ssid"] == "home" and other["password"] == "old"
