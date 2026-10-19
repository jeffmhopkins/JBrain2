"""Enrolling a phone by QR on the box's own screen (docs/plans/OWNER_TUNNEL_PLAN.md §3.5,
wave A4): the window and its single-use code, the labelled MACs, device CSR signing, the
enroll route's home-network rule, and the wall's view of it all."""

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from jbrain.api import client_ip
from jbrain.api import endpoint as endpoint_api
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.main import create_app
from jbrain.owner_tunnel import enrollment, keys
from jbrain.owner_tunnel.enrollment import Claim, Enrolled, EnrollmentWindow
from jbrain.owner_tunnel.words import WORDS
from jbrain.settings_store import OWNER_ENROLL_SUBNET_KEY
from jbrain.storage import FsOwnerKeyFiles
from tests.unit.fakes import FakeAuthRepo
from tests.unit.test_owner_devices import FakeOwnerDeviceRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_COOKIE = "jbrain_session"
PROXY = "172.18.0.3"
TUNNEL = "172.18.0.7"
PHONE_IP = "192.168.1.50"
WALL_TOKEN = b"wall-token"
_LAN = {"x-jbrain-ingress": "lan", "x-real-ip": PHONE_IP}


# --- the window ----------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now


def test_an_armed_window_shows_its_code_until_it_lapses() -> None:
    clock = _Clock()
    window = EnrollmentWindow(clock)
    assert window.snapshot().state == "idle"
    expires = window.arm()
    snap = window.snapshot()
    assert (snap.state, snap.expires_at) == ("armed", expires)
    assert snap.code is not None and len(snap.code) == 32
    clock.now += enrollment.WINDOW
    assert window.snapshot().state == "idle"
    assert window.claim(lambda _c: True) is None


def test_arming_again_replaces_the_code() -> None:
    window = EnrollmentWindow(_Clock())
    window.arm()
    first = window.snapshot().code
    window.arm()
    assert window.snapshot().code not in (None, first)


def test_a_code_is_claimed_once_and_a_wrong_one_leaves_it_open() -> None:
    window = EnrollmentWindow(_Clock())
    window.arm()
    code = window.snapshot().code
    assert window.claim(lambda _c: False) is None
    assert window.snapshot().code == code
    claim = window.claim(lambda c: c == code)
    assert claim is not None and claim.code == code
    assert window.claim(lambda c: c == code) is None
    # A claimed window no longer shows its code: it can never be scanned twice.
    snap = window.snapshot()
    assert (snap.state, snap.code) == ("armed", None)


def _claimed(window: EnrollmentWindow) -> Claim:
    window.arm()
    claim = window.claim(lambda _c: True)
    assert claim is not None
    return claim


def test_the_enrolled_phone_is_shown_for_a_full_window() -> None:
    clock = _Clock()
    window = EnrollmentWindow(clock)
    claim = _claimed(window)
    clock.now += enrollment.WINDOW - dt.timedelta(seconds=5)
    window.record(claim, Enrolled("dev-0", ("acorn",)))
    snap = window.snapshot()
    assert (snap.state, snap.enrolled, snap.code) == (
        "enrolled",
        Enrolled("dev-0", ("acorn",)),
        None,
    )
    # Enrolled with five seconds left, the owner still has the whole window to compare.
    assert snap.expires_at == clock.now + enrollment.WINDOW
    window.close()
    assert window.snapshot().state == "idle"


def test_a_late_record_never_lands_on_another_window() -> None:
    clock = _Clock()
    window = EnrollmentWindow(clock)
    first = _claimed(window)
    second = _claimed(window)
    window.record(first, Enrolled("dev-0", ("first",)))
    assert window.snapshot().enrolled is None
    window.record(second, Enrolled("dev-1", ("second",)))
    assert window.snapshot().enrolled == Enrolled("dev-1", ("second",))


def test_a_claim_at_the_last_moment_still_shows_its_words() -> None:
    clock = _Clock()
    window = EnrollmentWindow(clock)
    window.arm()
    clock.now += enrollment.WINDOW - dt.timedelta(milliseconds=1)
    claim = window.claim(lambda _c: True)
    assert claim is not None
    clock.now += dt.timedelta(seconds=1)
    assert window.in_use()
    window.record(claim, Enrolled("dev-0", ("late",)))
    assert window.snapshot().enrolled == Enrolled("dev-0", ("late",))


