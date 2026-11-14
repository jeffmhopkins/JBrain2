"""The kept policies: what policies/index.toml may say, how a refresh stores the files,
and that a stored copy is served only while it still is the repo's blob."""

from __future__ import annotations

import json

import httpx
import pytest

from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.app import create_app
from jlaunch_ctl.catalog import Catalog, CatalogError
from jlaunch_ctl.config import Settings
from jlaunch_ctl.jobs import JobManager
from jlaunch_ctl.policy_index import PolicyIndexError, blob_id, parse
from jlaunch_ctl.terminal import TerminalRegistry
from tests.test_catalog import FakeRepo

LOOK = b'{"obs_spec": [], "layers": [[1]]}'
WALK = b'{"obs_spec": [], "layers": [[2]]}'
FILES = {
    "policies/look-v1/policy_final.json": LOOK,
    "policies/walk-v2/policy_final.json": WALK,
}
_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _clean_repo_specs(monkeypatch):
    monkeypatch.setattr(specs_mod, "REPO_SPECS", {})
    monkeypatch.setattr(specs_mod, "LISTED_REPO_SPECS", [])


def _entry(**fields: object) -> str:
    base: dict[str, object] = {
        "slug": "look-v1",
        "title": "Look v1",
        "job": "growbot_look",
        "file": "policies/look-v1/policy_final.json",
    }
    base.update(fields)
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in base.items())
    return f"[[policy]]\n{body}\n"


INDEX = _entry(run="a1b2c3d4", notes="steady") + _entry(
    slug="walk-v2",
    title="Walk v2",
    job="growbot_heading_walk",
    file="policies/walk-v2/policy_final.json",
)


def _blobs(files: dict[str, bytes] = FILES) -> dict[str, tuple[str, int]]:
    return {p: (blob_id(b), len(b)) for p, b in files.items()}


