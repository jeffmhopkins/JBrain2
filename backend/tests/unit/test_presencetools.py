"""jerv's owner-location tool: `current_location` — a `web`-gated, jerv-only on-box
reverse-geocode of the live PWA fix the turn carried (no saved-place / device read)."""

import pytest

from jbrain.agent.loop import ToolContext
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.db.session import SessionContext
from jbrain.geocode import GeocodeResult


class _Geo:
    def __init__(self, result: GeocodeResult | None) -> None:
        self._result = result

    async def reverse(self, latitude, longitude):  # noqa: ANN001, ANN201
        return self._result

    async def forward(self, query, limit=5):  # noqa: ANN001, ANN201
        return []


def _here_ctx(lat: float, lon: float) -> ToolContext:
    session = SessionContext(principal_id="own", principal_kind="owner", owner_scoped=True)
    return ToolContext(session=session, scopes=(), here=(lat, lon))


def _tool(geo: "_Geo | None" = None):  # noqa: ANN202
    return build_presence_handlers(geo)["current_location"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_current_location_reverse_geocodes_the_live_fix_to_an_address() -> None:
    geo = _Geo(GeocodeResult(label="221B Baker St, London", latitude=51.52, longitude=-0.16))
    out = await _tool(geo)({}, _here_ctx(51.52, -0.16))
    assert "221B Baker St, London" in out


@pytest.mark.asyncio
async def test_current_location_returns_coordinates_when_the_geocoder_misses() -> None:
    # No address resolved → the coordinate itself is an acceptable answer.
    out = await _tool(_Geo(None))({}, _here_ctx(51.52, -0.16))
    assert "51.52" in out and "-0.16" in out


@pytest.mark.asyncio
async def test_current_location_returns_coordinates_with_no_geocoder() -> None:
    out = await _tool(None)({}, _here_ctx(51.52000, -0.16000))
    assert "51.52" in out and "-0.16" in out


@pytest.mark.asyncio
async def test_current_location_without_a_live_fix_asks_to_share() -> None:
    # No coords on the turn → nothing to report; never reads the owner's location DB.
    session = SessionContext(principal_id="own", principal_kind="owner", owner_scoped=True)
    out = await _tool(_Geo(None))({}, ToolContext(session=session, scopes=()))
    assert "share it" in out
