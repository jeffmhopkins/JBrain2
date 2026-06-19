"""The jerv web-search sidecar's deploy guarantees, asserted on the compose file.

SearXNG must be part of the stock stack (no compose profile) so it comes up with
every deploy and is recreated by `jbrain update` — the deploy tooling has no
per-service enable flag for it and the api points JBRAIN_SEARXNG_URL here by
default, so a gated service would just leave jerv reporting "unavailable". It
must also bind IPv4 explicitly: the granian-based image defaults to an IPv6 (::)
bind that crashes at boot on an IPv6-disabled Docker host."""

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_searxng_is_in_the_stock_stack() -> None:
    searxng = _spec()["services"]["searxng"]
    # No profile: it starts with every `docker compose up -d`, unlike the
    # profile-gated geocoder/tunnel/mqtt services.
    assert "profiles" not in searxng, (
        "searxng must NOT be profile-gated — nothing in the deploy tooling "
        "activates a searxng profile, so a gated service never starts"
    )


def test_searxng_binds_ipv4() -> None:
    env = _spec()["services"]["searxng"]["environment"]
    # Guards against the granian default :: (IPv6) bind, which crashes the
    # container on hosts without IPv6 and makes searxng:8080 unreachable.
    assert env.get("GRANIAN_HOST") == "0.0.0.0"


def test_searxng_mounts_config_dir_not_settings_file() -> None:
    # Bind the /etc/searxng DIRECTORY, never the settings.yml file alone. A
    # single-file bind is a footgun: if the host file is missing when compose
    # runs, Docker creates an empty directory there, the image entrypoint sees
    # /etc/searxng/settings.yml as a directory, and the container crash-loops
    # ("not a valid file"). A directory bind can only ever be a directory.
    volumes = _spec()["services"]["searxng"]["volumes"]
    assert "./searxng:/etc/searxng" in volumes, (
        "searxng must bind the config directory so a missing host file cannot "
        "make Docker create a settings.yml directory that crash-loops the image"
    )
    assert not any(v.endswith("/etc/searxng/settings.yml") for v in volumes), (
        "a single-file settings.yml bind crash-loops when the host file is missing"
    )
