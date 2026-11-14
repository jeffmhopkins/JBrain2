"""The debug token's read-only window on the job launcher.

What an assistant relies on with no terminal: the runs are listed, a running job's live
folder reads back exactly as the owner sees it (sandboxed), the finished result downloads,
a registered spec starts, a finished run deletes, and nothing here can stop a run or delete
a live one."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from jbrain.api.jshare_public import SANDBOX
from jbrain.jlaunch import FakeJlaunchClient, JlaunchError
from tests.unit.test_debug_api import _auth, _state, debug_client  # noqa: F401 - fixture
from tests.unit.test_jlaunch_api import _no_db  # noqa: F401 - fixture

_PROGRESS = b'{"percent": 42}'


def _launcher(client: TestClient) -> tuple[FakeJlaunchClient, str]:
    fake = FakeJlaunchClient(artifact=b"TARBALL")
    jid = asyncio.run(fake.start_job("growbot_heading_walk"))["id"]
    fake.live[jid] = {
        "progress.json": (_PROGRESS, "application/json"),
        "replay.html": (b"<script>1</script>", "text/html; charset=utf-8"),
    }
    _state(client).jlaunch_client = fake
    return fake, jid


def test_needs_a_token(debug_client: tuple[TestClient, str]) -> None:  # noqa: F811
    client, _ = debug_client
    _, jid = _launcher(client)
    for path in ("", f"/jobs/{jid}", f"/jobs/{jid}/live", f"/jobs/{jid}/artifact"):
        assert client.get(f"/api/debug/jlaunch{path}").status_code == 401


def test_lists_runs_and_reads_a_live_folder(debug_client: tuple[TestClient, str]) -> None:  # noqa: F811
    client, key = debug_client
    _, jid = _launcher(client)
    over = client.get("/api/debug/jlaunch", headers=_auth(key)).json()
    assert [j["id"] for j in over["jobs"]] == [jid]
    assert over["specs"]
    assert client.get(f"/api/debug/jlaunch/jobs/{jid}", headers=_auth(key)).json()["id"] == jid
    names = {
        f["name"]
        for f in client.get(f"/api/debug/jlaunch/jobs/{jid}/live", headers=_auth(key)).json()
    }
    assert names == {"progress.json", "replay.html"}
    got = client.get(f"/api/debug/jlaunch/jobs/{jid}/live/progress.json", headers=_auth(key))
    assert got.content == _PROGRESS
    page = client.get(f"/api/debug/jlaunch/jobs/{jid}/live/replay.html", headers=_auth(key))
    assert page.headers["content-security-policy"] == SANDBOX
    assert client.get("/api/debug/jlaunch/jobs/nope", headers=_auth(key)).status_code == 404
    assert "jlaunch.read" in client.get("/api/debug/whoami", headers=_auth(key)).json()["scopes"]


def test_result_downloads_once_finished(debug_client: tuple[TestClient, str]) -> None:  # noqa: F811
    client, key = debug_client
    fake, jid = _launcher(client)
    url = f"/api/debug/jlaunch/jobs/{jid}/artifact"
    assert client.get(url, headers=_auth(key)).status_code == 404
    fake.mark_succeeded(jid)
    got = client.get(url, headers=_auth(key))
    assert got.status_code == 200
    assert got.content == b"TARBALL"
    assert got.headers["content-disposition"].startswith("attachment;")


def test_cannot_stop_a_run(debug_client: tuple[TestClient, str]) -> None:  # noqa: F811
    client, key = debug_client
    fake, jid = _launcher(client)
    got = client.post(f"/api/debug/jlaunch/jobs/{jid}/stop", headers=_auth(key))
    assert got.status_code in (404, 405)
    assert (asyncio.run(fake.get_job(jid)))["state"] == "running"


def test_deletes_a_finished_run_but_never_a_live_one(
    debug_client: tuple[TestClient, str],  # noqa: F811
    _no_db: None,  # noqa: F811
) -> None:
    client, key = debug_client
    fake, jid = _launcher(client)
    url = f"/api/debug/jlaunch/jobs/{jid}"
    assert client.delete(url).status_code == 401
    live = client.delete(url, headers=_auth(key))
    assert live.status_code == 409
    assert (asyncio.run(fake.get_job(jid)))["state"] == "running"
    fake.mark_succeeded(jid)
    assert client.delete(url, headers=_auth(key)).status_code == 204
    assert client.get(url, headers=_auth(key)).status_code == 404
    # Already gone (or never known): still 204, so a retried delete is harmless.
    assert client.delete(url, headers=_auth(key)).status_code == 204
    assert client.delete("/api/debug/jlaunch/jobs/bad.id", headers=_auth(key)).status_code == 404
    assert "jlaunch.delete" in client.get("/api/debug/whoami", headers=_auth(key)).json()["scopes"]


def test_starts_a_registered_spec_and_nothing_else(
    debug_client: tuple[TestClient, str],  # noqa: F811
    _no_db: None,  # noqa: F811
) -> None:
    client, key = debug_client
    fake, _ = _launcher(client)
    body = {"spec": "growbot_camera_walk"}
    assert client.post("/api/debug/jlaunch/jobs", json=body).status_code == 401
    bad = client.post("/api/debug/jlaunch/jobs", json={"spec": "x" * 200}, headers=_auth(key))
    assert bad.status_code == 422
    got = client.post("/api/debug/jlaunch/jobs", json=body, headers=_auth(key))
    assert got.status_code == 201 and fake.started_specs[-1] == "growbot_camera_walk"
    jid = got.json()["id"]
    assert client.get(f"/api/debug/jlaunch/jobs/{jid}", headers=_auth(key)).status_code == 200

    async def refuse(spec: str) -> dict[str, object]:
        raise JlaunchError("busy" if spec == "x" else "nope", status=409 if spec == "x" else 404)

    fake.start_job = refuse  # type: ignore[method-assign]
    busy = client.post("/api/debug/jlaunch/jobs", json={"spec": "x"}, headers=_auth(key))
    assert busy.status_code == 409
    unknown = client.post("/api/debug/jlaunch/jobs", json={"spec": "y"}, headers=_auth(key))
    assert unknown.status_code == 404
    assert "jlaunch.start" in client.get("/api/debug/whoami", headers=_auth(key)).json()["scopes"]
