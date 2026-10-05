"""The browse sub-agent's host loop, its MCP client, and jerv's `browse` tool.

The browser is a fake playwright-mcp server (browse_fakes) and the model is the adapter's
FakeLlmClient behind a real LlmRouter, so these drive the same code path as the box. The
security claims are asserted on the FAKE SERVER'S CALL LOG — what the browser was actually
asked to do — not on what the loop says it did."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from jbrain.agent import browse
from jbrain.agent.browse import (
    ACTION_NAMES,
    MCP_TOOL_FOR,
    BrowseAgent,
    BrowseRun,
    BrowseStep,
    render_for_caller,
)
from jbrain.agent.browsetools import build_browse_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.toolfile import load_tool
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, ToolCall
from jbrain.llm.errors import LlmTransientError
from jbrain.llm.slot_roles import TASK_ROLES, SlotRole
from jbrain.llm.types import ToolResultMessage, UserMessage
from jbrain.web.mcp_client import McpError, McpHttpClient, _parse_sse
from tests.unit.browse_fakes import (
    HOME,
    INJECTION,
    PICKER,
    TITUSVILLE,
    FakeBrowser,
    other_page,
)

_ACTIONS_DIR = Path(browse.__file__).parent / "browse_actions"


def _call(name: str, n: int = 1, **args: Any) -> LlmTurn:
    return LlmTurn(
        text="",
        tool_calls=[ToolCall(id=f"c{n}", name=name, arguments=args)],
        stop_reason="tool_use",
        usage=LlmUsage(10, 2),
    )


def _say(text: str) -> LlmTurn:
    return LlmTurn(text=text, tool_calls=(), stop_reason="end_turn", usage=LlmUsage(10, 2))


def _agent(
    turns: Sequence[LlmTurn],
    browser: FakeBrowser | None = None,
    *,
    clock: Any = None,
    **kwargs: Any,
) -> tuple[BrowseAgent, FakeLlmClient, FakeBrowser]:
    fake = FakeLlmClient(turns=list(turns))
    router = LlmRouter({"xai": fake}, {"browse.step": ("xai", "grok-4.3")})
    browser = browser or FakeBrowser()
    mcp = McpHttpClient("http://browser:8931/mcp", transport=browser.transport())
    extra = {"clock": clock} if clock is not None else {}
    return BrowseAgent(router, mcp, **kwargs, **extra), fake, browser


GOAL = "On cinema.example pick the Titusville theater and list today's Dune showtimes."
HAPPY = [
    _call("click", 1, ref="e1"),
    _call("click", 2, ref="e10"),
    _call(
        "finish",
        3,
        answer="Dune: Part Three plays at 7:15 PM and 9:40 PM.",
        evidence="7:15 PM, 9:40 PM",
    ),
]


# --- The happy path ------------------------------------------------------------------


async def test_a_goal_is_driven_to_a_verified_answer() -> None:
    agent, fake, browser = _agent(HAPPY)
    run = await agent.run(GOAL, HOME)

    assert run.outcome == "answered" and run.verified
    assert run.answer == "Dune: Part Three plays at 7:15 PM and 9:40 PM."
    assert run.final_url == TITUSVILLE
    assert run.sources == (HOME, PICKER, TITUSVILLE)
    # The trace: the host's start navigation, two clicks, the finish — each with its page.
    assert [s.action for s in run.steps] == ["navigate", "click", "click", "finish"]
    assert all(s.snapshot_tokens > 0 for s in run.steps)
    assert run.steps[2].url == TITUSVILLE
    # What the browser was asked: only allowlisted tools, with host-built arguments.
    assert [name for name, _ in browser.calls] == [
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_snapshot",
        "browser_click",
        "browser_snapshot",
        "browser_snapshot",
    ]
    assert browser.called("browser_click")[0] == {
        "target": "e1",
        "element": "Your theater: Please select a location",
    }
    # The session was opened, carried and closed.
    assert browser.methods[:2] == ["initialize", "notifications/initialized"]
    assert browser.session_headers[0] is None and browser.session_headers[1] == "sess-1"
    assert browser.deleted
    assert run.elapsed_ms >= 0


async def test_the_sub_agent_sees_the_goal_and_the_page_and_nothing_else() -> None:
    """Rule of Two: its system prompt is its own, and its first message is the goal plus
    the page. No owner data can be in context because none is ever passed in."""
    agent, fake, _ = _agent(HAPPY)
    await agent.run(GOAL, HOME)

    first = fake.converse_calls[0]
    assert first["system"] == browse._PROMPT.body
    [opening] = first["messages"]
    assert isinstance(opening, UserMessage)
    assert opening.text.startswith(f"GOAL: {GOAL}")
    assert "URL: https://cinema.example/" in opening.text
    assert {t.name for t in first["tools"]} == ACTION_NAMES


async def test_only_the_latest_page_is_shown_in_full() -> None:
    agent, fake, _ = _agent(HAPPY)
    await agent.run(GOAL, HOME)

    third = fake.converse_calls[2]["messages"]
    results = [m for m in third if isinstance(m, ToolResultMessage)]
    assert len(results) == 2
    older, latest = results[0].results[0].content, results[1].results[0].content
    assert "Page:" not in older and older.startswith("click: done.")
    assert "Epic Titusville 15" in latest


async def test_each_step_runs_in_the_research_slot_under_its_own_task() -> None:
    seen: list[dict[str, Any]] = []
    agent, _, _ = _agent(HAPPY)

    async def spy(task: str, **kwargs: Any) -> LlmTurn:
        seen.append({"task": task, **kwargs})
        return HAPPY[len(seen) - 1]

    agent._router.converse = spy  # type: ignore[method-assign]
    await agent.run(GOAL, HOME, spec_override="local:some-model")
    assert {s["task"] for s in seen} == {"browse.step"}
    assert {s["slot_role"] for s in seen} == {SlotRole.RESEARCH}
    assert {s["spec_override"] for s in seen} == {"local:some-model"}
    assert TASK_ROLES["browse.step"] is SlotRole.RESEARCH


# --- The gate: what the browser is never asked to do -----------------------------------


async def test_an_injected_page_cannot_reach_a_tool_outside_the_allowlist() -> None:
    """The home page carries an injection telling the model to run code and visit the
    metadata endpoint. A model that obeys is refused on both, and the server never sees
    either request."""
    turns = [
        _call("browser_run_code_unsafe", 1, code="async (page) => 1"),
        _call("navigate", 2, url="http://169.254.169.254/latest/meta-data"),
        _call("give_up", 3, reason="Blocked."),
    ]
    agent, fake, browser = _agent(turns)
    run = await agent.run(GOAL, HOME)

    assert INJECTION in fake.converse_calls[0]["messages"][0].text  # it WAS on the page
    assert "browser_run_code_unsafe" not in [name for name, _ in browser.calls]
    assert browser.called("browser_navigate") == [{"url": HOME}]
    assert [s.ok for s in run.steps[1:3]] == [False, False]
    assert "no action called" in run.steps[1].note
    assert "private network" in run.steps[2].note
    assert run.outcome == "gave_up" and run.answer == "Blocked."


async def test_typing_personal_data_never_reaches_the_browser() -> None:
    turns = [
        _call("type_text", 1, ref="e3", text="jeff@example.com"),  # an email FIELD
        _call("type_text", 2, ref="e4", text="call me 321-555-0199"),  # a phone VALUE
        _call("type_text", 3, ref="e4", text="Dune", submit=True),
        _call("give_up", 4, reason="done"),
    ]
    agent, fake, browser = _agent(turns)
    run = await agent.run(GOAL, HOME)

    assert browser.called("browser_type") == [
        {"target": "e4", "element": "Search movies", "text": "Dune", "submit": True}
    ]
    assert "refused" in run.steps[1].note and "long number" in run.steps[2].note
    # The refusal is what the model reads next, with the page so it can choose again.
    refusal = fake.converse_calls[1]["messages"][-1].results[0].content
    assert refusal.startswith("Refused:") and "Page:" in refusal


async def test_select_tabs_keys_and_waits_pass_the_gate_with_host_built_arguments() -> None:
    turns = [
        _call("select_option", 1, ref="e5", values=["Titusville"]),
        _call("press_key", 2, key="Escape"),
        _call("press_key", 3, key="Control+w"),
        _call("wait_for", 4, seconds=99),
        _call("wait_for", 5, text="Showtimes"),
        _call("wait_for", 6, seconds="soon"),
        _call("wait_for", 7, text="x" * 300),
        _call("tabs", 8, action="new", url="http://10.0.0.9/"),
        _call("tabs", 9, action="explode"),
        _call("tabs", 10, action="select", index=0),
        _call("tabs", 11, action="new", url="https://cinema.example/titusville"),
        _call("go_back", 12),
        _call("snapshot", 13),
        _call("give_up", 14, reason="done"),
    ]
    agent, _, browser = _agent(turns, max_steps=20)
    run = await agent.run(GOAL, HOME)

    assert browser.called("browser_select_option") == [
        {"target": "e5", "element": "Select a location", "values": ["Titusville"]}
    ]
    assert browser.called("browser_press_key") == [{"key": "Escape"}]
    assert browser.called("browser_wait_for") == [
        {"time": 5.0},
        {"text": "Showtimes"},
        {"time": 2.0},
    ]
    assert browser.called("browser_tabs") == [
        {"action": "select", "index": 0},
        {"action": "new", "url": "https://cinema.example/titusville"},
    ]
    assert len(browser.called("browser_navigate_back")) == 1
    notes = {s.n: s.note for s in run.steps}
    assert "Only these keys" in notes[3]
    assert "short phrase" in notes[7]
    assert "private network" in notes[8]
    assert "tabs action must be" in notes[9]
    assert "Open tabs" in notes[10]
    assert run.outcome == "gave_up"


async def test_only_the_mapped_playwright_tools_are_reachable() -> None:
    """The whole surface of the server this loop can touch, pinned. Adding a mapping here is
    a decision about the fence, not a refactor."""
    assert set(MCP_TOOL_FOR.values()) | {"browser_snapshot"} == {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_select_option",
        "browser_press_key",
        "browser_navigate_back",
        "browser_wait_for",
        "browser_tabs",
        "browser_snapshot",
    }
    assert set(MCP_TOOL_FOR) | {"snapshot", "finish", "give_up"} == ACTION_NAMES


def test_the_action_sidecars_are_pinned() -> None:
    """The browse model's actions are prompt surface like any `.tool`: a prose or schema
    change is a deliberate version bump."""
    pins = {
        "click": (1, "b0dffdd233a2b070be8139a6187864a03853d5522b073200d5e98d694d8c0145"),
        "finish": (1, "1f8069caa13e3a5abb46fbbeb67185fe0f21fcdacd5d311681793301918c0972"),
        "give_up": (1, "975f7830cd71d5496c668c45bc8b8010ff0d1cc8ce7805e3b319134f2a3704f2"),
        "go_back": (1, "ab8adbc61e0727d0a42b74b9b02c4c8c721737297f32bb3d4276ee7a5b998366"),
        "navigate": (1, "62ec34021e5f589249277837d01837c6d85d1330bef460937d74b597d159a9d8"),
        "press_key": (1, "5b8287cf957e903c6080a373301f15401dcd7da14242d902f4c367c00123a503"),
        "select_option": (1, "4ae5f31dea217ed21916008b0afaf59929f6f681a168d234a5f10ae6b2b76133"),
        "snapshot": (1, "011c58bdce9163697ba660ee3c04e45c27b965efc3f0f6a8dbd2bef3fbc490a6"),
        "tabs": (1, "8118ea0c52f8a90253bd7f729ce6991155b3e7e75a0420ba7aa1bb47d240d09e"),
        "type_text": (1, "e0eb99b683fd0391d54c3538338e5ff3fc3d4d375c40904ba0f1bb91ff57d98f"),
        "wait_for": (1, "9356c4f1984df89264669b4df184164284c68f80183a5f6bb655e13cd71aefc9"),
    }
    on_disk = {p.stem for p in _ACTIONS_DIR.glob("*.tool")}
    assert on_disk == set(pins)
    for name, (version, digest) in pins.items():
        tool = load_tool(_ACTIONS_DIR / f"{name}.tool")
        assert (tool.spec.name, tool.spec.version, tool.digest) == (name, version, digest)


def test_the_browse_prompt_is_pinned() -> None:
    import hashlib

    assert browse._PROMPT.version == "agent-browse-v1"
    assert (
        hashlib.sha256(browse._PROMPT.body.encode()).hexdigest()
        == "6bc597ff61ab433dc6ba9f8d579005cf11d5adb6d7a1c886b03d3116ca0b588c"
    )


# --- Verification ------------------------------------------------------------------


async def test_an_answer_whose_evidence_is_not_on_the_page_is_sent_back_once() -> None:
    turns = [
        _call("finish", 1, answer="It plays at 8 PM.", evidence="8:00 PM"),
        _call("click", 2, ref="e1"),
        _call("click", 3, ref="e10"),
        _call("finish", 4, answer="7:15 and 9:40.", evidence="7:15 PM, 9:40 PM"),
    ]
    agent, fake, _ = _agent(turns)
    run = await agent.run(GOAL, HOME)

    assert run.outcome == "answered" and run.verified and run.answer == "7:15 and 9:40."
    bounce = fake.converse_calls[1]["messages"][-1].results[0].content
    assert bounce.startswith("Not accepted")


async def test_a_second_unverified_finish_is_accepted_but_flagged() -> None:
    turns = [
        _call("finish", 1, answer="8 PM", evidence="8:00 PM"),
        _call("finish", 2, answer="8 PM [buy](https://evil.example/)", evidence="8:00 PM"),
    ]
    agent, _, _ = _agent(turns)
    run = await agent.run(GOAL, HOME)

    assert run.outcome == "answered" and not run.verified
    assert run.answer == "8 PM buy"
    text = render_for_caller(run)
    assert "UNVERIFIED" in text and "evil.example" not in text


async def test_a_finish_without_an_answer_is_not_accepted() -> None:
    turns = [_call("finish", 1, answer="", evidence="7:15 PM"), _call("give_up", 2, reason="x")]
    agent, fake, _ = _agent(turns)
    run = await agent.run(GOAL, TITUSVILLE)
    assert run.outcome == "gave_up"
    assert "needs the answer" in fake.converse_calls[1]["messages"][-1].results[0].content


# --- Budgets and loop detection --------------------------------------------------------


async def test_the_same_action_is_refused_then_ends_the_run() -> None:
    turns = [_call("click", n, ref="e2") for n in range(1, 6)]
    agent, _, browser = _agent(turns, FakeBrowser(inert_clicks=True))
    run = await agent.run(GOAL, HOME)

    assert run.outcome == "loop"
    assert len(browser.called("browser_click")) == 2  # the 3rd is refused, the 4th stops
    assert "tried this exact click 2 times" in run.steps[3].note


async def test_actions_that_change_nothing_end_the_run() -> None:
    # Six DIFFERENT clicks (so this is not the repeat rule), none of which changes the page.
    refs = ["e1", "e2", "e4", "e5", "e9", "e3"]
    agent, _, _ = _agent(
        [_call("click", n, ref=r) for n, r in enumerate(refs, 1)], FakeBrowser(inert_clicks=True)
    )
    run = await agent.run(GOAL, HOME)
    assert run.outcome == "stuck"


async def test_the_step_budget_ends_a_wandering_run() -> None:
    agent, _, _ = _agent([_call("snapshot", 1)], max_steps=3)
    run = await agent.run(GOAL, HOME)
    assert run.outcome == "step_budget"
    assert len([s for s in run.steps if s.action == "snapshot"]) == 3


async def test_a_requested_step_count_is_clamped() -> None:
    agent, fake, _ = _agent([_call("snapshot", 1)], max_steps=3)
    await agent.run(GOAL, HOME, max_steps=500)
    assert len(fake.converse_calls) == browse.MAX_STEPS_CEILING


async def test_the_page_budget_ends_a_run_that_keeps_opening_sites() -> None:
    turns = [_call("navigate", n, url=other_page(n)) for n in range(1, 6)]
    agent, _, _ = _agent(turns, max_pages=2)
    run = await agent.run(GOAL)
    assert run.outcome == "page_budget"
    assert len(run.sources) == 3


async def test_the_wall_clock_ends_a_slow_run() -> None:
    ticks = iter([0.0, 0.0, 0.0, 0.0, 500.0, 500.0, 500.0, 500.0])
    agent, _, _ = _agent([_call("snapshot", 1)], clock=lambda: next(ticks, 500.0), wall_seconds=60)
    run = await agent.run(GOAL)
    assert run.outcome == "timeout"


async def test_a_hung_browser_is_cut_off_by_the_hard_timeout() -> None:
    import asyncio

    class Hung(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200)

    router = LlmRouter({"xai": FakeLlmClient()}, {"browse.step": ("xai", "grok-4.3")})
    agent = BrowseAgent(router, McpHttpClient("http://browser:8931/mcp", transport=Hung()))
    agent._wall = -29.9  # the hard cap is wall + 30 s; this makes it 0.1 s
    run = await agent.run(GOAL)
    assert run.outcome == "timeout"


async def test_a_model_that_stops_choosing_actions_is_nudged_then_stopped() -> None:
    agent, fake, _ = _agent([_say("I think the answer is 7pm."), _say("Still thinking.")])
    run = await agent.run(GOAL, HOME)
    assert run.outcome == "no_action"
    nudge = fake.converse_calls[1]["messages"][-1]
    assert isinstance(nudge, UserMessage) and "exactly one action" in nudge.text


async def test_only_the_first_of_several_calls_runs() -> None:
    double = LlmTurn(
        text="",
        tool_calls=[
            ToolCall(id="a", name="click", arguments={"ref": "e1"}),
            ToolCall(id="b", name="click", arguments={"ref": "e6"}),
        ],
        stop_reason="tool_use",
        usage=LlmUsage(1, 1),
    )
    agent, fake, browser = _agent([double, _call("give_up", 2, reason="x")])
    await agent.run(GOAL, HOME)
    assert browser.called("browser_click") == [
        {"target": "e1", "element": "Your theater: Please select a location"}
    ]
    skipped = fake.converse_calls[1]["messages"][-1].results[1]
    assert skipped.tool_call_id == "b" and "one action per step" in skipped.content


async def test_a_refused_start_url_is_recorded_and_the_model_starts_blank() -> None:
    agent, fake, browser = _agent([_call("give_up", 1, reason="no site")])
    run = await agent.run(GOAL, "http://db:5432/")
    assert browser.calls == []
    assert run.steps[0].action == "navigate" and not run.steps[0].ok
    assert "URL: (none)" in fake.converse_calls[0]["messages"][0].text


async def test_a_browser_error_is_reported_to_the_model_not_raised() -> None:
    browser = FakeBrowser(fail_tool="browser_click")
    agent, fake, _ = _agent([_call("click", 1, ref="e1"), _call("give_up", 2, reason="x")], browser)
    run = await agent.run(GOAL, HOME)
    assert not run.steps[1].ok and "ERR_FAILED" in run.steps[1].note
    assert (
        "the browser reported an error" in fake.converse_calls[1]["messages"][-1].results[0].content
    )


async def test_an_unreachable_browser_is_an_outcome_not_a_crash() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    router = LlmRouter({"xai": FakeLlmClient()}, {"browse.step": ("xai", "grok-4.3")})
    agent = BrowseAgent(
        router, McpHttpClient("http://browser:8931/mcp", transport=httpx.MockTransport(down))
    )
    run = await agent.run(GOAL)
    assert run.outcome == "error" and "unreachable" in run.error
    assert "failed" in render_for_caller(run)


async def test_a_model_failure_is_an_outcome_not_a_crash() -> None:
    agent, _, _ = _agent([])

    async def boom(task: str, **kwargs: Any) -> LlmTurn:
        raise LlmTransientError("overloaded")

    agent._router.converse = boom  # type: ignore[method-assign]
    run = await agent.run(GOAL, HOME)
    assert run.outcome == "error" and "model call failed" in run.error


# --- What jerv reads ----------------------------------------------------------------------


def test_the_caller_reads_fenced_data_with_host_observed_sources() -> None:
    run = BrowseRun(
        goal="g",
        outcome="answered",
        answer="7:15 PM",
        verified=True,
        final_url="https://u:p@cinema.example/t#x",
        sources=("https://cinema.example/t",),
        steps=[BrowseStep(1, "click", {}, True, "done")],
    )
    text = render_for_caller(run)
    assert text.startswith("[BROWSE RESULT — quoted data")
    assert "Checked:" in text and "Answer: 7:15 PM" in text
    assert "Final page: https://cinema.example/t\n" in text
    assert "Pages visited: https://cinema.example/t" in text
    stopped = render_for_caller(BrowseRun(goal="g", outcome="stuck", final_url="about:blank"))
    assert "stopped" in stopped and "No answer was read" in stopped and "Final page" not in stopped


def _ctx(tools: frozenset[str] = frozenset(), model: str | None = None) -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        agent_tools=tools,
        model_override=model,
    )


async def test_the_browse_tool_hands_over_the_goal_and_returns_citable_data() -> None:
    agent, fake, _ = _agent(HAPPY)
    emitted: list[tuple[str, str | None]] = []

    def emit(kind: str, text: str | None = None) -> None:
        emitted.append((kind, text))

    handler = build_browse_handlers(agent, emit=emit)["browse"]
    out = await handler({"goal": f"  {GOAL}\n", "start_url": HOME}, _ctx())

    assert isinstance(out, ToolOutput)
    assert out.startswith("[BROWSE RESULT")
    assert [s.url for s in out.web_sources] == [HOME, PICKER, TITUSVILLE]
    assert [s.read for s in out.web_sources] == [False, False, True]
    assert out.result_brief == "verified · 4 steps"
    assert emitted == [("web_fetch", HOME)]
    assert fake.converse_calls[0]["messages"][0].text.startswith(f"GOAL: {GOAL}\n")


async def test_the_browse_tool_refuses_an_empty_or_rambling_goal() -> None:
    agent, fake, _ = _agent([])
    handler = build_browse_handlers(agent)["browse"]
    assert "needs a goal" in await handler({"goal": "  "}, _ctx())
    assert "too long" in await handler({"goal": "x " * 400}, _ctx())
    assert fake.converse_calls == []


async def test_the_browse_tool_reports_a_stop_in_its_brief() -> None:
    agent, _, _ = _agent([_call("give_up", 1, reason="closed")])
    out = await build_browse_handlers(agent)["browse"]({"goal": "g"}, _ctx())
    assert isinstance(out, ToolOutput) and out.result_brief == "gave up · 1 steps"


async def test_the_browse_tool_runs_on_the_conversation_model() -> None:
    agent, _, _ = _agent(HAPPY)
    seen: list[Any] = []
    original = agent.run

    async def spy(goal: str, start_url: str | None = None, **kw: Any) -> BrowseRun:
        seen.append(kw.get("spec_override"))
        return await original(goal, start_url, **kw)

    agent.run = spy  # type: ignore[method-assign]
    await build_browse_handlers(agent)["browse"]({"goal": "g"}, _ctx(model="xai:grok-other"))
    assert seen == ["xai:grok-other"]


def test_the_jerv_tool_sidecar_is_web_gated() -> None:
    tool = load_tool(Path(browse.__file__).parent / "tools" / "browse.tool")
    assert tool.spec.permission == "web"
    assert tool.spec.params["required"] == ["goal"]


# --- The MCP client -------------------------------------------------------------------


async def test_the_client_reads_sse_replies_too() -> None:
    browser = FakeBrowser(sse=True)
    client = McpHttpClient("http://browser:8931/mcp", transport=browser.transport())
    async with client.session() as session:
        assert await session.list_tools() == ["browser_navigate", "browser_run_code_unsafe"]
        result = await session.call_tool("browser_snapshot", {})
    assert "Page URL" in result.text and not result.is_error
    assert browser.deleted


def test_sse_parsing_skips_junk_and_keeps_the_last_event() -> None:
    body = 'data: not json\n\n: comment\ndata: {"id": 1,\ndata: "result": {}}\n'
    assert _parse_sse(body) == [{"id": 1, "result": {}}]


def _client(handler: Any) -> McpHttpClient:
    return McpHttpClient("http://browser:8931/mcp", transport=httpx.MockTransport(handler))


def _init_then(reply: httpx.Response) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise httpx.ConnectError("gone")
        if b'"initialize"' in request.content:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"mcp-session-id": "s"}
            )
        if b"notifications/initialized" in request.content:
            return httpx.Response(202)
        return reply

    return handle


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (httpx.Response(500), "HTTP 500"),
        (
            httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
            "other than JSON",
        ),
        (
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "error": {"message": "nope"}}),
            "refused the request: nope",
        ),
        (httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": "x"}), "no result"),
        (httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 9, "result": {}}]), "no response"),
    ],
)
async def test_malformed_server_replies_raise_mcp_errors(
    reply: httpx.Response, message: str
) -> None:
    async with _client(_init_then(reply)).session() as session:
        with pytest.raises(McpError, match=message):
            await session.call_tool("browser_snapshot", {})


async def test_a_failed_initialize_closes_the_http_client() -> None:
    with pytest.raises(McpError):
        async with _client(lambda r: httpx.Response(503)).session():
            pass


async def test_an_unconfigured_client_refuses_to_open() -> None:
    client = McpHttpClient("")
    assert not client.configured
    with pytest.raises(McpError, match="no browser server"):
        async with client.session():
            pass


async def test_a_huge_result_is_cut_and_closing_twice_is_harmless() -> None:
    big = httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "x" * 500_000}], "isError": True},
        },
    )
    async with _client(_init_then(big)).session() as session:
        result = await session.call_tool("browser_snapshot", {})
        await session.close()  # the DELETE fails; a dropped session is not an error
        await session.close()  # and a second close is a no-op
    assert len(result.text) == 400_000 and result.is_error
