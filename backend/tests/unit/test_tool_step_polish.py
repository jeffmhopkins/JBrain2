"""Roster gate for the PWA's "Worked" step polish (frontend/src/agent/toolSummary.ts).

Every tool the agent can run renders in the owner's Worked strip, and a tool that
skips its polish shows up as a raw snake_case row with no visible target (the
jmolt_observe regression this gate came from). So each `.tool` sidecar must
register, in toolSummary.ts:

- a friendly STEP_LABELS entry ("Observed jmolt", never `jmolt_observe`), and
- an inline-arg policy: INLINE_ARGS keys naming the call's human-readable target
  (query/url/action/…, shown to the right of the label), or an explicit NO_INLINE
  opt-out when every argument is an opaque id / boolean / structured blob.

Parsed here with regexes over the literal maps — the frontend file is the single
source of truth and this test only keeps it honest against the sidecar roster
(docs/reference/ASSISTANT.md "Tools as `.tool` sidecars")."""

import re
from pathlib import Path

from jbrain.agent.toolfile import load_tool

_REPO = Path(__file__).resolve().parents[3]
_TOOLS_DIR = _REPO / "backend" / "src" / "jbrain" / "agent" / "tools"
_SUMMARY_TS = _REPO / "frontend" / "src" / "agent" / "toolSummary.ts"

# Step names the backend emits that are not `.tool` sidecars (e.g. the synthetic
# "queued" job step). They may appear in the frontend maps without a sidecar.
_SYNTHETIC = {"queued"}

# Tools polished in `toolSummary.ts` ahead of the `.tool` sidecar that defines them —
# the shape W3 needed while the note-conversation write surface landed across sibling
# branches. EMPTY now: every W3 tool has its sidecar, so the staleness gate covers the
# whole surface again. The assertion below is self-clearing in both directions — a name
# added here whose sidecar exists fails immediately.
_FORWARD: set[str] = set()


def test_forward_entries_are_deleted_once_their_sidecar_lands() -> None:
    landed = sorted(_FORWARD & set(_roster()))
    assert not landed, (
        "these tools now HAVE a .tool sidecar, so they are no longer forward-declared — "
        f"remove them from _FORWARD so the staleness gate covers them again: {landed}"
    )


def _summary_src() -> str:
    return _SUMMARY_TS.read_text(encoding="utf-8")


def _block(src: str, start: str, end: str) -> str:
    begin = src.index(start) + len(start)
    return src[begin : src.index(end, begin)]


def _step_labels(src: str) -> set[str]:
    body = _block(src, "const STEP_LABELS: Record<string, string> = {", "\n};")
    return set(re.findall(r"^\s*(\w+):", body, re.MULTILINE))


def _inline_args(src: str) -> dict[str, list[str]]:
    body = _block(src, "const INLINE_ARGS: Record<string, readonly string[]> = {", "\n};")
    return {
        name: re.findall(r'"([^"]+)"', keys)
        for name, keys in re.findall(r"^\s*(\w+): \[([^\]]*)\]", body, re.MULTILINE)
    }


def _no_inline(src: str) -> set[str]:
    body = _block(src, "const NO_INLINE: ReadonlySet<string> = new Set([", "\n]);")
    return set(re.findall(r'"([^"]+)"', body))


def _roster() -> dict[str, dict[str, object]]:
    """Every sidecar's name -> its params `properties` map."""
    tools = {}
    for path in sorted(_TOOLS_DIR.glob("*.tool")):
        tool = load_tool(path)
        props = tool.spec.params.get("properties") or {}
        tools[tool.spec.name] = props
    assert tools, f"no .tool sidecars found under {_TOOLS_DIR}"
    return tools


def test_every_tool_has_a_friendly_step_label() -> None:
    labels = _step_labels(_summary_src())
    # `lookup_*` tools are labelled by the stepLabel prefix rule, not the map.
    missing = [n for n in _roster() if n not in labels and not n.startswith("lookup_")]
    assert not missing, (
        "these tools would render as raw snake_case rows in the Worked strip — add a "
        f"STEP_LABELS entry in {_SUMMARY_TS.relative_to(_REPO)}: {missing}"
    )


