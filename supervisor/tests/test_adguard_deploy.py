"""The house's DNS (deploy/adguard): how it is wired, and the config pins it lives by.

What is pinned here is what keeps it safe and terminal-free: the UI and its API never
on the LAN and never without a password, port 53 on the LAN address rather than the
wildcard systemd-resolved already holds, and an update that never takes the house's
DNS down.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
PIN = DEPLOY / "adguard" / "pin.awk"
SEED = DEPLOY / "adguard" / "AdGuardHome.yaml"

HASH = "$2y$10$abcdefghijklmnopqrstuuW2b0d0hL1P2N3Q4R5S6T7U8V9W0X1Y2"


def _compose() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def _pin(text: str, tmp_path: Path, ptr: str = "192.168.1.1") -> str:
    src = tmp_path / "AdGuardHome.yaml"
    src.write_text(text)
    out = subprocess.run(
        [
            "awk",
            "-v",
            "dns=192.168.1.50",
            "-v",
            "ui=172.17.0.1:3053",
            "-v",
            f"hash={HASH}",
            "-v",
            f"ptr={ptr}",
            "-f",
            str(PIN),
            str(src),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def test_adguard_is_stock_stack_on_the_host_network() -> None:
    svc = _compose()["services"]["adguard"]
    assert "profiles" not in svc, "turning it on must not be an .env edit (#10)"
    assert svc["network_mode"] == "host"
    # A published :53 binds 0.0.0.0, which collides with systemd-resolved's stub.
    assert "ports" not in svc
    assert "internal_secrets:/run/jbrain-internal:ro" in svc["volumes"]


def test_the_api_reaches_adguard_on_the_bridge_gateway() -> None:
    api_env = _compose()["services"]["api"]["environment"]
    assert "host.docker.internal:3053" in api_env["JBRAIN_ADGUARD_URL"]
    config = (ROOT / "backend/src/jbrain/config.py").read_text()
    assert 'adguard_url: str = "http://host.docker.internal:3053"' in config


def test_an_update_keeps_the_houses_dns_up() -> None:
    text = (DEPLOY / "update-inner.sh").read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("QUIESCE_KEEP="))
    assert "adguard" in line.split("=", 1)[1].strip('"').split()


def test_the_image_is_pinned_by_digest() -> None:
    text = (DEPLOY / "Dockerfile.adguard").read_text()
    assert "adguard/adguardhome:v" in text
    assert "@sha256:" in text


def test_entrypoint_refuses_a_public_dns_address() -> None:
    """A box with a public default-route address would be an open resolver."""
    text = (DEPLOY / "adguard" / "entrypoint.sh").read_text()
    assert "not a private address" in text
    assert 'case "$dns_addr" in' in text


def test_entrypoint_parses_under_posix_sh() -> None:
    sh = shutil.which("sh")
    assert sh
    subprocess.run([sh, "-n", str(DEPLOY / "adguard" / "entrypoint.sh")], check=True)


def test_the_seed_skips_the_wizard_and_is_current_schema() -> None:
    seed = yaml.safe_load(SEED.read_text())
    assert seed["schema_version"] == 34
    assert seed["dns"]["port"] == 53
    assert all(f["enabled"] for f in seed["filters"])
    assert len({f["id"] for f in seed["filters"]}) == len(seed["filters"])


def test_the_seed_pins_to_the_lan_ui_and_one_user(tmp_path: Path) -> None:
    pinned = yaml.safe_load(_pin(SEED.read_text(), tmp_path))
    assert pinned["dns"]["bind_hosts"] == ["192.168.1.50"]
    assert pinned["http"]["address"] == "172.17.0.1:3053"
    assert pinned["users"] == [{"name": "jbrain", "password": HASH}]
    assert pinned["dns"]["local_ptr_upstreams"] == ["192.168.1.1"]
    # The house is one /24: AdGuard's default per-/24 limit would throttle all of it.
    assert pinned["dns"]["ratelimit"] == 0
    # Everything else is left as found.
    seed = yaml.safe_load(SEED.read_text())
    assert pinned["filters"] == seed["filters"]
    assert pinned["dns"]["upstream_dns"] == seed["dns"]["upstream_dns"]


# AdGuard writes block sequences at the parent's indent; the seed indents them.
# Both must be replaced whole, and a second user someone added is not kept.
ADGUARD_WRITTEN = """\
http:
  pprof:
    port: 6060
    enabled: false
  address: 0.0.0.0:80
  session_ttl: 720h
users:
- name: someone
  password: $2a$10$old
- name: jbrain
  password: $2a$10$stale
dns:
  bind_hosts:
  - 0.0.0.0
  - 10.0.0.9
  port: 53
  upstream_dns:
  - https://dns.quad9.net/dns-query
  local_ptr_upstreams:
  - 127.0.0.53
user_rules:
- '||samsungads.com^'
schema_version: 34
"""


def test_pins_a_config_adguard_rewrote(tmp_path: Path) -> None:
    pinned = yaml.safe_load(_pin(ADGUARD_WRITTEN, tmp_path))
    assert pinned["dns"]["bind_hosts"] == ["192.168.1.50"]
    assert pinned["dns"]["port"] == 53
    assert pinned["dns"]["upstream_dns"] == ["https://dns.quad9.net/dns-query"]
    assert pinned["http"]["address"] == "172.17.0.1:3053"
    assert pinned["http"]["pprof"] == {"port": 6060, "enabled": False}
    assert pinned["users"] == [{"name": "jbrain", "password": HASH}]
    assert pinned["user_rules"] == ["||samsungads.com^"]
    assert pinned["dns"]["local_ptr_upstreams"] == ["192.168.1.1"]


def test_no_gateway_leaves_the_ptr_resolvers_alone(tmp_path: Path) -> None:
    pinned = yaml.safe_load(_pin(ADGUARD_WRITTEN, tmp_path, ptr=""))
    assert pinned["dns"]["local_ptr_upstreams"] == ["127.0.0.53"]


def test_pinning_twice_changes_nothing(tmp_path: Path) -> None:
    once = _pin(ADGUARD_WRITTEN, tmp_path)
    assert _pin(once, tmp_path) == once


@pytest.mark.parametrize("users", ["users: []\n", ""])
def test_a_config_without_our_user_gets_one(users: str, tmp_path: Path) -> None:
    """No users block: AdGuard would serve its API to anything reaching the bridge."""
    text = f"http:\n  address: 0.0.0.0:80\n{users}dns:\n  bind_hosts:\n  - 0.0.0.0\n"
    pinned = yaml.safe_load(_pin(text, tmp_path))
    assert pinned["users"] == [{"name": "jbrain", "password": HASH}]
