"""The live pipeline flow trace (analysis.flow_trace): flag gating and the
structured payload each seam emits. Pure projection over the pipeline's objects,
so the inputs are lightweight stand-ins (cast to the real types) exercising only
the attributes each emitter reads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import structlog

from jbrain.analysis import flow_trace
from jbrain.analysis.arbiter import ArbiterPlan
from jbrain.analysis.extraction import Extraction
from jbrain.analysis.intent import IntegrationIntent
from jbrain.analysis.supersession import Decision, FactView


def _ext_fact(
    entity_ref: str,
    predicate: str,
    obj: str | None,
    *,
    kind: str = "relationship",
    qualifier: str = "",
    assertion: str = "asserted",
    inferred: bool = False,
    value_json: dict[str, Any] | None = None,
    temporal: Any = None,
    attested_span: Any = None,
    self_confidence: float = 0.9,
) -> Any:
    return SimpleNamespace(
        entity_ref=entity_ref,
        predicate=predicate,
        qualifier=qualifier,
        object_entity_ref=obj,
        kind=kind,
        assertion=assertion,
        inferred=inferred,
        value_json=value_json,
        temporal=temporal,
        attested_span=attested_span,
        self_confidence=self_confidence,
    )


def _extraction() -> Extraction:
    return cast(
        Extraction,
        SimpleNamespace(
            mentions=[SimpleNamespace(name=n) for n in ("Me", "summer", "lydian")],
            facts=[
                _ext_fact("Me", "children", "summer"),
                _ext_fact("Me", "children", "lydian"),
                _ext_fact("summer", "name.full", None, kind="attribute"),
            ],
        ),
    )


def _intent(
    *,
    resolutions: list[Any] | None = None,
    facts: list[Any] | None = None,
    supersessions: list[Any] | None = None,
) -> IntegrationIntent:
    return cast(
        IntegrationIntent,
        SimpleNamespace(
            entity_resolutions=resolutions or [],
            facts=facts or [],
            supersession_proposals=supersessions or [],
        ),
    )


def _plan(
    *,
    rejected: bool = False,
    facts: list[Any] | None = None,
    violations: list[Any] | None = None,
) -> ArbiterPlan:
    return cast(
        ArbiterPlan,
        SimpleNamespace(rejected=rejected, facts=facts or [], fatal_violations=violations or []),
    )


def _factview(id: str, obj: str | None, status: str = "active") -> FactView:
    return cast(FactView, SimpleNamespace(id=id, object_entity_id=obj, status=status))


def setup_function() -> None:
    flow_trace.reset()


def teardown_function() -> None:
    flow_trace.reset()


def test_enabled_reads_settings_and_auto_arms_with_debug_access(monkeypatch: Any) -> None:
    def _settings(analysis_trace: bool, debug_access_enabled: bool) -> SimpleNamespace:
        return SimpleNamespace(
            analysis_trace=analysis_trace, debug_access_enabled=debug_access_enabled
        )

    # Off when both flags are off.
    monkeypatch.setattr(flow_trace, "get_settings", lambda: _settings(False, False))
    flow_trace.reset()
    assert flow_trace.enabled() is False

    # The explicit flag turns it on without the console.
    monkeypatch.setattr(flow_trace, "get_settings", lambda: _settings(True, False))
    flow_trace.reset()
    assert flow_trace.enabled() is True

    # Debug access alone auto-arms it — independent of any mint event, so an
    # already-minted token (console enabled) keeps tracing on across restarts.
    monkeypatch.setattr(flow_trace, "get_settings", lambda: _settings(False, True))
    flow_trace.reset()
    assert flow_trace.enabled() is True

    # Cached after first read: a later settings change is not seen until reset().
    monkeypatch.setattr(flow_trace, "get_settings", lambda: _settings(False, False))
    assert flow_trace.enabled() is True
    flow_trace.reset()
    assert flow_trace.enabled() is False


def test_disabled_emits_nothing() -> None:
    flow_trace.set_enabled(False)
    with structlog.testing.capture_logs() as logs:
        flow_trace.extract("n1", _extraction())
        flow_trace.intent("n1", "integrate", _intent())
        flow_trace.plan("n1", _plan())
        flow_trace.commit(
            "n1",
            entity_ref="Me",
            predicate="children",
            qualifier="",
            object_ref="summer",
            subject_id="s",
            object_id="o",
            existing=[],
            decision=Decision(insert=True),
        )
    assert logs == []


def test_extract_lists_only_relationship_edges() -> None:
    flow_trace.set_enabled(True)
    with structlog.testing.capture_logs() as logs:
        flow_trace.extract("n1", _extraction())
    [ev] = logs
    assert ev["event"] == "analysis.flow.extract"
    assert ev["note_id"] == "n1"
    assert ev["facts"] == 3  # all facts counted
    assert ev["mentions"] == ["Me", "summer", "lydian"]
    # the attribute fact is excluded — only edges are shown
    assert ev["edges"] == ["Me.children -> summer", "Me.children -> lydian"]


def test_intent_stage_resolutions_and_supersessions() -> None:
    flow_trace.set_enabled(True)
    intent = _intent(
        resolutions=[
            SimpleNamespace(mention_ref="summer", mode="new"),
            SimpleNamespace(mention_ref="Me", mode="existing"),
        ],
        facts=[_ext_fact("Me", "children", "summer", qualifier="step")],
        supersessions=[SimpleNamespace(entity_ref="Me", predicate="children", action="supersede")],
    )
    with structlog.testing.capture_logs() as logs:
        flow_trace.intent("n1", "recover", intent)
    [ev] = logs
    assert ev["event"] == "analysis.flow.intent"
    assert ev["stage"] == "recover"
    assert ev["resolutions"] == ["summer:new", "Me:existing"]
    assert ev["facts"] == [
        {
            "edge": "Me.children.step -> summer",
            "kind": "relationship",
            "assertion": "asserted",
            "inferred": False,
        }
    ]
    assert ev["supersessions"] == ["Me.children:supersede"]


def test_intent_facts_surface_value_and_temporal() -> None:
    flow_trace.set_enabled(True)
    intent = _intent(
        facts=[
            _ext_fact(
                "Allan",
                "birthDate",
                None,
                kind="attribute",
                inferred=True,
                value_json={"value": "1985-02-15"},
                temporal=SimpleNamespace(phrase="February 15th 1985", resolved_start="1985-02-15"),
            )
        ],
    )
    with structlog.testing.capture_logs() as logs:
        flow_trace.intent("n1", "integrate", intent)
    [ev] = logs
    [f] = ev["facts"]
    assert f["value"] == "1985-02-15"
    assert f["temporal"] == {"phrase": "February 15th 1985", "start": "1985-02-15"}
    assert f["inferred"] is True


def test_plan_rejected_lists_violation_codes() -> None:
    flow_trace.set_enabled(True)
    plan = _plan(rejected=True, violations=[SimpleNamespace(code="resolution_missing_entity")])
    with structlog.testing.capture_logs() as logs:
        flow_trace.plan("n1", plan)
    [ev] = logs
    assert ev["rejected"] is True
    assert ev["violations"] == ["resolution_missing_entity"]


def test_plan_facts_show_status_and_weight() -> None:
    flow_trace.set_enabled(True)
    plan = _plan(
        facts=[
            SimpleNamespace(
                fact=_ext_fact("Me", "children", "summer"),
                status="active",
                weight=0.9123,
                review_reasons=(),
            ),
            SimpleNamespace(
                fact=_ext_fact("Me", "children", "lydian"),
                status="pending_review",
                weight=0.4,
                review_reasons=("below_threshold",),
            ),
        ]
    )
    signals = {
        0: SimpleNamespace(surface_attested=True, predicate_known=True),
        1: SimpleNamespace(surface_attested=False, predicate_known=True),
    }
    with structlog.testing.capture_logs() as logs:
        flow_trace.plan("n1", plan, signals)
    [ev] = logs
    f0 = ev["facts"][0]
    assert f0["edge"] == "Me.children -> summer"
    assert f0["status"] == "active"
    assert f0["weight"] == 0.912  # rounded to 3 places
    assert f0["review"] == []
    assert f0["surface_attested"] is True  # the signal behind the weight
    f1 = ev["facts"][1]
    assert f1["status"] == "pending_review"
    assert f1["review"] == ["below_threshold"]
    assert f1["surface_attested"] is False  # held: no grounding fired


def test_plan_facts_omit_signals_when_not_supplied() -> None:
    flow_trace.set_enabled(True)
    plan = _plan(
        facts=[
            SimpleNamespace(
                fact=_ext_fact("Me", "children", "summer"),
                status="active",
                weight=1.0,
                review_reasons=(),
            )
        ]
    )
    with structlog.testing.capture_logs() as logs:
        flow_trace.plan("n1", plan)
    [ev] = logs
    assert "surface_attested" not in ev["facts"][0]
    assert ev["facts"][0]["has_value"] is False


def _commit(**over: Any) -> dict[str, Any]:
    flow_trace.set_enabled(True)
    args: dict[str, Any] = dict(
        entity_ref="Me",
        predicate="children",
        qualifier="",
        object_ref="summer",
        subject_id="7d381675-0000-0000",
        object_id="68f005d9-0000-0000",
        existing=[],
        decision=Decision(insert=True, insert_status="active"),
    )
    args.update(over)
    with structlog.testing.capture_logs() as logs:
        flow_trace.commit("n1", **args)
    [ev] = logs
    return dict(ev)


def test_commit_insert_against_empty_graph() -> None:
    ev = _commit()
    assert ev["event"] == "analysis.flow.commit"
    assert ev["edge"] == "Me.children -> summer"
    assert ev["verb"] == "insert"
    assert ev["subject_id"] == "7d381675"  # shortened to first uuid segment
    assert ev["object_id"] == "68f005d9"
    assert ev["insert_status"] == "active"
    assert ev["existing"] == []


def test_commit_surfaces_collapse_when_lookup_hits_a_sibling_row() -> None:
    # An Elora candidate whose identity-key lookup pulls back the Summer row and
    # resolves to a refresh instead of an insert — the exact collapse signature
    # this trace exists to make visible.
    ev = _commit(
        object_ref="Elora",
        object_id="62c477b7-0000",
        existing=[_factview("68f005d9-aaaa", "68f005d9-aaaa", "active")],
        decision=Decision(refresh_id="68f005d9-aaaa"),
    )
    assert ev["edge"] == "Me.children -> Elora"
    assert ev["verb"] == "refresh"
    assert ev["object_id"] == "62c477b7"
    assert ev["existing"] == [{"id": "68f005d9", "obj": "68f005d9", "status": "active"}]


def test_commit_insert_plus_supersede_verb() -> None:
    ev = _commit(decision=Decision(insert=True, supersede_ids=["x-1", "x-2"]))
    assert ev["verb"] == "insert+supersede"
    assert ev["supersedes"] == 2
