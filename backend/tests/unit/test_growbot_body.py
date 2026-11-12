"""The Body tab's calibration and direct moves (api/growbot_body.py): the owner's read and save,
the same through the debug token, the sidecar's read, and a move handed to the sidecar.

The repo is an in-memory stand-in; the SQL and its RLS run against real Postgres in
tests/integration/test_growbot_rls.py. The sidecar is an httpx.MockTransport.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import growbot, growbot_body
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.storage import FsInternalSecrets

OWNER = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")
CODE = "gb-a1b2c3"


@dataclass
class FakeBody:
    cals: dict[str, dict[str, Any] | None] = field(default_factory=lambda: {CODE: None})
    contexts: list[SessionContext] = field(default_factory=list)

    async def get_cal(self, ctx: SessionContext, code: str) -> tuple[bool, dict[str, Any] | None]:
        self.contexts.append(ctx)
        return code in self.cals, self.cals.get(code)

    async def set_cal(self, ctx: SessionContext, code: str, cal: dict[str, Any]) -> bool:
        self.contexts.append(ctx)
        if code not in self.cals:
            return False
        self.cals[code] = cal
        return True


@dataclass
class Sidecar:
    status: int = 200
    sent: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(
            {"path": req.url.path, "auth": req.headers["authorization"], **json.loads(req.content)}
        )
        return httpx.Response(self.status, json={"sent": self.sent})


@pytest.fixture
def sidecar() -> Iterator[Sidecar]:
    fake = Sidecar()
    growbot._transport = httpx.MockTransport(fake)
    yield fake
    growbot._transport = None


def _app(tmp_path: Path, body: FakeBody) -> tuple[FastAPI, dict[str, str]]:
    app = FastAPI()
    app.include_router(growbot_body.router, prefix="/api")
    app.include_router(growbot_body.debug_router, prefix="/api")
    app.include_router(growbot_body.internal_router, prefix="/internal")
    app.state.settings = SimpleNamespace(growbot_url="http://growbot:8810")
    app.state.growbot_body = body
    secrets = FsInternalSecrets(tmp_path)
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    app.dependency_overrides[owner_only] = lambda: OWNER
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    token = (secrets.read("growbot") or b"").decode()
    return app, {"Authorization": f"Bearer {token}"}


DEFAULTS = {
    "L_OFF": 0,
    "R_OFF": 0,
    "SWAP": 0,
    "L_SIGN": -1,
    "R_SIGN": 1,
    "IMU_SIGN": [1, -1, 1],
    "gain": 1.0,
}


def test_an_uncalibrated_robot_reads_upstreams_defaults_unsaved(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, FakeBody())
    got = TestClient(app).get(f"/api/growbot/robots/{CODE}/cal").json()
    assert got == {"cal": DEFAULTS, "saved": False}


def test_the_owner_saves_a_calibration_and_reads_it_back(tmp_path: Path) -> None:
    body = FakeBody()
    app, _ = _app(tmp_path, body)
    client = TestClient(app)
    cal = {**DEFAULTS, "L_OFF": 7, "R_OFF": -4, "SWAP": 1, "IMU_SIGN": [-1, -1, 1], "gain": 0.9}
    put = client.put(f"/api/growbot/robots/{CODE}/cal", json=cal)
    assert put.json() == {"cal": cal, "saved": True}
    assert body.cals[CODE] == cal
    assert client.get(f"/api/growbot/robots/{CODE}/cal").json() == {"cal": cal, "saved": True}
    # A partial save fills the rest with the defaults, as the creature would.
    part = client.put(f"/api/growbot/robots/{CODE}/cal", json={"R_OFF": 2}).json()
    assert part["cal"] == {**DEFAULTS, "R_OFF": 2}
    assert body.contexts[0] == SessionContext(principal_id=OWNER.id, principal_kind="owner")


@pytest.mark.parametrize(
    "bad",
    [
        {"L_OFF": 31},
        {"R_OFF": -31},
        {"L_OFF": 1.5},
        {"SWAP": 2},
        {"L_SIGN": 0},
        {"IMU_SIGN": [1, 1]},
        {"IMU_SIGN": [1, 2, 1]},
        {"gain": 2.5},
        {"gain": -0.1},
    ],
)
def test_a_calibration_outside_upstreams_ranges_is_refused(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    body = FakeBody()
    app, _ = _app(tmp_path, body)
    assert TestClient(app).put(f"/api/growbot/robots/{CODE}/cal", json=bad).status_code == 422
    assert body.cals[CODE] is None


def test_an_unknown_robot_is_404_and_a_bad_code_422(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, FakeBody())
    client = TestClient(app)
    assert client.get("/api/growbot/robots/gb-zzz/cal").status_code == 404
    assert client.put("/api/growbot/robots/gb-zzz/cal", json={}).status_code == 404
    assert client.get("/api/growbot/robots/nope/cal").status_code == 422


def test_a_stored_row_of_an_older_shape_reads_as_unsaved_defaults(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, FakeBody(cals={CODE: {"L_OFF": 99}}))
    got = TestClient(app).get(f"/api/growbot/robots/{CODE}/cal").json()
    assert got == {"cal": DEFAULTS, "saved": False}


def test_the_sidecar_reads_null_until_saved_then_the_calibration(tmp_path: Path) -> None:
    body = FakeBody()
    app, auth = _app(tmp_path, body)
    client = TestClient(app)
    path = f"/internal/growbot/robots/{CODE}/cal"
    assert client.get(path, headers=auth).json() == {"cal": None}
    body.cals[CODE] = {**DEFAULTS, "L_OFF": 3}
    assert client.get(path, headers=auth).json() == {"cal": {**DEFAULTS, "L_OFF": 3}}
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert body.contexts[-1] == SessionContext(principal_kind="owner")


def test_the_debug_token_reads_and_saves_too(tmp_path: Path) -> None:
    body = FakeBody()
    app, _ = _app(tmp_path, body)
    client = TestClient(app)
    path = f"/api/debug/growbot/robots/{CODE}/cal"
    assert client.get(path).json()["saved"] is False
    assert client.put(path, json={"L_OFF": -2}).json()["cal"]["L_OFF"] == -2
    assert body.contexts[-1].principal_id == "debug-console"


def test_the_owner_routes_refuse_a_non_owner(tmp_path: Path, sidecar: Sidecar) -> None:
    app, _ = _app(tmp_path, FakeBody())

    def refuse() -> PrincipalInfo:
        raise HTTPException(status_code=403, detail="owner access required")

    app.dependency_overrides[owner_only] = refuse
    client = TestClient(app)
    assert client.get(f"/api/growbot/robots/{CODE}/cal").status_code == 403
    assert client.put(f"/api/growbot/robots/{CODE}/cal", json={}).status_code == 403
    assert client.post(f"/api/growbot/robots/{CODE}/act", json={"kind": "stop"}).status_code == 403
    assert sidecar.calls == []


def test_a_move_goes_to_the_sidecar_as_a_chip_frame(tmp_path: Path, sidecar: Sidecar) -> None:
    app, auth = _app(tmp_path, FakeBody())
    client = TestClient(app)
    steps = [{"l": 97, "r": 86, "ms": 300}, {"l": 90, "r": 90, "ms": 400}]
    out = client.post(f"/api/growbot/robots/{CODE}/act", json={"kind": "act", "steps": steps})
    assert out.json() == {"sent": True}
    assert sidecar.calls[0] == {
        "path": "/internal/robots/act",
        "auth": auth["Authorization"],
        "code": CODE,
        "frame": {"t": "act", "steps": steps, "mode": "replace"},
    }
    client.post(f"/api/growbot/robots/{CODE}/act", json={"kind": "wiggle"})
    client.post(f"/api/growbot/robots/{CODE}/act", json={"kind": "stop"})
    assert [c["frame"] for c in sidecar.calls[1:]] == [
        {"t": "routine", "name": "wiggle"},
        {"t": "stop"},
    ]


def test_a_saved_swap_is_applied_on_the_wire(tmp_path: Path, sidecar: Sidecar) -> None:
    body = FakeBody(cals={CODE: {**DEFAULTS, "SWAP": 1}})
    app, _ = _app(tmp_path, body)
    steps = [{"l": 125, "r": 90, "ms": 450}]
    TestClient(app).post(f"/api/growbot/robots/{CODE}/act", json={"kind": "act", "steps": steps})
    assert sidecar.calls[0]["frame"]["steps"] == [{"l": 90, "r": 125, "ms": 450}]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "act", "steps": []},
        {"kind": "dance"},
        {"kind": "act", "steps": [{"l": 200, "r": 90, "ms": 300}]},
        {"kind": "act", "steps": [{"l": 90, "r": 90, "ms": 20}]},
        {"kind": "act", "steps": [{"l": 90, "r": 90, "ms": 300}] * 13},
    ],
)
def test_a_bad_move_never_reaches_the_sidecar(
    tmp_path: Path, sidecar: Sidecar, bad: dict[str, Any]
) -> None:
    app, _ = _app(tmp_path, FakeBody())
    assert TestClient(app).post(f"/api/growbot/robots/{CODE}/act", json=bad).status_code == 422
    assert sidecar.calls == []


def test_a_move_says_when_the_robot_is_away_or_the_sidecar_down(
    tmp_path: Path, sidecar: Sidecar
) -> None:
    app, _ = _app(tmp_path, FakeBody())
    client = TestClient(app)
    path = f"/api/growbot/robots/{CODE}/act"
    sidecar.sent = False
    assert client.post(path, json={"kind": "stop"}).status_code == 409
    sidecar.status = 500
    assert client.post(path, json={"kind": "stop"}).status_code == 503
    assert client.post("/api/growbot/robots/gb-zzz/act", json={"kind": "stop"}).status_code == 404
