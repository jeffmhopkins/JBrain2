"""The jerv web-search sidecar's deploy guarantees, asserted on the compose file.

SearXNG must be part of the stock stack (no compose profile) so it comes up with
every deploy and is recreated by `jbrain update` — the deploy tooling has no
per-service enable flag for it and the api points JBRAIN_SEARXNG_URL here by
default, so a gated service would just leave jerv reporting "unavailable". It
must also bind IPv4 explicitly: the granian-based image defaults to an IPv6 (::)
bind that crashes at boot on an IPv6-disabled Docker host."""

from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_SETTINGS = _DEPLOY / "searxng" / "settings.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def _settings() -> dict:
    return yaml.safe_load(_SETTINGS.read_text())


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


def test_search_spreads_across_several_engines() -> None:
    # Redundancy against a single engine's rate-limit cooldown: several independent
    # sources are enabled so one engine's 429 (suspended_time) can't blank a query.
    settings = _settings()
    engines = {e["name"]: e for e in settings.get("engines", [])}
    for name in ("duckduckgo", "brave", "bing", "mojeek", "qwant", "startpage"):
        assert name in engines, f"{name} must be enabled to spread search load"
        assert engines[name].get("disabled") is not True, f"{name} must not be disabled"


def test_duckduckgo_is_de_weighted_so_it_is_not_the_sole_primary() -> None:
    # The observed failure: a deep-research fan's volume got DuckDuckGo rate-limited while
    # it carried most results. De-weighting it makes it one source among many.
    engines = {e["name"]: e for e in _settings().get("engines", [])}
    ddg_weight = engines["duckduckgo"].get("weight", 1.0)
    assert ddg_weight < 1.0, "DuckDuckGo must be de-weighted below the default 1.0"


def test_engine_merge_relies_on_default_settings() -> None:
    # The per-name engine merge (each entry overrides only its fields, the rest of the
    # built-in def stands) is only valid with use_default_settings on — without it, a
    # bare `- name: bing` is an incomplete engine def and searxng fails to start.
    assert _settings().get("use_default_settings") is True


# Known-valid SearXNG built-in engine ids (searx/settings.yml). A configured name that is
# NOT one of these is an unknown engine: with use_default_settings on, the merge finds no
# base def, so searxng fails to load it and returns 500s / crash-loops on the next deploy —
# a failure this YAML-only test would otherwise miss (a typo like `startpge` passes every
# other assertion). This closes that gap without booting a container; widen the set when
# adding a genuinely new engine (verify the id against searx/settings.yml first).
_KNOWN_SEARXNG_ENGINES = frozenset(
    {"duckduckgo", "brave", "bing", "mojeek", "qwant", "startpage", "google", "wikipedia"}
)


def test_configured_engine_names_are_real_searxng_ids() -> None:
    for engine in _settings().get("engines", []):
        assert engine["name"] in _KNOWN_SEARXNG_ENGINES, (
            f"{engine['name']!r} is not a known SearXNG engine id — a typo/unknown name "
            "fails to load and crash-loops searxng, killing all of jerv's web search"
        )
