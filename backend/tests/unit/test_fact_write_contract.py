"""The D3 write contract, pinned on the BACKEND side of `testdata/fact_write_contract.json`.

That fixture is the one artefact the frontend's `factWriteContract.test.ts` reads, and its
`event` blobs are real `ToolResultEvent.model_dump(mode="json")` output — not hand-built
objects shaped the way somebody assumed the wire looked. That distinction is the whole
point of the file. The rung shipped reading six field names (`status`, `predicate`,
`qualifier`, `value`, `replaced`, `from_attachment`) that existed on NEITHER side of the
wire, plus a `truncated` that existed nowhere at all, and every test on both sides passed,
because every test on both sides built its own input. A `held` fact — one `decide()`
refused to make live — therefore rendered to the owner as written.

So the two halves are asserted against the same bytes: this file says the backend still
EMITS them, `factWriteContract.test.ts` says the shipped helpers still READ them. Drift in
either direction fails a gate rather than reaching the owner's screen.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jbrain.agent.contracts import FactWriteRef, ToolResultEvent, write_status
from jbrain.analysis.pipeline import (
    ALREADY,
    CLOSED,
    HELD,
    HISTORICAL,
    PROMOTED,
    REPLACED,
    WRITTEN,
)

_FIXTURE = json.loads(
    (Path(__file__).parents[3] / "testdata" / "fact_write_contract.json").read_text()
)


def _ref(fid: str, label: str, domain: str, outcome: str, **kw: object) -> FactWriteRef:
    """A ref built the way `graphwritetools._assert_one` builds one: the outcome comes
    from the write path and `status` is derived from it, never passed independently."""
    return FactWriteRef(
        fact_id=fid,
        label=label,
        domain=domain,  # type: ignore[arg-type]
        outcome=outcome,
        status=write_status(outcome),
        **kw,  # type: ignore[arg-type]
    )


def _event(tool_call_id: str, **kw: object) -> dict:
    return ToolResultEvent(
        tool_call_id=tool_call_id,
        ok=True,
        summary="",
        **kw,  # type: ignore[arg-type]
    ).model_dump(mode="json")


# Keyed by the fixture's case name; each builds the event that case claims the backend
# emits. Written out here rather than read from the fixture so the fixture is CHECKED,
# not merely echoed.
_BUILDERS = {
    "a held fact is never reported as written": lambda: _event(
        "c1",
        facts=[
            _ref(
                "11111111-1111-1111-1111-111111111111",
                "Jeff lives in the Marina District",
                "general",
                REPLACED,
                predicate="lives_in",
                value="Marina District",
                replaced="Jeff lives in Noe Valley",
            ),
            _ref(
                "22222222-2222-2222-2222-222222222222",
                "Jeff's blood type is O-negative",
                "health",
                HELD,
                predicate="blood_type",
                value="O-negative",
            ),
        ],
    ),
    "an attachment-sourced write says so (D12)": lambda: _event(
        "c2",
        facts=[
            _ref(
                "33333333-3333-3333-3333-333333333333",
                "Jeff's A1C is 5.4",
                "health",
                WRITTEN,
                predicate="lab_result",
                qualifier="a1c",
                value="5.4",
                from_attachment=True,
            )
        ],
    ),
    "a clamped batch says truncated": lambda: _event(
        "c3",
        truncated=True,
        facts=[
            _ref(
                "44444444-4444-4444-4444-444444444444",
                "Jeff runs on Tuesdays",
                "general",
                WRITTEN,
                predicate="routine",
                value="Tuesdays",
            )
        ],
    ),
    "every outcome word the write path can report": lambda: _event(
        "c4",
        facts=[
            _ref(f"5{i}555555-5555-5555-5555-555555555555", f"outcome {o}", "general", o)
            for i, o in enumerate([WRITTEN, ALREADY, CLOSED, REPLACED, HELD, HISTORICAL, PROMOTED])
        ],
    ),
    "a write tool that wrote nothing": lambda: _event("c5"),
}


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=[c["name"] for c in _FIXTURE["cases"]])
def test_the_fixture_is_what_the_backend_actually_emits(case: dict) -> None:
    assert _BUILDERS[case["name"]]() == case["event"]


def test_every_write_outcome_has_a_state_and_held_is_never_one_of_the_live_ones() -> None:
    """`decide()`'s whole vocabulary maps, and only `held` reads as held.

    The mapping is the safety property: a fact the write path parked is the one thing
    `ask_owner.tool` and the persona prompt both tell the model to raise, and a rung that
    calls it written contradicts them silently."""
    assert write_status(HELD) == "held"
    assert write_status(REPLACED) == "replaced"
    for outcome in (WRITTEN, ALREADY, CLOSED, HISTORICAL, PROMOTED):
        assert write_status(outcome) == "written", outcome


def test_status_cannot_be_left_off_a_write_ref() -> None:
    """There is no default for `status`, and there must not be: "written" is a claim that
    a fact is live, and an emitter that forgot the field would make every held write say
    so with nothing failing. A refused construction is that bug at its cheapest."""
    with pytest.raises(ValidationError):
        FactWriteRef(fact_id="f", label="l", domain="general", outcome=HELD)  # type: ignore[call-arg]


def test_an_outcome_word_nobody_mapped_fails_towards_held() -> None:
    """A word added to the write path and not to the table must not become "written" by
    default. Understating a live fact is visible and recoverable; overstating a held one
    tells Jeff his graph says something it does not."""
    assert write_status("some_future_outcome") == "held"
    assert write_status("") == "held"
