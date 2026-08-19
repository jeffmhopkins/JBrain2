"""The ffmpeg install step in CI, which is the slowest thing in the pipeline.

On 2026-08-19 it took 21m10s while the tests it exists for took 2m36s. The cause was not
size: `Fetched 62.8 MB in 20min 54s (50.1 kB/s)` off azure.archive.ubuntu.com, seconds
after the index fetch from the SAME host ran at 5400 kB/s. The pool is load-balanced and
one backend was degraded.

The mitigation in place at the time -- `Acquire::Retries` plus `Acquire::*::Timeout` --
could not have helped: that timeout measures INACTIVITY, and a backend dribbling bytes is
never inactive. What works is a wall-clock cap that forces a reconnect. These tests pin
the shape of that fix, and pin the two copies of it together.
"""

import re
import shutil
import subprocess
from pathlib import Path

import yaml

_CI = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


def _ffmpeg_steps() -> dict[str, str]:
    """The `run:` body of every step that installs ffmpeg, keyed by job name."""
    workflow = yaml.safe_load(_CI.read_text())
    found = {}
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps") or []:
            if "ffmpeg" in step.get("run", ""):
                found[job_name] = step["run"]
    return found


def test_both_backend_jobs_install_ffmpeg_identically() -> None:
    """Byte-identical, because they are two copies of one decision.

    Every drift this repo has hit came from copies kept in step by a comment rather than
    by a test -- the deploy scripts did exactly this. A cap that lands on the unit shards
    but not on the integration job would leave the slowest job the unprotected one."""
    steps = _ffmpeg_steps()
    assert set(steps) == {"backend-unit", "backend-integration"}, sorted(steps)
    assert len(set(steps.values())) == 1, "the two ffmpeg install steps have drifted"


def test_the_download_is_capped_and_retried() -> None:
    """A cap with no retry is just a failure, and a retry with no cap is what we had."""
    body = next(iter(_ffmpeg_steps().values()))
    assert "--download-only" in body, (
        "the cap must apply to the download only -- killing apt during unpack/configure "
        "leaves a broken dpkg database"
    )
    capped = re.search(r"timeout (\d+) apt-get", body)
    assert capped, "no wall-clock cap on the fetch"
    # Long enough that a healthy mirror (~40s for this package set) never trips it.
    assert 60 <= int(capped.group(1)) <= 300, capped.group(1)
    assert re.search(r"for attempt in [\d ]+; do", body), "no retry loop"


def test_the_cap_runs_root_side_and_not_on_a_shell_function() -> None:
    """`timeout` is a binary: it cannot invoke a shell function, and under plain `sudo` it
    would signal sudo rather than apt.

    The first draft of this fix wrapped apt in an `apt_get()` helper and wrote
    `timeout 120 apt_get ...`, which fails instantly with 'No such file or directory',
    burns every attempt, and silently falls through to the uncapped install -- a change
    that looks like a fix and does nothing."""
    body = next(iter(_ffmpeg_steps().values()))
    assert "sudo timeout" in body, "timeout must run under sudo so it can signal apt-get"
    assert not re.search(r"timeout \d+ [a-z_]+\(\)", body)
    assert not re.search(r"timeout \d+ apt_get\b", body), (
        "`timeout` is being handed a shell function name; it can only exec a real binary"
    )


def test_a_degraded_mirror_still_ends_green() -> None:
    """The fallback matters as much as the cap. If every reconnect also lands on a slow
    backend the step must degrade to the old slow-but-successful behaviour, not fail --
    turning a 20-minute green into a red is not an improvement."""
    body = next(iter(_ffmpeg_steps().values()))
    # Comments stripped: they discuss `timeout` at length, and the claim here is about
    # what the shell actually runs after the loop.
    tail = "\n".join(
        line for line in body.rsplit("done", 1)[1].splitlines() if not line.strip().startswith("#")
    )
    assert "apt-get" in tail and "install ffmpeg" in tail, tail
    assert "timeout" not in tail, "the last-resort install must not be capped"


def test_the_step_is_valid_shell() -> None:
    """It runs under `bash -e`, where an unnoticed syntax error is a red pipeline."""
    bash = shutil.which("bash")
    assert bash
    body = next(iter(_ffmpeg_steps().values()))
    result = subprocess.run([bash, "-n"], input=body, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
