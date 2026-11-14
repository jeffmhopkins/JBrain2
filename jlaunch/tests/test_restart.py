"""A restart of the control server (every Ops -> Update) keeps the finished runs: their
record is read back from the state folder, so the result and the live folder (a training
run's 3D replay) still open. The record is in a folder the job can write, so it is read
as data: it never decides a path."""

from __future__ import annotations

import dataclasses
import json

import pytest

from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.jobs import JobError, JobManager
from jlaunch_ctl.specs import Phase
from jlaunch_ctl.terminal import TerminalRegistry
from tests.conftest import _spec, wait_terminal

_LIVE = (
    "mkdir -p out/live; "
    'printf "<p>replay</p>" > out/live/replay.html; '
    'printf "VERIFICATION OK\\n" > run_1e12_verify.log; '
    'printf "ARTIFACT-BYTES" > es_1e12_artifacts.tar.gz'
)


def _manager(tmp_path) -> JobManager:
    return JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        TerminalRegistry(),
    )


@pytest.fixture
def live_spec(monkeypatch):
    spec = dataclasses.replace(
        _spec("test_keep", (Phase("gen", _LIVE),)), live_dir="out/live"
    )
    monkeypatch.setitem(specs_mod.SPECS, spec.name, spec)
    return spec


async def test_a_finished_run_survives_a_restart(tmp_path, live_spec) -> None:
    first = _manager(tmp_path)
    job = await first.start("test_keep")
    done = await wait_terminal(first, job.id)
    assert done.state == "succeeded"

    again = _manager(tmp_path)
    kept = again.get(job.id)
    assert kept.public() == done.public()
    assert [f["name"] for f in again.live_files(job.id)] == ["replay.html"]
    assert again.read_live(job.id, "replay.html")[0] == b"<p>replay</p>"
    assert again.artifact_path(job.id).read_bytes() == b"ARTIFACT-BYTES"


async def test_its_files_still_open_once_its_spec_is_gone(
    tmp_path, live_spec, monkeypatch
) -> None:
    first = _manager(tmp_path)
    job = await first.start("test_keep")
    await wait_terminal(first, job.id)
    monkeypatch.delitem(specs_mod.SPECS, "test_keep")

    again = _manager(tmp_path)
    assert again.read_live(job.id, "replay.html")[0] == b"<p>replay</p>"
    assert again.artifact_path(job.id).read_bytes() == b"ARTIFACT-BYTES"
    assert again.artifact_name(job.id) == "es_1e12_artifacts.tar.gz"


async def test_a_run_live_at_the_restart_is_reaped_not_left_running(
    tmp_path, register_spec
) -> None:
    register_spec("test_cut", (Phase("sleep", "sleep 30"),))
    first = _manager(tmp_path)
    job = await first.start("test_cut")
    first.close_all()

    again = _manager(tmp_path)
    lost = again.get(job.id)
    assert lost.state == "failed"
    assert "terminal was lost" in (lost.error or "")
    # Recorded as ended, so a third start reads it the same way.
    assert _manager(tmp_path).get(job.id).state == "failed"


async def test_delete_forgets_it_for_good(tmp_path, live_spec) -> None:
    first = _manager(tmp_path)
    job = await first.start("test_keep")
    await wait_terminal(first, job.id)
    await first.delete(job.id)
    with pytest.raises(JobError):
        _manager(tmp_path).get(job.id)


def test_a_planted_record_cannot_point_outside_its_checkout(
    tmp_path, live_spec
) -> None:
    # The job wrote its own record: another workspace, a live folder up and out, an
    # absolute artifact path, a spec that isn't registered.
    jid = "deadbeef"
    state = tmp_path / "state" / jid
    state.mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "replay.html").write_text("secret")
    (state / "job.json").write_text(
        json.dumps(
            {
                "id": "cafebabe",
                "spec_name": "not_registered",
                "state": "succeeded",
                "workspace": str(tmp_path / "secret"),
                "live_dir": "../../secret",
                "artifact_path": str(tmp_path / "secret" / "replay.html"),
                "artifact_present": True,
            }
        )
    )
    manager = _manager(tmp_path)
    job = manager.get(jid)
    assert job.id == jid
    assert job.workspace == str(tmp_path / "work" / jid)
    with pytest.raises(JobError):
        manager.live_files(jid)
    with pytest.raises(JobError):
        manager.artifact_path(jid)


def test_only_job_folders_with_a_readable_record_load(tmp_path) -> None:
    state = tmp_path / "state"
    for name, body in [
        ("policies", '{"spec_name": "x", "state": "succeeded"}'),  # not a job id
        ("0123abcd", "not json"),
        ("89abcdef", '{"state": "succeeded"}'),  # no spec
        ("fedcba98", '["a list"]'),
    ]:
        (state / name).mkdir(parents=True)
        (state / name / "job.json").write_text(body)
    (state / "catalog.json").write_text("{}")
    assert _manager(tmp_path).list() == []


