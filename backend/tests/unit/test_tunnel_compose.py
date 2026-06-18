"""The opt-in Cloudflare Tunnel connector, asserted on the deploy config. A stock
deploy must never start cloudflared (its own profile), the connector must be able
to reach Caddy, and switching to tunnel mode must not break installs that predate
it — the proxy's site address falls back to JBRAIN_DOMAIN when JBRAIN_SITE_ADDR is
unset, and Caddy reads that address rather than the domain directly."""

from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_CADDYFILE = _DEPLOY / "Caddyfile"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_tunnel_connector_is_opt_in_and_can_reach_the_proxy() -> None:
    cloudflared = _spec()["services"]["cloudflared"]
    # Opt-in: never starts on a stock deploy.
    assert cloudflared.get("profiles") == ["tunnel"]
    # Shares the edge network with Caddy so it can dial http://proxy:80, and only
    # comes up once the proxy exists.
    assert "edge" in cloudflared["networks"]
    assert "proxy" in cloudflared["depends_on"]


def test_proxy_site_address_defaults_to_the_domain() -> None:
    # Pre-tunnel installs set only JBRAIN_DOMAIN; the default keeps their auto-TLS
    # behaviour untouched after they pull this compose file.
    env = _spec()["services"]["proxy"]["environment"]
    assert env["JBRAIN_SITE_ADDR"] == "${JBRAIN_SITE_ADDR:-${JBRAIN_DOMAIN}}"


def test_caddy_serves_the_switchable_site_address() -> None:
    # Caddy must key off JBRAIN_SITE_ADDR (bare domain vs http://domain), not the
    # raw domain, or tunnel mode could never disable Let's Encrypt.
    caddyfile = _CADDYFILE.read_text()
    assert "{$JBRAIN_SITE_ADDR}" in caddyfile
    assert "{$JBRAIN_DOMAIN}" not in caddyfile
