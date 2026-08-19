"""CI cannot be allowed to run long, and this is what enforces it.

Two runs on main burned SIX HOURS each before GitHub's 360-minute default killed them --
`backend-unit (2)` on 2026-08-18 sat on `apt-get install ffmpeg` from 20:37 to 02:37. A
third took 21m10s on the same step (`Fetched 62.8 MB in 20min 54s (50.1 kB/s)`) for a job
whose tests take 2m36s.

The install itself is hardened in .github/actions/setup-ffmpeg, but hardening one step
only fixes the step we already know about. What makes "CI is never long" true regardless
of cause is a declared ceiling on every job, and a test that refuses to let a new job ship
without one -- a policy nobody has to remember.
"""

from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"

# GitHub's default when a job declares nothing. Six hours of runner time, spent silently.
_GITHUB_DEFAULT_MINUTES = 360
# Nothing this repo does legitimately takes an hour; the slowest job observed is ~6m.
_MAX_SANE_MINUTES = 60


def _workflows() -> dict[str, dict]:
    return {f.name: yaml.safe_load(f.read_text()) for f in sorted(_WORKFLOWS.glob("*.yml"))}


def test_every_job_declares_a_ceiling() -> None:
    """The whole point. A job with no `timeout-minutes` inherits 360, which is not a
    timeout so much as an invitation."""
    missing = [
        f"{name}:{job}"
        for name, wf in _workflows().items()
        for job, spec in (wf.get("jobs") or {}).items()
        if "timeout-minutes" not in spec
    ]
    assert not missing, (
        f"these jobs inherit GitHub's {_GITHUB_DEFAULT_MINUTES}-minute default: {missing}. "
        "Add `timeout-minutes:` sized from the job's observed duration."
    )


def test_no_ceiling_is_so_loose_it_stops_being_one() -> None:
    """A 300-minute 'timeout' satisfies the check above while changing nothing."""
    for name, wf in _workflows().items():
        for job, spec in (wf.get("jobs") or {}).items():
            budget = spec["timeout-minutes"]
            assert isinstance(budget, int), f"{name}:{job} budget is not a number: {budget!r}"
            assert 0 < budget <= _MAX_SANE_MINUTES, (
                f"{name}:{job} allows {budget}m; nothing here needs more than "
                f"{_MAX_SANE_MINUTES}m, so this is a hang waiting to be billed"
            )


def test_superseded_runs_are_cancelled_on_pull_requests() -> None:
    """Pushing twice to a PR should not leave the first run grinding against a commit
    nobody will merge. Scoped to pull_request deliberately: a push to main publishes
    images and must be allowed to finish."""
    for name, wf in _workflows().items():
        concurrency = wf.get("concurrency")
        assert concurrency, f"{name} has no concurrency group, so superseded runs keep going"
        assert "github.ref" in concurrency["group"], concurrency["group"]
        cancel = str(concurrency["cancel-in-progress"])
        assert "pull_request" in cancel, (
            f"{name} cancels on {cancel!r}; cancelling a main/tag run can abort an "
            "image publish midway"
        )


def test_ffmpeg_is_installed_from_one_shared_action() -> None:
    """It used to be two identical inline blocks kept in step by a comment -- the exact
    arrangement behind every drift this repo has hit. A composite action cannot drift
    from itself, so this pins that it stays one copy rather than testing two for
    equality."""
    ci = _workflows()["ci.yml"]
    users = {
        job
        for job, spec in ci["jobs"].items()
        for step in spec.get("steps") or []
        if "setup-ffmpeg" in str(step.get("uses", ""))
    }
    assert users == {"backend-unit", "backend-integration"}, sorted(users)
    body = (_WORKFLOWS.parent / "actions" / "setup-ffmpeg" / "action.yml").read_text()
    assert "apt-get" not in "".join(
        line
        for line in _WORKFLOWS.joinpath("ci.yml").read_text().splitlines()
        if not line.strip().startswith("#")
    ), "an inline apt-get has reappeared in ci.yml; it belongs in the shared action"
    assert "sudo timeout" in body, "the download cap is gone"
    assert "--download-only" in body, (
        "the cap must apply to the download only -- killing apt during unpack leaves a "
        "broken dpkg database"
    )
    assert "actions/cache" in body, "the .deb cache is gone; every run pays the mirror again"