def test_a_closed_window_takes_no_late_record() -> None:
    clock = _Clock()
    window = EnrollmentWindow(clock)
    claim = _claimed(window)
    window.close()
    window.record(claim, Enrolled("dev-0", ("late",)))
    assert window.snapshot().state == "idle"
    # Nor one that lapsed and was purged by the screen's poll before the insert returned.
    claim = _claimed(window)
    clock.now += 2 * enrollment.WINDOW
    assert window.snapshot().state == "idle"
    window.record(claim, Enrolled("dev-1", ("late",)))
    assert window.snapshot().state == "idle"


def test_a_failed_enrollment_closes_only_its_own_window() -> None:
    window = EnrollmentWindow(_Clock())
    stale = _claimed(window)
    window.arm()
    window.abandon(stale)
    assert window.snapshot().code is not None
    window.abandon(_claimed(window))
    assert window.snapshot().state == "idle"


def test_fingerprint_reads_the_key_hash_in_words() -> None:
    assert enrollment.fingerprint("00ff01" + "0" * 58) == (
        WORDS[0],
        WORDS[255],
        WORDS[1],
        WORDS[0],
        WORDS[0],
    )
    assert len(WORDS) == len(set(WORDS)) == 256


def test_the_two_macs_cannot_stand_in_for_each_other() -> None:
    code = b"k" * 32
    csr = b"csr"
    app = enrollment.app_mac(code, csr)
    assert app == hmac.new(code, b"jbrain-enroll app->box\0csr", hashlib.sha256).digest()
    # The box's MAC over the same bytes is different, so neither can be reflected.
    assert enrollment.box_mac(code, b"", csr) != app
    # Length-prefixed parts: moving bytes between certificates changes the MAC.
    assert enrollment.box_mac(code, app, b"ab", b"c") != enrollment.box_mac(code, app, b"a", b"bc")


def test_b64url_round_trips_with_or_without_padding() -> None:
    data = bytes(range(31))
    text = enrollment.b64url(data)
    assert "=" not in text
    assert enrollment.b64url_decode(text) == data
    assert enrollment.b64url_decode(text + "=") == data
    for bad in ("!!!!", "a+b/", "abc d"):
        with pytest.raises(ValueError):
            enrollment.b64url_decode(bad)


def test_qr_payload_is_compact_versioned_json() -> None:
    payload = enrollment.qr_payload(
        b"\x01" * 32,
        owner_ca_pin="O",
        lan_url="https://jbrain.local",
        lan_root_pin="L",
        public_url="https://example.test",
    )
    assert " " not in payload
    assert json.loads(payload) == {
        "v": 1,
        "code": enrollment.b64url(b"\x01" * 32),
        "ca": "O",
        "lan": "https://jbrain.local",
        "lanRoot": "L",
        "public": "https://example.test",
    }


# --- signing a phone's request ---------------------------------------------------------------


@pytest.fixture
def files(tmp_path: Path) -> FsOwnerKeyFiles:
    f = FsOwnerKeyFiles(tmp_path / "keys", tmp_path / "site")
    keys.ensure(f)
    return f


def _csr(key: Any = None, *, extensions: bool = False) -> tuple[bytes, ec.EllipticCurvePrivateKey]:
    key = key or ec.generate_private_key(ec.SECP256R1())
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "evil.example")])
    )
    if extensions:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    csr = builder.sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM), key


def test_a_phone_request_is_signed_as_a_client_cert_and_nothing_else(
    files: FsOwnerKeyFiles,
) -> None:
    pem, key = _csr(extensions=True)
    issued = keys.issue_device(files, keys.parse_device_csr(pem))
    cert = x509.load_pem_x509_certificate(issued.cert_pem)
    enroll_ca = x509.load_pem_x509_certificate(issued.enroll_ca_pem)
    cert.verify_directly_issued_by(enroll_ca)
    # The phone asked to be a CA called evil.example; it gets the box's subject and a leaf.
    assert cert.subject.rfc4514_string() == "CN=JBrain owner phone"
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert issued.key_hash == keys.public_key_sha256(key.public_key())
    assert issued.owner_ca_pem == files.read("owner-ca.crt")
    assert cert.not_valid_after_utc - cert.not_valid_before_utc > dt.timedelta(days=9 * 365)


