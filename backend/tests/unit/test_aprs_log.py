"""The APRS heard log's drain: what gets stored, and what is refused entry.

The parsing half is unit-testable without a database or a radio, and it is the half
that matters for safety: every field on this path came off a shared channel from
anyone with a transmitter, so the bounds are the point rather than a nicety.

The loop half — attach when a logging session exists, let go when it ends — is
covered through `tick()` against a faked sidecar.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from jbrain.sdr.aprslog import DERIVED, INSERT_SQL, MAX_INFO, MAX_RAW, AprsLog, _parse

_ROW = {
    "source": "KE8XYZ-9",
    "destination": "APDW17",
    "path": ["WIDE1-1"],
    "info": "GATE 7K2M9",
    "raw": "deadbeef",
    "frequency_hz": 144_390_000,
    "heard_at": 1_760_000_000.5,
}


def test_a_decoded_frame_becomes_a_row() -> None:
    row = _parse(json.dumps(_ROW))

    assert row is not None
    assert row["heard_at"] == 1_760_000_000.5
    # Exact rather than a subset, deliberately: this dict IS the INSERT's bind
    # parameters, so a column added to the statement without a value here fails the
    # test rather than every insert at runtime.
    assert {k: v for k, v in row.items() if k != "heard_at"} == {
        "hz": 144_390_000,
        "src": "KE8XYZ-9",
        "dst": "APDW17",
        "path": ["WIDE1-1"],
        "info": "GATE 7K2M9",
        "raw": "deadbeef",
        # Derived on the way in, so the log is filterable without waiting for a sweep.
        "origin_call": "KE8XYZ-9",
        "data_type": "G",  # `raw` here is not a frame, so the identifier comes from info
        "kind": "Other",
        "gated": False,
        "heard_direct": True,
        "addressee": None,
        # No level in `_ROW`, and the sidecar is the only thing that can ever supply
        # one — so this stays null rather than becoming a zero.
        "audio_level": None,
    }


def test_the_insert_binds_every_column_the_parser_produces() -> None:
    """The parsed row and the INSERT are one contract.

    Both halves are hand-written strings; if they drift, every insert fails at runtime
    inside a handler that swallows the error to a log line — the log silently stops
    recording and nothing on the screen says so."""
    row = _parse(json.dumps(_ROW))
    assert row is not None

    bound = set(re.findall(r":(\w+)", INSERT_SQL))

    assert bound == set(row)


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


def test_a_frame_with_no_decode_time_is_stamped_now_not_1970() -> None:
    # A sidecar too old to stamp the frame, or a crafted one. `heard_at` is what the log
    # is FOR, and an epoch-zero row is worse than an approximate one.
    row = _parse(json.dumps({**_ROW, "heard_at": 0}))

    assert row is not None and row["heard_at"] > 1_700_000_000


def test_a_hostile_decode_time_does_not_end_the_drain() -> None:
    row = _parse(json.dumps({**_ROW, "heard_at": "yesterday"}))

    assert row is not None and row["heard_at"] > 1_700_000_000


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


class _TwoRadios(_Sidecar):
    """APRS on the long wire, the tuner on the desk whip — the measured two-dongle box.

    `listening` is the TUNER's session here, because that is the one the omnibox draws.
    Only `sessions` says APRS is running."""

    def __init__(self, lines: list[str] | None = None) -> None:
        super().__init__(purpose="aprs", lines=lines)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/healthz":
            return super().handler(request)
        tuner = {"purpose": "listen", "session_id": "s-tuner"}
        aprs = {"purpose": "aprs", "session_id": "s-aprs"}
        return httpx.Response(200, json={"listening": tuner, "sessions": [tuner, aprs]})


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


async def test_the_drain_stays_attached_when_the_tuner_takes_the_OTHER_radio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quietest way this could have failed.

    With a session per radio, `listening` is the one the omnibox draws and prefers the
    tuner. This check read it, so opening the tuner sheet while APRS logged made the
    drain decide nothing was logging — it detached, and every frame the radio kept
    decoding went unstored. No error, no log line: a busy channel that looks quiet."""
    sidecar = _TwoRadios(lines=[json.dumps(_ROW)])
    _client_factory(sidecar, monkeypatch)
    stored: list[dict[str, Any]] = []
    logger = AprsLog(maker=None, base_url="http://sdr:8000")  # type: ignore[arg-type]
    monkeypatch.setattr(logger, "_store", lambda row: _remember(stored, row))

    await logger.tick()

    assert sidecar.streamed is True
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


