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
from jbrain.agent.pythontools import MAX_OBSERVATION_CHARS, build_python_handlers, format_run
from jbrain.db.session import SessionContext
from jbrain.pysandbox import (
    MAX_CODE_BYTES,
    MAX_TIMEOUT_SECONDS,
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