def test_a_request_must_be_p256_and_self_signed() -> None:
    with pytest.raises(ValueError):
        keys.parse_device_csr(b"not a csr")
    p384, _ = _csr(ec.generate_private_key(ec.SECP384R1()))
    with pytest.raises(ValueError, match="P-256"):
        keys.parse_device_csr(p384)
    rsa_pem, _ = _csr(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with pytest.raises(ValueError, match="P-256"):
        keys.parse_device_csr(rsa_pem)
    # Another key's signature over this request: whoever sent it does not hold the key.
    good, _ = _csr()
    forged = x509.load_pem_x509_csr(good)
    other = ec.generate_private_key(ec.SECP256R1())
    resigned = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(forged.subject)
        .sign(other, hashes.SHA256())
        .public_bytes(serialization.Encoding.DER)
    )
    tampered = bytearray(resigned)
    tampered[-5] ^= 0xFF
    pem = (
        b"-----BEGIN CERTIFICATE REQUEST-----\n"
        + base64.encodebytes(bytes(tampered))
        + b"-----END CERTIFICATE REQUEST-----\n"
    )
    with pytest.raises(ValueError):
        keys.parse_device_csr(pem)


def test_no_keys_no_signing(tmp_path: Path) -> None:
    empty = FsOwnerKeyFiles(tmp_path / "k", tmp_path / "s")
    pem, _ = _csr()
    with pytest.raises(keys.KeysNotReady):
        keys.issue_device(empty, keys.parse_device_csr(pem))
    with pytest.raises(keys.KeysNotReady):
        keys.owner_ca_cert(empty)


def test_spki_pin_is_androids_pin_format(files: FsOwnerKeyFiles) -> None:
    cert = keys.owner_ca_cert(files)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert keys.spki_pin(cert) == base64.b64encode(hashlib.sha256(spki).digest()).decode()


# --- the routes ------------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def get(self, ctx: SessionContext, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    async def upsert(self, ctx: SessionContext, key: str, value: Any) -> None:
        self.values[key] = value


class _Secrets:
    def __init__(self, name: str, token: bytes) -> None:
        self.name, self.token = name, token

    def read(self, name: str) -> bytes | None:
        return self.token if name == self.name else None


class _Box:
    def __init__(
        self,
        client: TestClient,
        repo: FakeAuthRepo,
        phones: FakeOwnerDeviceRepo,
        store: _Store,
        app: Any,
    ) -> None:
        self.client, self.repo, self.phones, self.store, self.app = client, repo, phones, store, app

    @property
    def window(self) -> EnrollmentWindow:
        return self.app.state.owner_enrollment

    def owner(self) -> dict[str, str]:
        async def go() -> str:
            key = await auth_service.rotate_owner_key(self.repo)
            return await auth_service.login(self.repo, key, "t")

        return {"cookie": f"{_COOKIE}={asyncio.run(go())}"}

    def wall(self) -> dict[str, str]:
        return {"authorization": f"Bearer {WALL_TOKEN.decode()}"}

    def enroll(
        self, pem: bytes, code: bytes, headers: dict[str, str] | None = None, label: str = "Pixel"
    ) -> Any:
        mac = enrollment.b64url(enrollment.app_mac(code, pem))
        return self.client.post(
            "/api/owner-devices/enroll",
            json={"csr": pem.decode(), "label": label, "mac": mac},
            headers=_LAN if headers is None else headers,
        )


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Box]:
    root_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Caddy Local Authority")])
    now = dt.datetime.now(dt.UTC)
    root = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(root_key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(root_key, hashes.SHA256())
    )
    monkeypatch.setattr(
        endpoint_api, "_lan_ca", lambda: root.public_bytes(serialization.Encoding.PEM).decode()
    )
    settings = Settings(
        secure_cookies=False,
        database_url=_DB,
        owner_keys_dir=str(tmp_path / "keys"),
        owner_site_dir=str(tmp_path / "site"),
        lan_addr="https://jbrain.local",
        public_base_url="https://jbrain.example",
    )
    keys.ensure(FsOwnerKeyFiles(settings.owner_keys_dir, settings.owner_site_dir))
    app = create_app(settings)
    repo, phones, store = FakeAuthRepo(), FakeOwnerDeviceRepo(), _Store()
    phones.auth = repo
    with (
        client_ip.pinned(proxy=(PROXY,), tunnel=(TUNNEL,)),
        TestClient(app, client=(PROXY, 1)) as c,
    ):
        app.state.auth_repo = repo
        app.state.owner_device_repo = phones
        app.state.settings_store = store
        # The pet routes' token is a different one, and opens nothing here.
        app.state.internal_secrets = _Secrets("wall", b"pet-token")
        app.state.enroll_secrets = _Secrets("wall-enroll", WALL_TOKEN)
        yield _Box(c, repo, phones, store, app)


