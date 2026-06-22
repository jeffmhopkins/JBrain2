"""The live location feed (JBrain360 M3b owner, M4d member): broadcaster fan-out,
OwnTracks parsing, the deliver/audit helper, the per-viewer scope filter, the
CSWSH Origin gate, and the WS auth rejection.

The WS *pump* itself (racing queue.get against the socket) is operational glue
exercised at deploy, not in CI — so the testable core is factored into
`deliver_fix` / `visible_to` / `origin_allowed`, and only the auth gate is driven
through the real endpoint.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jbrain.api.live import deliver_fix, live_out, origin_allowed, visible_to
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.locations.live import LiveBroadcaster, LiveFix, live_fix_from_owntracks
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeViewScopeRepo

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
TST = int(NOW.timestamp())


def _fix(subject_id: str = "sub-1") -> LiveFix:
    return LiveFix(
        subject_id=subject_id,
        latitude=40.0,
        longitude=-74.0,
        accuracy_m=12.5,
        battery_pct=88,
        velocity_mps=10.0,
        captured_at=NOW,
    )


# --- LiveBroadcaster ---------------------------------------------------------


def test_broadcaster_fans_out_to_every_subscriber() -> None:
    async def go() -> None:
        b = LiveBroadcaster()
        a, c = b.subscribe(), b.subscribe()
        assert b.subscriber_count == 2
        fix = _fix()
        b.publish(fix)
        assert (await a.get()) is fix
        assert (await c.get()) is fix
        b.unsubscribe(a)
        assert b.subscriber_count == 1

    asyncio.run(go())


def test_broadcaster_drops_oldest_on_overflow() -> None:
    async def go() -> None:
        b = LiveBroadcaster(maxsize=2)
        q = b.subscribe()
        first, second, third = _fix("a"), _fix("b"), _fix("c")
        b.publish(first)
        b.publish(second)
        b.publish(third)  # queue full -> oldest (first) dropped to make room
        assert (await q.get()) is second
        assert (await q.get()) is third
        assert q.empty()

    asyncio.run(go())


# --- live_fix_from_owntracks -------------------------------------------------


def test_live_fix_from_owntracks_parses_location() -> None:
    body = {
        "_type": "location",
        "lat": 40.0,
        "lon": -74.0,
        "tst": TST,
        "acc": 9.0,
        "batt": 77,
        "vel": 36.0,
    }
    fix = live_fix_from_owntracks("sub-9", body)
    assert fix is not None
    assert fix.subject_id == "sub-9"
    assert (fix.latitude, fix.longitude) == (40.0, -74.0)
    assert fix.accuracy_m == 9.0 and fix.battery_pct == 77
    assert fix.velocity_mps == pytest.approx(10.0)  # 36 km/h -> 10 m/s
    assert fix.captured_at == NOW


@pytest.mark.parametrize(
    "subject,body",
    [
        ("", {"_type": "location", "lat": 1.0, "lon": 2.0, "tst": TST}),  # no subject
        ("s", {"_type": "transition", "lat": 1.0, "lon": 2.0, "tst": TST}),  # not a location
        ("s", {"_type": "location", "lat": 999.0, "lon": 2.0, "tst": TST}),  # schema-invalid
        ("s", "not-a-dict"),
    ],
)
def test_live_fix_from_owntracks_rejects(subject: str, body: object) -> None:
    assert live_fix_from_owntracks(subject, body) is None


# --- live_out ----------------------------------------------------------------


def test_live_out_shape() -> None:
    out = live_out(_fix("sub-1"))
    assert out == {
        "subject_id": "sub-1",
        "lat": 40.0,
        "lon": -74.0,
        "accuracy_m": 12.5,
        "battery_pct": 88,
        "velocity_mps": 10.0,
        "captured_at": NOW.isoformat(),
    }


# --- deliver_fix -------------------------------------------------------------


class _RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []  # (target_subject_id, path)
        self.fail = fail

    async def record_view(
        self,
        ctx: SessionContext,
        *,
        viewer_principal_id: str,
        viewer_subject_id: str,
        target_subject_id: str,
        path: str,
    ) -> None:
        if self.fail:
            raise RuntimeError("audit down")
        self.calls.append((target_subject_id, path))


OWNER = PrincipalInfo(id="p-owner", kind="owner", label="owner", subject_id="")
CTX = SessionContext(principal_id="p-owner", principal_kind="owner")
# A member viewer whose subject is "sub-self", grouped with "sub-1".
MEMBER = PrincipalInfo(id="p-mem", kind="device_key", label="phone", subject_id="sub-self")
MEMBER_CTX = SessionContext(
    principal_id="p-mem", principal_kind="device_key", subject_id="sub-self"
)


def _all_visible() -> FakeViewScopeRepo:
    return FakeViewScopeRepo(allowed={("sub-self", "sub-1")})


def test_deliver_fix_sends_and_audits_once_per_subject() -> None:
    async def go() -> None:
        sent: list[dict] = []
        sink = _RecordingSink()
        audited: set[str] = set()

        async def send(payload: dict) -> None:
            sent.append(payload)

        vs = _all_visible()
        for sid in ("sub-1", "sub-1", "sub-2"):  # same subject twice, then a new one
            await deliver_fix(send, sink, vs, CTX, OWNER, _fix(sid), audited, {})

        # The owner sees every fix...
        assert [p["subject_id"] for p in sent] == ["sub-1", "sub-1", "sub-2"]
        # ...but the who-saw-whom row is written once per subject per connection.
        assert sink.calls == [("sub-1", "live"), ("sub-2", "live")]
        assert audited == {"sub-1", "sub-2"}

    asyncio.run(go())


def test_deliver_fix_survives_audit_failure() -> None:
    async def go() -> None:
        sent: list[dict] = []
        sink = _RecordingSink(fail=True)

        async def send(payload: dict) -> None:
            sent.append(payload)

        # An audit-write failure is swallowed (logged) — the stream is never dropped.
        await deliver_fix(send, sink, _all_visible(), CTX, OWNER, _fix("sub-1"), set(), {})
        assert len(sent) == 1

    asyncio.run(go())


def test_deliver_fix_drops_a_subject_the_member_cannot_see() -> None:
    async def go() -> None:
        sent: list[dict] = []
        sink = _RecordingSink()
        vs = _all_visible()  # member sub-self may see sub-1, NOT sub-2

        async def send(payload: dict) -> None:
            sent.append(payload)

        await deliver_fix(send, sink, vs, MEMBER_CTX, MEMBER, _fix("sub-1"), set(), {})  # group
        await deliver_fix(send, sink, vs, MEMBER_CTX, MEMBER, _fix("sub-self"), set(), {})  # own
        await deliver_fix(send, sink, vs, MEMBER_CTX, MEMBER, _fix("sub-2"), set(), {})  # hidden

        assert [p["subject_id"] for p in sent] == ["sub-1", "sub-self"]
        # The hidden subject is never sent AND never audited.
        assert ("sub-2", "live") not in sink.calls


# --- visible_to / origin_allowed --------------------------------------------


def test_visible_to_owner_sees_every_subject() -> None:
    vs = FakeViewScopeRepo()  # empty scope
    assert asyncio.run(visible_to(OWNER, "anyone", vs, {})) is True


def test_visible_to_member_self_and_group_only() -> None:
    async def go() -> None:
        vs = _all_visible()
        cache: dict[str, bool] = {}
        assert await visible_to(MEMBER, "sub-self", vs, cache) is True  # own subject
        assert await visible_to(MEMBER, "sub-1", vs, cache) is True  # group member
        assert await visible_to(MEMBER, "sub-2", vs, cache) is False  # outsider
        assert cache == {"sub-1": True, "sub-2": False}  # decisions are cached

    asyncio.run(go())


def test_origin_allowed_csrf_gate() -> None:
    open_settings = Settings(database_url="postgresql+asyncpg://nobody@localhost:1/none")
    locked = Settings(
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        dashboard_allowed_origins="https://dash.example",
    )

    class _WS:
        def __init__(self, origin: str | None) -> None:
            self.headers = {"origin": origin} if origin is not None else {}

    # No allow-list configured -> any origin permitted (dev).
    assert origin_allowed(_WS("https://evil.example"), open_settings) is True  # type: ignore[arg-type]
    # Configured: the listed origin passes, a cross-site one is rejected, and an
    # absent Origin (native client) is allowed.
    assert origin_allowed(_WS("https://dash.example"), locked) is True  # type: ignore[arg-type]
    assert origin_allowed(_WS("https://evil.example"), locked) is False  # type: ignore[arg-type]
    assert origin_allowed(_WS(None), locked) is True  # type: ignore[arg-type]


# --- WS owner auth gate ------------------------------------------------------


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        yield test_client


def test_live_ws_rejects_without_a_session_cookie(client: TestClient) -> None:
    # No session cookie -> the handshake is closed 4401 before accept.
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/locations/live"),
    ):
        pass
    assert exc.value.code == 4401


def test_live_ws_rejects_a_disallowed_origin() -> None:
    # With an allow-list set, a cross-site Origin is closed 4403 before auth (CSWSH).
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        dashboard_allowed_origins="https://dash.example",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(
                "/api/locations/live", headers={"origin": "https://evil.example"}
            ),
        ):
            pass
    assert exc.value.code == 4403
