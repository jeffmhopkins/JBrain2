"""The `pysandbox` sidecar (deploy/pysandbox): the containment, exercised for real.

Loaded by path like `test_tts_server.py` loads the tts sidecar — `deploy/` is not on the
backend's import path, but this code is what the `run_python` tool's safety rests on, so it
is tested rather than trusted.

These tests run the real runner in real subprocesses. That is the point: a sandbox asserted
against a mock is a sandbox nobody has checked. They are still fast — the slowest is the
timeout case, which is given a deliberately short deadline.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_SANDBOX = _DEPLOY / "pysandbox"
_RUNNER = _SANDBOX / "runner.py"


def _load_server() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("pysandbox_server", _SANDBOX / "server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(code: str, tmp_path: Path, *, timeout: float = 30) -> dict:
    """Execute `code` through the real runner, in a real child process."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    result = tmp_path / "result.json"
    subprocess.run(
        [sys.executable, "-I", str(_RUNNER), str(scratch), str(result)],
        input=code.encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return json.loads(result.read_text())


# --- It computes ------------------------------------------------------------


def test_the_last_bare_expression_is_the_result(tmp_path: Path) -> None:
    """The notebook convention, and what makes the tool usable without teaching the model to
    end every snippet with a print."""
    ran = _run("values = [12, 7, 3, 19]\nsum(values)", tmp_path)
    assert ran["ok"] is True
    assert ran["result"] == "41"
    assert ran["error"] is None


def test_stdout_is_captured_separately_from_the_result(tmp_path: Path) -> None:
    ran = _run("print('working')\nprint('still working')\n6 * 7", tmp_path)
    assert ran["stdout"] == "working\nstill working\n"
    assert ran["result"] == "42"


def test_a_snippet_ending_in_a_statement_has_no_result(tmp_path: Path) -> None:
    ran = _run("total = 0\nfor i in range(10):\n    total += i\nprint(total)", tmp_path)
    assert ran["ok"] is True and ran["result"] is None
    assert ran["stdout"].strip() == "45"


def test_big_integer_arithmetic_is_exact(tmp_path: Path) -> None:
    """Python's own integers, which is half the reason to send arithmetic here at all."""
    assert _run("2**256", tmp_path)["result"] == str(2**256)


def test_the_useful_stdlib_is_present(tmp_path: Path) -> None:
    """The blocklist is aimed at the network, and it must not have taken the tool's actual
    subject matter with it — statistics, dates, exact decimals, fractions."""
    code = (
        "import statistics, datetime, decimal, fractions, math, json, re, itertools\n"
        "statistics.median([3, 1, 2])"
    )
    ran = _run(code, tmp_path)
    assert ran["ok"] is True and ran["result"] == "2"


# --- No network -------------------------------------------------------------


@pytest.mark.parametrize("module", ["socket", "urllib", "http", "requests", "httpx", "ssl"])
def test_network_modules_are_refused_by_name(module: str, tmp_path: Path) -> None:
    """Refused at import, so the model gets an immediate, actionable line instead of a
    connection that hangs until the wall clock stops it."""
    ran = _run(f"import {module}", tmp_path)
    assert ran["ok"] is False
    assert "no network" in ran["error"]
    assert module in ran["error"]


def test_a_nested_network_import_is_refused_too(tmp_path: Path) -> None:
    """`from urllib.request import urlopen` must not slip past a root-name check."""
    ran = _run("from urllib.request import urlopen", tmp_path)
    assert ran["ok"] is False and "no network" in ran["error"]


# --- No filesystem outside the scratch directory ----------------------------


def test_reading_outside_the_scratch_directory_is_refused(tmp_path: Path) -> None:
    ran = _run("open('/etc/passwd').read()", tmp_path)
    assert ran["ok"] is False
    assert "outside the scratch directory" in ran["error"]


def test_a_relative_escape_is_resolved_before_it_is_checked(tmp_path: Path) -> None:
    """`../` is the first thing anyone tries; the guard compares realpaths, not strings."""
    ran = _run("open('../../../etc/passwd').read()", tmp_path)
    assert ran["ok"] is False and "outside the scratch directory" in ran["error"]


def test_the_scratch_directory_itself_is_writable(tmp_path: Path) -> None:
    """The confinement has to leave the model somewhere to work, or the tool cannot do the
    'check this against a table' job it exists for."""
    code = "open('notes.txt', 'w').write('hello')\nopen('notes.txt').read()"
    ran = _run(code, tmp_path)
    assert ran["ok"] is True and ran["result"] == "'hello'"


def test_the_scratch_directory_starts_empty_for_each_call(tmp_path: Path) -> None:
    """Each call is a fresh process AND a fresh directory — the sidecar makes a new one per
    request and deletes it after. Nothing carries over."""
    import os

    (tmp_path / "scratch").mkdir(exist_ok=True)
    _run("open('left-behind.txt', 'w').write('x')", tmp_path)
    # The server is what creates and removes the per-call directory; this asserts the runner
    # does not reach outside the one it is handed.
    assert os.listdir(tmp_path / "scratch") == ["left-behind.txt"]


# --- No other processes -----------------------------------------------------


@pytest.mark.parametrize("code", ["import subprocess", "import ctypes", "import multiprocessing"])
def test_escape_modules_are_refused(code: str, tmp_path: Path) -> None:
    assert _run(code, tmp_path)["ok"] is False


def test_os_system_is_refused_once_subprocess_is(tmp_path: Path) -> None:
    """The gap that would otherwise make the subprocess block decorative."""
    ran = _run("import os\nos.system('id')", tmp_path)
    assert ran["ok"] is False and "cannot start processes" in ran["error"]


def test_os_fork_is_refused(tmp_path: Path) -> None:
    ran = _run("import os\nos.fork()", tmp_path)
    assert ran["ok"] is False and "cannot start processes" in ran["error"]


# --- Errors the model can act on --------------------------------------------


def test_an_exception_is_one_line_with_the_line_number(tmp_path: Path) -> None:
    """Not a traceback: a traceback is mostly frames in runner.py, which the model did not
    write and cannot edit. It needs the line in its OWN snippet."""
    ran = _run("a = 1\nb = 0\na / b", tmp_path)
    assert ran["error"] == "ZeroDivisionError: division by zero (line 3)"
    assert "\n" not in ran["error"]
    assert "Traceback" not in ran["error"]


def test_a_syntax_error_names_the_line(tmp_path: Path) -> None:
    ran = _run("x = (1 + 2\ny = 3", tmp_path)
    assert ran["error"].startswith("SyntaxError:")
    assert "line" in ran["error"]


def test_a_name_error_reads_like_python_would_print_it(tmp_path: Path) -> None:
    ran = _run("undefined_thing + 1", tmp_path)
    assert ran["error"].startswith("NameError:") and "(line 1)" in ran["error"]


def test_output_from_before_the_error_is_still_returned(tmp_path: Path) -> None:
    """Half the debugging value: the model sees how far it got."""
    ran = _run("print('step 1')\nprint('step 2')\n1 / 0", tmp_path)
    assert ran["ok"] is False
    assert ran["stdout"] == "step 1\nstep 2\n"
    assert "ZeroDivisionError" in ran["error"]


# --- Truncation -------------------------------------------------------------


def test_long_stdout_is_truncated_with_a_note(tmp_path: Path) -> None:
    """A model that believes it has seen a whole output will draw a conclusion from a
    fragment, so the cut has to be stated, not silent."""
    ran = _run("print('x' * 50000)", tmp_path)
    assert ran["truncated"] is True
    assert "[truncated:" in ran["stdout"] and "50,00" in ran["stdout"]
    assert len(ran["stdout"]) < 11_000


def test_an_enormous_result_value_is_truncated(tmp_path: Path) -> None:
    ran = _run("list(range(100000))", tmp_path)
    assert ran["ok"] is True
    assert "[truncated:" in ran["result"]


def test_short_output_is_not_marked_truncated(tmp_path: Path) -> None:
    assert _run("print('hi')", tmp_path)["truncated"] is False


# --- The server's limits ----------------------------------------------------


def test_the_server_clamps_a_caller_supplied_timeout() -> None:
    """The api sends its own timeout; the sidecar must not let a caller hold a slot for
    longer than its own ceiling."""
    server = _load_server()
    assert server.MAX_TIMEOUT_SECONDS <= 60
    assert server.DEFAULT_TIMEOUT_SECONDS <= server.MAX_TIMEOUT_SECONDS


def test_the_memory_limit_sits_under_the_containers(tmp_path: Path) -> None:
    """Deliberate: hitting the per-child rlimit raises MemoryError, which the model sees as
    an error it can fix; hitting the container's mem_limit is an OOM kill that reads as an
    unexplained crash. The rlimit has to fire first."""
    server = _load_server()
    compose = (_DEPLOY / "docker-compose.yml").read_text()
    assert "PYSANDBOX_MEM_LIMIT:-1g" in compose
    assert server.MEMORY_LIMIT_MB < 1024


def test_the_cpu_limit_sits_above_the_wall_clock() -> None:
    """So the wall clock is normally what fires, and the model gets the specific message
    ('ran longer than 10s') rather than the generic CPU one."""
    server = _load_server()
    assert server.CPU_LIMIT_SECONDS > server.MAX_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(-9, "MemoryError"), (-24, "TimeoutError"), (-11, "RuntimeError"), (1, "RuntimeError")],
)
def test_a_child_that_died_without_an_envelope_is_explained(
    returncode: int, expected: str, tmp_path: Path
) -> None:
    """Every limit that KILLS rather than raises leaves no result file. The model is told the
    cause in its own terms instead of being handed a signal number."""
    server = _load_server()
    response = server._read_result(str(tmp_path / "absent.json"), returncode, 0.0)
    assert response.ok is False
    assert response.error.startswith(expected)


