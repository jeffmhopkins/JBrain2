"""The pure halves of `models/note_conversation.py`: the body hash a conversation is
opened against, and the cap on a recorded tool call's arguments."""

import json

from jbrain.models.note_conversation import (
    MAX_ARG_CHARS,
    MAX_ARGS_CHARS,
    cap_tool_args,
    note_body_sha,
)


def test_the_body_hash_moves_with_the_body() -> None:
    """D6 appends a clarification block, so a resumed pass must see a different sha."""
    assert note_body_sha("a note") == note_body_sha("a note")
    assert note_body_sha("a note") != note_body_sha("a note\n\n[2026-09-09] and also…")


def test_small_args_pass_through_untouched() -> None:
    args = {"entity": "e1", "predicate": "treatedBy", "quote": "she saw Dr Patel"}
    assert cap_tool_args(args) == args


def test_a_long_string_is_truncated_at_any_depth() -> None:
    """The batch shapes in TOOL_SURFACE.md put the quotes inside arrays of objects, so a
    top-level-only cap would miss the field that actually carries note text."""
    capped = cap_tool_args({"facts": [{"quote": "x" * 50_000, "predicate": "weight"}]})
    assert len(capped["facts"][0]["quote"]) == MAX_ARG_CHARS
    assert capped["facts"][0]["predicate"] == "weight"  # a short sibling is untouched
    assert capped["_truncated"] is True


def test_many_small_elements_degrade_to_key_names() -> None:
    """Each element clears the per-string cap and the blob is still huge — the ledger
    then keeps the shape of the call, which is what the D3 chip renders."""
    capped = cap_tool_args({"facts": [{"quote": "y" * 500} for _ in range(200)]})
    assert capped == {"_keys": ["facts"], "_truncated": True}
    assert len(json.dumps(capped)) < MAX_ARGS_CHARS


def test_capping_never_raises_on_a_non_json_value() -> None:
    """A handler recording its own args must not lose the audit row to a serializer."""
    assert cap_tool_args({"when": object()})["when"] is not None
