"""Agent contracts: the session policy, .tool sidecar frontmatter, citation
refs, tool-result views, and the streaming chat-event discriminated union."""

import pytest
from pydantic import TypeAdapter, ValidationError

from jbrain.agent.contracts import (
    DEFAULT_OWNER_POLICY,
    ChatEvent,
    CitationRef,
    EntityRef,
    FactRef,
    NoteRef,
    TextDelta,
    ToolSpec,
    ToolViewEvent,
    ViewPayload,
)


def test_default_owner_policy_stages_writes_and_egress() -> None:
    assert DEFAULT_OWNER_POLICY["read"] == "direct"
    assert DEFAULT_OWNER_POLICY["mutate"] == "staged"
    assert DEFAULT_OWNER_POLICY["sensitive"] == "staged"
    # Every off-box call stages an egress Proposal — never silently run or denied.
    assert DEFAULT_OWNER_POLICY["external"] == "staged"


def test_toolspec_parses_with_defaults() -> None:
    spec = ToolSpec(name="search", version=1, params={"type": "object"}, permission="read")
    assert spec.mutating is False
    assert spec.side_effecting is False
    assert spec.cost_class == "cheap"
    assert spec.response_format == "concise"
    assert spec.domains == []


def test_toolspec_forbids_unknown_frontmatter_keys() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="x", version=1, params={}, permission="read", typo=True)  # type: ignore[call-arg]


def test_toolspec_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="x", version=0, params={}, permission="read")


def test_citation_ref_discriminates_on_kind() -> None:
    adapter = TypeAdapter(CitationRef)
    fact = adapter.validate_python({"kind": "fact", "fact_id": "f1", "label": "BP 120/80"})
    assert isinstance(fact, FactRef)
    entity = adapter.validate_python(
        {"kind": "entity", "entity_id": "e1", "label": "Dr. Lin", "domain": "health"}
    )
    assert isinstance(entity, EntityRef)
    assert entity.domain == "health"
    note = adapter.validate_python({"kind": "note", "note_id": "n1", "label": "intake"})
    assert isinstance(note, NoteRef)


def test_view_payload_carries_refs() -> None:
    view = ViewPayload(
        view="lab_plot",
        surface="inline",
        data={"test": "LDL"},
        refs=[FactRef(fact_id="f1", label="LDL 100")],
    )
    dumped = view.model_dump()
    assert dumped["view"] == "lab_plot"
    assert dumped["refs"][0]["kind"] == "fact"


def test_chat_event_discriminates_on_type() -> None:
    adapter = TypeAdapter(ChatEvent)
    text = adapter.validate_python({"type": "text_delta", "text": "hi"})
    assert isinstance(text, TextDelta)
    view_event = adapter.validate_python(
        {
            "type": "tool_view",
            "tool_call_id": "c1",
            "view": {"view": "lab_plot", "data": {}, "surface": "inline"},
        }
    )
    assert isinstance(view_event, ToolViewEvent)
    assert view_event.view.view == "lab_plot"


def test_chat_event_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ChatEvent).validate_python({"type": "nope"})
