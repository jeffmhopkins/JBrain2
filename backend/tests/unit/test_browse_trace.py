"""The `browse_trace` step view: what the owner sees of a browse run (binding mock
docs/mocks/browse-trace/a-timeline.html). Driven through the real loops against the fake
playwright-mcp server, plus hand-built runs for the caps and the sanitising — everything a
page shaped is untrusted, and the stored view must stay bounded however busy the page."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from jbrain.agent import browse_trace
from jbrain.agent.browse import BrowseAgent, BrowseRun, BrowseStep, BrowseTurn, render_for_caller
from jbrain.agent.browse_trace import build_view, command_text
from jbrain.agent.browsetools import build_browse_handlers
from jbrain.agent.contracts import ToolCallEvent, ToolResultEvent, ToolViewEvent
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.transcript_accumulator import TranscriptAccumulator
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, ToolCall
from jbrain.web.mcp_client import McpHttpClient
from tests.unit.browse_fakes import HOME, TITUSVILLE, FakeBrowser

GOAL = "On cinema.example pick the Titusville theater and list today's Dune showtimes."
DUNE = "Dune: Part Three: 7:15 PM, 9:40 PM"
THEATER, TITUSVILLE_LINK, T_DATE = 1, 9, 2


def _act(*commands: dict[str, Any], n: int = 1) -> LlmTurn:
    return LlmTurn(
        text="",
        tool_calls=[ToolCall(id=f"c{n}", name="act", arguments={"commands": list(commands)})],
        stop_reason="tool_use",
        usage=LlmUsage(10, 2, cached_tokens=4),
    )


def _call(name: str, n: int = 1, reasoning: str = "", **args: Any) -> LlmTurn:
    return LlmTurn(
        text="",
        tool_calls=[ToolCall(id=f"c{n}", name=name, arguments=args)],
        stop_reason="tool_use",
        usage=LlmUsage(10, 2),
        reasoning=reasoning,
    )


def _cmd(do: str, index: int | None = None, value: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"do": do}
    if index is not None:
        out["index"] = index
    if value is not None:
        out["value"] = value
    return out


def _say(text: str) -> LlmTurn:
    return LlmTurn(text=text, tool_calls=(), stop_reason="end_turn", usage=LlmUsage(10, 2))


def _agent(
    turns: Sequence[LlmTurn], browser: FakeBrowser | None = None, loop: str = "fast"
) -> BrowseAgent:
    fake = FakeLlmClient(turns=list(turns))
    router = LlmRouter({"xai": fake}, {"browse.step": ("xai", "grok-4.3")})
    mcp = McpHttpClient("http://browser:8931/mcp", transport=(browser or FakeBrowser()).transport())
    return BrowseAgent(router, mcp, loop=loop, settle_seconds=0)


def _ctx(reason: str = "js_shell") -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        agent_tools=frozenset({"browse"}),
        browser_needed={"cinema.example": reason},
    )


class _Rerender(FakeBrowser):
    """The date picker re-renders with a new ref after a choice — or vanishes."""

    vanish: bool = False

    def _run(self, name: str, args: dict[str, Any]) -> str:
        text = super()._run(name, args)
        if name == "browser_snapshot" and self.day != "Today":
            if self.vanish:
                return text.replace('  - combobox "Show date" [ref=e13]:\n', "")
            return text.replace("[ref=e13]", "[ref=e99]")
        return text


# --- Through the handler -----------------------------------------------------------------


async def test_the_browse_tool_carries_the_trace_as_a_step_view_jerv_never_reads() -> None:
    turns = [
        _act(_cmd("click", THEATER)),
        _act(_cmd("click", TITUSVILLE_LINK), n=2),
        _act(_cmd("done", value=DUNE), n=3),
    ]
    agent = _agent(turns)
    out = await build_browse_handlers(agent)["browse"]({"goal": GOAL, "start_url": HOME}, _ctx())

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.view == "browse_trace" and out.view.surface == "inline"
    # What jerv reads is exactly the caller text, with nothing of the trace in it.
    assert "Clicked" not in out and "act(" not in out and out.startswith("[BROWSE RESULT")
    data = out.view.data
    assert data["site"] == "cinema.example" and data["loop"] == "fast" and data["pages"] == 3
    host, first, second, done = data["steps"]
    assert host["kind"] == "host" and host["summary"] == "Opened “cinema.example”"
    assert host["badges"][0] == {"kind": "warn", "text": "JS shell"}
    assert host["title"] == "Home — Cinema" and host["url"] == HOME
    assert first["summary"] == "Clicked “Your theater: Please select a location”"
    (command,) = first["commands"]
    assert command["element"] == '[1] button "Your theater: Please select a location"'
    assert command["status"] == "ok" and command["moved_to"] == "/locations"
    assert first["reasoning"] == ""
    assert first["call"] == 'act({"commands": [{"do": "click", "index": 1}]})'
    assert first["nums"]["prompt_tokens"] == 10 and first["nums"]["cached_tokens"] == 4
    page = first["page"]
    assert page["title"] == "Home — Cinema" and page["changes"] is False
    assert page["excerpt"] and page["lines_shown"] <= browse_trace.EXCERPT_LINES
    # The excerpt is the page: its address and title are shown beside it, not in it.
    assert not page["excerpt"].startswith(("URL:", "Title:", "Page:"))
    assert second["page"]["title"] == "Choose a theater — Cinema"
    assert done["summary"] == "Wrote the answer" and done["title"] == "Titusville — Cinema"
    check = data["check"]
    assert check["verified"] and check["answered"] and not check["extracted"]
    assert check["answer"] == DUNE
    assert check["rows"][0]["label"] == "done's answer" and check["rows"][0]["ok"]
    assert check["rows"][-1]["text"] == "not needed — done's answer passed"
    assert check["summary"] == "Checked the answer against the page"


async def test_a_batch_shows_what_was_re_found_refused_and_not_run() -> None:
    browser = _Rerender()
    browser.vanish = True
    turns = [
        _say("NOT FOUND"),  # the start page's extraction
        _act(
            _cmd("select", T_DATE, "Tomorrow"),
            _cmd("select", T_DATE, "Today"),
            _cmd("read", value="Dune"),
        ),
        _act(_cmd("done", value="NOT FOUND: x"), n=2),
    ]
    run = await _agent(turns, browser).run(GOAL, TITUSVILLE)
    data = build_view(run).data

    host = data["steps"][0]
    assert [c["text"] for c in host["commands"]] == [
        "Went to /titusville",
        "Tried reading the answer off the start page",
    ]
    assert host["commands"][1]["status"] == "refused"
    rung = data["steps"][1]
    picked, gone, skipped = rung["commands"]
    assert picked["text"] == "Picked “Tomorrow” in “Show date”" and picked["status"] == "ok"
    assert gone["status"] == "refused" and gone["element"] == '[2] combobox "Show date"'
    assert "is not on the current page" in gone["note"]
    assert skipped == {"status": "not_run", "text": 'read "Dune"', "element": ""}
    assert rung["status"] == "part"
    assert rung["badges"] == [
        {"kind": "ref", "text": "1 refused"},
        {"kind": "warn", "text": "1 not run"},
    ]
    assert data["check"]["summary"] == "The browser could not do this on that site"


async def test_a_re_rendered_target_shows_as_re_found_with_its_settle() -> None:
    turns = [
        _say("NOT FOUND"),
        _act(_cmd("select", T_DATE, "Tomorrow"), _cmd("select", T_DATE, "Today")),
        _act(_cmd("done", value=DUNE), n=2),
    ]
    run = await _agent(turns, _Rerender()).run(GOAL, TITUSVILLE)
    rung = build_view(run).data["steps"][1]
    first, again = rung["commands"]
    assert "refound" not in first and again["refound"] is True
    assert rung["badges"] == [{"kind": "re", "text": "1 re-found"}]
    assert rung["summary"] == "Picked “Tomorrow” in “Show date”, picked “Today” in “Show date”"


async def test_the_b1_loop_fills_the_trace_with_its_reasoning_and_extraction() -> None:
    turns = [
        _call("click", 1, reasoning="Open the picker to find Titusville.", ref="e1"),
        _call("click", 2, reasoning="Titusville is the match.", ref="e10"),
        _call("finish", 3, note="the Dune listing"),
        _say(DUNE),
    ]
    run = await _agent(turns, loop="b1").run(GOAL, HOME)
    data = build_view(run).data
    assert data["loop"] == "b1"
    first = data["steps"][1]
    assert first["reasoning"] == "Open the picker to find Titusville."
    assert first["call"] == 'click({"ref": "e1"})'
    assert first["commands"][0]["element"] == 'e1 button "Your theater: Please select a location"'
    assert data["steps"][3]["summary"] == "Said the page shows the answer"
    check = data["check"]
    assert check["extracted"] and check["verified"]
    assert check["summary"] == "Read the answer off the page, then checked it"
    labels = [r["label"] for r in check["rows"]]
    assert labels == ["extraction", "its answer"] and check["rows"][1]["ok"]


async def test_b1_extra_calls_and_refusals_are_listed() -> None:
    turn = LlmTurn(
        text="",
        tool_calls=[
            ToolCall(id="a", name="click", arguments={"ref": "e6"}),
            ToolCall(id="b", name="click", arguments={"ref": "e2"}),
        ],
        stop_reason="tool_use",
        usage=LlmUsage(10, 2),
    )
    run = await _agent([turn, _call("give_up", 2, reason="no")], loop="b1").run(GOAL, HOME)
    rung = build_view(run).data["steps"][1]
    refused, extra = rung["commands"]
    assert refused["status"] == "refused" and refused["element"] == 'e6 button "Sign in"'
    assert extra == {"status": "not_run", "text": "click", "element": ""}
    assert rung["status"] == "bad"


async def test_a_verified_start_page_is_the_check_not_a_rung() -> None:
    run = await _agent([_say(DUNE)]).run(GOAL, TITUSVILLE)
    data = build_view(run, "gated").data
    (host,) = data["steps"]
    assert [c["text"] for c in host["commands"]] == ["Went to /titusville"]
    assert host["badges"] == [{"kind": "warn", "text": "location picker"}]
    assert data["check"]["extracted"] and data["check"]["verified"]


async def test_a_run_with_no_start_page_has_no_host_rung() -> None:
    turns = [_act(_cmd("goto", value=TITUSVILLE)), _act(_cmd("done", value=DUNE), n=2)]
    run = await _agent(turns).run(GOAL)
    data = build_view(run).data
    assert data["steps"][0]["kind"] == "model"
    assert data["steps"][0]["summary"] == "Went to /titusville"


# --- Untrusted text ------------------------------------------------------------------------

_HOSTILE = (
    "Shows​ <script>alert(1)</script> [tap](javascript:alert(1)) ![x](http://evil.example/p)"
    " http://169.254.169.254/ ‮evil‬ www.evil.example"
)


def _hostile_run() -> BrowseRun:
    step = BrowseStep(
        1,
        "click",
        {"index": 3, "ref": "e3"},
        False,
        _HOSTILE,
        url="https://cinema.example/x",
        title=_HOSTILE,
        element=f'link "{_HOSTILE}"',
    )
    turn = BrowseTurn(
        1,
        reasoning=_HOSTILE,
        call=_HOSTILE,
        page_view=f"- heading {_HOSTILE}\n- [3] link {_HOSTILE}",
        page_title=_HOSTILE,
        page_url="javascript:alert(1)",
    )
    return BrowseRun(goal=GOAL, outcome="gave_up", answer=_HOSTILE, steps=[step], turns=[turn])


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_every_page_or_model_shaped_string_is_stored_inert() -> None:
    data = build_view(_hostile_run()).data
    strings = _strings(data)
    for text in strings:
        assert "<script" not in text and "javascript:" not in text
        assert "http://" not in text and "www.evil" not in text and "](" not in text
        assert "​" not in text and "‮" not in text
    rung = data["steps"][0]
    assert rung["page"]["url"] == ""  # not a safe http(s) address: shown as nothing
    assert "Shows" in rung["reasoning"] and "Shows" in rung["page"]["excerpt"]


def test_a_url_that_would_break_a_line_is_dropped() -> None:
    assert browse_trace._url("https://cinema.example/a b") == ""
    assert browse_trace._path("https://cinema.example/") == "cinema.example"
    assert browse_trace._path("https://cinema.example/a?b=1") == "/a?b=1"
    assert browse_trace._path("") == "" and browse_trace._site("nope") == ""


# --- Caps ----------------------------------------------------------------------------------


def _busy_turn(n: int) -> tuple[list[BrowseStep], BrowseTurn]:
    long = "word " * 2_000
    steps = [
        BrowseStep(
            n,
            "click",
            {"index": i, "ref": f"e{i}"},
            True,
            long,
            url=f"https://cinema.example/{n}",
            title=long,
            element=f'button "{long}"',
        )
        for i in range(5)
    ]
    view = "\n".join(f"- [{i}] link {long[:500]}" for i in range(500))
    turn = BrowseTurn(n, reasoning=long, call=long, page_view=view, page_title=long, page_lines=500)
    return steps, turn


def test_each_field_is_capped() -> None:
    steps, turn = _busy_turn(1)
    run = BrowseRun(goal=GOAL, answer="a" * 5_000, steps=steps, turns=[turn])
    rung = build_view(run).data["steps"][0]
    assert len(rung["reasoning"]) <= browse_trace.REASONING_CHARS
    assert len(rung["call"]) <= browse_trace.CALL_CHARS
    assert len(rung["title"]) <= browse_trace.TITLE_CHARS
    assert len(rung["summary"]) <= browse_trace.LINE_CHARS + len(" +3 more")
    for command in rung["commands"]:
        assert len(command["note"]) <= browse_trace.NOTE_CHARS
        assert len(command["element"]) <= browse_trace.ELEMENT_CHARS + len("[4] ")
    page = rung["page"]
    lines = page["excerpt"].splitlines()
    assert len(lines) == browse_trace.EXCERPT_LINES == page["lines_shown"]
    assert all(len(line) <= browse_trace.EXCERPT_LINE_CHARS for line in lines)
    assert page["lines_total"] == 500


def test_a_long_busy_run_is_trimmed_under_the_payload_cap_oldest_first() -> None:
    steps: list[BrowseStep] = []
    turns: list[BrowseTurn] = []
    for n in range(1, 31):
        s, t = _busy_turn(n)
        steps += s
        turns.append(t)
    data = build_view(BrowseRun(goal=GOAL, steps=steps, turns=turns)).data
    assert len(json.dumps(data, ensure_ascii=False)) <= browse_trace.MAX_VIEW_CHARS
    assert data["trimmed"] is True
    # The newest turn keeps the most.
    assert len(data["steps"]) == 30 and "page" not in data["steps"][0]


def test_a_pathological_run_drops_middle_rungs_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browse_trace, "MAX_VIEW_CHARS", 3_000)
    steps: list[BrowseStep] = []
    turns: list[BrowseTurn] = []
    for n in range(1, 11):
        s, t = _busy_turn(n)
        steps += s
        turns.append(t)
    data = build_view(BrowseRun(goal=GOAL, steps=steps, turns=turns)).data
    assert len(data["steps"]) == 2 and data["steps"][-1]["n"] == 10


def test_a_small_run_is_not_marked_trimmed() -> None:
    data = build_view(BrowseRun(goal=GOAL)).data
    assert "trimmed" not in data and data["steps"] == []
    assert data["check"]["rows"] == [
        {"label": "extraction", "text": "did not run", "ok": None},
    ]


# --- The host's own words --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "args", "element", "text"),
    [
        ("click", {"index": 4}, 'button "Go"', "Clicked “Go”"),
        ("click", {"index": 4}, "", "Clicked [4]"),
        ("type", {"index": 2, "value": "dune"}, 'searchbox "Search"', "Typed “dune” into “Search”"),
        ("type_text", {"ref": "e2", "text": "dune"}, "searchbox", "Typed “dune” into “searchbox”"),
        ("select", {"index": 2, "value": "Today"}, 'combobox "Day"', "Picked “Today” in “Day”"),
        ("select_option", {"values": ["A", "B"]}, 'listbox "L"', "Picked “A B” in “L”"),
        ("enter", {}, "", "Pressed Enter"),
        ("press_key", {"key": "Escape"}, "", "Pressed Escape"),
        ("navigate", {"url": "https://x.example/a"}, "", "Went to /a"),
        ("goto", {"value": "javascript:x"}, "", "Went to a new address"),
        ("back", {}, "", "Went back"),
        ("go_back", {}, "", "Went back"),
        ("read", {}, "", "Read the page"),
        ("read", {"value": "Dune"}, "", "Read the page from “Dune”"),
        ("done", {}, "", "Wrote the answer"),
        ("finish", {}, "", "Said the page shows the answer"),
        ("give_up", {}, "", "Gave up"),
        ("wait_for", {}, "", "Waited for the page"),
        ("snapshot", {}, "", "Looked at the page again"),
        ("tabs", {}, "", "Switched tabs"),
        ("(none)", {}, "", "Chose no action"),
        ("act", {}, "", "Sent commands the host could not run"),
        ("evaluate", {}, "", "Asked for evaluate"),
    ],
)
def test_each_action_reads_in_plain_words(
    action: str, args: dict[str, Any], element: str, text: str
) -> None:
    step = BrowseStep(1, action, args, True, "", element=element)
    assert command_text(step) == text


def test_a_failed_or_looping_command_reads_as_failed() -> None:
    failed = BrowseStep(1, "click", {}, False, "the browser reported an error: x", settle_ms=300)
    loop = BrowseStep(1, "act", {}, False, "loop")
    run = BrowseRun(goal=GOAL, steps=[failed, loop], outcome="loop")
    rung = build_view(run).data["steps"][0]
    assert [c["status"] for c in rung["commands"]] == ["failed", "failed"]
    assert rung["commands"][0]["settle_ms"] == 300 and rung["nums"]["settle_ms"] == 300
    assert rung["badges"] == [{"kind": "ref", "text": "2 failed"}] and rung["status"] == "bad"
    assert rung["summary"] == "Clicked, sent commands the host could not run"


def test_a_turn_with_only_skipped_commands_ran_nothing() -> None:
    run = BrowseRun(goal=GOAL, turns=[BrowseTurn(1, not_run=["click [3]"])])
    rung = build_view(run).data["steps"][0]
    assert rung["summary"] == "Ran nothing" and rung["status"] == "bad"
    assert "page" not in rung


def test_the_caller_text_is_unchanged_by_the_trace() -> None:
    run = _hostile_run()
    before = render_for_caller(run)
    build_view(run)
    assert render_for_caller(run) == before


async def test_the_trace_persists_on_its_step_so_a_reopened_chat_shows_it() -> None:
    """The view rides the persisted step (as `code_run`'s does), and the step's text — what
    the model saw — is the caller text alone."""
    turns = [_act(_cmd("done", value=DUNE))]
    out = await build_browse_handlers(_agent(turns))["browse"](
        {"goal": GOAL, "start_url": TITUSVILLE}, _ctx()
    )
    assert isinstance(out, ToolOutput) and out.view is not None
    acc = TranscriptAccumulator()
    acc.feed(ToolCallEvent(id="b1", name="browse", arguments={"goal": GOAL}))
    acc.feed(ToolResultEvent(tool_call_id="b1", ok=True, summary=str(out)))
    acc.feed(ToolViewEvent(tool_call_id="b1", view=out.view))
    (step,) = acc.tool_steps()
    assert step["view"]["view"] == "browse_trace"
    assert step["view"]["data"] == json.loads(json.dumps(out.view.data))
    assert step["summary"] == str(out) and "browse_trace" not in step["summary"]
