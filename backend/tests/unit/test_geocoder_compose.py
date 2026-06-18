"""The geocoder's no-off-box-route guarantee, asserted on the compose file (Phase 7
Wave 4). The Photon service must be opt-in (its own profile) and sit only on
networks marked `internal: true`, so even a compromised geocoder cannot reach the
internet."""

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_geocoder_is_opt_in_and_has_no_off_box_route() -> None:
    spec = _spec()
    networks = spec["networks"]
    geocoder = spec["services"]["geocoder"]
    # Opt-in: never starts on a stock deploy.
    assert geocoder.get("profiles") == ["geocoder"]
    # Every network the geocoder is on is internal (no NAT to the internet).
    geo_nets = geocoder["networks"]
    assert geo_nets, "geocoder must declare its networks"
    for net in geo_nets:
        assert networks[net] and networks[net].get("internal") is True, (
            f"geocoder network {net!r} is not internal:true — it has an off-box route"
        )


def test_app_can_reach_the_geocoder_network() -> None:
    # api + worker join geocoder_net so they can query Photon, while keeping their
    # own egress via `internal` (which is NOT internal-only).
    spec = _spec()
    for svc in ("api", "worker"):
        assert "geocoder_net" in spec["services"][svc]["networks"]
