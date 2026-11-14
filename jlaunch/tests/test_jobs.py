"""JobManager lifecycle over a REAL PTY: success, verify/artifact failure, stop/kill,
concurrency, reaper-exemption, artifact collection. Fast (each fake job is quick)."""

from __future__ import annotations

import asyncio
import dataclasses
import os

import pytest

from jlaunch_ctl import jobs as jobs_mod
from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.jobs import BusyError, JobError, JobManager
from jlaunch_ctl.specs import Phase
from jlaunch_ctl.terminal import TerminalRegistry
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


async def test_a_whole_machine_job_runs_alone(manager, register_spec) -> None:
    register_spec("test_busy", (Phase("sleep", "sleep 30"),))
    first = await manager.start("test_busy")
    with pytest.raises(BusyError):
        await manager.start("test_busy")
    # A running job is never swept (no reaper): it's still live and listed.
    assert manager.get(first.id).state == "running"
    assert first.id in {j.id for j in manager.list()}
    # It runs on the whole machine: unpinned.
    assert manager.get(first.id).cpu_slot == -1
    assert "pinned_cpus" not in manager.get(first.id).machine
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


def _pinned(manager, jid: str) -> set[int]:
    return os.sched_getaffinity(manager._registry._terms[jid].pid)


async def test_runs_that_share_go_side_by_side_on_their_own_cores(
    manager, sharing_spec
) -> None:
    for name in ("test_share_a", "test_share_b", "test_share_c", "test_share_d"):
        sharing_spec(name)
    started = [
        (await manager.start(name)).id
        for name in ("test_share_a", "test_share_b", "test_share_c")
    ]
    jobs = [manager.get(j) for j in started]
    assert all(j.state == "running" for j in jobs)
    assert sorted(j.cpu_slot for j in jobs) == [0, 1, 2]
    for job in jobs:
        # Pinned before the job ran, so every process it forks keeps to its share.
        want = jobs_mod.cpu_share(job.cpu_slot, 3)
        assert _pinned(manager, job.id) == want
        assert job.machine["pinned_cpus"] == jobs_mod.cpu_list(want)
        assert job.machine["pinned_cores"] == len(want)
        assert "shares_box" not in job.public() and "cpu_slot" not in job.public()
    with pytest.raises(BusyError, match="3 jobs"):
        await manager.start("test_share_d")
    # The freed share goes to the next run.
    await _kill_all(manager, [started[1]])
    fourth = await manager.start("test_share_d")
    assert fourth.cpu_slot == jobs[1].cpu_slot
    await _kill_all(manager, [started[0], started[2], fourth.id])


async def test_a_run_that_cant_be_pinned_never_starts(
    manager, sharing_spec, monkeypatch
) -> None:
    sharing_spec("test_share_nopin")

    def refuse(_pid: int, _cpus: object) -> None:
        raise OSError("not allowed")

    monkeypatch.setattr(jobs_mod.os, "sched_setaffinity", refuse)
    job = await manager.start("test_share_nopin")
    assert job.state == "failed" and "pin" in (job.error or "")
    assert job.id not in manager._registry._terms
    # It holds no slot: the next run starts.
    monkeypatch.undo()
    sharing_spec("test_share_after")
    after = await manager.start("test_share_after")
    assert after.cpu_slot == 0
    await _kill_all(manager, [after.id])


def test_no_fewer_than_one_slot(tmp_path) -> None:
    roots = [str(tmp_path / d) for d in ("work", "state", "artifacts")]
    manager = JobManager(
        roots[0], roots[1], roots[2], TerminalRegistry(), max_concurrent_jobs=0
    )
    assert manager._free_slot() == 0


async def test_one_live_run_per_job(manager, sharing_spec) -> None:
    sharing_spec("test_share_once")
    first = await manager.start("test_share_once")
    with pytest.raises(BusyError, match="already running"):
        await manager.start("test_share_once")
    await _kill_all(manager, [first.id])


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


def _cores(n: int) -> list[list[int]]:
    # A 2-way SMT machine as Linux numbers it: core i is CPUs i and i + n.
    return [[i, i + n] for i in range(n)]


@pytest.mark.parametrize(("cores", "slots"), [(16, 3), (16, 1), (4, 3), (3, 3)])
def test_cpu_shares_are_disjoint_whole_cores(monkeypatch, cores, slots) -> None:
    monkeypatch.setattr(jobs_mod, "_sibling_groups", lambda _cpus: _cores(cores))
    shares = [jobs_mod.cpu_share(i, slots) for i in range(slots)]
    assert set().union(*shares) == {c for g in _cores(cores) for c in g}
    assert sum(len(s) for s in shares) == 2 * cores  # no CPU in two shares
    for share in shares:
        # Whole cores only: a share never takes one SMT thread of a core.
        assert all((c % cores) + cores in share for c in share if c < cores)
        assert len(share) >= 2 * (cores // slots)


def test_more_shares_than_cores_still_gives_each_a_core(monkeypatch) -> None:
    monkeypatch.setattr(jobs_mod, "_sibling_groups", lambda _cpus: _cores(2))
    assert [jobs_mod.cpu_share(i, 3) for i in range(3)] == [{0, 2}, {1, 3}, {0, 2}]


def test_cpu_list_reads_like_taskset() -> None:
    assert jobs_mod.cpu_list({0, 1, 2, 16, 17, 18, 5}) == "0-2,5,16-18"
    assert jobs_mod.cpu_list({7}) == "7"


async def test_a_reloaded_record_keeps_its_claim_on_the_box(
    tmp_path, sharing_spec
) -> None:
    sharing_spec("test_share_rec")
    roots = [str(tmp_path / d) for d in ("work", "state", "artifacts")]
    first = JobManager(roots[0], roots[1], roots[2], TerminalRegistry())
    job = await first.start("test_share_rec")
    await _kill_all(first, [job.id])
    again = JobManager(roots[0], roots[1], roots[2], TerminalRegistry())
    assert again.get(job.id).shares_box is True
    assert again.get(job.id).cpu_slot == 0


@pytest.mark.parametrize(
    ("shares", "slot"), [("true", "0"), (1, True), (None, -5), (True, 99)]
)
def test_a_record_claims_nothing_by_odd_values(tmp_path, shares, slot) -> None:
    rec = {"spec_name": "x", "state": "succeeded", "shares_box": shares}
    rec["cpu_slot"] = slot
    job = jobs_mod._job_from_record("deadbeef", rec, tmp_path)
    assert job is not None
    assert job.shares_box is (shares is True)
    assert job.cpu_slot == -1


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
