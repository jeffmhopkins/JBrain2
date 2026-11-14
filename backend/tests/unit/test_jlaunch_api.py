"""The jlaunch owner api surface: owner-gating, fail-closed-when-unconfigured, the
start/stop/kill/delete proxy, and the share mint (no DB — the gating + proxy decisions land
before/around any query; DB writes are neutralized)."""

from __future__ import annotations

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jlaunch
from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.external import jlaunch_shares
from jbrain.jlaunch import FakeJlaunchClient, JlaunchError

OWNER = PrincipalInfo(id="owner1", kind="owner", label="owner")
NON_OWNER = PrincipalInfo(id="cap1", kind="capability_token", label="token")


def _app(principal: PrincipalInfo, *, jlaunch_client: object, blob_store: object = None) -> FastAPI:
    app = FastAPI()
    app.include_router(jlaunch.router, prefix="/api")
    app.state.jlaunch_client = jlaunch_client
    app.state.session_maker = None  # not reached by the gating tests
    app.state.blob_store = blob_store
    app.dependency_overrides[current_principal] = lambda: principal
    return app


@pytest.fixture
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the owner-index DB writes so a route's happy path runs without a pool."""

    @contextlib.asynccontextmanager
    async def _fake_scoped(*_a: object, **_k: object):
        yield None

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(jlaunch, "scoped_session", _fake_scoped)
    monkeypatch.setattr(jlaunch._REPO, "upsert", _noop)
    monkeypatch.setattr(jlaunch._REPO, "delete", _noop)
    monkeypatch.setattr(jlaunch._REPO, "set_artifact", _noop)


def test_non_owner_is_forbidden() -> None:
    client = TestClient(_app(NON_OWNER, jlaunch_client=FakeJlaunchClient()))
    assert client.get("/api/jlaunch/specs").status_code == 403
    assert client.post("/api/jlaunch/jobs", json={"spec": "x"}).status_code == 403
    assert client.get("/api/jlaunch/jobs").status_code == 403
    assert client.post("/api/jlaunch/jobs/j1/stop").status_code == 403
    assert client.post("/api/jlaunch/jobs/j1/kill").status_code == 403
    assert client.delete("/api/jlaunch/jobs/j1").status_code == 403
    assert client.post("/api/jlaunch/jobs/j1/share", json={}).status_code == 403


def test_owner_but_unconfigured_is_404() -> None:
    client = TestClient(_app(OWNER, jlaunch_client=None))
    assert client.get("/api/jlaunch/specs").status_code == 404
    r = client.post("/api/jlaunch/jobs", json={"spec": "erdos_straus_1e12"})
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_specs_are_listed(_no_db: None) -> None:
    client = TestClient(_app(OWNER, jlaunch_client=FakeJlaunchClient()))
    specs = client.get("/api/jlaunch/specs").json()
    assert {s["name"] for s in specs} == {"erdos_straus_1e12"}


def test_start_forwards_spec(_no_db: None) -> None:
    fake = FakeJlaunchClient()
    client = TestClient(_app(OWNER, jlaunch_client=fake))
    r = client.post("/api/jlaunch/jobs", json={"spec": "erdos_straus_1e12"})
    assert r.status_code == 201
    assert fake.started_specs == ["erdos_straus_1e12"]
    assert r.json()["spec_name"] == "erdos_straus_1e12"


