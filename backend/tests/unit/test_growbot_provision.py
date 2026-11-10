"""The secrets.py PWA flashing writes to a GrowBot (jbrain.growbot.provision), read back by the
robot's own parser.

`secrets.py` is parsed on the robot by `wifi_store.parse_config` (a restricted literal parser,
no bytes literals) and imported as Python by the patched `relay_chip.py`. So the test loads the
committed `growbot/firmware/wifi_store.py` by path and round-trips what the box renders through
it, and checks the names the firmware patch reads are exactly the ones rendered.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from jbrain.growbot import provision

FIRMWARE = Path(__file__).resolve().parents[3] / "growbot" / "firmware"
KEY = "k" * 43


def _load_wifi_store() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("growbot_wifi_store", FIRMWARE / "wifi_store.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["growbot_wifi_store"] = module
    spec.loader.exec_module(module)
    return module


wifi_store = _load_wifi_store()


@pytest.fixture(scope="module")
def ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Caddy Local Authority")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _render(ca_pem: str, networks: list[tuple[str, str]]) -> str:
    return provision.render_secrets("192.168.1.20", 8444, "jbrain.local", KEY, ca_pem, networks)


NETWORKS = [
    ("home", "correct horse battery"),
    ('quo"te\\back', "p@ss'w\"o\\rd!"),
    ("café ☕ 🤖", "12345678"),
]


def test_parse_config_round_trips_every_network(ca_pem: str) -> None:
    text = _render(ca_pem, NETWORKS)
    base, networks = wifi_store.parse_config(text)
    assert networks == NETWORKS
    # Only the managed block is the robot's to rewrite; the box's lines are the rest.
    assert wifi_store.BEGIN not in base and "GROWBOT_KEY" in base


def test_the_robot_can_add_a_network_to_what_the_box_wrote(ca_pem: str) -> None:
    text = _render(ca_pem, NETWORKS[:1])
    updated = wifi_store.replacement(text, "hotspot", "hotspot-pass")
    assert wifi_store.parse_config(updated)[1] == [NETWORKS[0], ("hotspot", "hotspot-pass")]
    assert "GROWBOT_KEY" in updated


def test_it_imports_as_python_with_the_values_the_firmware_reads(ca_pem: str) -> None:
    text = _render(ca_pem, NETWORKS)
    values: dict[str, object] = {}
    exec(compile(text, "secrets.py", "exec"), values)  # noqa: S102 - our own rendered literals
    assert values["GROWBOT_HOST"] == "192.168.1.20"
    assert values["GROWBOT_PORT"] == 8444
    assert values["GROWBOT_SNI"] == "jbrain.local"
    assert values["GROWBOT_KEY"] == KEY
    assert values["WIFI_NETWORKS"] == [list(n) for n in NETWORKS]
    der = base64.b64decode(str(values["GROWBOT_CA"]))
    assert x509.load_der_x509_certificate(der).subject.rfc4514_string() == (
        "CN=Caddy Local Authority"
    )


def test_it_is_exactly_the_contracts_lines(ca_pem: str) -> None:
    lines = _render(ca_pem, NETWORKS[:1]).splitlines()
    assert [line.split(" = ")[0] for line in lines] == [
        "GROWBOT_HOST",
        "GROWBOT_PORT",
        "GROWBOT_SNI",
        "GROWBOT_KEY",
        "GROWBOT_CA",
        provision.BEGIN,
        "WIFI_NETWORKS",
        provision.END,
    ]


def test_the_names_are_the_ones_the_firmware_patch_reads() -> None:
    """One set of names everywhere: what relay_chip.py getattr()s off secrets.py, and the key
    line the sidecar's rekey script swaps."""
    chip = (FIRMWARE / "relay_chip.py").read_text()
    read = set(re.findall(r'getattr\(_box, "(GROWBOT_[A-Z]+)"', chip))
    assert read == {"GROWBOT_HOST", "GROWBOT_PORT", "GROWBOT_SNI", "GROWBOT_KEY", "GROWBOT_CA"}
    # The CA is a base64 string the firmware decodes, never a bytes literal.
    assert "binascii.a2b_base64(_ca)" in chip
    sidecar = (Path(__file__).resolve().parents[3] / "deploy/endpoint/pico.py").read_text()
    assert 'startswith("GROWBOT_KEY = ")' in sidecar


