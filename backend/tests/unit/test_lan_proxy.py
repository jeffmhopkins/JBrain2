"""Opt-in LAN access, asserted on the deploy config. A stock deploy serves only
the public site; setting JBRAIN_LAN_ADDR makes the proxy add a second site that
serves the same app over local HTTPS (Caddy's internal CA), so the Secure
session cookie works on the LAN when the tunnel/internet is down. The shared
handlers live in one Caddy snippet both sites import; the LAN site is rendered
from the env at container start by deploy/proxy-lan-conf.sh."""

import importlib.util
import re
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
    # jbrain.local, so every deploy gets the local HTTPS site. Bare `-` (not `:-`)
    # so an explicitly-empty value opts out instead of re-defaulting — that is what
    # "blank it to disable" in LOCAL_ACCESS.md relies on.
    env = yaml.safe_load(_COMPOSE.read_text())["services"]["proxy"]["environment"]
    assert env["JBRAIN_LAN_ADDR"] == "${JBRAIN_LAN_ADDR-https://jbrain.local}"


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


def test_every_app_site_overwrites_x_real_ip_with_its_peer() -> None:
    # The api trusts X-Real-IP from this container only (api/client_ip.py), so the value
    # must be Caddy's own peer on every site that imports the snippet (public and LAN), never
    # one a client sent. One reverse_proxy to the api, and it carries the header_up.
    caddyfile = _CADDYFILE.read_text()
    snippet = caddyfile[caddyfile.index("(app) {") : caddyfile.index("{$JBRAIN_SITE_ADDR} {")]
    assert snippet.count("reverse_proxy api:8000") == 1
    assert "header_up X-Real-IP {remote_host}" in snippet
    assert "import app" in _LAN_CONF.read_text()


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


def test_lan_conf_renders_the_growbot_site_on_its_own_port(tmp_path: Path) -> None:
    for addr in ("https://jbrain.local", "https://jbrain.local:9443/", "jbrain.local"):
        subprocess.run(
            ["sh", str(_LAN_CONF), str(tmp_path)],
            check=True,
            env={"JBRAIN_LAN_ADDR": addr, "PATH": "/usr/bin:/bin"},
        )
        rendered = (tmp_path / "growbot.caddy").read_text()
        assert rendered.startswith("https://jbrain.local:8444 {\n"), (addr, rendered)
    assert "tls internal" in rendered
    assert "reverse_proxy growbot:8810" in rendered
    # Rebuilt, not filtered: the owner's session can never ride along, present or not.
    assert 'header_up Cookie "gb_session={http.request.cookie.gb_session}"' in rendered
    assert "header_up -Cookie" in rendered
    assert "handle /internal* {\n\t\trespond 404" in rendered
    assert "import app" not in rendered  # never the PWA's handlers or its api
    subprocess.run(["sh", str(_LAN_CONF), str(tmp_path)], check=True, env={"PATH": "/usr/bin:/bin"})
    assert list(tmp_path.iterdir()) == []


def test_proxy_publishes_the_growbot_port_and_never_the_owner_site() -> None:
    proxy = yaml.safe_load(_COMPOSE.read_text())["services"]["proxy"]
    assert "8444:8444" in proxy["ports"]
    assert all("8443" not in str(p) for p in proxy["ports"])


def test_the_growbot_port_offers_no_http3_it_cannot_answer() -> None:
    # 8444 is published over TCP alone; Caddy's default Alt-Svc h3 would point Chrome at UDP.
    ports = yaml.safe_load(_COMPOSE.read_text())["services"]["proxy"]["ports"]
    assert "8444:8444/udp" not in ports
    block = re.search(r"servers :8444 \{(.*?)\}", _CADDYFILE.read_text(), re.S)
    assert block is not None and "protocols h1 h2" in block.group(1)