def test_start_busy_is_409(_no_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeJlaunchClient()

    async def _busy(_spec: str) -> dict:
        raise JlaunchError("busy", status=409)

    monkeypatch.setattr(fake, "start_job", _busy)
    client = TestClient(_app(OWNER, jlaunch_client=fake))
    assert client.post("/api/jlaunch/jobs", json={"spec": "x"}).status_code == 409


def test_malformed_id_is_404(_no_db: None) -> None:
    client = TestClient(_app(OWNER, jlaunch_client=FakeJlaunchClient()))
    assert client.get("/api/jlaunch/jobs/bad.id").status_code == 404
    assert client.post("/api/jlaunch/jobs/bad.id/stop").status_code == 404
    assert client.delete("/api/jlaunch/jobs/bad.id").status_code == 404


def test_stop_and_kill_proxy(_no_db: None) -> None:
    import asyncio

    fake = FakeJlaunchClient()
    job = asyncio.run(fake.start_job("erdos_straus_1e12"))
    client = TestClient(_app(OWNER, jlaunch_client=fake))
    assert client.post(f"/api/jlaunch/jobs/{job['id']}/stop").json()["state"] == "killed"
    assert client.post(f"/api/jlaunch/jobs/{job['id']}/kill").json()["state"] == "killed"


def test_delete_is_idempotent_204(_no_db: None) -> None:
    fake = FakeJlaunchClient()
    client = TestClient(_app(OWNER, jlaunch_client=fake))
    assert client.delete("/api/jlaunch/jobs/deadbeef").status_code == 204


class _FakeBlobs:
    async def put_stream(self, chunks) -> str:  # noqa: ANN001
        async for _ in chunks:  # drain so the client's stream generator runs
            pass
        return "sha-of-artifact"


def test_mint_share_requires_finished_job(_no_db: None) -> None:
    fake = FakeJlaunchClient()  # a running (not finished) job
    import asyncio

    job = asyncio.run(fake.start_job("erdos_straus_1e12"))
    client = TestClient(_app(OWNER, jlaunch_client=fake, blob_store=_FakeBlobs()))
    r = client.post(f"/api/jlaunch/jobs/{job['id']}/share", json={})
    assert r.status_code == 400


def test_mint_share_streams_and_returns_token(
    _no_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from jbrain.external.jlaunch_shares import ShareLink

    fake = FakeJlaunchClient()
    job = asyncio.run(fake.start_job("erdos_straus_1e12"))
    fake.mark_succeeded(job["id"])

    captured: dict = {}

    async def _mint(_maker, _ctx, *, run_id: str, label: str):
        captured["run_id"] = run_id
        from datetime import UTC, datetime

        link = ShareLink(
            id="link-1",
            label=label,
            run_id=run_id,
            created_at=datetime.now(UTC),
            revoked_at=None,
            last_viewed_at=None,
            view_count=0,
        )
        return "secret-token", link

    monkeypatch.setattr(jlaunch_shares, "mint_share", _mint)
    client = TestClient(_app(OWNER, jlaunch_client=fake, blob_store=_FakeBlobs()))
    r = client.post(f"/api/jlaunch/jobs/{job['id']}/share", json={"label": "paper"})
    assert r.status_code == 201
    assert r.json()["token"] == "secret-token"
    assert captured["run_id"] == job["id"]


# -- live folder + result download ----------------------------------------------------


def _live_app(principal: PrincipalInfo = OWNER) -> tuple[TestClient, FakeJlaunchClient, str]:
    import asyncio

    fake = FakeJlaunchClient(artifact=b"RESULT")
    jid = asyncio.run(fake.start_job("growbot_heading_walk"))["id"]
    fake.live[jid] = {
        "progress.json": (b'{"percent": 10}', "application/json"),
        "replay.html": (b"<script>alert(1)</script>", "text/html; charset=utf-8"),
    }
    return TestClient(_app(principal, jlaunch_client=fake)), fake, jid


def test_live_and_download_are_owner_only() -> None:
    client, _, jid = _live_app(NON_OWNER)
    assert client.get(f"/api/jlaunch/jobs/{jid}/live").status_code == 403
    assert client.get(f"/api/jlaunch/jobs/{jid}/live/progress.json").status_code == 403
    assert client.get(f"/api/jlaunch/jobs/{jid}/artifact").status_code == 403


def test_live_page_is_sandboxed_and_never_cached() -> None:
    from jbrain.api.jshare_public import SANDBOX

    client, _, jid = _live_app()
    listed = client.get(f"/api/jlaunch/jobs/{jid}/live").json()
    assert {f["name"] for f in listed} == {"progress.json", "replay.html"}
    page = client.get(f"/api/jlaunch/jobs/{jid}/live/replay.html")
    assert page.status_code == 200
    # Job-written script on the app's origin: the sandbox is what keeps it off the session.
    assert page.headers["content-security-policy"] == SANDBOX
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-store"
    assert "content-disposition" not in page.headers
    dl = client.get(f"/api/jlaunch/jobs/{jid}/live/progress.json?download=1")
    assert dl.headers["content-disposition"] == 'attachment; filename="progress.json"'
    assert dl.headers["content-security-policy"] == SANDBOX


def test_live_bad_or_missing_names_are_404() -> None:
    client, _, jid = _live_app()
    for name in ("nope.json", ".hidden.json", "a%2Fb.json"):
        assert client.get(f"/api/jlaunch/jobs/{jid}/live/{name}").status_code == 404, name
    assert client.get("/api/jlaunch/jobs/zzz/live").status_code == 404
    assert client.get("/api/jlaunch/jobs/bad$id/live").status_code == 404


def test_job_without_live_folder_is_404() -> None:
    import asyncio

    fake = FakeJlaunchClient()
    jid = asyncio.run(fake.start_job("erdos_straus_1e12"))["id"]
    client = TestClient(_app(OWNER, jlaunch_client=fake))
    assert client.get(f"/api/jlaunch/jobs/{jid}/live").status_code == 404


def test_live_upstream_failure_is_502() -> None:
    class _Down(FakeJlaunchClient):
        async def list_live(self, jid: str) -> list[dict[str, object]]:
            raise JlaunchError("connection refused")

    fake = _Down()
    client = TestClient(_app(OWNER, jlaunch_client=fake))
    assert client.get("/api/jlaunch/jobs/j1/live").status_code == 502


def test_result_downloads_only_once_succeeded() -> None:
    client, fake, jid = _live_app()
    assert client.get(f"/api/jlaunch/jobs/{jid}/artifact").status_code == 404
    fake.mark_succeeded(jid)
    got = client.get(f"/api/jlaunch/jobs/{jid}/artifact")
    assert got.status_code == 200
    assert got.content == b"RESULT"
    assert got.headers["content-disposition"] == ('attachment; filename="es_1e12_artifacts.tar.gz"')
    assert got.headers["content-length"] == str(len(b"RESULT"))
    assert client.get("/api/jlaunch/jobs/zzz/artifact").status_code == 404
