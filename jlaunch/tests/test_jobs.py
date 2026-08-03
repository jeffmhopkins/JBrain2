"""JobManager lifecycle over a REAL PTY: success, verify/artifact failure, stop/kill,
concurrency, reaper-exemption, artifact collection. Fast (each fake job is quick)."""

from __future__ import annotations

import pytest

from jlaunch_ctl.jobs import BusyError, JobError
from jlaunch_ctl.specs import Phase
from tests.conftest import wait_terminal


async def test_success_headline_and_artifact(manager, ok_spec) -> None:
    job = await manager.start("test_ok")
    assert job.state in ("running", "cloning")
    done = await wait_terminal(manager, job.id)

    assert done.state == "succeeded"
    assert done.verify_ok is True
    assert done.artifact_present is True
    assert done.artifact_bytes == len(b"ARTIFACT-BYTES")
    assert any("hard primes: 42" in line for line in done.headline)
    assert any("record R=107 stands" in line for line in done.headline)
    assert done.finished_at is not None
    # The deliverable is copied out of the checkout and is retrievable.
    assert manager.artifact_path(job.id).read_bytes() == b"ARTIFACT-BYTES"
    assert done.machine.get("cores")


async def test_verify_failure_is_failed_not_succeeded(manager, register_spec) -> None:
    # Writes the artifact but a verify log without the sentinel -> the gate fails.
    body = (
        'printf "hard primes: 1\\n"; '
        'printf "VERIFICATION FAILED\\n" > run_1e12_verify.log; '
        'printf "x" > es_1e12_artifacts.tar.gz'
    )
    register_spec("test_verifyfail", (Phase("gen", body),))
    job = await manager.start("test_verifyfail")
    done = await wait_terminal(manager, job.id)
    assert done.state == "failed"
    assert done.verify_ok is False


async def test_missing_artifact_is_failed(manager, register_spec) -> None:
    body = 'printf "VERIFICATION OK\\n" > run_1e12_verify.log'  # no artifact written
    register_spec("test_noartifact", (Phase("gen", body),))
    job = await manager.start("test_noartifact")
    done = await wait_terminal(manager, job.id)
    assert done.state == "failed"
    assert done.artifact_present is False


async def test_phase_failure_records_failing_phase(manager, register_spec) -> None:
    register_spec("test_phasefail", (Phase("first", "true"), Phase("boom", "exit 7")))
    job = await manager.start("test_phasefail")
    done = await wait_terminal(manager, job.id)
    assert done.state == "failed"
    assert done.phase == "boom"


async def test_stop_marks_killed(manager, register_spec) -> None:
    register_spec("test_stop", (Phase("sleep", "sleep 30"),))
    job = await manager.start("test_stop")
    await manager.stop(job.id)
    done = await wait_terminal(manager, job.id)
    assert done.state == "killed"


async def test_kill_marks_killed(manager, register_spec) -> None:
    register_spec("test_kill", (Phase("sleep", "sleep 30"),))
    job = await manager.start("test_kill")
    await manager.kill(job.id)
    done = await wait_terminal(manager, job.id)
    assert done.state == "killed"


async def test_max_concurrent_jobs_blocks_second(manager, register_spec) -> None:
    register_spec("test_busy", (Phase("sleep", "sleep 30"),))
    first = await manager.start("test_busy")
    with pytest.raises(BusyError):
        await manager.start("test_busy")
    # A running job is never swept (no reaper): it's still live and listed.
    assert manager.get(first.id).state == "running"
    assert first.id in {j.id for j in manager.list()}
    await manager.kill(first.id)
    await wait_terminal(manager, first.id)
    # After it ends a new job may start.
    second = await manager.start("test_busy")
    assert second.id != first.id
    await manager.kill(second.id)
    await wait_terminal(manager, second.id)


async def test_unknown_spec_raises(manager) -> None:
    with pytest.raises(JobError):
        await manager.start("does_not_exist")


async def test_delete_removes_job_and_files(manager, ok_spec) -> None:
    job = await manager.start("test_ok")
    await wait_terminal(manager, job.id)
    await manager.delete(job.id)
    with pytest.raises(JobError):
        manager.get(job.id)
    # Idempotent.
    await manager.delete(job.id)


async def test_delete_kills_running_job(manager, register_spec) -> None:
    register_spec("test_delrun", (Phase("sleep", "sleep 30"),))
    job = await manager.start("test_delrun")
    await manager.delete(job.id)
    with pytest.raises(JobError):
        manager.get(job.id)
