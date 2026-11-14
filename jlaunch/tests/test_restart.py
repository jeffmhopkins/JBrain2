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
