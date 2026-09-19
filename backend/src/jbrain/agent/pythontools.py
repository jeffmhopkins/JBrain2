"""The `run_python` tool: a snippet in, what it printed and what it evaluated to out.

The multi-step half of the arithmetic backstop. `calculate` answers one expression exactly;
this answers the questions that are not one expression — a loop, a median over twenty
numbers, a date difference, a running total, a unit conversion chain, checking a figure
against a table the model is holding.

**It runs nowhere near here.** The handler does one thing: POST the snippet to the
`pysandbox` sidecar (`jbrain.pysandbox`) and shape the reply. That indirection is the
feature, not plumbing — see that module and `deploy/pysandbox/server.py` for why
`docs/reference/ASSISTANT.md`'s "no code execution in the agent" is intact with this tool
shipped, and `docs/archive/EXACT_MATH_TOOLS_PLAN.md` for the decision record.

**It is sent the model's snippet and nothing else.** No note body, no lab value, no location
fix, no session context — the sandbox has no way to fetch anything, so the only owner data
that could ever reach it is data a handler put there, and this one puts none. That is why
the tool is `web`-classed: not because it egresses (it cannot), but because `web` is the
opt-in, never-in-the-wildcard gate, and a tool that executes model-authored code must be
granted per persona rather than absorbed by a wildcard.
"""

from __future__ import annotations

import structlog

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.pysandbox import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    SANDBOX_SEALS,
    PySandboxClient,
    PySandboxError,
    Ran,
)

log = structlog.get_logger()

# What a tool result may occupy in the model's context. The sidecar already truncates each
# stream; this is the envelope around all of them, because three capped fields still add up.
MAX_OBSERVATION_CHARS = 12_000


def _requested_timeout(raw: object) -> float:
    """Clamp rather than refuse. A malformed optional argument is never worth failing a
    correct snippet over, and the sidecar clamps again on its own side regardless."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(0.1, min(value, MAX_TIMEOUT_SECONDS))


def format_run(ran: Ran) -> str:
    """Render one execution for the model.

    Labelled sections rather than JSON: the model reads this as prose and copies numbers out
    of it, and an empty section is omitted so a successful one-line computation comes back as
    one line. A failure leads with the error, because that is the only part the model needs
    in order to write the next version."""
    blocks: list[str] = []
    if ran.error:
        blocks.append(f"error:  {ran.error}")
    if ran.stdout.strip():
        blocks.append(f"stdout:\n{ran.stdout.rstrip()}")
    # Reported separately from stdout: a warning printed alongside a correct answer should
    # not read as part of the answer.
    if ran.stderr.strip():
        blocks.append(f"stderr:\n{ran.stderr.rstrip()}")
    if ran.result is not None:
        blocks.append(f"result: {ran.result}")
    if not blocks:
        # Ran fine, said nothing — the commonest beginner shape, and worth naming so the
        # model adds a `print` instead of concluding the sandbox is broken.
        return "The code ran with no errors, but printed nothing and produced no final value."
    rendered = "\n".join(blocks)
    if len(rendered) > MAX_OBSERVATION_CHARS:
        rendered = (
            rendered[:MAX_OBSERVATION_CHARS]
            + f"\n… [truncated: the output was {len(rendered):,} characters, showing the first"
            f" {MAX_OBSERVATION_CHARS:,}]"
        )
    elif ran.truncated:
        # The sidecar cut a stream. Said plainly, because a model that believes it has seen
        # the whole of a long output will draw a conclusion from a fragment.
        rendered += "\n[the output was long and was cut short]"
    return rendered


# One line on a phone; the full value is in the step's result text.
MAX_BRIEF_CHARS = 32


def run_brief(ran: Ran) -> str:
    """The run's ANSWER for the Worked row, read off `Ran`'s fields rather than off the text
    `format_run` built from them.

    A trailing bare expression is the answer when there is one — the notebook convention the
    sandbox already honours. Otherwise the LAST line printed is: a snippet that computes and
    prints ends on its conclusion, and the lines above it are working. A run that said
    nothing says so, because a row with a blank right-hand side is a row you cannot check,
    and "it printed nothing" is itself the thing worth seeing."""
    answer = ran.result if ran.result is not None else _last_line(ran.stdout)
    if not answer:
        return "no output"
    answer = " ".join(answer.split())
    if len(answer) > MAX_BRIEF_CHARS:
        answer = answer[: MAX_BRIEF_CHARS - 1] + "\u2026"
    return answer


def _last_line(stdout: str) -> str:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def run_view(ran: Ran, code: str, timeout: float) -> ViewPayload:
    """The `code_run` view: data-only slots the registered component renders.

    No markup, no colour and no URL cross this boundary (DESIGN.md invariant #1/#9) — the
    component owns the syntax highlighting, applied from `language`, so a snippet can never
    smuggle a span into the transcript. The same view serves `calculate`, with the code
    section replaced by the expression: one component, two tools, because they are the
    same act.

    The containment chips are built from the sandbox's ACTUAL settings rather than a
    sentence typed here. "Where did this run?" is a fair question to ask of something that
    executed code, and an answer that cannot drift from the compose file is worth more than
    one that reads well."""
    return ViewPayload(
        view="code_run",
        surface="inline",
        data={
            "language": "python",
            "code": code,
            "stdout": ran.stdout,
            "stderr": ran.stderr,
            "result": ran.result,
            "error": ran.error,
            "ok": ran.ok,
            "duration_ms": ran.duration_ms,
            "truncated": ran.truncated,
            "timeout_seconds": timeout,
            "containment": list(SANDBOX_SEALS),
        },
    )


def build_python_handlers(sandbox: PySandboxClient) -> dict[str, ToolHandler]:
    """The `run_python` tool. Built only when a sandbox URL is configured; otherwise the
    sidecar is dropped from the registry and the tool simply does not exist on that box
    (the same graceful degrade as the image/transcribe tools)."""

    async def run_python_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        code = str(arguments.get("code", ""))
        timeout = _requested_timeout(arguments.get("timeout_seconds"))
        try:
            ran = await sandbox.run(code, timeout_seconds=timeout)
        except PySandboxError as exc:
            # The sandbox being unreachable is not the model's mistake, so the message says
            # what to do about it rather than inviting a retry of the same call.
            return ToolOutput(f"run_python is unavailable: {exc}. Answer another way.")
        log.info(
            "agent.run_python",
            ok=ran.ok,
            duration_ms=ran.duration_ms,
            code_chars=len(code),
            truncated=ran.truncated,
            # The snippet itself is NOT logged: it is model-authored text that may quote
            # whatever the owner just said, and the run log already records the call's
            # arguments under the owner's own RLS scope. A second, unscoped copy in the
            # container logs is a domain-firewall hole for no debugging gain.
        )
        return ToolOutput(
            format_run(ran),
            view=run_view(ran, code, timeout),
            result_brief=run_brief(ran),
        )

    return {"run_python": run_python_tool}
