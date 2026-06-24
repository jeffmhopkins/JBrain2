"""The live pipeline flow trace (analysis.flow_trace): flag gating and the
structured payload each seam emits. Pure projection over the pipeline's objects,
so the inputs are lightweight stand-ins exercising the attributes it reads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import structlog

from jbrain.analysis import flow_trace
from jbrain.analysis.supersession import Decision


def _ext_fact(
    entity_ref: str,
    predicate: str,
    obj: str | None,
    *,
    kind: str = "relationship",
    qualifier: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        entity_ref=entity_ref,
        predicate=predicate,
        qualifier=qualifier,
        object_entity_ref=obj,
        kind=kind,
    )


def _extraction() -> SimpleNamespace:
    return SimpleNamespace(
        mentions=[SimpleNamespace(name=n) for n in ("Me", "summer", "lydian")],
        facts=[
            _ext_fact("Me", "children", "summer"),
            _ext_fact("Me", "children", "lydian"),
            _ext_fact("summer", "name.full", None, kind="attribute"),
        ],
    )


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

    # Debug access alone auto-arms it.
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
        flow_trace.intent("n1", "integrate", _extraction())
        flow_trace.plan("n1", SimpleNamespace(rejected=False, facts=[]))
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
    intent = SimpleNamespace(
        entity_resolutions=[
            SimpleNamespace(mention_ref="summer", mode="new"),
            SimpleNamespace(mention_ref="Me", mode="existing"),
        ],
        facts=[_ext_fact("Me", "children", "summer", qualifier="step")],
        supersession_proposals=[
            SimpleNamespace(entity_ref="Me", predicate="children", action="supersede")
        ],
    )
    with structlog.testing.capture_logs() as logs:
        flow_trace.intent("n1", "recover", intent)
    [ev] = logs
    assert ev["event"] == "analysis.flow.intent"
    assert ev["stage"] == "recover"
    assert ev["resolutions"] == ["summer:new", "Me:existing"]
    assert ev["edges"] == ["Me.children.step -> summer"]
    assert ev["supersessions"] == ["Me.children:supersede"]


def test_plan_rejected_lists_violation_codes() -> None:
    flow_trace.set_enabled(True)
    plan = SimpleNamespace(
        rejected=True, fatal_violations=[SimpleNamespace(code="resolution_missing_entity")]
    )
    with structlog.testing.capture_logs() as logs:
        flow_trace.plan("n1", plan)
    [ev] = logs
    assert ev["rejected"] is True
    assert ev["violations"] == ["resolution_missing_entity"]


def test_plan_facts_show_status_and_weight() -> None:
    flow_trace.set_enabled(True)
    facts = [
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
    with structlog.testing.capture_logs() as logs:
        flow_trace.plan("n1", SimpleNamespace(rejected=False, facts=facts))
    [ev] = logs
    assert ev["facts"][0] == {
        "edge": "Me.children -> summer",
        "status": "active",
        "weight": 0.912,  # rounded to 3 places
        "review": [],
    }
    assert ev["facts"][1]["status"] == "pending_review"
    assert ev["facts"][1]["review"] == ["below_threshold"]


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
    return ev


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
        existing=[
            SimpleNamespace(
                id="68f005d9-aaaa", object_entity_id="68f005d9-aaaa", status="active"
            )
        ],
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