def _armed(box: _Box) -> bytes:
    assert box.client.post("/api/owner-devices/enroll/arm", headers=box.owner()).status_code == 200
    code = box.window.snapshot().code
    assert code is not None
    return code


def test_a_phone_enrolls_with_the_qr_code_and_can_check_the_reply(box: _Box) -> None:
    code = _armed(box)
    pem, key = _csr()
    resp = box.enroll(pem, code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # What the app does before it pins anything: the box's MAC over exactly what came back.
    app_tag = enrollment.app_mac(code, pem)
    expected = enrollment.box_mac(
        code,
        app_tag,
        body["device_id"].encode(),
        body["cert"].encode(),
        body["owner_ca"].encode(),
        body["enroll_ca"].encode(),
    )
    assert hmac.compare_digest(enrollment.b64url_decode(body["mac"]), expected)
    key_hash = keys.public_key_sha256(key.public_key())
    (device,) = box.phones.devices
    assert (device.id, device.label, device.key_hash) == (body["device_id"], "Pixel", key_hash)
    assert body["words"] == list(enrollment.fingerprint(key_hash))
    # The box's screen now shows the same words for the owner to compare.
    screen = box.client.get("/internal/owner-enroll", headers=box.wall()).json()
    assert (screen["state"], screen["words"], screen["qr_svg"]) == ("enrolled", body["words"], None)


def test_the_code_is_single_use(box: _Box) -> None:
    code = _armed(box)
    assert box.enroll(_csr()[0], code).status_code == 200
    resp = box.enroll(_csr()[0], code)
    assert resp.status_code == 403
    assert len(box.phones.devices) == 1


def test_a_wrong_mac_is_refused_and_leaves_the_window_open(box: _Box) -> None:
    code = _armed(box)
    pem, _ = _csr()
    assert box.enroll(pem, b"x" * 32).status_code == 403
    # Signed over another request: the MAC binds the code to this CSR.
    other = _csr()[0]
    mac = enrollment.b64url(enrollment.app_mac(code, other))
    resp = box.client.post(
        "/api/owner-devices/enroll",
        json={"csr": pem.decode(), "label": "P", "mac": mac},
        headers=_LAN,
    )
    assert resp.status_code == 403
    assert box.enroll(pem, code).status_code == 200


def test_no_window_no_enrollment(box: _Box) -> None:
    assert box.enroll(_csr()[0], b"\0" * 32).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"x-jbrain-ingress": "public", "x-real-ip": PHONE_IP},
        {"x-jbrain-ingress": "owner-tunnel", "x-real-ip": PHONE_IP},
        {"x-real-ip": PHONE_IP},
        # The LAN site reached through cloudflared: a repointed tunnel.
        {"x-jbrain-ingress": "lan", "x-real-ip": TUNNEL, "cf-connecting-ip": PHONE_IP},
    ],
    ids=["public", "owner-site", "no-ingress", "tunnel"],
)
def test_enrollment_is_lan_only(box: _Box, headers: dict[str, str]) -> None:
    code = _armed(box)
    resp = box.enroll(_csr()[0], code, headers)
    # The owner site's own device guard may refuse first; either way, nothing gets in.
    assert resp.status_code == 403
    # Refused before the code is touched.
    assert box.window.snapshot().code == code


