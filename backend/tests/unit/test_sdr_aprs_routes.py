"""The two routes behind the PWA's APRS tab (docs/plans/APRS_CONTROL_PLAN.md P1/P3).

These had NO tests, and an independent review proved it the honest way: replacing both
route bodies with `raise RuntimeError` passed the entire backend unit suite. Four real
defects were living in that gap, and each one made the switch lie in the direction that
looks harmless — "logging is on" when nothing decodes, "off" when nothing was stopped,
"not logging" when the receiver is unreachable.

So these tests are about what the routes say when the sidecar does NOT cooperate. The
happy path is the easy half and the half that was already obviously working.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.api import sdr as sdr_api

OWNER = object()


class _Sidecar:
    """A sidecar the test scripts: what /healthz says, and what each POST answers."""

    def __init__(
        self,
        *,
        health: dict[str, Any] | None,
        start: dict[str, Any] | None = None,
        stop: dict[str, Any] | None = None,
        health_after: dict[str, Any] | None = None,
    ) -> None:
        self.health = health
        self.health_after = health_after
        self.start = start or {}
        self.stop = stop or {"stopped": True}
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._healths = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def health(_base: str) -> dict[str, Any] | None:
            self._healths += 1
            if self._healths > 1 and self.health_after is not None:
                return self.health_after
            return self.health

        async def post(_settings: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
            self.posts.append((path, body))
            return self.start if path == "/listen/start" else self.stop

        monkeypatch.setattr(sdr_api, "_health", health)
        monkeypatch.setattr(sdr_api, "_post", post)


def _listening(purpose: str, session_id: str = "s1") -> dict[str, Any]:
    return {
        "purposes": ["listen", "aprs"],
        "listening": {
            "purpose": purpose,
            "session_id": session_id,
            "frequency_hz": 144_390_000,
        },
    }


_IDLE: dict[str, Any] = {"purposes": ["listen", "aprs"], "listening": None}


class _Settings:
    sdr_url = "http://sdr:8000"


async def _aprs(enabled: bool) -> dict[str, Any]:
    return await sdr_api.aprs_logging(_Settings(), OWNER, enabled)  # type: ignore[arg-type]


async def test_turning_it_on_starts_an_aprs_session(monkeypatch: pytest.MonkeyPatch) -> None:
    box = _Sidecar(health=_IDLE, start={"purpose": "aprs", "frequency_hz": 144_390_000})
    box.install(monkeypatch)

    out = await _aprs(True)

    assert out == {"logging": True, "changed": True, "frequency_hz": 144_390_000}
    assert box.posts[0][1]["purpose"] == "aprs"
    assert box.posts[0][1]["mode"] == "fm"  # 1200-baud AFSK is narrowband FM, always


async def test_a_sidecar_too_old_to_log_is_refused_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An older sidecar IGNORES an unknown `purpose` and returns 200 with a plain
    # listening session. Without this check the switch reads "logging on" while nothing
    # decodes and the one tuner sits held on 144.39 — the exact hole the jerv tool
    # already refused by name, left open on the surface the owner actually touches.
    box = _Sidecar(health=_IDLE, start={"purpose": "listen", "frequency_hz": 144_390_000})
    box.install(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await _aprs(True)

    assert raised.value.status_code == 502
    assert "too old" in str(raised.value.detail)


async def test_an_unreachable_sidecar_is_not_reported_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "Off" is a state; "we cannot tell" is not. Answering off here flips the switch in
    # front of the owner while logging, if it is running, carries on.
    box = _Sidecar(health=None)
    box.install(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await _aprs(False)

    assert raised.value.status_code == 502
    assert box.posts == []


async def test_turning_it_off_stops_this_session_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    box = _Sidecar(health=_listening("aprs", "abc123"))
    box.install(monkeypatch)

    out = await _aprs(False)

    # By id, so it can never release a listening session the owner started — on a
    # one-tuner box that would silence the radio they were actually using.
    assert box.posts == [("/listen/stop", {"session_id": "abc123"})]
    assert out == {"logging": False, "changed": True}


async def test_turning_it_off_never_touches_a_listening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _Sidecar(health=_listening("listen"))
    box.install(monkeypatch)

    assert await _aprs(False) == {"logging": False, "changed": False}
    assert box.posts == []


async def test_a_stop_that_stopped_nothing_is_not_reported_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this route exists to avoid.

    The sidecar answers 200 `{"stopped": false}` when the id no longer matches — the
    session restarted between the health read and the stop. Both callers used to treat
    200 as done. For the timed window the feature is for ("turn logging off at 09:00")
    that means nothing was stopped, the owner was told it was, and the tuner stays held
    all day: verbatim the failure the plan names."""
    box = _Sidecar(
        health=_listening("aprs"),
        stop={"stopped": False},
        health_after=_listening("aprs", "different"),
    )
    box.install(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await _aprs(False)

    assert raised.value.status_code == 409


async def test_a_session_that_ended_on_its_own_is_simply_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other reading of `stopped: false` — the session was already gone. The owner
    # asked for logging to be off and logging is off, so this is a success with nothing
    # changed, not an error. Distinguishing the two is why the route re-reads health.
    box = _Sidecar(health=_listening("aprs"), stop={"stopped": False}, health_after=_IDLE)
    box.install(monkeypatch)

    assert await _aprs(False) == {"logging": False, "changed": False}


async def test_turning_it_on_when_it_is_already_on_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _Sidecar(health=_listening("aprs"))
    box.install(monkeypatch)

    out = await _aprs(True)

    # Idempotent, because a scheduled retry of "turn it on" must not be a failure.
    assert out == {"logging": True, "changed": False, "frequency_hz": 144_390_000}
    assert box.posts == []


async def test_a_busy_radio_reaches_the_client_as_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def health(_base: str) -> dict[str, Any] | None:
        return _IDLE

    async def post(_settings: Any, _path: str, _body: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(status_code=409, detail="the radio is already listening")

    monkeypatch.setattr(sdr_api, "_health", health)
    monkeypatch.setattr(sdr_api, "_post", post)

    with pytest.raises(HTTPException) as raised:
        await _aprs(True)

    # The sidecar names the holder, and the tab shows that name — the two jobs need
    # opposite advice from the owner.
    assert raised.value.status_code == 409
    assert "already listening" in str(raised.value.detail)


# --- the heard log ------------------------------------------------------------------


class _Reader:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.limit: int | None = None

    def __call__(self, _maker: Any) -> _Reader:
        return self

    async def recent(self, _ctx: Any, *, limit: int = 20) -> list[dict[str, Any]]:
        self.limit = limit
        return self._rows


def _row() -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "heard_at": datetime(2026, 9, 2, 12, tzinfo=UTC),
        "frequency_hz": 144_390_000,
        "source": "KE8XYZ-9",
        "destination": "APDW17",
        "path": ["WIDE1-1"],
        "info": "hello from the truck",
    }


class _Request:
    """Just enough request for the route: `app.state` is where the running drain hangs.

    Its `store_failures` is how a heard log that has silently stopped recording becomes
    visible from the PWA — `_store` swallows its own errors so one bad frame cannot end
    the drain, which means a broken INSERT stops the log with no symptom otherwise."""

    def __init__(self, failures: int | None = 0) -> None:
        state = SimpleNamespace()
        if failures is not None:
            state.aprs_logger = SimpleNamespace(store_failures=failures)
        self.app = SimpleNamespace(state=state)


async def _packets(
    monkeypatch: pytest.MonkeyPatch,
    box: _Sidecar,
    rows: list[dict[str, Any]],
    request: _Request | None = None,
):
    box.install(monkeypatch)
    monkeypatch.setattr(sdr_api, "AprsReader", _Reader(rows))
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _owner: object())
    return await sdr_api.packets(
        request or _Request(),  # type: ignore[arg-type]
        _Settings(),  # type: ignore[arg-type]
        OWNER,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        50,
    )


