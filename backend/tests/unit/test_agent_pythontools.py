"""The `run_python` tool: the sandbox client, and how a run is rendered for the model.

The sandbox's own containment is tested against the real thing in `test_pysandbox_server.py`.
This file covers the api side — that the handler sends only the model's snippet, that a
failure comes back as an observation rather than a raise, and that the rendering tells the
model what it needs to fix its next attempt.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.pythontools import (
    MAX_BRIEF_CHARS,
    MAX_OBSERVATION_CHARS,
    build_python_handlers,
    format_run,
    run_brief,
    run_view,
)
from jbrain.db.session import SessionContext
from jbrain.pysandbox import (
    MAX_CODE_BYTES,
    MAX_TIMEOUT_SECONDS,
    SANDBOX_SEALS,
    PySandboxClient,
    PySandboxError,
    Ran,
)


def _ctx() -> ToolContext:
    return ToolContext(session=SessionContext(principal_id="p", principal_kind="owner"), scopes=())


def _client(handler, url: str = "http://pysandbox:8000") -> PySandboxClient:
    return PySandboxClient(url, transport=httpx.MockTransport(handler))


def _responding(body: dict) -> PySandboxClient:
    return _client(lambda _request: httpx.Response(200, json=body))


async def _call(client: PySandboxClient, **arguments: object) -> str:
    return str(await build_python_handlers(client)["run_python"](dict(arguments), _ctx()))


# --- The client -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_is_posted_to_the_pinned_url_with_only_the_code() -> None:
    """The base URL comes from config, never from the model — and the payload is the
    snippet and a timeout, nothing else. There is no field here through which owner data
    could reach the sandbox even by mistake."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": "42"})

    ran = await _client(handler).run("6 * 7")
    assert seen["url"] == "http://pysandbox:8000/run"
    assert set(seen["body"]) == {"code", "timeout_seconds"}
    assert seen["body"]["code"] == "6 * 7"
    assert ran.ok and ran.result == "42"


@pytest.mark.asyncio
async def test_an_unconfigured_sandbox_is_an_error_not_a_silent_success() -> None:
    with pytest.raises(PySandboxError, match="not configured"):
        await PySandboxClient("").run("1 + 1")


@pytest.mark.asyncio
async def test_an_unreachable_sandbox_reports_itself_unreachable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(PySandboxError, match="not reachable"):
        await _client(handler).run("1 + 1")


@pytest.mark.asyncio
async def test_an_oversized_snippet_fails_here_rather_than_at_the_container() -> None:
    with pytest.raises(PySandboxError, match="too long"):
        await _responding({"ok": True}).run("x = 1\n" * MAX_CODE_BYTES)


@pytest.mark.asyncio
async def test_the_requested_timeout_is_clamped_before_it_is_sent() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    await _client(handler).run("1", timeout_seconds=9999)
    assert seen["timeout_seconds"] == MAX_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_code_that_raised_is_a_successful_call_not_a_client_error() -> None:
    """The distinction the tool rests on: the sandbox doing its job and reporting a failing
    snippet is NOT the sandbox failing. Conflating them would tell the model to give up when
    it should fix its code."""
    ran = await _responding({"ok": False, "error": "ZeroDivisionError: division by zero"}).run(
        "1/0"
    )
    assert ran.ok is False
    assert ran.error == "ZeroDivisionError: division by zero"


# --- Rendering a run for the model ------------------------------------------


def test_a_successful_run_shows_stdout_and_the_result() -> None:
    rendered = format_run(Ran(ok=True, stdout="working\n", result="41"))
    assert "stdout:\nworking" in rendered
    assert "result: 41" in rendered


def test_a_failure_leads_with_the_error() -> None:
    """It is the only part the model needs in order to write the next version."""
    rendered = format_run(Ran(ok=False, stdout="step 1\n", error="NameError: x (line 2)"))
    assert rendered.startswith("error:  NameError: x (line 2)")
    # ...and the output from before the failure is still there, so it can see how far it got.
    assert "step 1" in rendered


def test_stderr_is_reported_apart_from_stdout() -> None:
    """A warning printed alongside a correct answer should not read as part of the answer."""
    rendered = format_run(Ran(ok=True, stdout="42\n", stderr="DeprecationWarning: x"))
    assert "stderr:\nDeprecationWarning: x" in rendered
    assert "stdout:\n42" in rendered


def test_an_empty_section_is_omitted() -> None:
    """A one-line computation should come back as one line, not three empty headings."""
    assert format_run(Ran(ok=True, result="42")) == "result: 42"


def test_a_run_that_said_nothing_says_so() -> None:
    """The commonest beginner shape, and worth naming — otherwise the model concludes the
    sandbox is broken instead of adding a print."""
    rendered = format_run(Ran(ok=True))
    assert "printed nothing and produced no final value" in rendered