def test_the_home_subnet_rule(box: _Box) -> None:
    code = _armed(box)
    box.store.values[OWNER_ENROLL_SUBNET_KEY] = "10.0.0.0/8"
    assert box.enroll(_csr()[0], code).status_code == 403
    box.store.values[OWNER_ENROLL_SUBNET_KEY] = "garbage"
    assert box.enroll(_csr()[0], code).status_code == 403
    box.store.values[OWNER_ENROLL_SUBNET_KEY] = "192.168.1.0/24"
    assert box.enroll(_csr()[0], code).status_code == 200


def test_a_bad_request_never_spends_the_code(box: _Box) -> None:
    code = _armed(box)
    resp = box.client.post(
        "/api/owner-devices/enroll",
        json={"csr": "nope", "label": "P", "mac": "AAAA"},
        headers=_LAN,
    )
    assert resp.status_code == 422
    resp = box.client.post(
        "/api/owner-devices/enroll",
        json={"csr": _csr()[0].decode(), "label": "P", "mac": "!!!"},
        headers=_LAN,
    )
    assert resp.status_code == 422
    assert box.enroll(_csr()[0], code, label="  ").status_code == 422
    assert box.enroll(_csr()[0], code, label="🦊" * 30).status_code == 422
    assert box.window.snapshot().code == code


def test_a_phone_key_already_on_record_is_a_conflict(box: _Box) -> None:
    pem, _ = _csr()
    assert box.enroll(pem, _armed(box)).status_code == 200
    box.window.close()
    assert box.enroll(pem, _armed(box)).status_code == 409
    # The spent window closes, so the screen asks for a new one instead of a blank QR.
    assert box.window.snapshot().state == "idle"


def test_without_keys_the_box_says_so(box: _Box, tmp_path: Path) -> None:
    box.app.state.settings.owner_keys_dir = str(tmp_path / "none")
    code = _armed(box)
    assert box.enroll(_csr()[0], code).status_code == 503
    assert box.window.snapshot().state == "idle"


def test_arming_is_owner_only(box: _Box) -> None:
    assert box.client.post("/api/owner-devices/enroll/arm").status_code == 401
    assert box.window.snapshot().state == "idle"


def test_a_session_cannot_arm_over_an_enrollment_in_flight(box: _Box) -> None:
    # Between the claim and the record (the phone's insert in flight) the screen shows no
    # code yet, but a re-arm would drop the words before they reach it.
    _armed(box)
    claim = box.window.claim(lambda _c: True)
    assert claim is not None
    resp = box.client.post("/api/owner-devices/enroll/arm", headers=box.owner())
    assert resp.status_code == 409
    box.window.record(claim, Enrolled("dev-0", ("words",)))
    assert box.window.snapshot().state == "enrolled"


