"""GrowBot's access routes (api/growbot.py): the owner's ticket and robot keys, the same four
through the debug token, and the sidecar's redeem and verify behind `growbot.token`.

The repo is an in-memory stand-in with the real one's rules (one-time, 120 s, hashed keys);
the SQL itself, and its RLS, run against real Postgres in
tests/integration/test_growbot_rls.py.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import growbot
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.auth.keys import hash_token
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.growbot.repo import TICKET_TTL, MintedTicket, RobotInfo
from jbrain.storage import FsInternalSecrets

OWNER = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")


@dataclass
class FakeRepo:
    now: datetime = field(default_factory=lambda: datetime(2026, 11, 10, tzinfo=UTC))
    tickets: dict[str, tuple[datetime, bool]] = field(default_factory=dict)
    robots: dict[str, tuple[str, datetime, datetime | None]] = field(default_factory=dict)
    contexts: list[SessionContext] = field(default_factory=list)

    async def mint_ticket(self, ctx: SessionContext) -> MintedTicket:
        self.contexts.append(ctx)
        ticket = f"t{len(self.tickets)}-secret"
        self.tickets[hash_token(ticket)] = (self.now + TICKET_TTL, False)
        return MintedTicket(ticket=ticket, expires_at=self.now + TICKET_TTL)

    async def redeem_ticket(self, ctx: SessionContext, ticket: str) -> bool:
        self.contexts.append(ctx)
        h = hash_token(ticket)
        entry = self.tickets.get(h)
        if entry is None or entry[1] or entry[0] <= self.now:
            return False
        self.tickets[h] = (entry[0], True)
        return True

    async def put_robot(self, ctx: SessionContext, code: str) -> str:
        self.contexts.append(ctx)
        key = f"key-{code}-{len(self.contexts)}"
        old = self.robots.get(code)
        created = old[1] if old else self.now
        self.robots[code] = (hash_token(key), created, self.now if old else None)
        return key

    async def list_robots(self, ctx: SessionContext) -> list[RobotInfo]:
        self.contexts.append(ctx)
        return [RobotInfo(c, r[1], r[2]) for c, r in sorted(self.robots.items())]

    async def delete_robot(self, ctx: SessionContext, code: str) -> bool:
        self.contexts.append(ctx)
        return self.robots.pop(code, None) is not None

    async def verify_robot(self, ctx: SessionContext, code: str, key: str) -> bool:
        self.contexts.append(ctx)
        stored = self.robots.get(code)
        return stored is not None and hmac.compare_digest(hash_token(key), stored[0])


def _app(
    tmp_path: Path, repo: FakeRepo, *, lan_addr: str = "https://jbrain.local", mint: bool = True
) -> tuple[FastAPI, dict[str, str]]:
    app = FastAPI()
    app.include_router(growbot.router, prefix="/api")
    app.include_router(growbot.debug_router, prefix="/api")
    app.include_router(growbot.internal_router, prefix="/internal")
    app.state.settings = SimpleNamespace(lan_addr=lan_addr)
    app.state.growbot_repo = repo
    secrets = FsInternalSecrets(tmp_path)
    app.state.growbot_secrets = secrets
    if mint:
        secrets.ensure("growbot")
    app.dependency_overrides[owner_only] = lambda: OWNER
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    token = (secrets.read("growbot") or b"").decode()
    return app, {"Authorization": f"Bearer {token}"}


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()


def test_the_owner_mints_a_ticket_and_its_lan_link(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo, lan_addr="https://box.lan/")
    out = TestClient(app).post("/api/growbot/ticket").json()
    url = urlsplit(out["url"])
    assert (url.scheme, url.hostname, url.port, url.path) == ("https", "box.lan", 8444, "/open")
    assert parse_qs(url.query) == {"ticket": [out["ticket"]]}
    assert hash_token(out["ticket"]) in repo.tickets
    assert repo.contexts == [SessionContext(principal_id=OWNER.id, principal_kind="owner")]


def test_a_ticket_for_a_robot_carries_its_code(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)
    out = TestClient(app).post("/api/growbot/ticket", json={"code": "gb-a1b2c3"}).json()
    assert parse_qs(urlsplit(out["url"]).query)["id"] == ["gb-a1b2c3"]
    bad = TestClient(app).post("/api/growbot/ticket", json={"code": "../x"})
    assert bad.status_code == 422


def test_no_lan_site_refuses_the_ticket_and_mints_nothing(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo, lan_addr="")
    r = TestClient(app).post("/api/growbot/ticket")
    assert r.status_code == 409 and "JBRAIN_LAN_ADDR" in r.json()["detail"]
    assert repo.tickets == {}


def test_robots_mint_rotate_list_and_forget(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)
    client = TestClient(app)
    first = client.post("/api/growbot/robots", json={"code": "gb-a1b2c3"}).json()
    assert first["code"] == "gb-a1b2c3" and first["key"]
    second = client.post("/api/growbot/robots", json={"code": "gb-a1b2c3"}).json()
    assert second["key"] != first["key"]
    listed = client.get("/api/growbot/robots").json()
    assert [r["code"] for r in listed] == ["gb-a1b2c3"]
    assert listed[0]["rotated_at"] is not None
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    assert client.delete("/api/growbot/robots/gb-a1b2c3").status_code == 204
    assert client.delete("/api/growbot/robots/gb-a1b2c3").status_code == 404
    assert client.post("/api/growbot/robots", json={"code": "nope"}).status_code == 422


def test_the_owner_routes_refuse_a_non_owner(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)

    def refuse() -> PrincipalInfo:
        raise HTTPException(status_code=403, detail="owner access required")

    app.dependency_overrides[owner_only] = refuse
    client = TestClient(app)
    assert client.post("/api/growbot/ticket").status_code == 403
    assert client.post("/api/growbot/robots", json={"code": "gb-x"}).status_code == 403
    assert client.get("/api/growbot/robots").status_code == 403
    assert client.delete("/api/growbot/robots/gb-x").status_code == 403
    assert repo.contexts == []


def test_the_debug_token_reaches_the_same_four(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)
    client = TestClient(app)
    assert client.post("/api/debug/growbot/ticket", json={"code": "gb-x"}).status_code == 200
    key = client.post("/api/debug/growbot/robots", json={"code": "gb-x"}).json()["key"]
    assert key
    assert [r["code"] for r in client.get("/api/debug/growbot/robots").json()] == ["gb-x"]
    assert client.delete("/api/debug/growbot/robots/gb-x").status_code == 204
    debug_ctx = SessionContext(principal_id="debug-console", principal_kind="owner")
    assert set(repo.contexts) == {debug_ctx}


def test_the_debug_routes_refuse_without_a_debug_token(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)

    def refuse() -> PrincipalInfo:
        raise HTTPException(status_code=401, detail="unauthorized")

    app.dependency_overrides[current_debug_principal] = refuse
    client = TestClient(app)
    assert client.post("/api/debug/growbot/ticket").status_code == 401
    assert client.get("/api/debug/growbot/robots").status_code == 401
    assert client.post("/api/debug/growbot/robots", json={"code": "gb-x"}).status_code == 401
    assert client.delete("/api/debug/growbot/robots/gb-x").status_code == 401
    assert repo.contexts == []


# --- the sidecar's routes -------------------------------------------------------------------

_INTERNAL = [
    ("/internal/growbot/ticket/redeem", {"ticket": "x"}),
    ("/internal/growbot/robot/verify", {"code": "gb-x", "key": "k"}),
]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer n\xf6".encode("latin-1")},
    ],
)
def test_the_internal_routes_need_the_growbot_token(
    tmp_path: Path, repo: FakeRepo, headers: dict
) -> None:
    app, _ = _app(tmp_path, repo)
    client = TestClient(app)
    for path, body in _INTERNAL:
        assert client.post(path, json=body, headers=headers).status_code == 401
    assert repo.contexts == []


def test_no_minted_token_fails_closed(tmp_path: Path, repo: FakeRepo) -> None:
    app, headers = _app(tmp_path, repo, mint=False)
    assert headers == {"Authorization": "Bearer "}
    client = TestClient(app)
    for path, body in _INTERNAL:
        assert client.post(path, json=body, headers=headers).status_code == 401


def test_another_callers_token_is_refused(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)
    shared = FsInternalSecrets(tmp_path / "internal")
    (tmp_path / "internal").mkdir()
    shared.ensure("wall")
    wall = {"Authorization": f"Bearer {(shared.read('wall') or b'').decode()}"}
    app.state.internal_secrets = shared
    for path, body in _INTERNAL:
        assert TestClient(app).post(path, json=body, headers=wall).status_code == 401


def test_a_ticket_opens_once(tmp_path: Path, repo: FakeRepo) -> None:
    app, headers = _app(tmp_path, repo)
    client = TestClient(app)
    ticket = client.post("/api/growbot/ticket").json()["ticket"]
    redeem = "/internal/growbot/ticket/redeem"
    first = client.post(redeem, json={"ticket": ticket}, headers=headers)
    assert first.status_code == 200 and first.json() == {"ok": True}
    assert client.post(redeem, json={"ticket": ticket}, headers=headers).status_code == 401
    assert repo.contexts[-1] == SessionContext(principal_kind="owner")


def test_an_expired_or_unknown_ticket_is_refused(tmp_path: Path, repo: FakeRepo) -> None:
    app, headers = _app(tmp_path, repo)
    client = TestClient(app)
    ticket = client.post("/api/growbot/ticket").json()["ticket"]
    repo.now += TICKET_TTL + timedelta(seconds=1)
    redeem = "/internal/growbot/ticket/redeem"
    assert client.post(redeem, json={"ticket": ticket}, headers=headers).status_code == 401
    assert client.post(redeem, json={"ticket": "made-up"}, headers=headers).status_code == 401
    # Malformed bodies are refusals too, never a 422 the sidecar must tell apart.
    assert client.post(redeem, json={}, headers=headers).status_code == 401
    long = {"ticket": "x" * 257}
    assert client.post(redeem, json=long, headers=headers).status_code == 401


def test_a_chip_key_is_checked_against_its_code(tmp_path: Path, repo: FakeRepo) -> None:
    app, headers = _app(tmp_path, repo)
    client = TestClient(app)
    key = client.post("/api/growbot/robots", json={"code": "gb-a1"}).json()["key"]
    other = client.post("/api/growbot/robots", json={"code": "gb-b2"}).json()["key"]
    verify = "/internal/growbot/robot/verify"
    ok = client.post(verify, json={"code": "gb-a1", "key": key}, headers=headers)
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    for body in (
        {"code": "gb-a1", "key": other},  # another robot's key
        {"code": "gb-a1", "key": ""},
        {"code": "gb-zz", "key": key},  # no such robot
        {"code": "../gb-a1", "key": key},  # not a code at all
        {"code": "gb-a1", "key": key + "x" * 300},
        {},
    ):
        assert client.post(verify, json=body, headers=headers).status_code == 401, body


def test_a_rotated_key_revokes_the_old_one(tmp_path: Path, repo: FakeRepo) -> None:
    app, headers = _app(tmp_path, repo)
    client = TestClient(app)
    old = client.post("/api/growbot/robots", json={"code": "gb-a1"}).json()["key"]
    new = client.post("/api/growbot/robots", json={"code": "gb-a1"}).json()["key"]
    verify = "/internal/growbot/robot/verify"
    assert (
        client.post(verify, json={"code": "gb-a1", "key": old}, headers=headers).status_code == 401
    )
    assert (
        client.post(verify, json={"code": "gb-a1", "key": new}, headers=headers).status_code == 200
    )
    client.delete("/api/growbot/robots/gb-a1")
    assert (
        client.post(verify, json={"code": "gb-a1", "key": new}, headers=headers).status_code == 401
    )


def test_signing_the_phones_out_rotates_the_session_epoch(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)
    client = TestClient(app)
    epoch = tmp_path / "growbot-session.token"
    assert not epoch.exists()
    assert client.post("/api/growbot/sessions/reset").status_code == 204
    first = epoch.read_bytes()
    assert len(first) == 64 and (epoch.stat().st_mode & 0o777) == 0o600
    assert client.post("/api/debug/growbot/sessions/reset").status_code == 204
    second = epoch.read_bytes()
    assert second != first
    # The sidecar's bearer is untouched: only the cookie key moves.
    assert (tmp_path / "growbot.token").read_bytes() == app.state.growbot_secrets.read("growbot")
    assert list(tmp_path.glob(".*.tmp")) == []


def test_signing_out_needs_the_owner_or_a_debug_token(tmp_path: Path, repo: FakeRepo) -> None:
    app, _ = _app(tmp_path, repo)

    def refuse() -> PrincipalInfo:
        raise HTTPException(status_code=403, detail="no")

    app.dependency_overrides[owner_only] = refuse
    app.dependency_overrides[current_debug_principal] = refuse
    client = TestClient(app)
    assert client.post("/api/growbot/sessions/reset").status_code == 403
    assert client.post("/api/debug/growbot/sessions/reset").status_code == 403
    assert not (tmp_path / "growbot-session.token").exists()


def test_a_sign_out_that_cannot_write_says_so(
    tmp_path: Path, repo: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _app(tmp_path, repo)

    def broken(name: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(app.state.growbot_secrets, "rotate", broken)
    r = TestClient(app).post("/api/growbot/sessions/reset")
    assert r.status_code == 503
