"""Opt-in LAN access, asserted on the deploy config. A stock deploy serves only
the public site; setting JBRAIN_LAN_ADDR makes the proxy add a second site that
serves the same app over local HTTPS (Caddy's internal CA), so the Secure
session cookie works on the LAN when the tunnel/internet is down. The shared
handlers live in one Caddy snippet both sites import; the LAN site is rendered
from the env at container start by deploy/proxy-lan-conf.sh."""

import importlib.util
import subprocess
from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_CADDYFILE = _DEPLOY / "Caddyfile"
_DOCKERFILE = _DEPLOY / "Dockerfile.proxy"
_LAN_CONF = _DEPLOY / "proxy-lan-conf.sh"
_LAN_SETUP = _DEPLOY / "lan-setup.sh"
_AVAHI_ALIAS = _DEPLOY / "avahi_alias.py"
_JBRAIN = _DEPLOY / "jbrain"


def _load_avahi_alias():
    # Module-level imports avoid dbus/gi (those live inside main()), so the pure
    # wire-format encoder loads without the host bindings present.
    spec = importlib.util.spec_from_file_location("avahi_alias", _AVAHI_ALIAS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_proxy_lan_addr_defaults_on() -> None:
    # LAN access is on by default: an unset JBRAIN_LAN_ADDR falls back to
    # jbrain.local, so every deploy gets the local HTTPS site (blank it to opt out).
    env = yaml.safe_load(_COMPOSE.read_text())["services"]["proxy"]["environment"]
    assert env["JBRAIN_LAN_ADDR"] == "${JBRAIN_LAN_ADDR:-https://jbrain.local}"


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


def test_avahi_alias_encodes_cname_target_in_dns_wire_format() -> None:
    # Length-prefixed labels, NUL-terminated — what avahi's AddRecord expects.
    encode = _load_avahi_alias().encode_rdata
    assert encode("myhost.local") == b"\x06myhost\x05local\x00"
    # Trailing dot / empty labels are tolerated (no zero-length label emitted).
    assert encode("a.local.") == b"\x01a\x05local\x00"


def test_lan_setup_provisions_mdns_and_a_cname_alias() -> None:
    setup = _LAN_SETUP.read_text()
    # Installs the responder + the bindings avahi_alias.py needs (no python3-avahi).
    assert "avahi-daemon" in setup
    assert "python3-dbus" in setup and "python3-gi" in setup
    # Runs the alias publisher under a systemd service derived from JBRAIN_LAN_ADDR.
    assert "avahi_alias.py" in setup
    assert "jbrain-avahi-alias.service" in setup
    # Only .local names use mDNS; a custom DNS name is the operator's to resolve.
    assert "*.local)" in setup


def test_jbrain_helper_exposes_and_automates_lan_setup() -> None:
    helper = _JBRAIN.read_text()
    # A manual entrypoint for the one-time bootstrap after the first update...
    assert "enable-lan)" in helper
    # ...and `update` re-provisions it from the freshly pulled source.
    assert "lan-setup.sh" in helper