def test_an_unexpected_failure_closes_the_window(
    box: _Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create(label: str, key_hash: str, public_key: bytes) -> Any:
        raise RuntimeError("database gone")

    monkeypatch.setattr(box.phones, "create", create)
    code = _armed(box)
    with pytest.raises(RuntimeError):
        box.enroll(_csr()[0], code)
    assert box.window.snapshot().state == "idle"


def test_words_waiting_to_be_compared_cannot_be_cleared(box: _Box) -> None:
    assert box.enroll(_csr()[0], _armed(box)).status_code == 200
    resp = box.client.post("/api/owner-devices/enroll/arm", headers=box.owner())
    assert resp.status_code == 409
    assert box.window.snapshot().state == "enrolled"
    # Nor at the box: whatever shares its loopback could otherwise hide a phone it enrolled.
    assert box.client.post("/internal/owner-enroll/close", headers=box.wall()).status_code == 409
    assert box.window.snapshot().state == "enrolled"


def test_enrollment_is_rate_limited_per_address(box: _Box) -> None:
    for _ in range(10):
        assert box.enroll(b"x", b"k" * 32).status_code == 422
    assert box.enroll(b"x", b"k" * 32).status_code == 429
    other = {**_LAN, "x-real-ip": "192.168.1.51"}
    assert box.enroll(b"x", b"k" * 32, other).status_code == 422


# --- the box's screen ------------------------------------------------------------------------


def test_the_screen_shows_a_qr_only_while_the_code_is_unused(box: _Box) -> None:
    assert box.client.get("/internal/owner-enroll", headers=box.wall()).json()["state"] == "idle"
    _armed(box)
    screen = box.client.get("/internal/owner-enroll", headers=box.wall()).json()
    assert screen["state"] == "armed"
    assert screen["qr_svg"].startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert screen["problem"] is None


def test_the_screen_needs_a_lan_site(box: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
    _armed(box)
    monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "")
    screen = box.client.get("/internal/owner-enroll", headers=box.wall()).json()
    assert (screen["qr_svg"], screen["problem"]) == (None, "this box has no LAN site")


def test_an_unreadable_lan_root_is_a_problem_not_a_crash(
    box: _Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    _armed(box)
    monkeypatch.setattr(
        endpoint_api,
        "_lan_ca",
        lambda: "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    )
    screen = box.client.get("/internal/owner-enroll", headers=box.wall()).json()
    assert (screen["qr_svg"], screen["problem"]) == (
        None,
        "the LAN site's certificate cannot be read",
    )


def test_the_screen_needs_the_keys(box: _Box, tmp_path: Path) -> None:
    _armed(box)
    box.app.state.settings.owner_keys_dir = str(tmp_path / "none")
    screen = box.client.get("/internal/owner-enroll", headers=box.wall()).json()
    assert (screen["qr_svg"], screen["problem"]) == (None, "the box's keys are not made yet")


def test_the_qr_holds_the_pins_the_app_trusts(box: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = enrollment.qr_payload

    def spy(code: bytes, **kw: str) -> str:
        out = real(code, **kw)
        seen.append(out)
        return out

    monkeypatch.setattr(enrollment, "qr_payload", spy)
    code = _armed(box)
    box.client.get("/internal/owner-enroll", headers=box.wall())
    (payload,) = [json.loads(s) for s in seen]
    files = FsOwnerKeyFiles(
        box.app.state.settings.owner_keys_dir, box.app.state.settings.owner_site_dir
    )
    lan_root = x509.load_pem_x509_certificate(endpoint_api._lan_ca().encode())
    assert payload == {
        "v": 1,
        "code": enrollment.b64url(code),
        "ca": keys.spki_pin(keys.owner_ca_cert(files)),
        "lan": "https://jbrain.local",
        "lanRoot": keys.spki_pin(lan_root),
        "public": "https://jbrain.example",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/internal/owner-enroll"),
        ("POST", "/internal/owner-enroll/close"),
        ("POST", "/internal/owner-enroll/reject"),
    ],
)
def test_the_screen_routes_need_the_wall_token(box: _Box, method: str, path: str) -> None:
    assert box.client.request(method, path).status_code == 401
    bad = {"authorization": "Bearer nope"}
    assert box.client.request(method, path, headers=bad).status_code == 401
    # Not the pet routes' token, which the cast sidecar and mosquitto can also read.
    pet = {"authorization": "Bearer pet-token"}
    assert box.client.request(method, path, headers=pet).status_code == 401


def test_the_box_keyboard_closes_but_never_arms(box: _Box) -> None:
    assert box.client.post("/internal/owner-enroll/arm", headers=box.wall()).status_code == 404
    _armed(box)
    assert box.client.post("/internal/owner-enroll/close", headers=box.wall()).status_code == 204
    assert box.window.snapshot().state == "idle"


def test_a_mismatch_on_the_screen_revokes_the_phone(box: _Box) -> None:
    assert box.client.post("/internal/owner-enroll/reject", headers=box.wall()).status_code == 404
    assert box.enroll(_csr()[0], _armed(box)).status_code == 200
    # Whatever the phone has open over the owner site is woken to be re-checked.
    woken = asyncio.Event()
    box.app.state.owner_device_watchers = {box.phones.devices[0].id: {woken}}
    assert box.client.post("/internal/owner-enroll/reject", headers=box.wall()).status_code == 204
    assert woken.is_set()
    assert box.phones.devices[0].revoked_at is not None
    assert box.window.snapshot().state == "idle"


# --- the home subnet setting -----------------------------------------------------------------


def test_the_subnet_is_set_from_the_lan_only(box: _Box) -> None:
    owner = box.owner()
    put = box.client.put(
        "/api/owner-devices/enroll/subnet",
        json={"subnet": "192.168.1.7/24"},
        headers={**owner, **_LAN},
    )
    assert put.json() == {"subnet": "192.168.1.0/24"}
    assert box.client.get("/api/owner-devices/enroll/subnet", headers=owner).json() == {
        "subnet": "192.168.1.0/24"
    }
    public = {**owner, "x-jbrain-ingress": "public"}
    resp = box.client.put("/api/owner-devices/enroll/subnet", json={"subnet": ""}, headers=public)
    assert resp.status_code == 403
    tunnel = {**owner, "x-jbrain-ingress": "lan", "x-real-ip": TUNNEL}
    resp = box.client.put("/api/owner-devices/enroll/subnet", json={"subnet": ""}, headers=tunnel)
    assert resp.status_code == 403
    bad = box.client.put(
        "/api/owner-devices/enroll/subnet", json={"subnet": "nope"}, headers={**owner, **_LAN}
    )
    assert bad.status_code == 422
    cleared = box.client.put(
        "/api/owner-devices/enroll/subnet", json={"subnet": " "}, headers={**owner, **_LAN}
    )
    assert cleared.json() == {"subnet": ""}


def test_a_revoke_kicks_by_the_rows_own_spelling_of_the_id() -> None:
    from jbrain.api.owner_devices import _canonical

    row = "0b9c7b1e-6f0e-4c39-9a35-2f3b9f1f4d10"
    assert _canonical(row.upper()) == row
    assert _canonical("{" + row + "}") == row
    assert _canonical("dev-0") == "dev-0"


# --- the public base URL (A5c) -------------------------------------------------------------


def test_the_public_url_is_set_from_the_lan_only(box: _Box) -> None:
    owner = box.owner()
    assert box.client.get("/api/owner-devices/public-url", headers=owner).json() == {
        "url": "",
        "env_url": "https://jbrain.example",
    }
    put = box.client.put(
        "/api/owner-devices/public-url",
        json={"url": " https://brain.example.net/ "},
        headers={**owner, **_LAN},
    )
    assert put.json() == {"url": "https://brain.example.net", "env_url": "https://jbrain.example"}
    public = {**owner, "x-jbrain-ingress": "public"}
    resp = box.client.put("/api/owner-devices/public-url", json={"url": ""}, headers=public)
    assert resp.status_code == 403
    tunnel = {**owner, "x-jbrain-ingress": "lan", "x-real-ip": TUNNEL}
    resp = box.client.put("/api/owner-devices/public-url", json={"url": ""}, headers=tunnel)
    assert resp.status_code == 403
    bad = box.client.put(
        "/api/owner-devices/public-url",
        json={"url": "http://evil.example"},
        headers={**owner, **_LAN},
    )
    assert bad.status_code == 422
    assert box.client.get("/api/owner-devices/public-url", headers=owner).json()["url"] == (
        "https://brain.example.net"
    )
    assert box.client.get("/api/owner-devices/public-url").status_code == 401


def test_the_qr_uses_the_owners_public_url(box: _Box, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = box.owner()
    box.client.put(
        "/api/owner-devices/public-url",
        json={"url": "https://brain.example.net"},
        headers={**owner, **_LAN},
    )
    seen: list[str] = []
    real = enrollment.qr_payload

    def spy(code: bytes, **kw: str) -> str:
        seen.append(kw["public_url"])
        return real(code, **kw)

    monkeypatch.setattr(enrollment, "qr_payload", spy)
    _armed(box)
    box.client.get("/internal/owner-enroll", headers=box.wall())
    assert seen == ["https://brain.example.net"]
