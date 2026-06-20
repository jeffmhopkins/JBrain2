"""Opt-in LAN access, asserted on the deploy config. A stock deploy serves only
the public site; setting JBRAIN_LAN_ADDR makes the proxy add a second site that
serves the same app over local HTTPS (Caddy's internal CA), so the Secure
session cookie works on the LAN when the tunnel/internet is down. The shared
handlers live in one Caddy snippet both sites import; the LAN site is rendered
from the env at container start by deploy/proxy-lan-conf.sh."""

import subprocess
from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_CADDYFILE = _DEPLOY / "Caddyfile"
_DOCKERFILE = _DEPLOY / "Dockerfile.proxy"
_LAN_CONF = _DEPLOY / "proxy-lan-conf.sh"


def test_proxy_receives_optional_lan_addr() -> None:
    # Default empty: absent JBRAIN_LAN_ADDR is "off", so stock deploys gain no
    # LAN site and behave exactly as before.
    env = yaml.safe_load(_COMPOSE.read_text())["services"]["proxy"]["environment"]
    assert env["JBRAIN_LAN_ADDR"] == "${JBRAIN_LAN_ADDR:-}"


def test_caddy_shares_handlers_and_imports_the_lan_site() -> None:
    caddyfile = _CADDYFILE.read_text()
    # Shared snippet imported by the public site (so the LAN site can reuse it).
    assert "(app) {" in caddyfile
    assert "import app" in caddyfile
    # The public switchable site is preserved (tunnel/direct mode unaffected).
    assert "{$JBRAIN_SITE_ADDR}" in caddyfile
    # The optional LAN site is pulled from a glob that matches nothing when no
    # file is rendered — a missing-glob import is not an error in Caddy.
    assert "import /etc/caddy/lan/*.caddy" in caddyfile


def test_proxy_image_runs_the_rendering_entrypoint() -> None:
    dockerfile = _DOCKERFILE.read_text()
    assert 'ENTRYPOINT ["proxy-entrypoint.sh"]' in dockerfile
    assert "proxy-lan-conf.sh" in dockerfile


def test_lan_conf_renders_internal_tls_site_when_addr_set(tmp_path: Path) -> None:
    subprocess.run(
        ["sh", str(_LAN_CONF), str(tmp_path)],
        check=True,
        env={"JBRAIN_LAN_ADDR": "https://jbrain.local", "PATH": "/usr/bin:/bin"},
    )
    rendered = (tmp_path / "lan.caddy").read_text()
    assert "https://jbrain.local {" in rendered
    # Internal CA (no Let's Encrypt / inbound) and reuse of the shared handlers.
    assert "tls internal" in rendered
    assert "import app" in rendered


def test_lan_conf_renders_nothing_and_clears_stale_when_unset(tmp_path: Path) -> None:
    stale = tmp_path / "lan.caddy"
    stale.write_text("https://old.local {\n}\n")
    subprocess.run(
        ["sh", str(_LAN_CONF), str(tmp_path)],
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    # Disabling LAN access (blank/removed addr) tears the site back down.
    assert not stale.exists()
