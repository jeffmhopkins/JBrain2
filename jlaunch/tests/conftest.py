"""Shared fixtures: test job specs registered into the registry, and a JobManager over
tmp dirs. Test specs are built directly (bypassing spec URL validation) and inserted
into SPECS so ``get_spec`` — the manager's lookup — resolves them like a real spec.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.jobs import Job, JobManager
from jlaunch_ctl.specs import JobSpec, Phase
from jlaunch_ctl.terminal import TerminalRegistry

# Marker lines a fake run prints so the headline capture + verify gate have something
# real to match, mirroring the Erdos-Straus output shape.
_HEADLINE = (
    'printf "hard primes: 42\\n"; '
    'printf "max minimal R: 5 at p = 3 (record R=107 stands)\\n"; '
    'printf "R >= 87 counts: {87: 1}\\n"'
)


def _spec(name: str, phases: tuple[Phase, ...]) -> JobSpec:
    return JobSpec(
        name=name,
        title=f"test {name}",
        repo_url="",  # empty -> no clone; phases run in the pre-created workspace dir
        branch="main",
        phases=phases,
        artifact_path="es_1e12_artifacts.tar.gz",
        artifact_media_type="application/gzip",
        verify_log="run_1e12_verify.log",
        verify_must_end_with="VERIFICATION OK",
        headline_markers=("hard primes:", "max minimal R:", "R >= 87 counts:"),
        est_hours="1s",
        disk_gb=1,
        all_cores=False,
        notes="test",
    )


@pytest.fixture
def register_spec(monkeypatch) -> Callable[[str, tuple[Phase, ...]], JobSpec]:
    def _register(name: str, phases: tuple[Phase, ...]) -> JobSpec:
        spec = _spec(name, phases)
        monkeypatch.setitem(specs_mod.SPECS, name, spec)
        return spec

    return _register


@pytest.fixture
def ok_spec(register_spec) -> JobSpec:
    """A fast-succeeding spec: prints the headline, writes the verify log + artifact."""
    body = (
        f"{_HEADLINE}; "
        'printf "VERIFICATION OK\\n" > run_1e12_verify.log; '
        'printf "ARTIFACT-BYTES" > es_1e12_artifacts.tar.gz'
    )
    return register_spec("test_ok", (Phase("gen", body),))


@pytest.fixture
def manager(tmp_path) -> JobManager:
    return JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        TerminalRegistry(),
    )


async def wait_terminal(manager: JobManager, jid: str, timeout: float = 15.0) -> Job:
    """Poll until the job reaches a terminal state (succeeded/failed/killed)."""
    for _ in range(int(timeout / 0.05)):
        job = manager.get(jid)
        if job.state in ("succeeded", "failed", "killed"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"job {jid} did not finish in {timeout}s (state={manager.get(jid).state})"
    )  # noqa: E501
