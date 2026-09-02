"""The gate's decisions that need no database, and the hand-off that reaches it.

The verify path proper is tested against real Postgres (test_command_gate_pg.py) because
the consume is a conditional UPDATE. What is here is the part a fake would not weaken:
which callsigns a filter admits, how a stored row becomes a `Heard`, and — the one that
matters operationally — that a gate blowing up on one frame cannot end the drain and
take the heard log down with it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jbrain.sdr.aprslog import AprsLog
from jbrain.sdr.gate import _callsign_allows, heard_from_row

_ROW = {
    "src": "KE8XYZ-9",
    "info": "GATE 7K2M9",
    "heard_at": 1_760_000_000.5,
}


@pytest.mark.parametrize(
    ("configured", "heard", "allowed"),
    [
        (None, "N0BODY-1", True),  # no filter: the code alone decides
        ("", "N0BODY-1", True),
        ("KE8XYZ", "KE8XYZ-9", True),  # bare call means the operator, any radio
        ("KE8XYZ", "KE8XYZ-7", True),
        ("KE8XYZ", "KE8XYZ", True),
        ("ke8xyz", "KE8XYZ-9", True),  # what the owner typed, not how they typed it
        ("KE8XYZ", "KE8XYZZ-9", False),  # a prefix is not a match
        ("KE8XYZ", "N0BODY-1", False),
        ("KE8XYZ-9", "KE8XYZ-7", False),  # an SSID typed is an SSID meant
        ("KE8XYZ-9", "KE8XYZ-9", True),
    ],
)
def test_the_callsign_filter_admits_what_the_owner_meant(
    configured: str | None, heard: str, allowed: bool
) -> None:
    assert _callsign_allows(configured, heard) is allowed


def test_a_stored_row_becomes_a_heard_frame() -> None:
    heard = heard_from_row(_ROW)

    assert heard.source == "KE8XYZ-9"
    assert heard.info == "GATE 7K2M9"
    assert heard.heard_at == datetime.fromtimestamp(1_760_000_000.5, UTC)


def test_a_frame_with_no_usable_time_is_judged_against_now() -> None:
    # The alternative is 1970, which would put every command outside every window — an
    # owner locked out of their own gate by a sidecar that forgot to stamp a frame.
    before = datetime.now(UTC)

    heard = heard_from_row({"src": "KE8XYZ-9", "info": "GATE 7K2M9", "heard_at": None})

    assert heard.heard_at >= before


def _stream(lines: list[str]):
    """A sidecar that reports a logging session and then plays `lines` as its stream."""

    class Fake:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, path: str, **_kw: Any):  # noqa: ASYNC109 — mirrors httpx
            return httpx.Response(
                200,
                json={"listening": {"purpose": "aprs"}},
                request=httpx.Request("GET", f"http://sdr{path}"),
            )

        def stream(self, _method: str, path: str):
            outer = self

            class Ctx:
                async def __aenter__(self):
                    return outer._Upstream(lines)

                async def __aexit__(self, *_a: Any) -> None: ...

            return Ctx()

        class _Upstream:
            status_code = 200

            def __init__(self, lines: list[str]) -> None:
                self._lines = lines

            async def aiter_lines(self):
                for line in self._lines:
                    yield line

    return Fake


async def test_a_gate_that_raises_does_not_end_the_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [json.dumps({"source": "KE8XYZ-9", "info": f"GATE {n}"}) for n in range(3)]
    monkeypatch.setattr(httpx, "AsyncClient", _stream(frames))
    seen: list[str] = []

    async def exploding(row: dict[str, Any]) -> None:
        seen.append(row["info"])
        raise RuntimeError("boom")

    log = AprsLog(maker=None, base_url="http://sdr:8000", on_packet=exploding)  # type: ignore[arg-type]

    await log.tick()

    # All three, not one: the failure mode being closed off is the radio going quiet
    # after a single bad frame with nothing saying so.
    assert seen == ["GATE 0", "GATE 1", "GATE 2"]


async def test_a_store_failure_does_not_stop_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _stream([json.dumps({"source": "K", "info": "G 1"})]))
    offered: list[str] = []

    async def gate(row: dict[str, Any]) -> None:
        offered.append(row["info"])

    # No database at all, so the real `_store` fails on every frame — the state a box
    # in the middle of a migration or a Postgres restart is actually in.
    log = AprsLog(maker=None, base_url="http://sdr:8000", on_packet=gate)  # type: ignore[arg-type]

    await log.tick()

    # The owner's gate must still open. Losing the log row is a bookkeeping failure;
    # refusing the command because of it would be the radio deciding on its own that the
    # owner is locked out.
    assert offered == ["G 1"]
