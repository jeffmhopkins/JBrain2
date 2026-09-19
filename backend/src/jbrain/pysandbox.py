"""Client for the on-box `pysandbox` sidecar — Python code in, stdout + value + error out.

Mirrors `jbrain.htmlrender.HtmlRenderClient`: a pinned base URL from config (never
model-supplied) and an injectable httpx transport so tests run with no network
(DEVELOPMENT.md "no network in tests").

**The api does not execute anything.** That is the whole architectural point, and it is what
keeps `docs/reference/ASSISTANT.md`'s "no code execution in the agent" true while the agent
nevertheless has a `run_python` tool: this module makes an HTTP request to a container that
holds no owner data, no database credentials and no route off the box. The relationship is
the one `docs/archive/JCODE_PLAN.md` settled — JBrain *fronts* a sandbox rather than
*embodying* one — and the same one the api has with `htmlrender` and ComfyUI.

**One property callers must not undo: never send owner content.** The sandbox is a
calculator for numbers the model already has in front of it, not somewhere to paste a note
body, a lab result, a location fix or an email. It has no way to fetch anything itself, so
the only way owner data reaches it is if a caller puts it there — which is why the handler
(`jbrain.agent.pythontools`) passes the model's snippet and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

# The sidecar enforces its own wall clock and kills the child at it. This is the HTTP read
# timeout, sized above the sidecar's own ceiling plus its queue wait, so a slow-but-legal run
# comes back as the sandbox's own TimeoutError — a message the model can act on — rather than
# as a transport failure that says nothing about the code.
_TIMEOUT = httpx.Timeout(90.0)

# Mirrors the sidecar's cap so an oversized snippet fails here, cheaply, with a message the
# tool can hand the model rather than as a 422 from a container.
MAX_CODE_BYTES = 128_000

# What the tool asks for when the caller says nothing. The sidecar clamps anything past its
# own PYSANDBOX_MAX_TIMEOUT_SECONDS, so this is a request, not a grant.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 60.0

# What the owner is told about where their code ran, shown as chips under a run
# (`code_run` view). Each phrase names a control that is REAL and declared in
# `deploy/docker-compose.yml`, and `test_pysandbox_server.py` ties every one of them back to
# that file — because a containment claim nobody checks is worth less than no claim at all,
# and this one is shown to the owner as reassurance.
SANDBOX_SEALS: tuple[str, ...] = (
    "no network",
    "scratch only",
    "stdlib only",
    "no root",
)


class PySandboxError(RuntimeError):
    """The sandbox is unconfigured or unreachable. Recoverable: surfaced to the model as a
    tool error, never a crash.

    Note what is NOT this: code that raised, timed out, or was refused is a SUCCESSFUL call —
    the sandbox did its job and reported a failure — and comes back as a `Ran` with `ok`
    False. Conflating the two would tell the model to give up when it should fix its snippet.
    """


@dataclass(frozen=True)
class Ran:
    """One execution. `ok` is whether the code ran to completion, not whether it was right.

    `result` is the value of a trailing bare expression (the notebook convention), None when
    the snippet ends in a statement. `truncated` says the sidecar cut the streams, so a
    caller can say so rather than presenting a fragment as the whole output."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    result: str | None = None
    error: str | None = None
    truncated: bool = False
    duration_ms: int = 0


class PySandboxClient:
    """POST a snippet to the pinned sandbox and get its output back."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def run(self, code: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Ran:
        """Execute `code` in the sandbox and return what it produced."""
        if not self._base_url:
            raise PySandboxError("the Python sandbox is not configured on this instance")
        if not code.strip():
            raise PySandboxError("nothing to run — the code was empty")
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise PySandboxError(
                f"that snippet is too long to run ({MAX_CODE_BYTES // 1000}KB limit)"
            )
        payload = {
            "code": code,
            "timeout_seconds": max(0.1, min(timeout_seconds, MAX_TIMEOUT_SECONDS)),
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                response = await client.post(f"{self._base_url}/run", json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The sidecar being down is an operational fact about the box, not something the
            # model did — so it is logged here and surfaced as the "unavailable" class, which
            # the tool turns into "say so and answer another way" rather than "retry".
            log.warning("pysandbox.unreachable", error=repr(exc))
            raise PySandboxError("the Python sandbox is not reachable right now") from exc
        return Ran(
            ok=bool(body.get("ok")),
            stdout=str(body.get("stdout") or ""),
            stderr=str(body.get("stderr") or ""),
            result=body.get("result"),
            error=body.get("error"),
            truncated=bool(body.get("truncated")),
            duration_ms=int(body.get("duration_ms") or 0),
        )