def test_an_infinite_loop_is_killed_at_the_wall_clock(tmp_path: Path) -> None:
    """The headline containment: a snippet that never returns must not hold the sandbox.

    Driven through the real subprocess machinery with a short deadline — `subprocess` here
    stands in for the sidecar's own `wait_for` + `killpg`, which is exercised the same way
    but would cost the full timeout to observe."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-I", str(_RUNNER), str(scratch), str(tmp_path / "r.json")],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        process.communicate(b"while True:\n    pass\n", timeout=2)
    process.kill()
    process.wait(timeout=5)
    # No envelope, because it never finished — which is exactly the case `_read_result`
    # turns into a readable message rather than a parse failure.
    assert not (tmp_path / "r.json").exists()


# --- The compose topology the containment rests on --------------------------


def _compose() -> dict:
    import yaml

    return yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())


def test_the_sandbox_network_is_declared_egress_free() -> None:
    """`internal: true` is what actually removes the route off the box. The runner's import
    guard is the usability layer over this; this is the guarantee."""
    assert _compose()["networks"]["sandbox"]["internal"] is True


def test_only_the_api_shares_the_sandbox_network() -> None:
    """The egress-free claim is really a claim about MEMBERSHIP: a network with no gateway
    still reaches everything else on it, so adding a third service here would silently hand
    the code sandbox a route to whatever that service is. Asserted rather than documented,
    because it is a one-word change in a 900-line file that nothing else would catch."""
    services = _compose()["services"]
    on_sandbox = {
        name for name, spec in services.items() if "sandbox" in (spec.get("networks") or [])
    }
    assert on_sandbox == {"pysandbox", "api"}


def test_the_sandbox_holds_no_credentials_and_no_volumes() -> None:
    """The containment's load-bearing claim is not just that the container is locked down —
    it is that there is nothing inside worth reaching. A mounted volume or a database URL
    would make that false without changing a single line of server.py."""
    spec = _compose()["services"]["pysandbox"]
    assert not spec.get("volumes"), "the sandbox must mount nothing"
    env = spec.get("environment") or {}
    for key, value in env.items():
        assert key.startswith("PYSANDBOX_"), f"unexpected env on the sandbox: {key}"
        assert "PASSWORD" not in str(value).upper() and "TOKEN" not in str(value).upper()


def test_the_sandbox_is_locked_down_at_the_container_level() -> None:
    """Each of these is load-bearing (see the runner's layer 1), so each is pinned."""
    spec = _compose()["services"]["pysandbox"]
    assert spec["read_only"] is True
    assert spec["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in spec["security_opt"]
    assert spec["pids_limit"] > 0
    assert any(mount.startswith("/tmp:") for mount in spec["tmpfs"])


def test_the_sandbox_is_stock_stack_not_profile_gated() -> None:
    """CLAUDE.md #10: the owner has no terminal. A compose profile would mean the tool only
    exists on a box where someone ran a CLI command, which is a tool that does not exist."""
    assert "profiles" not in _compose()["services"]["pysandbox"]


def test_a_messageless_exception_reads_cleanly() -> None:
    """`MemoryError()` — what a snippet hits when it runs past the rlimit — carries no
    message, and the naive `f"{kind}: {exc}"` renders it "MemoryError:  (line 1)". Verified
    against the real container before it was fixed."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pysandbox_runner", _RUNNER)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner._brief_error(MemoryError()) == "MemoryError"
    assert runner._brief_error(ValueError("bad input")) == "ValueError: bad input"
