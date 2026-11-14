"""The debug token's read-only window on the job launcher.

What an assistant relies on with no terminal: the runs are listed, a running job's live
folder reads back exactly as the owner sees it (sandboxed), the finished result downloads,
and nothing here can start, stop or delete a run."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from jbrain.api.jshare_public import SANDBOX
from jbrain.jlaunch import FakeJlaunchClient
from tests.unit.test_debug_api import _auth, _state, debug_client  # noqa: F401 - fixture

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


def test_cannot_control_a_run(debug_client: tuple[TestClient, str]) -> None:  # noqa: F811
    client, key = debug_client
    fake, jid = _launcher(client)
    for method, path in (
        ("POST", "/api/debug/jlaunch/jobs"),
        ("POST", f"/api/debug/jlaunch/jobs/{jid}/stop"),
        ("DELETE", f"/api/debug/jlaunch/jobs/{jid}"),
    ):
        assert client.request(method, path, headers=_auth(key)).status_code in (404, 405)
    assert (asyncio.run(fake.get_job(jid)))["state"] == "running"