async def test_the_log_says_whether_anything_is_receiving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _packets(monkeypatch, _Sidecar(health=_listening("aprs")), [_row()])

    assert out["logging"] is True
    assert out["reachable"] is True
    assert out["packets"][0]["source"] == "KE8XYZ-9"
    assert out["packets"][0]["heard_at"].startswith("2026-09-02")


async def test_an_unreachable_receiver_is_not_the_same_as_a_quiet_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A watch that silently died is worse than no watch. With only `logging` to go on,
    # the tab would render a dead receiver exactly like a switched-off one.
    out = await _packets(monkeypatch, _Sidecar(health=None), [_row()])

    assert out["logging"] is False
    assert out["reachable"] is False
    # The rows are still returned: the log is in Postgres and does not need the radio.
    assert len(out["packets"]) == 1


async def test_a_listening_session_is_not_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _packets(monkeypatch, _Sidecar(health=_listening("listen")), [])

    assert out["logging"] is False and out["reachable"] is True


# --- what is armed, and what has been tried ------------------------------------------


class _Rows:
    """Two result sets in the order the route asks for them."""

    def __init__(self, armed: list[dict[str, Any]], tried: list[dict[str, Any]]) -> None:
        self._sets = [armed, tried]

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        rows = self._sets.pop(0)

        class Result:
            def mappings(self) -> Any:
                class M:
                    def all(self) -> list[dict[str, Any]]:
                        return rows

                return M()

        return Result()


