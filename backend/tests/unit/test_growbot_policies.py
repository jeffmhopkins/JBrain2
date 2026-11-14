"""GrowBot's policy library (api/growbot_policies.py, growbot/policies.py): adding a finished
training job's policy, the contract the Body tab reads off it, rename and delete, the debug
token's window, the sidecar's read, and a move that names a library policy.

The repo is an in-memory stand-in (the SQL and its RLS run against real Postgres in
tests/integration/test_growbot_policies_pg.py); the job launcher is FakeJlaunchClient.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import growbot, growbot_policies
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.growbot.policies import (
    MAX_POLICY_BYTES,
    PolicyError,
    PolicyRow,
    parse,
    summary,
)
from jbrain.jlaunch import FakeJlaunchClient, JlaunchError
from jbrain.storage import FsInternalSecrets

OWNER = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")


def policy(channels: list[str] | None = None, facing: str = "nose", **over: Any) -> dict[str, Any]:
    chans = channels or ["accel_x", "gyro_z", "cmd_yaw", "cycle:gait_sin"]
    return {
        "format": "growbot-policy",
        "name": "growbot-heading_walk",
        "obs_size": len(chans),
        "mean": [0.0] * len(chans),
        "std": [1.0] * len(chans),
        "layers": [{"W": [[0.0, 0.0]] * len(chans), "b": [0.0, 0.0], "act": "linear"}],
        "obs_spec": {
            "control_hz": 25,
            "segments": [{"channels": chans, "frames": 1}],
            "cmd_yaw": {"meaning": "heading_error", "scale": "degrees / 180", "facing": facing},
        },
        **over,
    }


class FakePolicies:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[PolicyRow, dict[str, Any]]] = {}
        self.contexts: list[SessionContext] = []
        # Rows summary() no longer reads, as SqlPolicyRepo.rename raises for them.
        self.unreadable: set[str] = set()

    async def list(self, ctx: SessionContext) -> list[PolicyRow]:
        self.contexts.append(ctx)
        return [r for r, _ in self.rows.values()]

    async def add(
        self,
        ctx: SessionContext,
        name: str,
        policy: dict[str, Any],
        size: int,
        source_job: str,
        source_spec: str,
    ) -> PolicyRow:
        self.contexts.append(ctx)
        row = PolicyRow(
            id=str(uuid.uuid4()),
            name=name,
            source_job=source_job,
            source_spec=source_spec,
            bytes=size,
            created_at=datetime(2026, 11, 14, tzinfo=UTC),
            contract=summary(policy),
        )
        self.rows[row.id] = (row, policy)
        return row

    async def rename(self, ctx: SessionContext, pid: str, name: str) -> PolicyRow | None:
        got = self.rows.get(pid)
        if got is None:
            return None
        if pid in self.unreadable:
            raise PolicyError("its control rate (obs_spec.control_hz) is missing or out of range")
        row = PolicyRow(**{**got[0].__dict__, "name": name})
        self.rows[pid] = (row, got[1])
        return row

    async def replace_policy(
        self,
        ctx: SessionContext,
        pid: str,
        policy: dict[str, Any],
        size: int,
        source_job: str,
    ) -> bool:
        got = self.rows.get(pid)
        if got is None:
            return False
        fields = {**got[0].__dict__, "bytes": size, "source_job": source_job}
        self.rows[pid] = (PolicyRow(**{**fields, "contract": summary(policy)}), policy)
        return True

    async def delete(self, ctx: SessionContext, pid: str) -> bool:
        return self.rows.pop(pid, None) is not None

    async def get_json(self, ctx: SessionContext, pid: str) -> dict[str, Any] | None:
        self.contexts.append(ctx)
        got = self.rows.get(pid)
        return got[1] if got else None

    async def exists(self, ctx: SessionContext, pid: str) -> bool:
        self.contexts.append(ctx)
        return pid in self.rows


async def _finished(jl: FakeJlaunchClient, spec: str, files: dict[str, bytes]) -> str:
    job = await jl.start_job(spec)
    jl.mark_succeeded(job["id"])
    jl.live[job["id"]] = {n: (b, "application/json") for n, b in files.items()}
    return job["id"]


def _app(tmp_path: Path) -> tuple[TestClient, FakePolicies, FakeJlaunchClient, dict[str, str]]:
    app = FastAPI()
    app.include_router(growbot_policies.router, prefix="/api")
    app.include_router(growbot_policies.debug_router, prefix="/api")
    app.include_router(growbot_policies.internal_router, prefix="/internal")
    lib, jl = FakePolicies(), FakeJlaunchClient()
    app.state.growbot_policies = lib
    app.state.jlaunch_client = jl
    secrets = FsInternalSecrets(tmp_path)
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    app.dependency_overrides[owner_only] = lambda: OWNER
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    token = (secrets.read("growbot") or b"").decode()
    return TestClient(app), lib, jl, {"Authorization": f"Bearer {token}"}


FINAL = json.dumps(policy()).encode()


async def test_a_finished_training_job_offers_its_policy_and_adds_it(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    jid = await _finished(jl, "growbot_heading_walk", {"policy_final.json": FINAL})
    # A run still training, a math job and a job with only a mid-run snapshot offer nothing.
    await jl.start_job("growbot_camera_walk")
    await _finished(jl, "erdos_straus_1e12", {"policy_final.json": FINAL})
    await _finished(jl, "growbot_camera_walk", {"policy_latest.json": FINAL})
    sources = client.get("/api/growbot/policies/sources").json()
    assert [s["job_id"] for s in sources] == [jid]
    assert sources[0]["spec"] == "growbot_heading_walk" and sources[0]["added"] is False

    resp = client.post("/api/growbot/policies", json={"job_id": jid, "name": "  First\nwalk "})
    assert resp.status_code == 201
    got = resp.json()
    assert got["name"] == "First walk" and got["source_job"] == jid
    assert got["contract"] == {"facing": "nose", "speed": False, "pitch": False, "control_hz": 25}
    assert got["bytes"] == len(FINAL)
    assert client.get("/api/growbot/policies/sources").json()[0]["added"] is True
    assert [p["id"] for p in client.get("/api/growbot/policies").json()] == [got["id"]]
    assert lib.contexts[0] == SessionContext(principal_id=OWNER.id, principal_kind="owner")


async def test_a_job_that_is_not_a_finished_walk_adds_nothing(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    running = (await jl.start_job("growbot_heading_walk"))["id"]
    jl.live[running] = {"policy_final.json": (FINAL, "application/json")}
    math = await _finished(jl, "erdos_straus_1e12", {"policy_final.json": FINAL})
    bare = await _finished(jl, "growbot_heading_walk", {})
    for jid, want in ((running, 409), (math, 409), (bare, 404), ("nope", 404)):
        resp = client.post("/api/growbot/policies", json={"job_id": jid, "name": "x"})
        assert resp.status_code == want, jid
    assert (
        client.post("/api/growbot/policies", json={"job_id": "../x", "name": "x"}).status_code
        == 422
    )
    assert lib.rows == {}


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"not json", "isn't JSON"),
        (b"[" * 100_000, "isn't JSON"),
        (b'{"mean": [NaN]}', "isn't JSON"),
        (b"[]", "not a policy"),
        (json.dumps({"mean": [1]}).encode(), "obs_spec"),
        (json.dumps(policy(["accel_x", "gyro_z"])).encode(), "takes no heading"),
        (
            json.dumps(
                {**policy(), "obs_spec": {**policy()["obs_spec"], "cmd_yaw": None}}
            ).encode(),
            "isn't a heading error",
        ),
        (
            json.dumps(
                {**policy(), "obs_spec": {**policy()["obs_spec"], "control_hz": 0}}
            ).encode(),
            "control rate",
        ),
        (json.dumps(policy(layers=[])).encode(), "no layers"),
        (json.dumps(policy(["gyro_z", "cmd_yaw", "lidar_0"])).encode(), "can't feed"),
    ],
)
async def test_a_file_the_pad_cannot_steer_is_refused_with_why(
    tmp_path: Path, raw: bytes, why: str
) -> None:
    client, lib, jl, _ = _app(tmp_path)
    jid = await _finished(jl, "growbot_heading_walk", {"policy_final.json": raw})
    resp = client.post("/api/growbot/policies", json={"job_id": jid, "name": "x"})
    assert resp.status_code == 422 and why in resp.json()["detail"]
    assert lib.rows == {}


def test_the_contract_says_what_the_pad_must_feed() -> None:
    cam = summary(policy(["gyro_z", "cmd_yaw", "cmd_forward", "cmd_pitch"], facing="camera"))
    assert (cam.facing, cam.speed, cam.pitch, cam.control_hz) == ("camera", True, True, 25)
    with pytest.raises(PolicyError, match="too large"):
        parse(b" " * (MAX_POLICY_BYTES + 1))


async def test_rename_and_delete(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    jid = await _finished(jl, "growbot_heading_walk", {"policy_final.json": FINAL})
    pid = client.post("/api/growbot/policies", json={"job_id": jid, "name": "a"}).json()["id"]
    assert client.patch(f"/api/growbot/policies/{pid}", json={"name": "Brisk"}).json()["name"] == (
        "Brisk"
    )
    assert client.patch(f"/api/growbot/policies/{pid}", json={"name": "\n"}).status_code == 422
    # A row the runner contract moved past: refused with why, never a 500.
    lib.unreadable.add(pid)
    got = client.patch(f"/api/growbot/policies/{pid}", json={"name": "Old"})
    assert got.status_code == 409 and "control rate" in got.json()["detail"]
    assert client.delete(f"/api/growbot/policies/{pid}").status_code == 204
    assert client.delete(f"/api/growbot/policies/{pid}").status_code == 404
    other = str(uuid.uuid4())
    assert client.patch(f"/api/growbot/policies/{other}", json={"name": "x"}).status_code == 404
    assert client.delete("/api/growbot/policies/not-an-id").status_code == 422


async def test_no_launcher_means_nothing_to_add(tmp_path: Path) -> None:
    client, _, jl, _ = _app(tmp_path)

    async def down() -> list[dict[str, Any]]:
        from jbrain.jlaunch import JlaunchError

        raise JlaunchError("off")

    jl.list_jobs = down  # type: ignore[method-assign]
    assert client.get("/api/growbot/policies/sources").json() == []


async def test_the_debug_token_lists_and_adds(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    jid = await _finished(jl, "growbot_camera_walk", {"policy_final.json": FINAL})
    assert client.get("/api/debug/growbot/policies/sources").json()[0]["job_id"] == jid
    resp = client.post("/api/debug/growbot/policies", json={"job_id": jid, "name": "cam"})
    assert resp.status_code == 201
    assert client.get("/api/debug/growbot/policies").json()[0]["name"] == "cam"
    assert lib.contexts[-1].principal_id == "debug-console"


async def test_the_sidecar_reads_a_policys_json_with_its_token(tmp_path: Path) -> None:
    client, _, jl, auth = _app(tmp_path)
    jid = await _finished(jl, "growbot_heading_walk", {"policy_final.json": FINAL})
    pid = client.post("/api/growbot/policies", json={"job_id": jid, "name": "a"}).json()["id"]
    assert client.get(f"/internal/growbot/policies/{pid}", headers=auth).json() == policy()
    assert client.get(f"/internal/growbot/policies/{pid}").status_code == 401
    missing = str(uuid.uuid4())
    assert client.get(f"/internal/growbot/policies/{missing}", headers=auth).status_code == 404


def test_the_library_needs_the_owner(tmp_path: Path) -> None:
    client, *_ = _app(tmp_path)

    def refuse() -> PrincipalInfo:
        raise HTTPException(status_code=403, detail="no")

    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[owner_only] = refuse
    app.dependency_overrides[current_debug_principal] = refuse
    for path in ("/api/growbot/policies", "/api/debug/growbot/policies"):
        assert client.get(path).status_code == 403


# --- a move that names a library policy (api/growbot.py) -----------------------------------


class _Repo:
    async def list_robots(self, ctx: SessionContext) -> list[Any]:
        return [SimpleNamespace(code="gb-a1", revoked_at=None)]


async def test_a_walk_names_a_library_policy_for_the_creature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    def car(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "state": "accepted", "why": ""})

    monkeypatch.setattr(growbot, "_transport", httpx.MockTransport(car))
    client, lib, jl, _ = _app(tmp_path)
    app = client.app
    assert isinstance(app, FastAPI)
    app.include_router(growbot.router, prefix="/api")
    app.state.growbot_repo = _Repo()
    app.state.settings = SimpleNamespace(growbot_url="http://growbot:8000")
    jid = await _finished(jl, "growbot_heading_walk", {"policy_final.json": FINAL})
    pid = client.post("/api/growbot/policies", json={"job_id": jid, "name": "a"}).json()["id"]
    move = "/api/growbot/robots/gb-a1/move"
    resp = client.post(move, json={"move": "spin_left", "secs": 5, "policy": pid})
    assert resp.status_code == 200
    assert sent == [{"code": "gb-a1", "move": "spin_left", "secs": 5, "policy": pid}]
    # A gesture runs no policy, a missing one is never sent, and an id is a uuid.
    assert client.post(move, json={"move": "bow", "policy": pid}).status_code == 422
    missing = str(uuid.uuid4())
    assert client.post(move, json={"move": "forward", "policy": missing}).status_code == 404
    assert client.post(move, json={"move": "forward", "policy": "../x"}).status_code == 422
    assert len(sent) == 1


def _keep(jl: FakeJlaunchClient, slug: str, title: str, body: bytes, job: str) -> None:
    blob = hashlib.sha1(b"blob %d\0" % len(body) + body, usedforsecurity=False).hexdigest()
    jl.kept[slug] = ({"slug": slug, "title": title, "job": job, "blob": blob}, body)


async def test_kept_policies_sync_from_the_training_repo(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    # A walk added from a job stays whatever the repo says.
    jid = await _finished(jl, "growbot_heading_walk", {"policy_final.json": FINAL})
    from_job = client.post("/api/growbot/policies", json={"job_id": jid, "name": "Mine"}).json()
    _keep(jl, "look-v1", "Look v1", FINAL, "growbot_look")
    _keep(jl, "walk-v2", "Walk  v2", FINAL, "growbot_heading_walk")
    got = client.post("/api/growbot/policies/refresh")
    assert got.status_code == 200
    assert got.json()["added"] == ["Look v1", "Walk v2"] and got.json()["skipped"] == []
    rows = {r["name"]: r for r in client.get("/api/growbot/policies").json()}
    assert rows["Look v1"]["source_spec"] == "growbot_look"
    assert rows["Look v1"]["source_job"].startswith("repo:look-v1@")
    look_id = rows["Look v1"]["id"]

    # Nothing changed: nothing happens.
    again = client.post("/api/growbot/policies/refresh").json()
    assert (again["added"], again["updated"], again["removed"]) == ([], [], [])

    # The owner renames it, then the repo changes its file: same row, new weights, name kept.
    client.patch(f"/api/growbot/policies/{look_id}", json={"name": "Steady look"})
    newer = json.dumps(policy(facing="camera")).encode()
    _keep(jl, "look-v1", "Look v1", newer, "growbot_look")
    del jl.kept["walk-v2"]
    _keep(jl, "bad", "Broken", b"not json", "growbot_look")
    _keep(jl, "math", "Math", FINAL, "erdos_straus_1e12")
    changed = client.post("/api/growbot/policies/refresh").json()
    assert changed["updated"] == ["Steady look"] and changed["removed"] == ["Walk v2"]
    assert changed["added"] == [] and changed["skipped"][0].startswith("Broken: ")
    row, body = lib.rows[look_id]
    assert row.name == "Steady look" and row.contract.facing == "camera" and body["obs_spec"]
    names = {r["name"] for r in client.get("/api/growbot/policies").json()}
    assert names == {"Steady look", from_job["name"]}


async def test_kept_policy_refresh_errors_and_debug_twin(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    _keep(jl, "look-v1", "Look v1", FINAL, "growbot_look")
    assert client.post("/api/debug/growbot/policies/refresh").json()["added"] == ["Look v1"]
    jl.refresh_result = JlaunchError("policies/index.toml policy 1: slug must be", status=422)
    bad = client.post("/api/growbot/policies/refresh")
    assert bad.status_code == 422 and "slug must be" in bad.json()["detail"]
    jl.refresh_result = {"commit": "b" * 40, "added": [], "removed": []}

    async def down() -> dict[str, Any]:
        raise JlaunchError("connect refused")

    jl.list_kept_policies = down  # type: ignore[method-assign]
    assert client.post("/api/growbot/policies/refresh").status_code == 502
    assert [r.name for r, _p in lib.rows.values()] == ["Look v1"]


async def test_kept_sync_heals_races_and_odd_listings(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    _keep(jl, "look-v1", "Look v1", FINAL, "growbot_look")
    client.post("/api/growbot/policies/refresh")
    # A second process's sync added the same kept walk again: the extra row goes.
    ((row, body),) = lib.rows.values()
    await lib.add(lib.contexts[0], "Look v1", body, row.bytes, row.source_job, row.source_spec)
    assert len(lib.rows) == 2
    client.post("/api/growbot/policies/refresh")
    assert [r.name for r, _p in lib.rows.values()] == ["Look v1"]

    # The launcher re-read the repo between the list and the read: skipped, not mis-tagged.
    entry, _body = jl.kept["look-v1"]
    newer = json.dumps(policy(facing="camera")).encode()
    jl.kept["look-v1"] = ({**entry, "blob": "f" * 40}, newer)
    got = client.post("/api/growbot/policies/refresh").json()
    assert got["updated"] == [] and "changed while being read" in got["skipped"][0]

    # A row deleted mid-sync isn't reported as updated.
    async def gone(*_a: Any, **_k: Any) -> bool:
        return False

    _keep(jl, "look-v1", "Look v1", newer, "growbot_look")
    lib.replace_policy = gone  # type: ignore[method-assign]
    assert client.post("/api/growbot/policies/refresh").json()["updated"] == []

    # A listing entry that breaks the launcher's own rules is ignored, and so its row goes.
    jl.kept = {
        "Bad Slug": (
            {"slug": "Bad Slug", "title": "x", "job": "growbot_look", "blob": "a" * 40},
            FINAL,
        )
    }
    assert client.post("/api/growbot/policies/refresh").json()["removed"] == ["Look v1"]
    assert lib.rows == {}


async def test_a_bad_index_on_the_launcher_is_a_422(tmp_path: Path) -> None:
    client, lib, jl, _ = _app(tmp_path)
    jl.kept_error = "policies/index.toml policy 1: policies/x/y.json isn't in the repo"
    bad = client.post("/api/growbot/policies/refresh")
    assert bad.status_code == 422 and "isn't in the repo" in bad.json()["detail"]
    assert lib.rows == {}
