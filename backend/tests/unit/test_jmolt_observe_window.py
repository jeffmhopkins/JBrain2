"""`jmolt_observe`'s reading window (docs/plans/JMOLT_PLAN.md, W4, M16).

The regression these lock down is a real one: on 2026-08-26 a single
`jmolt_observe(action=transcript)` returned 1,226,144 characters — jmolt's night rendered
whole — into a 131k-token window, and the turn died on a context overflow before the model
saw a byte of it. So the tool now returns a WINDOW: `find`/`regex` to jump, `offset` to page,
and a hard ceiling no argument can raise.

These cover the pure renderer/presenter; the RLS scoping and the egress guard are exercised
by the integration tests over a real Postgres.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jbrain.agent.jmoltobservetools import (
    _FENCE,
    _WINDOW,
    _match_offsets,
    _offset,
    _present,
    _render_transcript,
)


@dataclass
class _Row:
    """One `app.agent_turns` row, as the transcript SELECT hands it over."""

    seq: int
    role: str
    content: str
    reasoning: str
    tools: Any
    created_at: datetime = datetime(2026, 8, 26, 7, 0, tzinfo=UTC)


def _show(body: str, *, find: str = "", use_regex: bool = False, offset: int = 0) -> str:
    return _present("jmolt's night", body, find=find, use_regex=use_regex, offset=offset)


# --- the ceiling -------------------------------------------------------------------------


def test_a_huge_record_is_windowed_not_returned_whole() -> None:
    out = _show("x" * 1_226_144)  # the exact size of the read that overflowed
    assert len(out) < _WINDOW + 2_000  # the window plus its fence/header/footer, nothing more
    assert "1226144" in out  # the true size is still reported, so nothing looks complete


def test_the_footer_names_the_exact_next_call() -> None:
    out = _show("y" * (_WINDOW * 2))
    assert f"offset={_WINDOW}" in out
    assert str(_WINDOW * 2) in out  # how much there is in total
    # Paging with that offset returns the NEXT window, not the same one again.
    nxt = _show("y" * (_WINDOW * 2), offset=_WINDOW)
    assert f"continued from offset {_WINDOW}" in nxt
    assert "remain below" not in nxt  # that second window is the end of it


def test_a_short_record_is_returned_whole_with_no_paging_noise() -> None:
    out = _show("a short night")
    assert "a short night" in out
    assert "Truncated" not in out and "offset=" not in out


def test_paging_past_the_end_says_so_instead_of_returning_nothing() -> None:
    out = _show("short", offset=9_000)
    assert "already read past" in out
    assert "5 characters" in out


# --- find / regex ------------------------------------------------------------------------


def test_find_positions_the_window_at_the_match_deep_in_the_record() -> None:
    body = "x" * 900_000 + "Dave followed Luna24" + "x" * 300_000
    out = _show(body, find="Luna24")
    assert "Dave followed Luna24" in out  # the needle, from one call, not 30 pages of paging
    assert "found 1 match(es)" in out
    assert "first at offset 900014" in out


def test_find_is_case_insensitive_and_literal_by_default() -> None:
    out = _show("... luna24. ...", find="Luna24.")
    assert "found 1 match(es)" in out
    # A literal search means regex metacharacters are just characters.
    assert "No match" in _show("a-b", find="a.b")


def test_find_reports_the_other_match_offsets_so_the_model_can_jump_on() -> None:
    body = "Luna24" + "x" * 50_000 + "Luna24" + "x" * 50_000 + "Luna24"
    out = _show(body, find="Luna24")
    assert "found 3 match(es)" in out
    assert "other matches at offsets: 50006, 100012" in out
    # And jumping to one of those reported offsets lands on it.
    assert "window opens at 100012" in _show(body, find="Luna24", offset=100_012)


def test_find_opens_the_window_before_the_hit_so_the_lead_up_is_visible() -> None:
    # The reason jmolt did a thing is what precedes the mention, so landing exactly on the
    # term would put the explanation just off the top of the window.
    body = "x" * 500_000 + "Luna24 reminds me of my first molt. " + "Dave followed Luna24"
    out = _show(body, find="Dave followed")
    assert "Luna24 reminds me of my first molt." in out


def test_a_miss_says_so_rather_than_dumping_an_irrelevant_window() -> None:
    out = _show("z" * 500_000, find="Luna24")
    assert "No match for 'Luna24'" in out
    assert "z" * 100 not in out  # a miss must not become the unbounded read again


def test_regex_opts_into_a_pattern() -> None:
    body = "x" * 1_000 + "Dave followed  Luna24" + "x" * 1_000
    assert "No match" in _show(body, find="follow(ed)? +Luna24")  # literal by default
    out = _show(body, find="follow(ed)? +Luna24", use_regex=True)
    assert "found 1 match(es)" in out
    assert "regex 'follow(ed)? +Luna24'" in out


def test_zero_width_regex_matches_cannot_flood_the_map() -> None:
    offsets, total = _match_offsets("abc", "x*", True)
    assert (offsets, total) == ((), 0)


# --- the fence ---------------------------------------------------------------------------


def test_every_window_carries_the_data_fence() -> None:
    body = "n" * (_WINDOW * 3)
    for out in (_show(body), _show(body, offset=_WINDOW), _show(body, find="n")):
        assert out.startswith(_FENCE)
    # Including the paths that return no content at all.
    assert _show("small", find="absent").startswith(_FENCE)
    assert _show("small", offset=9_999).startswith(_FENCE)


# --- the transcript renderer -------------------------------------------------------------


def test_transcript_renders_as_text_not_escaped_json() -> None:
    rows = [
        _Row(5419, "user", "why did you follow Luna24?", "", []),
        _Row(
            5420,
            "assistant",
            "Because she posts about molting.",
            "Luna24 keeps posting good threads.",
            [{"id": "t1", "ok": True, "name": "moltbook", "summary": "feed:\nLuna24 posted…"}],
        ),
    ]
    out = _render_transcript(rows)
    assert "── turn 5420 · assistant" in out
    assert "[thinking] Luna24 keeps posting good threads." in out
    assert "[said] Because she posts about molting." in out
    assert "[tool] moltbook (ok)" in out
    # The tool summary's real newlines survive, so `find` lands on readable text rather than
    # inside a JSON string literal — the whole point of not dumping the rows.
    assert "feed:\nLuna24 posted…" in out
    assert "\\n" not in out


def test_transcript_render_is_far_smaller_than_the_json_dump_it_replaces() -> None:
    import json

    tools = [{"id": "t1", "ok": True, "name": "moltbook", "summary": 'a "quoted"\nline\n' * 500}]
    rows = [_Row(1, "assistant", "hi", "thinking", tools)]
    dumped = json.dumps(
        [
            {"role": r.role, "content": r.content, "reasoning": r.reasoning, "tools": r.tools}
            for r in rows
        ],
        indent=2,
        default=str,
    )
    assert len(_render_transcript(rows)) < len(dumped) * 0.8


def test_transcript_marks_a_failed_tool_call_and_tolerates_odd_rows() -> None:
    rows = [_Row(1, "assistant", "", "", [{"name": "moltbook", "ok": False}, "junk", None])]
    out = _render_transcript(rows)
    assert "[tool] moltbook (failed)" in out  # a failure must not read as a success
    assert "junk" not in out  # a non-dict entry is skipped, never rendered or raised on


def test_an_empty_night_says_so() -> None:
    assert "no turns" in _render_transcript([])


# --- offset coercion ---------------------------------------------------------------------


def test_offset_coercion_never_raises_on_model_junk() -> None:
    assert _offset(None) == 0
    assert _offset("30000") == 30_000
    assert _offset(-5) == 0  # a negative offset reads from the top, it doesn't index backwards
    assert _offset("not-a-number") == 0
    assert _offset({"a": 1}) == 0