def _scoped(rows: _Rows):
    class Ctx:
        async def __aenter__(self) -> _Rows:
            return rows

        async def __aexit__(self, *_a: Any) -> None: ...

    def scoped_session(_maker: Any, _ctx: Any) -> Ctx:
        return Ctx()

    return scoped_session


async def test_the_radio_tab_can_see_what_is_armed_and_what_was_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    when = datetime(2026, 9, 2, 12, tzinfo=UTC)
    rows = _Rows(
        armed=[
            {
                "id": "t1",
                "name": "Open the gate",
                "enabled": True,
                "command_word": "GATE",
                "command_callsign": "KE8XYZ-9",
                "command_days": [1, 2, 3, 4, 5],
                "command_from": "06:00",
                "command_until": "09:00",
                "command_once": False,
                "command_failures": 5,
                "command_last_at": when,
            }
        ],
        tried=[
            {
                "heard_at": when,
                "source": "N0BODY-1",
                "word": "GATE",
                "accepted": False,
                "reason": "code did not verify",
            }
        ],
    )
    monkeypatch.setattr(sdr_api, "scoped_session", _scoped(rows))
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _owner: object())

    out = await sdr_api.commands(OWNER, object())  # type: ignore[arg-type]

    assert out["commands"][0]["word"] == "GATE"
    # Five failures is the lockout, and the tab has to say so — nothing fires until the
    # owner clears it, and a command that silently stopped working is the failure this
    # whole surface exists to prevent.
    assert out["commands"][0]["locked"] is True
    # A REFUSAL is the row worth keeping: three of these from an unknown station last
    # Tuesday is a fact the owner must be able to find, and a push does not keep.
    assert out["attempts"][0] == {
        "heard_at": when.isoformat(),
        "source": "N0BODY-1",
        "word": "GATE",
        "accepted": False,
        "reason": "code did not verify",
    }


async def test_no_key_ever_leaves_the_box_through_this_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _Rows(armed=[], tried=[])
    monkeypatch.setattr(sdr_api, "scoped_session", _scoped(rows))
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _owner: object())

    out = await sdr_api.commands(OWNER, object())  # type: ignore[arg-type]

    # Belt and braces on a read that runs beside the secret: the SELECT does not name
    # `command_key`, and this asserts the shape rather than trusting the SQL to stay
    # that way. An empty box is also a valid answer, not an error.
    assert out == {"commands": [], "attempts": []}


# --- the stations roster's chip parsing (F2) -------------------------------------------


def test_only_the_five_known_kinds_reach_the_query() -> None:
    """The chip selection arrives as text in a URL and leaves as a whitelist.

    Not because a bound parameter would be unsafe — it would not — but because the five
    buckets are a closed set, and anything else in that parameter is either a stale
    client or someone probing. Neither should reach the database as a value."""
    assert sdr_api._kinds("Position,Weather") == ["Position", "Weather"]
    assert sdr_api._kinds(" Object , Message ") == ["Object", "Message"]
    assert sdr_api._kinds("Position'; DROP TABLE app.aprs_packets --") == []


def test_an_unknown_chip_shows_everything_rather_than_erroring() -> None:
    """A PWA cached from before a rename asks for a kind we no longer have.

    The owner opens the radio tab and gets the unfiltered roster, which is the screen
    they wanted, instead of an error page they cannot act on from a phone."""
    assert sdr_api._kinds("Telemetry") == []
    assert sdr_api._kinds("") == []
    assert sdr_api._kinds(None) == []
    # And a mixed list keeps the half that still means something.
    assert sdr_api._kinds("Telemetry,Weather") == ["Weather"]


async def test_the_log_says_when_packets_are_being_heard_and_LOST(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third way this surface can lie, alongside `reachable` and `logging`.

    The radio decodes, the drain runs, and every row fails to store. `_store` swallows
    its errors so one bad frame cannot end the log — which means a broken INSERT, the
    real case being new code against an un-migrated schema, stops the log with no
    symptom whatsoever. The owner has no terminal (CLAUDE.md rule 10), so the count has
    to reach the screen or it does not exist."""
    heard = _Sidecar(health=_listening("aprs"))

    out = await _packets(monkeypatch, heard, [_row()], _Request(failures=7))

    assert out["store_failures"] == 7


async def test_a_log_that_is_recording_fine_reports_no_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _packets(monkeypatch, _Sidecar(health=_listening("aprs")), [_row()])

    assert out["store_failures"] == 0


async def test_the_tab_still_loads_before_the_drain_has_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No logger on app.state yet — during startup, and in any process that never runs a
    # drain. Reading zero is right; raising would take the whole APRS tab down over a
    # diagnostic field.
    out = await _packets(
        monkeypatch, _Sidecar(health=_listening("aprs")), [_row()], _Request(failures=None)
    )

    assert out["store_failures"] == 0
