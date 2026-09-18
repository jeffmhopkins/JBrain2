"""The child process that actually runs the model's code. One call, one process, one exit.

Launched by `server.py` as `python -I runner.py <scratch-dir> <result-path>`, with the code
on stdin. It is a separate FILE and a separate PROCESS on purpose: the server has to survive
whatever this does, and the only way to be sure of that is for "whatever this does" to happen
somewhere the server can kill.

**What bounds it, outermost first.** Each layer assumes the ones inside it will be defeated:

1. The container. `internal: true` network (no route off the box), `read_only` root
   filesystem, all capabilities dropped, `no-new-privileges`, non-root user, `mem_limit`
   and `pids_limit` — and nothing inside it worth reaching: no owner data, no database
   credentials, no blob volume, no Docker socket.
2. The process. rlimits set by the parent before exec (address space, CPU seconds, file
   size, process count) and its own session, so a wall-clock timeout kills the whole
   process GROUP rather than a parent that has forked away from it.
3. This file. The import hook and socket guard below.

Layer 3 is the weakest and is not pretended otherwise: a determined escape through `ctypes`
or a raw syscall gets past it. It is here because it turns the ordinary case — a model that
reflexively writes `import requests` — into a clear, immediate error message it can act on,
instead of a mysterious timeout thirty seconds later. The guarantee lives in layers 1 and 2.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

# Captured before the guards below replace it. `_write` has to reach a path OUTSIDE the
# scratch directory, which is exactly what `_install_filesystem_guard` exists to stop.
_REAL_OPEN = builtins.open

MAX_STREAM_CHARS = 10_000
MAX_RESULT_CHARS = 2_000

# Modules that exist only to reach the network. Blocked by name at import so the failure is
# "no network access in this sandbox" rather than a connection that hangs until the timeout.
_BLOCKED_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib2",
        "urllib3",
        "httplib",
        "ftplib",
        "telnetlib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "asyncio",
        "socketserver",
        "xmlrpc",
        "requests",
        "httpx",
        "aiohttp",
        "webbrowser",
        "ctypes",
        "multiprocessing",
        "subprocess",
    }
)


class SandboxError(Exception):
    """Raised inside the child for something the sandbox refuses outright."""


def _install_import_guard() -> None:
    """Refuse the networking and escape modules at import, with a message that says what to
    do instead. `ctypes` and `subprocess` are here as escape hatches out of layer 3, not
    because they reach the network."""
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        root = name.split(".")[0]
        if root in _BLOCKED_MODULES:
            raise SandboxError(
                f"{root!r} is not available: this sandbox has no network and no subprocesses. "
                "Compute from the data in your code instead."
            )
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded


def _install_filesystem_guard(scratch: str) -> None:
    """Confine `open` to the scratch directory.

    The container's root filesystem is already read-only, so this is about READS: the model
    should not be rummaging through the image, and — far more usefully — it should be TOLD
    that when it tries, rather than reading an empty file and reasoning from it. Paths are
    resolved before comparison so `../` cannot walk out."""
    real_open = builtins.open
    root = os.path.realpath(scratch)

    def guarded_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        # An already-open file descriptor (a numeric `file`) is not a path and is how
        # `print` and friends reach the captured streams.
        if isinstance(file, int):
            return real_open(file, *args, **kwargs)
        resolved = os.path.realpath(os.fspath(file))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise SandboxError(
                f"no filesystem access outside the scratch directory ({scratch}). "
                f"Refused: {os.fspath(file)}"
            )
        return real_open(file, *args, **kwargs)

    builtins.open = guarded_open


def _install_process_guard() -> None:
    """Close the process-spawning doors `os` leaves open once `subprocess` is blocked.

    `RLIMIT_NPROC` already makes a fork bomb fail, so this is not the containment — it is
    the difference between a model learning "no subprocesses here" and one discovering that
    `subprocess` is blocked but `os.system` is not, and building on that."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise SandboxError("this sandbox cannot start processes — compute it in Python instead")

    for name in dir(os):
        if name.startswith(("exec", "spawn", "posix_spawn", "fork")) or name in {
            "system",
            "popen",
            "kill",
            "killpg",
        }:
            setattr(os, name, refuse)


def _truncate(text: str, limit: int, what: str) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    note = f"\n… [truncated: {what} was {len(text):,} characters, showing the first {limit:,}]"
    return text[:limit] + note, True