def test_a_truncated_run_says_it_was_cut() -> None:
    """A model that believes it has seen a whole output will draw a conclusion from a
    fragment."""
    rendered = format_run(Ran(ok=True, stdout="x" * 100, truncated=True))
    assert "cut short" in rendered


def test_the_whole_observation_is_capped() -> None:
    """The sidecar caps each stream; this caps the envelope, because three capped fields
    still add up to more context than a tool result should take."""
    huge = Ran(ok=True, stdout="x" * 9000, stderr="y" * 9000, result="z" * 2000)
    rendered = format_run(huge)
    assert len(rendered) < MAX_OBSERVATION_CHARS + 200
    assert "[truncated:" in rendered


# --- The handler ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_handler_returns_the_rendered_run() -> None:
    out = await _call(_responding({"ok": True, "stdout": "hi\n", "result": "7"}), code="7")
    assert "stdout:\nhi" in out and "result: 7" in out


@pytest.mark.asyncio
async def test_an_unavailable_sandbox_comes_back_as_an_observation_never_a_raise() -> None:
    """A tool error is something the model recovers from on the next step (loop.py
    `_dispatch`), and this one tells it to stop trying rather than to retry — the sandbox
    being down is not its mistake to fix."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    out = await _call(_client(handler), code="1 + 1")
    assert isinstance(out, str)
    assert out.startswith("run_python is unavailable:")
    assert "Answer another way" in out


@pytest.mark.asyncio
async def test_an_empty_snippet_is_reported_not_sent() -> None:
    out = await _call(_responding({"ok": True}), code="   ")
    assert "unavailable" in out and "empty" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [0, -3, "soon", None, 10**9])
async def test_a_bad_timeout_argument_is_clamped_not_refused(requested: object) -> None:
    """Never fail a correct snippet over a malformed optional argument."""
    out = await _call(_responding({"ok": True, "result": "1"}), code="1", timeout_seconds=requested)
    assert "result: 1" in out


@pytest.mark.asyncio
async def test_the_handler_returns_a_tool_output() -> None:
    """ToolOutput is what the loop pulls surfaced data off; every handler returns one."""
    handler = build_python_handlers(_responding({"ok": True, "result": "1"}))["run_python"]
    assert isinstance(await handler({"code": "1"}, _ctx()), ToolOutput)


# --- the Worked row's answer (SHOW_THE_WORKING_PLAN.md W1) -------------------


def test_the_answer_is_the_trailing_expression_when_there_is_one() -> None:
    """The notebook convention the sandbox already honours: a bare final expression IS the
    result, whatever the snippet printed on the way there."""
    assert run_brief(Ran(ok=True, stdout="working\n", result="Decimal('2917.80')")) == (
        "Decimal('2917.80')"
    )


def test_otherwise_the_answer_is_the_last_line_printed() -> None:
    """A snippet that computes and prints ends on its conclusion; the lines above it are
    working, and showing the first would put the working in the answer's column."""
    assert run_brief(Ran(ok=True, stdout="q1 640\nq2 702\ntotal 2917.80\n")) == "total 2917.80"


def test_the_row_says_so_when_a_run_printed_nothing() -> None:
    """A blank right-hand side is a row you cannot check, and "it printed nothing" is itself
    the thing worth seeing — it is the commonest beginner shape."""
    assert run_brief(Ran(ok=True)) == "no output"


def test_the_answer_is_capped_and_flattened_to_one_row() -> None:
    assert run_brief(Ran(ok=True, result="x" * 200)) == "x" * (MAX_BRIEF_CHARS - 1) + "\u2026"
    assert run_brief(Ran(ok=True, result="a\n  b")) == "a b"


# --- the code_run view (SHOW_THE_WORKING_PLAN.md W2) -------------------------


def test_the_run_emits_a_data_only_code_run_view() -> None:
    """Slots, not markup. The model fills them and authors no span, colour or URL — the
    component owns the highlighting, applied from `language`."""
    view = run_view(Ran(ok=True, stdout="214\n", result="214", duration_ms=44), "1+1", 10.0)
    assert view.view == "code_run"
    assert view.data["language"] == "python"
    assert view.data["code"] == "1+1"
    assert view.data["stdout"] == "214\n"
    assert view.data["duration_ms"] == 44
    assert view.data["ok"] is True


def test_the_view_states_the_containment_rather_than_assuming_it() -> None:
    """ "Where did this run?" is a fair question to ask of something that executed code.
    The chips come from `SANDBOX_SEALS`, which `test_pysandbox_server.py` ties back to the
    compose file declaration by declaration."""
    view = run_view(Ran(ok=True), "x", 10.0)
    assert tuple(view.data["containment"]) == SANDBOX_SEALS


def test_a_failed_run_still_carries_its_error_into_the_view() -> None:
    """The failure is the most useful thing to see, and the step opens itself on it."""
    view = run_view(Ran(ok=False, error="ZeroDivisionError (line 1)"), "1/0", 10.0)
    assert view.data["ok"] is False
    assert view.data["error"] == "ZeroDivisionError (line 1)"