def test_every_tool_declares_an_inline_arg_policy() -> None:
    src = _summary_src()
    with_keys = set(_inline_args(src))
    opted_out = _no_inline(src)
    roster = set(_roster())
    missing = sorted(roster - with_keys - opted_out)
    assert not missing, (
        "these tools have no inline-arg policy — list the human-readable target key(s) "
        "in INLINE_ARGS, or add the tool to NO_INLINE if every argument is opaque "
        f"({_SUMMARY_TS.relative_to(_REPO)}): {missing}"
    )
    both = sorted(with_keys & opted_out)
    assert not both, f"these tools are in both INLINE_ARGS and NO_INLINE — pick one: {both}"


def test_inline_arg_keys_exist_in_each_tool_schema() -> None:
    roster = _roster()
    bad = []
    for name, keys in _inline_args(_summary_src()).items():
        if name in _SYNTHETIC or name not in roster:
            continue
        unknown = [k for k in keys if k not in roster[name]]
        if unknown:
            bad.append((name, unknown))
    assert not bad, f"INLINE_ARGS names keys the tool schema doesn't have: {bad}"


# --- the result-brief policy (SHOW_THE_WORKING_PLAN.md W1) -------------------
#
# A Worked row carries what was ASKED; this is the gate for the other half — what CAME
# BACK. Every tool is in exactly one of the two sets below, so a NEW tool cannot ship
# without deciding which, and the second set is the remaining work written down rather
# than assumed away.

# Tools whose handler authors the row's answer itself — `ToolOutput(result_brief=...)`.
# Prefer this: only the handler knows which part of its own result was the answer.
_AUTHORS_BRIEF = {
    "add_list_item",
    "add_source_exclusion",
    "analyze_image",
    "analyze_stream",
    "analyze_video",
    "aprs_recent",
    "archivist_memory_read",
    "archivist_memory_write",
    "ask_owner",
    "calculate",
    "canvas",
    "chart_measurements",
    "check_list_item",
    "compare_images",
    "create_list",
    "crop_regions",
    "current_location",
    "current_time",
    "decompose_research",
    "deep_produce",
    "deep_research",
    "deepest_research",
    "device_status",
    "external_video",
    "fetch_image",
    "file_correction",
    "find_when_at",
    "geocode_reverse",
    "gmail_archive",
    "gmail_bulk_label",
    "gmail_count",
    "gmail_create_label",
    "gmail_label",
    "gmail_list_labels",
    "gmail_read",
    "gmail_search",
    "gmail_sender_breakdown",
    "grab_frame",
    "home_status",
    "hurricane",
    "jmolt_observe",
    "journal",
    "location_history",
    "location_query",
    "lookup_condition",
    "lookup_medication",
    "make_intake_link",
    "manage_appointment",
    "memory_edit",
    "memory_read",
    "merge_entities",
    "moltbook",
    "moltbook_comment",
    "moltbook_post",
    "moltbook_profile_update",
    "moltbook_social",
    "moltbook_vote",
    "name_session",
    "nearby_now",
    "ocr",
    "prefs_read",
    "prefs_write",
    "propose_correction",
    "propose_merge",
    "query_server_metrics",
    "read_appointment",
    "read_appointments",
    "read_artifact",
    "read_encounters",
    "read_labs",
    "read_list",
    "read_lists",
    "read_plan",
    "recall",
    "remember",
    "remove_external_video",
    "remove_list_item",
    "remove_research_report",
    "render_bars",
    "render_chart",
    "render_html",
    "request_rebuild",
    "research_report",
    "run_python",
    "save_place",
    "scratch_list",
    "scratch_manage",
    "scratch_read",
    "scratch_write",
    "sdr_aprs_logging",
    "sdr_listen",
    "sdr_read",
    "sdr_signal",
    "sdr_stop",
    "show_canvas",
    "show_external_video",
    "show_research_report",
    "spawn_subagent",
    "time_at_place",
    "time_left",
    "transcribe",
    "weather",
    "weather_history",
    "web_fetch",
    "where_is",
    "where_was_i",
    "write_plan",
    "write_plan_result",
}

