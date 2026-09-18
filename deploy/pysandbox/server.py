"""The `pysandbox` sidecar: Python code in, stdout + final value + error out.

**Why this is a container and not a function.** `docs/reference/ASSISTANT.md` refuses code
execution *in the agent*, and `docs/proposed/JERV_CONTEXT_BUDGET_PLAN.md` §5 rejected an
in-process Python tool on two specific costs: CLAUDE.md #2 (an exec tool is a
by-construction hole in the storage abstraction) and #10 (its failure modes are
undebuggable without a terminal). Neither cost is paid here, because the executing code
never runs inside the api. It runs in a container that holds no owner data, has no
database credentials, no blob volume and no route off the box — the same shape
`docs/archive/JCODE_PLAN.md` settled for the coding sandbox, and the same relationship the
api has with `htmlrender` and ComfyUI. The api makes an HTTP call; the blast radius is this
image.

**What the api may send, and what it may never send.** Code and a timeout. Never owner
content: a note body, a lab result, a location fix or an email is not something to paste
into a sandbox, and the tool's handler is written so it cannot. The model composes a
snippet from what it already has in context; the sandbox has no way to fetch anything else.

**One process per call, and it is killed, not asked to stop.** Each request gets a fresh
scratch directory under the tmpfs, a child started in its own session with rlimits applied
before exec, and a wall-clock deadline. On a timeout the whole process GROUP is killed —
`SIGKILL`, not `SIGTERM` — because a snippet that has wedged the interpreter cannot be
relied on to run a signal handler. The scratch directory goes with it.

**Concurrency is capped.** A semaphore bounds how many children can exist at once, so a
model that fires five `run_python` calls in one turn cannot take the box's memory with it.
Past the cap the request waits, and past the queue deadline it is refused with a message
the model can act on.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

RUNNER = str(Path(__file__).parent / "runner.py")

# Wall clock. The api sends its own, clamped to MAX_TIMEOUT_SECONDS here so a caller cannot
# talk the sandbox into holding a slot indefinitely.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("PYSANDBOX_TIMEOUT_SECONDS", "10"))
MAX_TIMEOUT_SECONDS = float(os.environ.get("PYSANDBOX_MAX_TIMEOUT_SECONDS", "60"))

# Address space per child. Deliberately well under the container's own `mem_limit`: hitting
# this raises MemoryError inside the child, which the model sees as an error it can fix,
# whereas hitting the container limit is an OOM kill that reads as an unexplained crash.
MEMORY_LIMIT_MB = int(os.environ.get("PYSANDBOX_MEMORY_LIMIT_MB", "512"))
# CPU seconds — the backstop for the case the wall clock cannot cover, a child that has
# somehow escaped the parent's supervision. Sized above the wall clock so the wall clock is
# normally what fires and the error message is the specific one.
CPU_LIMIT_SECONDS = int(MAX_TIMEOUT_SECONDS) + 5
# A child may not write a file larger than this into its scratch directory — the tmpfs is
# RAM, so an unbounded write is a memory exhaustion by another name.
FILE_SIZE_LIMIT_MB = 32
# Enough for a thread or two; far short of a fork bomb.
PROCESS_LIMIT = 16

MAX_CODE_BYTES = 128_000
MAX_CONCURRENT = int(os.environ.get("PYSANDBOX_MAX_CONCURRENT", "4"))
# How long a call waits for a slot before giving up. Short on purpose: a model that is told
# "busy, try again" recovers, and one left hanging burns the whole turn's wall clock.
QUEUE_TIMEOUT_SECONDS = 5.0

app = FastAPI()
_slots = asyncio.Semaphore(MAX_CONCURRENT)


class RunRequest(BaseModel):
    code: str
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)


class RunResponse(BaseModel):
    """The shape every caller gets, including the failures. `ok` is whether the code ran to
    completion — not whether it did what the model wanted — and `error` is one readable line
    whenever it did not."""

    ok: bool = False
    stdout: str = ""
    stderr: str = ""
    result: str | None = None
    error: str | None = None
    truncated: bool = False
    duration_ms: int = 0


def _apply_limits() -> None:
    """Runs in the forked child, between fork and exec.

    `setsid` first: it is what makes the child a process group of its own, so a timeout can
    kill everything it spawned rather than just the one pid the parent knows about. The
    rlimits after it are inherited through the exec, so they bind the real interpreter and
    not this transient moment."""
    os.setsid()
    limits = [
        (resource.RLIMIT_AS, MEMORY_LIMIT_MB * 1024 * 1024),
        (resource.RLIMIT_CPU, CPU_LIMIT_SECONDS),
        (resource.RLIMIT_FSIZE, FILE_SIZE_LIMIT_MB * 1024 * 1024),
        (resource.RLIMIT_NPROC, PROCESS_LIMIT),
        (resource.RLIMIT_CORE, 0),
    ]
    for which, value in limits:
        resource.setrlimit(which, (value, value))


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group. Not SIGTERM: a snippet that has wedged the
    interpreter — the exact case a timeout catches — cannot be relied on to run a handler,
    and a sandbox that asks nicely is one that leaks processes."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest) -> RunResponse:
    if not request.code.strip():
        return RunResponse(error="ValueError: no code to run")
    if len(request.code.encode("utf-8")) > MAX_CODE_BYTES:
        return RunResponse(
            error=f"ValueError: that snippet is too long (limit {MAX_CODE_BYTES // 1000}KB)"
        )
    timeout = min(request.timeout_seconds, MAX_TIMEOUT_SECONDS)
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
    except TimeoutError:
        return RunResponse(error="BusyError: the sandbox is busy — try again in a moment")
    try:
        return await _execute(request.code, timeout)
    finally:
        _slots.release()


async def _execute(code: str, timeout: float) -> RunResponse:
    started = time.monotonic()
    scratch = tempfile.mkdtemp(prefix="run-", dir=tempfile.gettempdir())
    # Outside the scratch directory the child's filesystem guard confines the code to, so a
    # snippet cannot overwrite the envelope that reports on it.
    result_path = f"{scratch}.json"
    try:
        process = await asyncio.create_subprocess_exec(
            # -I: isolated mode — no user site-packages, no PYTHON* environment, no cwd on
            # sys.path. The last of those matters most: without it a file the snippet wrote
            # into its own scratch directory could shadow a stdlib module on the next call.
            sys.executable,
            "-I",
            RUNNER,
            scratch,
            result_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=scratch,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": scratch},
            preexec_fn=_apply_limits,  # noqa: PLW1509 — the point is that it runs pre-exec
        )
        try:
            await asyncio.wait_for(process.communicate(code.encode("utf-8")), timeout=timeout)
        except TimeoutError:
            await _kill_group(process)
            return RunResponse(
                error=f"TimeoutError: the code ran longer than {timeout:g}s and was stopped",
                duration_ms=_elapsed(started),
            )
        return _read_result(result_path, process.returncode, started)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        Path(result_path).unlink(missing_ok=True)


def _read_result(result_path: str, returncode: int | None, started: float) -> RunResponse:
    """Turn the child's envelope into a response — or, when there is no envelope, say why.

    A missing file means the child died before it could write one, which is the shape of
    every limit that kills rather than raises (the OOM killer, RLIMIT_CPU's SIGXCPU, a
    segfault). `returncode` is what distinguishes them, and the model is told the cause in
    its own terms rather than being handed a signal number."""
    try:
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RunResponse(error=_died(returncode), duration_ms=_elapsed(started))
    return RunResponse(**payload, duration_ms=_elapsed(started))


def _died(returncode: int | None) -> str:
    if returncode == -signal.SIGKILL:
        return f"MemoryError: the code was stopped for using too much memory ({MEMORY_LIMIT_MB}MB limit)"
    if returncode == -signal.SIGXCPU:
        return "TimeoutError: the code used too much CPU time and was stopped"
    if returncode == -signal.SIGSEGV:
        return "RuntimeError: the code crashed the interpreter"
    return "RuntimeError: the code stopped unexpectedly and produced no result"


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
