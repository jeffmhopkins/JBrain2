"""Opt-in host-mode jcode web preview, asserted on the deploy config. A stock deploy
renders no preview site; setting JBRAIN_JCODE_PREVIEW_BASE_HOST makes the proxy add a
wildcard *.<host> Caddy site that routes ONLY <slug>-preview.<host> to the api's
/__jcode_preview/<slug> prefix (404ing every other subdomain). The site is rendered from
the env at container start by deploy/proxy-preview-conf.sh, mirroring the LAN site.
See docs/archive/JCODE_PREVIEW_HOST_PLAN.md."""

import subprocess
from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_CADDYFILE = _DEPLOY / "Caddyfile"
_DOCKERFILE = _DEPLOY / "Dockerfile.proxy"
_CONF = _DEPLOY / "proxy-preview-conf.sh"


def test_proxy_gets_the_preview_base_host_from_the_shared_env() -> None:
    env = yaml.safe_load(_COMPOSE.read_text())["services"]["proxy"]["environment"]
    # Shares the .env var with the api + jcode services; empty by default (off).
    assert env["JBRAIN_JCODE_PREVIEW_BASE_HOST"] == "${JCODE_PREVIEW_BASE_HOST:-}"


def test_caddy_imports_the_preview_site_and_404s_the_prefix_on_the_app() -> None:
    caddyfile = _CADDYFILE.read_text()
    # The optional preview site, from a glob that matches nothing when unrendered.
    assert "import /etc/caddy/preview/*.caddy" in caddyfile
    # The internal preview prefix is refused on every app surface (public + LAN), so a
    # sandbox dev app can only ever be served on its own <slug>-preview.<host> subdomain.
    assert "handle /__jcode_preview* {" in caddyfile


def test_proxy_image_renders_the_preview_site() -> None:
    dockerfile = _DOCKERFILE.read_text()
    assert "proxy-preview-conf.sh" in dockerfile
    # The entrypoint runs it at start (alongside the LAN renderer).
    assert "proxy-preview-conf.sh" in (_DEPLOY / "proxy-entrypoint.sh").read_text()


# Tunnel mode (Cloudflare terminates TLS) is the host-preview deployment model; the
# renderer skips otherwise (an http:// wildcard in direct mode would have no cert).
_TUNNEL_ENV = {"JBRAIN_SITE_ADDR": "http://box.example", "PATH": "/usr/bin:/bin"}


def _run(conf_dir: Path, **env: str) -> None:
    subprocess.run(["sh", str(_CONF), str(conf_dir)], check=True, env={**_TUNNEL_ENV, **env})


def test_conf_renders_the_wildcard_site_when_base_host_set(tmp_path: Path) -> None:
    _run(tmp_path, JBRAIN_JCODE_PREVIEW_BASE_HOST="box.example")
    rendered = (tmp_path / "preview.caddy").read_text()
    assert "http://*.box.example {" in rendered
    # Matches only the slug-preview hosts, rewrites to the api's internal prefix.
    assert "header_regexp Host ^([0-9a-f]+)-preview" in rendered
    assert "rewrite * /__jcode_preview/{re.preview.1}{uri}" in rendered
    assert "reverse_proxy api:8000" in rendered
    # Every other subdomain on the catch-all wildcard is refused.
    assert "respond 404" in rendered


def test_conf_renders_nothing_and_clears_stale_when_unset(tmp_path: Path) -> None:
    stale = tmp_path / "preview.caddy"
    stale.write_text("http://*.old.example {\n}\n")
    _run(tmp_path)  # no base host
    # Blanking/removing the base host tears the preview site back down.
    assert not stale.exists()


def test_conf_skips_in_direct_mode(tmp_path: Path) -> None:
    # A bare (auto-TLS) JBRAIN_SITE_ADDR isn't tunnel mode — render nothing rather than
    # stand up an http:// wildcard that can't get a certificate.
    subprocess.run(
        ["sh", str(_CONF), str(tmp_path)],
        check=True,
        env={
            "JBRAIN_JCODE_PREVIEW_BASE_HOST": "box.example",
            "JBRAIN_SITE_ADDR": "box.example",
            "PATH": "/usr/bin:/bin",
        },
    )
    assert not (tmp_path / "preview.caddy").exists()


def test_conf_fails_closed_on_a_malformed_base_host(tmp_path: Path) -> None:
    # A typo'd host with a space would emit an invalid site address and crash-loop the
    # whole proxy — fail closed (no file) instead.
    _run(tmp_path, JBRAIN_JCODE_PREVIEW_BASE_HOST="bad host.example")
    assert not (tmp_path / "preview.caddy").exists()
