"""A job's live folder: only plain, allowlisted files inside the checkout are served.

The job is untrusted code, so the cases that matter are the ones it could plant: a
symlink out of the checkout, a dotfile, a path in the name, an unknown type."""

from __future__ import annotations

import dataclasses
import os
import shutil

import httpx
import pytest

from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.app import create_app
from jlaunch_ctl.config import Settings
from jlaunch_ctl.jobs import JobError, JobManager
from jlaunch_ctl.specs import Phase, SpecError
from jlaunch_ctl.terminal import TerminalRegistry
from tests.conftest import _spec, wait_terminal

_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_WRITE_LIVE = (
    "mkdir -p out/live; "
    "printf '{\"percent\": 40}' > out/live/progress.json; "
    'printf "<p>replay</p>" > out/live/replay.html; '
    'printf "x" > out/live/.replay.html.tmp; '
    'printf "x" > out/live/run.sh; '
    "ln -s /etc/hostname out/live/escape.txt; "
    "mkdir out/live/sub.json"
)


@pytest.fixture
def live_spec(monkeypatch):
    spec = dataclasses.replace(
        _spec("test_live", (Phase("gen", _WRITE_LIVE),)), live_dir="out/live"
    )
    monkeypatch.setitem(specs_mod.SPECS, spec.name, spec)
    return spec


@pytest.fixture
def ctx(tmp_path, live_spec, ok_spec):
    registry = TerminalRegistry()
    manager = JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        registry,
    )
    app = create_app(Settings(token=_TOKEN), manager, registry)  # type: ignore[call-arg]
    return app, manager


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def test_live_dir_must_stay_in_checkout() -> None:
    for bad in ("/etc", "../up", "a/../../b"):
        with pytest.raises(SpecError):
            specs_mod._validate(
                dataclasses.replace(_spec("bad", (Phase("p", "true"),)), live_dir=bad)
            )


def test_growbot_spec_declares_its_live_folder() -> None:
    spec = specs_mod.SPECS["growbot_heading_walk"]
    assert spec.live_dir == "runs/jlaunch/live"
    assert spec.public()["live"] is True
    assert specs_mod.SPECS["erdos_straus_1e12"].public()["live"] is False


async def test_lists_and_serves_only_plain_allowlisted_files(ctx) -> None:
    app, manager = ctx
    async with _client(app) as ac:
        jid = (
            await ac.post("/jobs", json={"spec": "test_live"}, headers=_AUTH)
        ).json()["id"]
        await wait_terminal(manager, jid)
        listed = (await ac.get(f"/jobs/{jid}/live", headers=_AUTH)).json()
        assert {f["name"] for f in listed} == {"progress.json", "replay.html"}

        got = await ac.get(f"/jobs/{jid}/live/progress.json", headers=_AUTH)
        assert got.status_code == 200
        assert got.json() == {"percent": 40}
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["x-content-type-options"] == "nosniff"
        page = await ac.get(f"/jobs/{jid}/live/replay.html", headers=_AUTH)
        assert page.headers["content-type"].startswith("text/html")

        for name in (
            "escape.txt",
            ".replay.html.tmp",
            "run.sh",
            "sub.json",
            "nope.json",
        ):
            resp = await ac.get(f"/jobs/{jid}/live/{name}", headers=_AUTH)
            assert resp.status_code == 404, name
        assert (await ac.get(f"/jobs/{jid}/live", headers={})).status_code == 401


async def test_job_without_live_folder_is_404(ctx) -> None:
    app, manager = ctx
    async with _client(app) as ac:
        jid = (await ac.post("/jobs", json={"spec": "test_ok"}, headers=_AUTH)).json()[
            "id"
        ]
        await wait_terminal(manager, jid)
        assert (await ac.get(f"/jobs/{jid}/live", headers=_AUTH)).status_code == 404
        assert (await ac.get("/jobs/nope/live", headers=_AUTH)).status_code == 404


async def test_live_folder_symlinked_out_of_checkout_is_refused(ctx, tmp_path) -> None:
    _, manager = ctx
    job = await manager.start("test_live")
    await wait_terminal(manager, job.id)
    live = os.path.join(manager.get(job.id).workspace, "out", "live")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}")
    os.rename(live, live + ".bak")
    os.symlink(outside, live)
    with pytest.raises(JobError):
        manager.live_files(job.id)
    with pytest.raises(JobError):
        manager.read_live(job.id, "secret.json")


async def test_empty_until_written_and_size_capped(ctx, monkeypatch) -> None:
    _, manager = ctx
    job = await manager.start("test_live")
    await wait_terminal(manager, job.id)
    import jlaunch_ctl.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "LIVE_MAX_BYTES", 4)
    with pytest.raises(JobError, match="too large"):
        manager.read_live(job.id, "replay.html")
    live = os.path.join(manager.get(job.id).workspace, "out", "live")
    shutil.rmtree(live)
    assert manager.live_files(job.id) == []