def test_a_nul_byte_cannot_make_a_frame_unstorable() -> None:
    """Postgres `text` rejects a NUL, and both INSERTs on this path swallow their own
    errors so one bad row cannot end the log. Together that means an unscrubbed NUL does
    not fail loudly — it deletes the row silently.

    The attack it enables: a code with a NUL appended still verifies or fails exactly
    like the clean one (the comparison strips control characters), so five of them lock
    the command while leaving NO packet row and NO attempt row. One byte against the
    evidence the whole design leans on."""
    row = _parse(json.dumps({**_ROW, "info": "GATE AAAAA\u0000", "source": "KE8\u0000XYZ"}))

    assert row is not None
    assert "\u0000" not in row["info"]
    assert "\u0000" not in row["src"]


def test_ordinary_text_survives_the_scrub() -> None:
    # A tab is legal in a status message, and the scrub must not eat the content.
    row = _parse(json.dumps({**_ROW, "info": "Op Jeff\tmobile — 73"}))

    assert row is not None
    assert row["info"] == "Op Jeff\tmobile — 73"


def test_a_classifier_that_blows_up_costs_a_label_and_never_a_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derived columns are a cache over `raw`, and this is what that buys.

    `classify` is total by construction, so reaching this is a bug in it — but the frame
    was still heard, and losing it would be unrecoverable while losing its label costs a
    re-run. The row goes in with NULLs and the sweep re-claims it later, which is the
    same mechanism that upgrades the whole table when the classifier improves."""
    monkeypatch.setattr(
        "jbrain.sdr.aprslog.classify",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    row = _parse(json.dumps(_ROW))

    assert row is not None
    assert row["info"] == "GATE 7K2M9"  # the frame is intact
    assert all(row[column] is None for column in DERIVED)
    # And it still binds the full statement, so the insert itself does not fail.
    assert set(re.findall(r":(\w+)", INSERT_SQL)) == set(row)


class TestAudioLevel:
    """How strong the transmission was, as the drain stores it.

    This is the one column that CANNOT be recovered from `raw` later — the reading
    exists only at decode time — so the rules about what NULL means are load-bearing in
    a way the derived columns' are not."""

    def test_the_level_the_sidecar_measured_reaches_the_row(self) -> None:
        row = _parse(json.dumps({**_ROW, "audio_level": 50}))

        assert row is not None
        assert row["audio_level"] == 50

    def test_a_sidecar_too_old_to_send_one_leaves_it_unmeasured(self) -> None:
        # Not zero. A rolling restart runs the new api against the old sidecar, and a
        # log claiming every station was inaudible for an hour would be worse than one
        # admitting it did not know.
        row = _parse(json.dumps({k: v for k, v in _ROW.items()}))

        assert row is not None
        assert row["audio_level"] is None

    def test_zero_is_kept_because_it_is_a_real_reading(self) -> None:
        row = _parse(json.dumps({**_ROW, "audio_level": 0}))

        assert row is not None
        assert row["audio_level"] == 0

    @pytest.mark.parametrize("bad", [101, -1, 5000, "loud", None, [50], {}])
    def test_a_level_out_of_range_or_the_wrong_shape_is_dropped_not_guessed(self, bad: Any) -> None:
        """The sidecar already clamps what direwolf says, so anything outside 0-100
        arriving here means the two are out of step. A made-up number is worse than a
        blank, and a crafted one must not cost the row — the CHECK constraint would
        reject the insert and the whole frame would vanish."""
        row = _parse(json.dumps({**_ROW, "audio_level": bad}))

        assert row is not None
        assert row["audio_level"] is None

    def test_the_insert_carries_it(self) -> None:
        # A column the row computes but the statement omits is a silent data loss that
        # no other test in this file would notice.
        assert "audio_level" in INSERT_SQL
        assert ":audio_level" in INSERT_SQL