def _brief_error(exc: BaseException) -> str:
    """One readable line, from the deepest frame that is the MODEL'S code.

    A full traceback through this runner tells the model about `runner.py`, which it cannot
    edit and did not write. The line number it needs is the one in its own snippet, so the
    frames are filtered to the executed module and the answer is shaped like the error
    Python itself would print at a prompt.

    `walk_tb`, NOT `extract_tb`: extract_tb resolves each frame's SOURCE LINE through
    `linecache`, which opens the file — and this runs with the filesystem guard installed,
    which refuses it. The result was that reporting an error raised an error, and every
    refusal the guards produced died on the way out instead of reaching the model. Walking
    the traceback reads only the code objects already in memory."""
    kind = type(exc).__name__
    if isinstance(exc, SyntaxError) and exc.lineno:
        return f"{kind}: {exc.msg} (line {exc.lineno})"
    lines = [
        lineno
        for frame, lineno in traceback.walk_tb(exc.__traceback__)
        if frame.f_code.co_filename == "<code>"
    ]
    where = f" (line {lines[-1]})" if lines else ""
    # Several of the exceptions that matter most here carry no message at all —
    # `MemoryError()` is the one a snippet hits when it runs past the rlimit — and
    # `f"{kind}: {exc}"` renders those as "MemoryError:  (line 1)", with a stray colon and a
    # double space. The type alone is the whole error in that case.
    detail = str(exc).strip()
    return f"{kind}: {detail}{where}" if detail else f"{kind}{where}"


def _split_final_expression(tree: ast.Module) -> tuple[ast.Module, ast.Expression | None]:
    """Peel a trailing bare expression off so its value can be reported.

    This is the notebook convention, and it is what makes the tool usable without teaching
    the model to end every snippet with a `print`. A body that ends in a statement (a loop,
    an assignment) simply has no final value, which is reported as such."""
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        final = ast.Expression(body=tree.body[-1].value)  # type: ignore[attr-defined]
        return ast.Module(body=tree.body[:-1], type_ignores=[]), ast.copy_location(
            final, tree.body[-1]
        )
    return tree, None


def main() -> int:
    scratch, result_path = sys.argv[1], sys.argv[2]
    code = sys.stdin.read()

    out, err = io.StringIO(), io.StringIO()
    # Every key present from the start, so a caller never has to distinguish "no error" from
    # "the envelope did not carry that field".
    payload: dict[str, object] = {
        "ok": False,
        "stdout": "",
        "stderr": "",
        "result": None,
        "error": None,
        "truncated": False,
    }

    try:
        tree = ast.parse(code, filename="<code>", mode="exec")
    except SyntaxError as exc:
        payload["error"] = _brief_error(exc)
        _write(result_path, payload)
        return 0

    body, final = _split_final_expression(tree)
    # A fresh namespace, not this module's: the model should not be able to see or rebind
    # the guards, and `__name__ == "__main__"` is what a snippet expects.
    namespace: dict[str, object] = {"__name__": "__main__", "__builtins__": builtins}

    # The scratch directory is the working directory, so a bare `open("data.csv")` in the
    # model's code lands inside the one place it is allowed to write.
    os.chdir(scratch)
    _install_import_guard()
    _install_filesystem_guard(scratch)
    _install_process_guard()

    try:
        with redirect_stdout(out), redirect_stderr(err):
            exec(compile(body, "<code>", "exec"), namespace)  # noqa: S102 — this IS the sandbox
            if final is not None:
                value = eval(compile(final, "<code>", "eval"), namespace)  # noqa: S307
                if value is not None:
                    payload["result"] = _truncate(repr(value), MAX_RESULT_CHARS, "the value")[0]
        payload["ok"] = True
    except BaseException as exc:  # noqa: BLE001 — SystemExit/KeyboardInterrupt are results too
        payload["error"] = _brief_error(exc)

    stdout_text, out_cut = _truncate(out.getvalue(), MAX_STREAM_CHARS, "stdout")
    stderr_text, err_cut = _truncate(err.getvalue(), MAX_STREAM_CHARS, "stderr")
    payload["stdout"] = stdout_text
    payload["stderr"] = stderr_text
    payload["truncated"] = out_cut or err_cut
    _write(result_path, payload)
    return 0


def _write(path: str, payload: dict[str, object]) -> None:
    # To a FILE, not to stdout: the child's stdout belongs to the model's code, and a `print`
    # racing the result envelope is how a sandbox starts returning unparseable answers. Via
    # the pre-guard `open`, because the envelope is deliberately written where the model's
    # code cannot reach it.
    with _REAL_OPEN(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


if __name__ == "__main__":
    sys.exit(main())
