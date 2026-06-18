"""The on-box geocoding agent tools (Phase 7 Wave 4): reverse (default) + forward
(owner-only). The geocoder client is faked; these assert the handler contracts and
the owner-only gate on the free-text forward lookup."""

from jbrain.agent.geocodetools import build_geocode_handlers
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.geocode import GeocodeResult

FULL_OWNER = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
NARROWED_OWNER = ToolContext(
    session=SessionContext(principal_kind="owner", owner_scoped=True), scopes=()
)
NON_OWNER = ToolContext(session=SessionContext(principal_kind="capability_token"), scopes=())


class FakeGeocoder:
    def __init__(self) -> None:
        self.reverse_calls = 0
        self.forward_calls: list[str] = []
        self.raise_on_reverse = False

    async def reverse(self, latitude: float, longitude: float) -> GeocodeResult | None:
        self.reverse_calls += 1
        if self.raise_on_reverse:
            raise RuntimeError("photon down")
        return GeocodeResult(label="Home, Townsville", latitude=latitude, longitude=longitude)

    async def forward(self, query: str, limit: int = 5) -> list[GeocodeResult]:
        self.forward_calls.append(query)
        return [GeocodeResult(label="Office, Metropolis", latitude=41.0, longitude=-75.0)]


def _tools(geo: FakeGeocoder):
    handlers = build_geocode_handlers(geo)  # type: ignore[arg-type]
    return handlers["geocode_reverse"], handlers["geocode_forward"]


async def test_reverse_returns_the_address() -> None:
    geo = FakeGeocoder()
    reverse, _ = _tools(geo)
    out = await reverse({"latitude": 40.0, "longitude": -74.0}, FULL_OWNER)
    assert out == "Home, Townsville"


async def test_reverse_rejects_non_numeric_coords() -> None:
    geo = FakeGeocoder()
    reverse, _ = _tools(geo)
    out = await reverse({"latitude": "north", "longitude": -74.0}, FULL_OWNER)
    assert "numeric" in out
    assert geo.reverse_calls == 0


async def test_reverse_survives_a_geocoder_outage() -> None:
    geo = FakeGeocoder()
    geo.raise_on_reverse = True
    reverse, _ = _tools(geo)
    out = await reverse({"latitude": 40.0, "longitude": -74.0}, FULL_OWNER)
    assert "unavailable" in out


async def test_forward_is_owner_only() -> None:
    geo = FakeGeocoder()
    _, forward = _tools(geo)
    # A narrowed agent scope and a non-owner are both refused BEFORE the query is
    # sent — the free-text slot never reaches the geocoder.
    for ctx in (NARROWED_OWNER, NON_OWNER):
        out = await forward({"query": "1600 Pennsylvania Ave"}, ctx)
        assert "owner-only" in out
    assert geo.forward_calls == []


async def test_forward_runs_for_a_full_owner() -> None:
    geo = FakeGeocoder()
    _, forward = _tools(geo)
    out = await forward({"query": "office"}, FULL_OWNER)
    assert "Office, Metropolis" in out
    assert geo.forward_calls == ["office"]
