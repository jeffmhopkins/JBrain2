"""The egress guard (the security core of the connector chokepoint) and the
medical connector parsers (docs/ASSISTANT.md "External connectors", invariant #9)."""

import pytest

from jbrain.connectors.base import (
    Connector,
    ConnectorRegistry,
    EgressGuardError,
    ParamSpec,
    build_egress,
)
from jbrain.connectors.medical import medical_connectors, parse_condition, parse_medication


def fake_connector(**over: object) -> Connector:
    defaults = {
        "name": "lookup_medication",
        "base_url": "https://rxnav.example/",
        "path": "/REST/drugs.json",
        "domain": "health",
        "params": (ParamSpec("name", str),),
        "parse": (lambda d: "ok"),
    }
    return Connector(**{**defaults, **over})  # type: ignore[arg-type]


class TestEgressGuard:
    def test_builds_the_outbound_request_from_typed_slots(self) -> None:
        req = build_egress(fake_connector(), {"name": "metformin"})
        assert req.method == "GET"
        assert req.url == "https://rxnav.example/REST/drugs.json"
        assert req.query == {"name": "metformin"}

    def test_rejects_an_undeclared_param(self) -> None:
        # The load-bearing rule: conversation/owner data cannot ride along (#9).
        with pytest.raises(EgressGuardError, match="undeclared params"):
            build_egress(fake_connector(), {"name": "x", "ssn": "123-45-6789"})

    def test_rejects_a_missing_required_param(self) -> None:
        with pytest.raises(EgressGuardError, match="missing required param"):
            build_egress(fake_connector(), {})

    def test_rejects_a_value_of_the_wrong_type(self) -> None:
        conn = fake_connector(params=(ParamSpec("lat", float),))
        with pytest.raises(EgressGuardError, match="not a float"):
            build_egress(conn, {"lat": "not-a-number"})

    def test_coerces_to_the_declared_type(self) -> None:
        conn = fake_connector(params=(ParamSpec("lat", float),))
        assert build_egress(conn, {"lat": "37.5"}).query == {"lat": "37.5"}

    def test_optional_param_may_be_omitted(self) -> None:
        conn = fake_connector(
            params=(ParamSpec("name", str), ParamSpec("near", str, required=False))
        )
        assert build_egress(conn, {"name": "x"}).query == {"name": "x"}

    def test_input_hash_is_stable_and_payload_free(self) -> None:
        a = build_egress(fake_connector(), {"name": "aspirin"})
        b = build_egress(fake_connector(), {"name": "aspirin"})
        c = build_egress(fake_connector(), {"name": "tylenol"})
        assert a.input_hash == b.input_hash and a.input_hash != c.input_hash


class TestRegistry:
    def test_get_rejects_unknown_and_disabled(self) -> None:
        registry = ConnectorRegistry(
            medical_connectors("https://rxnav.example", "https://mp.example"),
            disabled=frozenset({"lookup_condition"}),
        )
        assert registry.get("lookup_medication").domain == "health"
        assert registry.names() == {"lookup_medication"}
        with pytest.raises(EgressGuardError, match="disabled"):
            registry.get("lookup_condition")
        with pytest.raises(EgressGuardError, match="unknown connector"):
            registry.get("evil_fetch")


class TestParsers:
    def test_parse_medication_lists_concepts(self) -> None:
        data = {
            "drugGroup": {
                "conceptGroup": [
                    {"conceptProperties": [{"name": "metformin", "rxcui": "6809"}]},
                    {"conceptProperties": [{"name": "metformin 500 mg", "rxcui": "861007"}]},
                ]
            }
        }
        out = parse_medication(data)
        assert "metformin (rxcui 6809)" in out
        assert "RxNorm/RxNav" in out

    def test_parse_medication_empty(self) -> None:
        assert parse_medication({"drugGroup": {}}) == "No medication match found."

    def test_parse_condition_takes_first_entry(self) -> None:
        data = {
            "feed": {
                "entry": [
                    {
                        "title": {"_value": "Hypertension"},
                        "summary": {"_value": "High blood pressure."},
                    }
                ]
            }
        }
        out = parse_condition(data)
        assert "Hypertension" in out and "High blood pressure." in out and "MedlinePlus" in out

    def test_parse_condition_empty(self) -> None:
        assert parse_condition({"feed": {}}) == "No condition overview found."
