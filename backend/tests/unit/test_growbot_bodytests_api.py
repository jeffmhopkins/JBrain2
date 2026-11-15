"""The body test's routes: the sidecar keeps a chunk of a run's log, the owner lists runs (the Body
tab's live mirror) and reads one whole, and a debug token reads the same (owner, 2026-11-15:
"log everything so you can review after")."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import growbot_bodytests
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.growbot.bodytests import Chunk, Run
from jbrain.storage import FsInternalSecrets

OWNER = PrincipalInfo(id="o1", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")
_RUN = "bt-mhz8k2-a1b2c3"
_PID = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
_AT = datetime(2026, 11, 15, 22, 0, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.kept: list[tuple[SessionContext, Chunk]] = []
        self.asked: list[tuple[str, SessionContext, Any]] = []
        self.number: int | None = 7

    async def record(self, ctx: SessionContext, chunk: Chunk) -> int | None:
        self.kept.append((ctx, chunk))
        return self.number

    async def runs(self, ctx: SessionContext, *, robot: str = "", limit: int = 10) -> list[Run]:
        self.asked.append(("runs", ctx, (robot, limit)))
        return [_run()]

    async def run(self, ctx: SessionContext, number: int) -> Run | None:
        self.asked.append(("run", ctx, number))
        return _run() if number == 7 else None

    async def rows(self, ctx: SessionContext, number: int) -> list[dict[str, Any]]:
        return [{"t": 1, "k": "e", "e": "bt_start"}, {"t": 2, "k": "s", "h": 0.5}]


def _run() -> Run:
    return Run(
        id=7,
        run=_RUN,
        robot="gb-abc123",
        persona_id=None,
        started_at=_AT,
        updated_at=_AT,
        status="done",
        skills={"look": _PID},
        summary={"step": "result", "results": {"turn": {"v": "ok"}}},
        rows=2,
    )


def _refuse() -> None:
    raise HTTPException(401)


def _app(tmp_path: Path, store: _Store | None) -> tuple[FastAPI, str]:
    app = FastAPI()
    app.include_router(growbot_bodytests.router, prefix="/api")
    app.include_router(growbot_bodytests.debug_router, prefix="/api")
    app.include_router(growbot_bodytests.internal_router, prefix="/internal")
    if store is not None:
        app.state.growbot_bodytests = store
    secrets = FsInternalSecrets(tmp_path)
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    return app, (secrets.read("growbot") or b"").decode()


def _chunk(**over: Any) -> dict[str, Any]:
    return {
        "run": _RUN,
        "robot": "gb-abc123",
        "persona": _PID,
        "seq": 3,
        "status": "running",
        "summary": {"step": "turn", "falls": 0},
        "rows": [{"t": 1, "k": "s", "h": 12.5}],
        "skills": {"look": _PID},
        **over,
    }


def test_the_sidecar_keeps_a_chunk(tmp_path: Path) -> None:
    store = _Store()
    app, token = _app(tmp_path, store)
    client = TestClient(app)
    assert client.post("/internal/growbot/bodytests", json=_chunk()).status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    got = client.post("/internal/growbot/bodytests", json=_chunk(), headers=auth)
    assert got.status_code == 200 and got.json() == {"ok": True, "number": 7}
    ctx, chunk = store.kept[0]
    assert ctx.principal_kind == "owner"
    assert (chunk.run, chunk.robot, chunk.seq, chunk.persona_id) == (_RUN, "gb-abc123", 3, _PID)
    assert chunk.rows == [{"t": 1, "k": "s", "h": 12.5}] and chunk.skills == {"look": _PID}
    store.number = None
    got = client.post("/internal/growbot/bodytests", json=_chunk(), headers=auth)
    assert got.json() == {"ok": False, "number": None}


@pytest.mark.parametrize(
    "over",
    [
        {"run": "bt-../x"},
        {"robot": "Fish"},
        {"seq": -1},
        {"status": "lost"},
        {"persona": "not-a-uuid"},
        {"rows": [{"t": i} for i in range(1001)]},
        {"skills": {"a": "1", "b": "2", "c": "3", "d": "4"}},
    ],
)
def test_the_sidecar_route_refuses_odd_chunks(tmp_path: Path, over: dict[str, Any]) -> None:
    store = _Store()
    app, token = _app(tmp_path, store)
    got = TestClient(app).post(
        "/internal/growbot/bodytests",
        json=_chunk(**over),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert got.status_code == 422 and store.kept == []


def test_the_owner_lists_runs_and_reads_one_whole(tmp_path: Path) -> None:
    store = _Store()
    app, _ = _app(tmp_path, store)
    client = TestClient(app)
    app.dependency_overrides[owner_only] = _refuse
    assert client.get("/api/growbot/bodytests").status_code == 401
    app.dependency_overrides[owner_only] = lambda: OWNER
    got = client.get("/api/growbot/bodytests", params={"robot": "gb-abc123", "limit": 5})
    assert got.status_code == 200
    assert got.json()[0]["number"] == 7 and got.json()[0]["summary"]["step"] == "result"
    assert store.asked[0][2] == ("gb-abc123", 5)
    one = client.get("/api/growbot/bodytests/7").json()
    assert one["run"] == _RUN and [r["t"] for r in one["log"]] == [1, 2]
    assert client.get("/api/growbot/bodytests/8").status_code == 404
    assert client.get("/api/growbot/bodytests", params={"robot": "../x"}).status_code == 422
    assert client.get("/api/growbot/bodytests", params={"limit": 51}).status_code == 422


def test_a_debug_token_reads_the_same(tmp_path: Path) -> None:
    store = _Store()
    app, _ = _app(tmp_path, store)
    client = TestClient(app)
    app.dependency_overrides[current_debug_principal] = _refuse
    assert client.get("/api/debug/growbot/bodytests").status_code == 401
    assert client.get("/api/debug/growbot/bodytests/7").status_code == 401
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    assert client.get("/api/debug/growbot/bodytests").json()[0]["number"] == 7
    assert client.get("/api/debug/growbot/bodytests/7").json()["rows"] == 2
    assert all(a[1].principal_kind == "owner" for a in store.asked)


def test_a_box_without_the_store_says_so(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, None)
    app.dependency_overrides[owner_only] = lambda: OWNER
    assert TestClient(app).get("/api/growbot/bodytests").status_code == 503
