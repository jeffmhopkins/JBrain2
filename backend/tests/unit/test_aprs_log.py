"""The APRS heard log's drain: what gets stored, and what is refused entry.

The parsing half is unit-testable without a database or a radio, and it is the half
that matters for safety: every field on this path came off a shared channel from
anyone with a transmitter, so the bounds are the point rather than a nicety.

The loop half — attach when a logging session exists, let go when it ends — is
covered through `tick()` against a faked sidecar.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jbrain.sdr.aprslog import MAX_INFO, MAX_RAW, AprsLog, _parse

_ROW = {
    "source": "KE8XYZ-9",
    "destination": "APDW17",
    "path": ["WIDE1-1"],
    "info": "GATE 7K2M9",
    "raw": "deadbeef",
    "frequency_hz": 144_390_000,
}


def test_a_decoded_frame_becomes_a_row() -> None:
    row = _parse(json.dumps(_ROW))

    assert row == {
        "hz": 144_390_000,
        "src": "KE8XYZ-9",
        "dst": "APDW17",
        "path": ["WIDE1-1"],
        "info": "GATE 7K2M9",
        "raw": "deadbeef",
    }


@pytest.mark.parametrize(
    "line",
    ["", "   ", "not json", "[]", '"a string"', json.dumps({"keepalive": True})],
)
def test_a_line_that_is_not_a_packet_is_skipped(line: str) -> None:
    # Keep-alives hold the socket open on a quiet channel, which is the NORMAL state of
    # a packet frequency. Storing one would put invented rows in the log.
    assert _parse(line) is None


def test_a_frame_with_no_sender_is_not_a_row() -> None:
    # A packet nobody can be attributed to is not evidence of anything, and the command
    # path reads this table.
    assert _parse(json.dumps({**_ROW, "source": ""})) is None


def test_a_hostile_info_field_cannot_write_an_unbounded_row() -> None:
    row = _parse(json.dumps({**_ROW, "info": "A" * 10_000, "raw": "ff" * 10_000}))

    assert row is not None
    assert len(row["info"]) == MAX_INFO
    assert len(row["raw"]) == MAX_RAW


def test_a_hostile_path_cannot_grow_without_bound() -> None:
    row = _parse(json.dumps({**_ROW, "path": ["WIDE1-1"] * 500}))

    assert row is not None
    # AX.25 allows at most 8 digipeaters; anything past that is a malformed or crafted
    # frame, and the column is not a place to let one expand.
    assert len(row["path"]) == 8


def test_a_path_that_is_not_a_list_does_not_crash_the_drain() -> None:
    row = _parse(json.dumps({**_ROW, "path": "WIDE1-1"}))

    assert row is not None and row["path"] == []


def test_a_crafted_frequency_skips_one_row_rather_than_ending_the_log() -> None:
    # `_drain` catches ValueError around its whole read loop, so a parse that RAISED
    # would let one hostile frame stop logging until the session was restarted. The
    # frame is dropped; the stream carries on.
    assert _parse(json.dumps({**_ROW, "frequency_hz": "not a number"})) is None
    assert _parse(json.dumps(_ROW)) is not None


class _Sidecar:
    """A sidecar whose /healthz answer and packet stream the test chooses."""

    def __init__(self, purpose: str | None, lines: list[str] | None = None) -> None:
        self.purpose = purpose
        self.lines = lines or []
        self.streamed = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            listening = {"purpose": self.purpose} if self.purpose else None
            return httpx.Response(200, json={"listening": listening})
        if request.url.path == "/listen/packets":
            self.streamed = True
            body = "".join(line + "\n" for line in self.lines).encode()
            return httpx.Response(200, content=body)
        return httpx.Response(404)


def _client_factory(sidecar: _Sidecar, monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient

    def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(sidecar.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build)


async def test_nothing_is_drained_while_the_radio_is_merely_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _Sidecar(purpose="listen")
    _client_factory(sidecar, monkeypatch)

    await AprsLog(maker=None, base_url="http://sdr:8000").tick()  # type: ignore[arg-type]

    # A listening session decodes no packets. Opening the stream would 409 anyway; not
    # opening it is what keeps an idle box costing one cheap GET per tick.
    assert sidecar.streamed is False


async def test_nothing_is_drained_when_the_radio_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _Sidecar(purpose=None)
    _client_factory(sidecar, monkeypatch)

    await AprsLog(maker=None, base_url="http://sdr:8000").tick()  # type: ignore[arg-type]

    assert sidecar.streamed is False


async def test_a_logging_session_is_drained_and_its_rows_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _Sidecar(
        purpose="aprs",
        lines=[json.dumps({"keepalive": True}), json.dumps(_ROW), "garbage"],
    )
    _client_factory(sidecar, monkeypatch)
    stored: list[dict[str, Any]] = []
    logger = AprsLog(maker=None, base_url="http://sdr:8000")  # type: ignore[arg-type]
    monkeypatch.setattr(logger, "_store", lambda row: _remember(stored, row))

    await logger.tick()

    assert sidecar.streamed is True
    # The keep-alive and the garbage line are skipped; the one real frame is stored.
    assert [r["src"] for r in stored] == ["KE8XYZ-9"]


async def _remember(sink: list[dict[str, Any]], row: dict[str, Any]) -> None:
    sink.append(row)


async def test_an_unconfigured_box_does_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _Sidecar(purpose="aprs")
    _client_factory(sidecar, monkeypatch)

    await AprsLog(maker=None, base_url="").tick()  # type: ignore[arg-type]

    # No radio configured is the common case on a box that has none; it must not even
    # reach for the sidecar.
    assert sidecar.streamed is False