async def test_a_changed_spec_does_not_move_an_old_runs_files(
    tmp_path, live_spec, monkeypatch
) -> None:
    first = _manager(tmp_path)
    job = await first.start("test_keep")
    await wait_terminal(first, job.id)
    moved = dataclasses.replace(
        live_spec, live_dir="elsewhere", artifact_path="new.tar.gz"
    )
    monkeypatch.setitem(specs_mod.SPECS, "test_keep", moved)

    again = _manager(tmp_path)
    assert again.read_live(job.id, "replay.html")[0] == b"<p>replay</p>"
    assert again.artifact_path(job.id).read_bytes() == b"ARTIFACT-BYTES"
    await again.delete(job.id)
    assert list((tmp_path / "artifacts").iterdir()) == []


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 100_000,  # nesting deep enough to raise RecursionError
        b'{"n": ' + b"9" * 5000 + b"}",  # past Python's int-digit limit: ValueError
        b'{"spec_name": "x", "state": "succeeded", "pad": "' + b"x" * (2 << 20) + b'"}',
    ],
)
def test_a_hostile_record_never_stops_the_server_starting(tmp_path, body) -> None:
    folder = tmp_path / "state" / "0badf00d"
    folder.mkdir(parents=True)
    (folder / "job.json").write_bytes(body)
    assert _manager(tmp_path).list() == []


def test_a_fifo_or_symlink_record_is_not_read(tmp_path) -> None:
    import os

    real = tmp_path / "real.json"
    real.write_text('{"spec_name": "x", "state": "succeeded"}')
    a = tmp_path / "state" / "0000000a"
    b = tmp_path / "state" / "0000000b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    os.mkfifo(a / "job.json")
    (b / "job.json").symlink_to(real)
    assert _manager(tmp_path).list() == []


async def test_saving_never_follows_or_blocks_on_a_planted_temp_file(
    tmp_path, register_spec
) -> None:
    import asyncio
    import os

    register_spec("test_plant", (Phase("sleep", "sleep 30"),))
    manager = _manager(tmp_path)
    job = await manager.start("test_plant")
    folder = tmp_path / "state" / job.id
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    (folder / ".job.json.tmp").symlink_to(victim)
    await asyncio.wait_for(manager.kill(job.id), timeout=5)
    await wait_terminal(manager, job.id)
    assert victim.read_text() == "untouched"
    assert json.loads((folder / "job.json").read_text())["state"] == "killed"

    os.unlink(folder / "job.json")
    os.mkfifo(folder / ".job.json.tmp")
    manager._save(manager.get(job.id))  # returns, rather than blocking on the FIFO
    assert json.loads((folder / "job.json").read_text())["state"] == "killed"


def test_an_unknown_state_or_bad_path_in_a_record_is_made_safe(tmp_path) -> None:
    folder = tmp_path / "state" / "0000beef"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(
        json.dumps(
            {
                "spec_name": "gone",
                "state": "queued",
                "live_dir": "out/\u0000live",
                "artifact_path": "/etc/passwd",
                "artifact_present": True,
            }
        )
    )
    manager = _manager(tmp_path)
    job = manager.get("0000beef")
    assert job.state == "failed"  # never stuck "live" where nothing can end it
    assert (job.live_dir, job.artifact_path) == ("", "")
    with pytest.raises(JobError):
        manager.live_files("0000beef")
    with pytest.raises(JobError):
        manager.artifact_path("0000beef")


async def test_a_deeply_nested_record_cannot_break_the_run_list(tmp_path) -> None:
    import httpx

    from jlaunch_ctl.app import create_app
    from jlaunch_ctl.config import Settings

    deep: object = 1
    for _ in range(300):
        deep = [deep]
    folder = tmp_path / "state" / "00000d0e"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(
        json.dumps(
            {
                "spec_name": "x",
                "state": "succeeded",
                "machine": {"cores": 32, "deep": deep},
                "phase_times": [{"phase": "train", "seconds": 5, "deep": deep}],
                "headline": ["h" * 600] * 300,
            }
        )
    )
    manager = _manager(tmp_path)
    app = create_app(Settings(token="t"), manager, TerminalRegistry())  # type: ignore[call-arg]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/jobs", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    job = manager.get("00000d0e")
    assert job.machine == {"cores": 32}
    assert job.phase_times == [{"phase": "train", "seconds": 5}]
    assert len(job.headline) == 256 and len(job.headline[0]) == 512
