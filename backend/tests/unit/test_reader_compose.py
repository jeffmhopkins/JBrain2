"""The jerv web_fetch reader-fallback sidecar's deploy guarantees, asserted on the
compose file.

The reader renders a page web_fetch couldn't (a bot-wall or a JS-rendered shell) and
returns clean markdown — the sanctioned, on-box stand-in for the model routing a URL
through the public r.jina.ai itself. It must be part of the stock stack (no compose
profile) so it comes up with every deploy and the api's JBRAIN_READER_URL default
(http://reader:3000) resolves to a running instance. Because it renders with a headless
browser, it needs an enlarged /dev/shm or Chromium crashes at boot."""

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_reader_is_in_the_stock_stack() -> None:
    reader = _spec()["services"]["reader"]
    # No profile: it starts with every `docker compose up -d`, so the config default
    # JBRAIN_READER_URL=http://reader:3000 points at a service that is actually up.
    assert "profiles" not in reader, (
        "reader must NOT be profile-gated — the api defaults JBRAIN_READER_URL to it, "
        "so a gated service would leave web_fetch's fallback pointing at nothing"
    )


def test_reader_has_enlarged_shm_for_headless_chromium() -> None:
    # Headless Chromium crashes on the Docker default 64m /dev/shm; the reader image
    # renders with a browser, so it must declare a larger shm_size.
    reader = _spec()["services"]["reader"]
    assert reader.get("shm_size"), "reader renders with headless Chromium and needs shm_size set"


def test_reader_shares_searxng_egress_network() -> None:
    # The reader fetches the public target itself, so it needs egress — the same
    # `internal` network searxng uses, reachable by api/worker at reader:3000.
    assert _spec()["services"]["reader"]["networks"] == ["internal"]
