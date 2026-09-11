"""The live pipeline flow trace (analysis.flow_trace): flag gating and the
structured payload each seam emits. Pure projection over the pipeline's objects,
so the inputs are lightweight stand-ins (cast to the real types) exercising only
the attributes each emitter reads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import structlog

from jbrain.analysis import flow_trace
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


def test_disabled_emits_no_vision() -> None:
    flow_trace.set_enabled(False)
    with structlog.testing.capture_logs() as logs:
        flow_trace.vision(
            "att-1",
            note_id="n1",
            kind="caption",
            provider="local",
            model="qwen3-vl-30b",
            filename="x.png",
            text="hello",
        )
    assert logs == []


def test_vision_surfaces_the_model_text_capped() -> None:
    flow_trace.set_enabled(True)
    long_text = "x" * (flow_trace._VISION_TEXT_CAP + 50)
    with structlog.testing.capture_logs() as logs:
        flow_trace.vision(
            "5a747d5e-bab2-0000",
            note_id="ea6c62cb-0000",
            kind="ocr",
            provider="local",
            model="qwen3-vl-30b",
            filename="Screenshot.png",
            text=long_text,
        )
    [ev] = logs
    assert ev["event"] == "analysis.flow.vision"
    assert ev["attachment_id"] == "5a747d5e" and ev["note_id"] == "ea6c62cb"  # shortened ids
    assert ev["kind"] == "ocr" and ev["model"] == "qwen3-vl-30b"
    assert ev["chars"] == len(long_text)
    assert len(ev["text"]) == flow_trace._VISION_TEXT_CAP and ev["truncated"] is True


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
