"""JobManager lifecycle over a REAL PTY: success, verify/artifact failure, stop/kill,
concurrency, reaper-exemption, artifact collection. Fast (each fake job is quick)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from jlaunch_ctl import jobs as jobs_mod
from jlaunch_ctl import specs as specs_mod
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


async def test_a_running_job_reports_its_current_phase(manager, register_spec) -> None:
    phases = (Phase("first", "true"), Phase("second", "sleep 30"))
    register_spec("test_livephase", phases)
    job = await manager.start("test_livephase")
    for _ in range(100):
        if manager.get(job.id).phase == "second":
            break
        await asyncio.sleep(0.05)
    assert manager.get(job.id).phase == "second"
    assert [j.phase for j in manager.list() if j.id == job.id] == ["second"]
    await manager.kill(job.id)
    done = await wait_terminal(manager, job.id)
    assert done.state == "killed"
    assert done.phase == "second"


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


@pytest.fixture
def sharing_spec(register_spec, monkeypatch):
    def _register(name: str) -> None:
        spec = register_spec(name, (Phase("sleep", "sleep 30"),))
        monkeypatch.setitem(
            specs_mod.SPECS, name, dataclasses.replace(spec, shares_box=True)
        )

    return _register


async def _kill_all(manager, ids) -> None:
    for jid in ids:
        await manager.kill(jid)
        await wait_terminal(manager, jid)


async def test_runs_that_share_go_side_by_side_up_to_the_limit(
    manager, sharing_spec
) -> None:
    sharing_spec("test_share_a")
    sharing_spec("test_share_b")
    started = [
        (await manager.start("test_share_a")).id,
        (await manager.start("test_share_b")).id,
        (await manager.start("test_share_a")).id,
    ]
    assert all(manager.get(j).state == "running" for j in started)
    assert "shares_box" not in manager.get(started[0]).public()
    with pytest.raises(BusyError, match="3 jobs"):
        await manager.start("test_share_b")
    await manager.kill(started[0])
    await wait_terminal(manager, started[0])
    started.append((await manager.start("test_share_b")).id)
    await _kill_all(manager, started[1:])


async def test_a_whole_machine_run_never_overlaps_a_shared_one(
    manager, register_spec, sharing_spec
) -> None:
    sharing_spec("test_share")
    register_spec("test_alone", (Phase("sleep", "sleep 30"),))
    shared = await manager.start("test_share")
    with pytest.raises(BusyError, match="whole machine"):
        await manager.start("test_alone")
    await _kill_all(manager, [shared.id])
    alone = await manager.start("test_alone")
    with pytest.raises(BusyError, match="whole machine"):
        await manager.start("test_share")
    await _kill_all(manager, [alone.id])


async def test_a_reloaded_record_keeps_its_claim_on_the_box(
    tmp_path, sharing_spec
) -> None:
    from jlaunch_ctl.jobs import JobManager
    from jlaunch_ctl.terminal import TerminalRegistry

    sharing_spec("test_share_rec")
    roots = [str(tmp_path / d) for d in ("work", "state", "artifacts")]
    first = JobManager(roots[0], roots[1], roots[2], TerminalRegistry())
    job = await first.start("test_share_rec")
    await _kill_all(first, [job.id])
    again = JobManager(roots[0], roots[1], roots[2], TerminalRegistry())
    assert again.get(job.id).shares_box is True


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


def test_job_files_with_bad_bytes_read_without_raising(tmp_path) -> None:
    # A non-UTF-8 byte in a job-written file used to raise out of finalize, leaving the
    # run "running" for good with nothing to kill.
    lines = tmp_path / "headline.txt"
    lines.write_bytes(b"hard primes: \xff\n")
    assert jobs_mod._read_lines(lines) == ["hard primes: \ufffd"]
    status = tmp_path / "status.json"
    status.write_bytes(b"[1, 2]")  # valid JSON, not an object
    assert jobs_mod._read_json(status) is None
    times = tmp_path / "phase_times.jsonl"
    times.write_bytes(b'{"phase": "a", "seconds": 1}\n3\n\xff\n')
    assert jobs_mod._read_jsonl(times) == [{"phase": "a", "seconds": 1}]


async def test_a_failing_finalize_still_ends_the_job(
    manager, register_spec, monkeypatch
) -> None:
    register_spec("test_finboom", (Phase("gen", "true"),))

    def boom(*_a: object) -> object:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(manager, "_collect", boom)
    job = await manager.start("test_finboom")
    done = await wait_terminal(manager, job.id)
    assert done.state == "failed"
    assert "finalize failed: disk on fire" in (done.error or "")


async def test_an_orphaned_run_is_reaped_and_frees_the_slot(
    manager, register_spec
) -> None:
    # The run's terminal is gone but its end was never recorded (close() fires no
    # on_exit): it must not stay "running", unkillable, holding the one-job slot.
    register_spec("test_orphan", (Phase("sleep", "sleep 30"),))
    job = await manager.start("test_orphan")
    manager._registry._terms[job.id].close()
    reaped = manager.get(job.id)
    assert reaped.state == "failed"
    assert "terminal was lost" in (reaped.error or "")
    # The late on_exit finalize (or any other) changes nothing once it's recorded.
    finished_at = reaped.finished_at
    await manager._finalize(job.id, jobs_mod.get_spec("test_orphan"))
    assert (manager.get(job.id).state, manager.get(job.id).finished_at) == (
        "failed",
        finished_at,
    )
    nxt = await manager.start("test_orphan")
    assert manager.get(nxt.id).state == "running"
    await manager.kill(nxt.id)
    assert (await wait_terminal(manager, nxt.id)).state == "killed"


async def test_kill_on_an_orphan_ends_it_as_killed(manager, register_spec) -> None:
    register_spec("test_orphankill", (Phase("sleep", "sleep 30"),))
    job = await manager.start("test_orphankill")
    manager._killing.add(job.id)  # a kill was asked for, then the terminal went
    manager._registry._terms[job.id].close()
    done = await asyncio.wait_for(manager.kill(job.id), timeout=5)
    assert done.state == "killed"


async def test_a_run_whose_spec_is_gone_still_ends(
    manager, register_spec, monkeypatch
) -> None:
    register_spec("test_specgone", (Phase("sleep", "sleep 30"),))
    job = await manager.start("test_specgone")
    monkeypatch.delitem(specs_mod.SPECS, "test_specgone")
    manager._registry._terms[job.id].close()
    done = manager.get(job.id)
    assert done.state == "failed"
    assert "no longer registered" in (done.error or "")


def test_a_fifo_job_file_is_not_read(tmp_path) -> None:
    import os

    fifo = tmp_path / "headline.txt"
    os.mkfifo(fifo)
    assert jobs_mod._read_lines(fifo) == []  # returns at once instead of blocking
