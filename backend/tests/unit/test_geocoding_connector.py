"""The external reverse-geocoder fallback connector (Phase 7 Wave 4b): default-off,
typed coordinates only (no free-text egress), and Proposal-gated. The connector
cache's location+owner RLS is proven in test_agent_connectors_rls.py."""

import pytest

from jbrain.agent.connectortools import build_connector_handlers
from jbrain.agent.loop import ToolContext
from jbrain.connectors.base import ConnectorRegistry, EgressGuardError, build_egress
from jbrain.connectors.geocoding import geocode_connectors, parse_external_geocode
from jbrain.db.session import SessionContext

_URL = "https://geo.example"


class FakeProposals:
    def __init__(self) -> None:
        self.staged: list = []

    async def stage(self, ctx: object, *, principal_id: str, spec: object) -> str:
        self.staged.append(spec)
        return "prop-1"


def test_default_off_registers_no_connector() -> None:
    assert geocode_connectors("") == []


def test_configured_connector_is_location_reverse_only() -> None:
    [conn] = geocode_connectors(_URL)
    assert conn.name == "geocode_external"
    assert conn.domain == "location"
    assert conn.consent_required is True
    # Typed coordinates only — no free-text query slot exists to exfiltrate through.
    assert {p.name: p.kind for p in conn.params} == {"lat": float, "lon": float}


def test_build_egress_admits_coordinates_and_rejects_free_text() -> None:
    [conn] = geocode_connectors(_URL)
    req = build_egress(conn, {"lat": 40.0, "lon": -74.0})
    assert req.url == "https://geo.example/reverse?format=jsonv2"
    assert req.query == {"lat": "40.0", "lon": "-74.0"}
    # A smuggled free-text param is rejected before any call (the #9 guard).
    with pytest.raises(EgressGuardError):
        build_egress(conn, {"lat": 40.0, "lon": -74.0, "q": "owner secret"})


def test_parse_handles_nominatim_geojson_and_empty() -> None:
    assert "1 Main St, Townsville" in parse_external_geocode(
        {"display_name": "1 Main St, Townsville"}
    )
    geojson = {"features": [{"properties": {"name": "Home", "city": "Townsville"}}]}
    assert "Home, Townsville" in parse_external_geocode(geojson)
    assert parse_external_geocode({}) == "No address found."


async def test_external_geocode_tool_stages_a_proposal_not_a_call() -> None:
    registry = ConnectorRegistry(geocode_connectors(_URL))
    proposals = FakeProposals()
    handler = build_connector_handlers(registry, proposals)["geocode_external"]  # type: ignore[arg-type]
    ctx = ToolContext(
        session=SessionContext(
            principal_kind="owner", principal_id="p1", domain_scopes=("location",)
        ),
        scopes=("location",),
    )
    out = await handler({"lat": 40.0, "lon": -74.0}, ctx)
    assert "staged" in out.lower() and "prop-1" in out
    spec = proposals.staged[0]
    assert spec.kind == "egress" and spec.domain == "location"
    node = spec.nodes[0]
    assert node.op == "egress_call" and node.preview["connector"] == "geocode_external"
    assert node.preview["query"] == {"lat": "40.0", "lon": "-74.0"}