# Tools that need no authored brief, because their result is already STRUCTURED and
# `stepLedger.ts` phrases the row from it: `sources` ("3 notes"), `facts` (the D3 write
# phrase), `entities` (a resolve's cast), `web_sources` ("2 results").
#
# This is a different statement from "not done yet" — every one of these rows is filled.
# It is checked below rather than asserted, because the whole point of the split is that a
# tool cannot end up in here by being forgotten.
_STRUCTURAL_RESULT = {
    "assert_fact",
    "check_channel",
    "close_reading",
    "correct_fact",
    "find_entity",
    "grokipedia",
    "neighborhood",
    "news_feed",
    "news_search",
    "portal_search",
    "public_records",
    "read_entity",
    "read_note",
    "read_wiki",
    "relate",
    "resolve_entity",
    "science_search",
    "search",
    "web_search",
}


def test_every_tool_declares_a_result_brief_policy() -> None:
    roster = set(_roster())
    declared = _AUTHORS_BRIEF | _STRUCTURAL_RESULT
    missing = sorted(roster - declared)
    assert not missing, (
        "these tools declare no result-brief policy — make the handler pass "
        "`result_brief=` and add the name to _AUTHORS_BRIEF, or add it to "
        f"_STRUCTURAL_RESULT if its row is already filled from structured fields: {missing}"
    )
    stale = sorted(declared - roster)
    assert not stale, f"these names are no longer tools — drop them from the sets: {stale}"
    both = sorted(_AUTHORS_BRIEF & _STRUCTURAL_RESULT)
    assert not both, f"a tool is in both result-brief sets — pick one: {both}"


def test_the_tools_that_author_their_answer_still_do() -> None:
    """The set is a claim about the handlers, so it is checked against them.

    Without this the set rots in the safe direction-looking way: a refactor drops the
    `result_brief=` argument, every test still passes, and the row the plan exists to fill
    goes quietly blank."""
    modules = [
        path.read_text(encoding="utf-8")
        for path in sorted((_REPO / "backend" / "src" / "jbrain").rglob("*.py"))
    ]
    # Per tool, not repo-wide: one remaining `result_brief=` anywhere in the backend would
    # otherwise vouch for every name in the set. A handler's module registers the tool by
    # name, so the two appearing together is the check available without importing the
    # whole registry (which needs a database).
    silent = sorted(
        name
        for name in _AUTHORS_BRIEF
        if not any(f'"{name}"' in src and "result_brief=" in src for src in modules)
    )
    assert not silent, (
        "_AUTHORS_BRIEF claims these tools fill the Worked row's answer, but no handler "
        f"passes `result_brief=` any more: {silent}"
    )


def test_the_structural_tools_really_do_surface_structure() -> None:
    """The other half of the split, checked the same way.

    A name lands in `_STRUCTURAL_RESULT` to say "its row is already filled" — which is a
    claim about the handler, not a note to self. Unchecked, it becomes the place a tool goes
    when nobody could think what its answer was, and the blank row this gate exists to
    prevent comes back wearing a label that says it is fine.

    The handler's own module is searched for one of the fields `stepLedger` phrases from.
    `agents.py` and `spawn.py` are excluded: they name tools in rosters and allowlists
    without implementing any, so either would vouch for everything."""
    roster_files = {"agents.py", "spawn.py", "toolregistry.py"}
    modules = [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted((_REPO / "backend" / "src" / "jbrain").rglob("*.py"))
        if path.name not in roster_files
    ]
    fields = ("sources=", "web_sources=", "facts=", "entities=")
    hollow = sorted(
        name
        for name in _STRUCTURAL_RESULT
        if not any(f'"{name}"' in src and any(f in src for f in fields) for _, src in modules)
    )
    assert not hollow, (
        "_STRUCTURAL_RESULT says these tools' rows are filled from structured fields, but "
        "their handlers surface none — so the Worked row renders blank. Give each one a "
        f"`result_brief=` and move it to _AUTHORS_BRIEF: {hollow}"
    )


def test_frontend_maps_carry_no_stale_tools() -> None:
    src = _summary_src()
    known = set(_roster()) | _SYNTHETIC | _FORWARD
    stale = sorted((set(_step_labels(src)) | set(_inline_args(src)) | _no_inline(src)) - known)
    assert not stale, (
        "these entries in toolSummary.ts no longer match any .tool sidecar (renamed or "
        f"removed tool?): {stale}"
    )
