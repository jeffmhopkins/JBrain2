"""The HTTP surface: bearer auth, spec/job routes, and the artifact download.

Driven with httpx's ASGITransport (not TestClient) so the app shares the test's event
loop — the PTY reader that finalizes a job then fires on the same loop wait_terminal
polls, which a separate-loop TestClient would strand."""

from __future__ import annotations

import httpx
import pytest

from jlaunch_ctl.app import create_app
from jlaunch_ctl.config import Settings
from jlaunch_ctl.jobs import JobManager
from jlaunch_ctl.terminal import TerminalRegistry
from tests.conftest import wait_terminal

_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def app_ctx(tmp_path, ok_spec):
    settings = Settings(token=_TOKEN)  # type: ignore[call-arg]
    registry = TerminalRegistry()
    manager = JobManager(
        str(tmp_path / "work"),
        str(tmp_path / "state"),
        str(tmp_path / "artifacts"),
        registry,
    )
    return create_app(settings, manager, registry), manager


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_healthz_is_unauthenticated(app_ctx) -> None:
    app, _ = app_ctx
    async with _client(app) as ac:
        assert (await ac.get("/healthz")).json() == {"status": "ok"}


async def test_routes_require_token(app_ctx) -> None:
    app, _ = app_ctx
    async with _client(app) as ac:
        assert (await ac.get("/specs")).status_code == 401
        assert (await ac.post("/jobs", json={"spec": "test_ok"})).status_code == 401


async def test_specs_lists_registered(app_ctx) -> None:
    app, _ = app_ctx
    async with _client(app) as ac:
        resp = await ac.get("/specs", headers=_AUTH)
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    assert {"erdos_straus_1e12", "test_ok"} <= names


async def test_start_unknown_spec_404(app_ctx) -> None:
    app, _ = app_ctx
    async with _client(app) as ac:
        resp = await ac.post("/jobs", json={"spec": "nope"}, headers=_AUTH)
    assert resp.status_code == 404


async def test_unknown_and_malformed_ids_404(app_ctx) -> None:
    app, _ = app_ctx
    async with _client(app) as ac:
        assert (await ac.get("/jobs/deadbeef", headers=_AUTH)).status_code == 404
        assert (await ac.get("/jobs/bad!id", headers=_AUTH)).status_code == 404


async def test_start_run_download_delete(app_ctx) -> None:
    app, manager = app_ctx
    async with _client(app) as ac:
        start = await ac.post("/jobs", json={"spec": "test_ok"}, headers=_AUTH)
        assert start.status_code == 201
        jid = start.json()["id"]

        await wait_terminal(manager, jid)

        got = (await ac.get(f"/jobs/{jid}", headers=_AUTH)).json()
        assert got["state"] == "succeeded"
        assert got["artifact_present"] is True

        listed = (await ac.get("/jobs", headers=_AUTH)).json()
        assert jid in {j["id"] for j in listed}

        artifact = await ac.get(f"/jobs/{jid}/artifact", headers=_AUTH)
        assert artifact.status_code == 200
        assert artifact.content == b"ARTIFACT-BYTES"

        assert (await ac.delete(f"/jobs/{jid}", headers=_AUTH)).status_code == 204
        assert (await ac.get(f"/jobs/{jid}", headers=_AUTH)).status_code == 404