@pytest.mark.parametrize(
    ("ssid", "password"),
    [
        ("", "longenough"),
        ("x" * 33, "longenough"),
        ("☕" * 11, "longenough"),  # 33 bytes
        ("a\x00b", "longenough"),
        ("home", "short"),
        ("home", "x" * 64),
        ("home", "tab\tinside"),
        ("home", "naïve-password"),
    ],
)
def test_networks_the_robot_would_refuse_are_refused(ssid: str, password: str) -> None:
    with pytest.raises(provision.ProvisionError):
        provision.validate_network(ssid, password)
    with pytest.raises(wifi_store.WiFiError):
        wifi_store.validate_network(ssid, password)


def test_a_network_the_robot_accepts_is_accepted() -> None:
    for ssid, password in NETWORKS:
        assert provision.validate_network(ssid, password) == (ssid, password)
        assert wifi_store.validate_network(ssid, password) == (ssid, password)


def test_an_unencodable_ssid_is_refused() -> None:
    with pytest.raises(provision.ProvisionError):
        provision.validate_network("\ud800", "longenough")


def test_control_characters_are_escaped_like_wifi_store() -> None:
    assert provision.quoted("a\nb\x01") == '"a\\u000ab\\u0001"'
    literal = provision.quoted("a\nb")
    assert wifi_store.parse_config("X = " + literal + "\n")[0] == "X = " + literal + "\n"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"host": "jbrain.local"}, "IPv4"),
        ({"host": "::1"}, "IPv4"),
        ({"sni": "bad name"}, "hostname"),
        ({"key": "short"}, "key"),
        ({"key": 'x" + evil + "' + "x" * 40}, "key"),
        ({"port": 0}, "port"),
        ({"networks": []}, "at least one"),
        ({"networks": [(f"n{i}", "password") for i in range(9)]}, "at most"),
        ({"networks": [("dup", "password"), ("dup", "password2")]}, "twice"),
    ],
)
def test_render_refuses_what_the_robot_could_not_use(
    ca_pem: str, kwargs: dict[str, object], match: str
) -> None:
    args: dict[str, object] = {
        "host": "192.168.1.20",
        "port": 8444,
        "sni": "jbrain.local",
        "key": KEY,
        "ca_pem": ca_pem,
        "networks": NETWORKS[:1],
    }
    args.update(kwargs)
    with pytest.raises(provision.ProvisionError, match=match):
        provision.render_secrets(**args)  # type: ignore[arg-type]


def test_an_unreadable_ca_is_refused() -> None:
    with pytest.raises(provision.ProvisionError, match="certificate"):
        provision.ca_base64("not a pem")


def test_secrets_too_large_for_the_robot_are_refused(
    ca_pem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision, "MAX_CONFIG", 100)
    with pytest.raises(provision.ProvisionError, match="too large"):
        _render(ca_pem, NETWORKS[:1])


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (["172.17.0.1", "127.0.0.1", "192.168.1.20", "10.0.0.2"], "192.168.1.20"),
        (["::1", "fe80::1", "10.0.0.2"], "10.0.0.2"),
        (["169.254.3.4", "0.0.0.0", "8.8.8.8", "not an ip"], None),
        (["172.31.255.1", "172.32.0.1"], None),
        ([], None),
        (None, None),
        ("192.168.1.20", None),
    ],
)
def test_the_box_host_is_its_first_lan_ipv4(addresses: object, expected: str | None) -> None:
    assert provision.pick_box_host(addresses) == expected


def test_the_file_map_is_the_committed_firmware_and_its_sums() -> None:
    sums: dict[str, str] = {}
    for line in (FIRMWARE / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        sums[name] = digest
    assert [dest for _, dest in provision.FILE_MAP][-1] == "main.py"
    assert ("relay_chip.py", "main.py") in provision.FILE_MAP
    for src, _ in provision.FILE_MAP:
        assert hashlib.sha256((FIRMWARE / src).read_bytes()).hexdigest() == sums[src], src
    names = {src for src, _ in provision.FILE_MAP}
    assert "hello_legs.py" not in names and "secrets.example.py" not in names
    uf2 = "RPI_PICO2_W-20260406-v1.28.0.uf2"
    assert hashlib.sha256((FIRMWARE / uf2).read_bytes()).hexdigest() == sums[uf2]
    assert set(sums) == names | {uf2}
