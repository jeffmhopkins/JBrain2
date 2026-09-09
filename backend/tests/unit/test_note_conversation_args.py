"""The pure halves of `models/note_conversation.py`: the body hash a conversation is
opened against, the cap on a recorded tool call's arguments, and the domain-code check.

The cap's promise is that the ledger cannot be grown without bound by a hostile note
body (risk 1 + CLAUDE.md #10) and that the audit row is never the thing a call loses.
That the capped blob really survives the JSONB bind processor is proved end to end in
`tests/integration/test_note_conversations_pg.py` — a pure-function assertion here
cannot see the serializer that was the actual bug.
"""

import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain.analysis.extraction import DOMAINS
from jbrain.models.note_conversation import (
    KNOWLEDGE_DOMAINS,
    MAX_ARG_CHARS,
    MAX_ARGS_CHARS,
    cap_tool_args,
    note_body_sha,
    validate_domains,
)


def encoded(capped: dict[str, Any]) -> int:
    """The length the JSONB bind processor will actually produce — a bare `json.dumps`,
    with no `default=`, because that is what SQLAlchemy uses."""
    return len(json.dumps(capped))


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
    assert encoded(capped) < MAX_ARGS_CHARS


def test_a_long_key_is_truncated_like_a_value_at_any_depth() -> None:
    """The cap walked values only, so a long KEY sailed through it. Sized between the
    per-arg and the total cap on purpose: an oversized blob would be caught by the total
    bound anyway, and what has to hold here is that a key is capped IN PLACE, the same
    way a `quote` is."""
    top = cap_tool_args({"k" * 3_000: 1})
    assert [len(k) for k in top if k != "_truncated"] == [MAX_ARG_CHARS]
    assert top["_truncated"] is True

    nested = cap_tool_args({"facts": [{"q" * 3_000: "short"}]})
    assert [len(k) for k in nested["facts"][0]] == [MAX_ARG_CHARS]
    assert nested["_truncated"] is True


def test_one_enormous_key_cannot_grow_the_row_without_bound() -> None:
    """The measured lever: `{<200_000 chars>: 1}` stored 200,044 bytes, 12.5x the cap
    and unbounded in principle — a disk-exhaustion lever a hostile body can pull on a
    box whose storage the owner cannot reclaim from a terminal."""
    capped = cap_tool_args({"k" * 200_000: 1})
    assert encoded(capped) <= MAX_ARGS_CHARS
    assert capped["_truncated"] is True
    assert all(len(k) <= MAX_ARG_CHARS for k in capped)


def test_the_degraded_key_list_is_itself_bounded() -> None:
    """The escape hatch rebuilt `_keys` from the ORIGINAL keys, so degrading a blob of
    long keys produced something larger than the blob it was escaping. Every branch out
    of the capper is bounded, including the last one."""
    capped = cap_tool_args({f"{i}{'z' * 5_000}": "v" for i in range(40)})
    assert encoded(capped) <= MAX_ARGS_CHARS
    assert capped == {"_key_count": 40, "_truncated": True}


def test_a_non_json_value_is_coerced_not_carried() -> None:
    """`entity_id: UUID` is the most likely W3 shape and the JSONB bind processor uses a
    bare `json.dumps`: anything the capper passes through untouched aborts the whole
    transaction, graph write included."""
    eid = uuid.uuid4()
    capped = cap_tool_args({"entity_id": eid, "at": datetime(2026, 9, 9, tzinfo=UTC)})
    assert capped["entity_id"] == str(eid)
    json.dumps(capped)  # the real bind processor's call, and it must not raise


def test_non_finite_floats_do_not_reach_jsonb() -> None:
    """`json.dumps` emits bare `NaN`/`Infinity`, which `jsonb` rejects outright."""
    capped = cap_tool_args({"weight": math.nan, "height": math.inf})
    assert capped["weight"] == "nan" and capped["height"] == "inf"


def test_a_cycle_does_not_recurse_forever() -> None:
    """A handler builds these dicts, so a cycle is reachable — and a RecursionError
    would cost exactly the audit row the caps exist to keep."""
    cyclic: dict[str, Any] = {"name": "loop"}
    cyclic["self"] = cyclic
    capped = cap_tool_args(cyclic)
    assert capped["_truncated"] is True
    json.dumps(capped)


def test_a_model_supplied_truncated_flag_does_not_survive() -> None:
    """Only the capper writes that key; a call whose args happened to carry it must not
    make the ledger claim a truncation that never happened."""
    assert cap_tool_args({"_truncated": True, "quote": "short"}) == {"quote": "short"}


def test_unknown_domain_codes_are_refused() -> None:
    """A docstring contract is not a contract. The handler fills `domains` from what the
    write path reported, so an unrecognised code is a bug here, not untrusted input."""
    assert validate_domains(["general", "health"]) == ["general", "health"]
    assert validate_domains([]) == []
    with pytest.raises(ValueError, match="unknown domain code"):
        validate_domains(["health", "hea1th"])


def test_the_ledgers_domain_set_is_the_shipped_one() -> None:
    """`app.domains` also seeds the corpus-only `external` (0136) and `jmolt` (0172),
    which the owner-knowledge allow-lists exclude. Pinned to extraction's copy so the
    two cannot drift apart unnoticed."""
    assert KNOWLEDGE_DOMAINS == DOMAINS