def test_blob_id_is_gits() -> None:
    # `printf 'hello\n' | git hash-object --stdin`
    assert blob_id(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_the_index_fills_in_each_blob() -> None:
    got = parse(INDEX, _blobs())
    assert [e["slug"] for e in got] == ["look-v1", "walk-v2"]
    assert got[0]["blob"] == blob_id(LOOK) and got[0]["bytes"] == len(LOOK)
    assert got[0]["run"] == "a1b2c3d4" and got[0]["notes"] == "steady"
    assert parse("", {}) == []


@pytest.mark.parametrize(
    ("index", "why"),
    [
        (_entry(slug="Look"), "slug must be"),
        (_entry(job="look"), "job must be"),
        (_entry(file="policies/../x.json"), "file must be"),
        (_entry(file="configs/look.toml"), "file must be"),
        (_entry(file="policies/look-v1/missing.json"), "isn't in the repo"),
        (_entry(run="a b"), "run must be"),
        (_entry(title="x" * 61), "title is over 60"),
        (_entry(title="  "), "title is empty"),
        (_entry(notes=3), "notes must be text"),
        (_entry(cmd="x"), "unknown key cmd"),
        (_entry() + _entry(), "listed twice"),
        ("[[policy]]\nslug = 'a'\n", "missing"),
        ("[jobs]\n", "unknown top-level key"),
        ("policy = 1\n", "must be a list"),
        ("policy = [1]\n", "not a table"),
        ("[[policy\n", "isn't valid TOML"),
        ("#" * (65 * 1024), "over 64 KB"),
    ],
)
def test_the_index_rejects(index: str, why: str) -> None:
    with pytest.raises(PolicyIndexError) as err:
        parse(index, _blobs())
    assert why in str(err.value)


def test_size_limits() -> None:
    big = {"policies/look-v1/policy_final.json": (blob_id(LOOK), 5 * 1024 * 1024)}
    with pytest.raises(PolicyIndexError, match="over 4 MB"):
        parse(_entry(), big)
    many = "".join(
        _entry(slug=f"p{i}", file=f"policies/p{i}/policy_final.json") for i in range(20)
    )
    blobs = {
        f"policies/p{i}/policy_final.json": (blob_id(LOOK), 4 * 1024 * 1024)
        for i in range(20)
    }
    with pytest.raises(PolicyIndexError, match="over 64 MB"):
        parse(many, blobs)


async def test_refresh_stores_the_policies_and_reads_each_blob_once(tmp_path) -> None:
    repo = FakeRepo()
    repo.index, repo.files = INDEX, dict(FILES)
    cat = Catalog(str(tmp_path), fetch=repo)
    got = await cat.refresh()
    assert got["policies"] == ["look-v1", "walk-v2"]
    assert cat.policy_bytes("look-v1") == LOOK
    listed = cat.policies()
    assert (
        listed[0]["commit"] == repo.commit
        and listed[1]["job"] == "growbot_heading_walk"
    )
    assert repo.reads == 2
    await cat.refresh()
    assert repo.reads == 2  # unchanged blobs aren't fetched again

    repo.index = _entry(slug="look-v2", title="Look v2")
    await cat.refresh()
    assert [p["slug"] for p in cat.policies()] == ["look-v2"]
    with pytest.raises(CatalogError):
        cat.policy_bytes("walk-v2")
    # The dropped policy's file is gone from the box; the kept one is still there.
    assert sorted(p.name for p in (tmp_path / "policies").iterdir()) == [
        f"{blob_id(LOOK)}.json"
    ]


async def test_a_bad_index_fails_the_whole_refresh(tmp_path) -> None:
    repo = FakeRepo()
    repo.index, repo.files = INDEX, dict(FILES)
    cat = Catalog(str(tmp_path), fetch=repo)
    await cat.refresh()
    repo.index = _entry(file="policies/nope/policy_final.json")
    with pytest.raises(CatalogError) as err:
        await cat.refresh()
    assert err.value.invalid and "isn't in the repo" in str(err.value)
    assert [p["slug"] for p in cat.policies()] == ["look-v1", "walk-v2"]


async def test_a_blob_that_reads_back_wrong_is_refused(tmp_path) -> None:
    repo = FakeRepo()
    repo.index, repo.files = INDEX, dict(FILES)

    async def lying(dest):
        snap = await FakeRepo.__call__(repo, dest)

        async def read(_blob: str) -> bytes:
            return b"{}"

        return snap.__class__(**{**snap.__dict__, "read_blob": read})

    cat = Catalog(str(tmp_path), fetch=lying)
    with pytest.raises(CatalogError, match="didn't read back"):
        await cat.refresh()
    assert cat.policies() == []


async def test_a_tampered_copy_isnt_served(tmp_path) -> None:
    repo = FakeRepo()
    repo.index, repo.files = INDEX, dict(FILES)
    cat = Catalog(str(tmp_path), fetch=repo)
    await cat.refresh()
    (tmp_path / "policies" / f"{blob_id(LOOK)}.json").write_bytes(b'{"evil": 1}')
    with pytest.raises(CatalogError, match="doesn't match"):
        cat.policy_bytes("look-v1")
    (tmp_path / "policies" / f"{blob_id(LOOK)}.json").unlink()
    with pytest.raises(CatalogError, match="isn't on the box"):
        cat.policy_bytes("look-v1")
    # The next refresh puts the repo's file back.
    await cat.refresh()
    assert cat.policy_bytes("look-v1") == LOOK


async def test_the_policies_survive_a_restart(tmp_path) -> None:
    repo = FakeRepo()
    repo.index, repo.files = INDEX, dict(FILES)
    await Catalog(str(tmp_path), fetch=repo).refresh()
    data = json.loads((tmp_path / "catalog.json").read_text())
    data["policies"].append({"slug": "bad slug", "title": "x"})
    data["policies"].append({**data["policies"][0], "slug": "noblob", "blob": None})
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    again = Catalog(str(tmp_path), fetch=repo)
    again.load()
    assert [p["slug"] for p in again.policies()] == ["look-v1", "walk-v2"]
    assert again.policy_bytes("walk-v2") == WALK


async def test_the_routes(tmp_path) -> None:
    repo = FakeRepo()
    repo.index, repo.files = INDEX, dict(FILES)
    cat = Catalog(str(tmp_path / "state"), fetch=repo)
    await cat.refresh()
    registry = TerminalRegistry()
    manager = JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        registry,
        catalog=cat,
    )
    app = create_app(Settings(token=_TOKEN), manager, registry, cat)  # type: ignore[call-arg]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        assert (await ac.get("/catalog/policies")).status_code == 401
        listed = (await ac.get("/catalog/policies", headers=_AUTH)).json()
        assert listed["commit"] == repo.commit
        assert [p["slug"] for p in listed["policies"]] == ["look-v1", "walk-v2"]
        got = await ac.get("/catalog/policies/look-v1", headers=_AUTH)
        assert got.content == LOOK
        assert got.headers["content-type"] == "application/json"
        assert (
            await ac.get("/catalog/policies/nope", headers=_AUTH)
        ).status_code == 404
        bad = await ac.get("/catalog/policies/..%2Fx", headers=_AUTH)
        assert bad.status_code == 404

    bare = create_app(Settings(token=_TOKEN), manager, registry)  # type: ignore[call-arg]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bare), base_url="http://t"
    ) as ac:
        assert (await ac.get("/catalog/policies", headers=_AUTH)).status_code == 404
        assert (
            await ac.get("/catalog/policies/look-v1", headers=_AUTH)
        ).status_code == 404
