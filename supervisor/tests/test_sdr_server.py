"""The sidecar's HTTP surface, driven over a real socket.

`deploy/sdr/server.py` had no tests at all, which is how the lease's purpose could
reach `Session` and stop there: the one line that reads `purpose` off a request body
was covered by nothing. It is a stdlib `ThreadingHTTPServer`, so the honest way to
test it is to bind one on an ephemeral port and make real requests — the handler's
routing, body parsing and status codes are exactly what would otherwise be assumed.

The tuner itself is faked at the process boundary (`subprocess.Popen`), the same seam
`test_sdr_listen.py` uses, so no radio is involved and nothing is spawned.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np
import pytest

_SDR = Path(__file__).resolve().parents[2] / "deploy/sdr"
sys.path.insert(0, str(_SDR))
_spec = importlib.util.spec_from_file_location("sdr_server", _SDR / "server.py")
assert _spec and _spec.loader
server = importlib.util.module_from_spec(_spec)
sys.modules["sdr_server"] = server
_spec.loader.exec_module(server)
listen = sys.modules["listen"]
iq = sys.modules["iq"]


class _FakeProc:
    """A subprocess that is alive and produces nothing, so no radio is touched."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.stdout = _Empty()
        self.stderr = _Empty()
        self.stdin = _Sink()
        self.returncode = None

    def poll(self) -> int | None:
        """Alive until killed, which models rtl_fm streaming.

        Typed as `int | None` like `Popen.poll` rather than the bare `None` this used
        to return: a sweep is the one process that ENDS on its own, so `_DeadProc`
        below has a real exit code to report."""
        return None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _Empty:
    def read(self, _n: int = -1) -> bytes:
        return b""

    def readline(self) -> bytes:
        return b""

    def __iter__(self) -> Iterator[bytes]:
        return iter(())

    def close(self) -> None:
        return None


class _Sink:
    def write(self, _b: bytes) -> int:
        return 0

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def sidecar(monkeypatch) -> Iterator[str]:
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
    # A FRESH registry per test rather than a stopped one. `TUNER.stop()` releases the
    # sessions but deliberately not the capture reservations — an in-flight rtl_fm still
    # has the device open, and freeing its key would let the next session collide with a
    # process still running. That is right in production and leaks between tests.
    # `_confirm_started` watches a fresh pipeline for `STARTUP_GRACE_S` before believing
    # it — the check that turns an unopenable radio into a refusal instead of a session
    # that vanishes a second later. A fake process is alive the instant it exists, so
    # that wait buys nothing here and costs every case four tenths of a second.
    monkeypatch.setattr(listen, "STARTUP_GRACE_S", 0)
    monkeypatch.setattr(
        listen.radio.Radio,
        "open",
        staticmethod(
            lambda *, center_hz, rate_hz, **kw: _SpectrumRadio(center_hz, rate_hz, **kw)
        ),
    )
    monkeypatch.setattr(server, "TUNER", listen.Tuner())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.TUNER.stop()


def _post(base: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def _get(base: str, path: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def test_a_request_that_names_no_purpose_gets_a_listening_session(sidecar: str) -> None:
    status, body = _post(
        sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"}
    )

    assert status == 200
    assert body["purpose"] == listen.PURPOSE_LISTEN


def test_a_request_can_take_the_radio_for_APRS(sidecar: str) -> None:
    status, body = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs"},
    )

    assert status == 200
    assert body["purpose"] == "aprs"


def test_a_logging_session_refuses_a_listener_over_http_by_name(sidecar: str) -> None:
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs"},
    )

    status, body = _post(
        sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"}
    )

    # The whole point of P0, asserted at the boundary a caller actually sees rather
    # than only inside Tuner.start.
    assert status == 409
    assert "logging APRS" in body["detail"]


def test_an_unknown_purpose_is_a_400_not_a_session(sidecar: str) -> None:
    status, _ = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "transmit"},
    )

    assert status == 400


def test_a_logging_session_cannot_be_retuned_out_from_under_itself(
    sidecar: str,
) -> None:
    started = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs"},
    )[1]

    status, body = _post(
        sidecar,
        "/listen/tune",
        {
            "session_id": started["session_id"],
            "frequency_hz": 99_300_000,
            "mode": "wbfm",
        },
    )

    # Retuning would move the packet channel to broadcast FM while the lease went on
    # claiming to be logging APRS — and then refuse the next caller with a reason that
    # had become false. Releasing is the only honest way from one job into another.
    assert status == 409
    assert "logging APRS" in body["detail"]
    assert _get(sidecar, "/healthz")[1]["listening"]["frequency_hz"] == 144_390_000


def test_a_listening_session_can_still_be_retuned(sidecar: str) -> None:
    started = _post(
        sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"}
    )[1]

    status, _ = _post(
        sidecar,
        "/listen/tune",
        {
            "session_id": started["session_id"],
            "frequency_hz": 101_100_000,
            "mode": "wbfm",
        },
    )

    assert status == 200
    assert _get(sidecar, "/healthz")[1]["listening"]["frequency_hz"] == 101_100_000


def test_healthz_advertises_the_jobs_this_sidecar_understands(sidecar: str) -> None:
    _, body = _get(sidecar, "/healthz")

    # An OLDER sidecar ignores an unknown `purpose` and returns 200 with a plain
    # listening session — so "turn logging on" would succeed, log nothing, and report
    # success. This is how a caller tells the difference without trusting a 200.
    assert "aprs" in body["purposes"]


# --- the packet stream --------------------------------------------------------------


def test_packets_are_refused_when_the_radio_is_not_logging(sidecar: str) -> None:
    _post(sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"})

    status, body = _get(sidecar, "/listen/packets")

    # A listening session decodes nothing, so this is not "no packets yet" — it is the
    # wrong job, and saying so is the difference between a quiet channel and a mistake.
    assert status == 409
    assert "not logging" in body["detail"]


def test_packets_are_refused_when_the_radio_is_idle(sidecar: str) -> None:
    status, _ = _get(sidecar, "/listen/packets")

    assert status == 409


def test_a_decoded_frame_reaches_a_reader_as_a_row(sidecar: str) -> None:
    """The point of the wave: a frame direwolf decoded becomes a storable row.

    The frame is the REAL captured one (the same fixture `test_sdr_packets.py` parses),
    pushed in at the seam where the KISS reader would have put it — so this exercises
    the fan-out, the framing and the stamping without needing direwolf or a radio."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs"},
    )
    session = server.TUNER.current()
    assert session is not None

    rows: list[dict[str, Any]] = []
    reader = threading.Thread(
        target=lambda: rows.extend(_ndjson(sidecar, 1)), daemon=True
    )
    reader.start()
    for _ in range(50):  # let the reader subscribe before anything is published
        if session._packets:
            break
        time.sleep(0.05)
    session._publish_packet(_captured_packet())
    reader.join(timeout=10)

    assert rows, "no packet reached the reader"
    assert rows[0]["source"] == "KE8XYZ-9"
    assert rows[0]["info"] == "GATE 7K2M9"
    # Stamped with what the radio was tuned to: the log is read long after, and "which
    # channel was this?" is not recoverable from the frame itself.
    assert rows[0]["frequency_hz"] == 144_390_000


def _captured_packet():
    """The first real KISS frame from the committed direwolf capture."""
    fixture = Path(__file__).parent / "fixtures/aprs_kiss_frames.hex"
    line = next(
        ln for ln in fixture.read_text().splitlines() if ln and not ln.startswith("#")
    )
    return sys.modules["packets"].parse_kiss(bytes.fromhex(line))


def _ndjson(base: str, count: int) -> list[dict[str, Any]]:
    """Read `count` non-keepalive rows off the packet stream, then stop."""
    out: list[dict[str, Any]] = []
    with urllib.request.urlopen(base + "/listen/packets", timeout=10) as resp:
        for raw in resp:
            row = json.loads(raw)
            if row.get("keepalive"):
                continue
            out.append(row)
            if len(out) >= count:
                return out
    return out


class _DeadProc(_FakeProc):
    """A sweep that finishes almost at once — rtl_power's exit timer fired.

    Real `_FakeProc` stays alive until killed, which models rtl_fm streaming. A sweep
    is the opposite and ENDS on its own, so `/sweep` would wait out its deadline.

    ALIVE for the first few polls, not dead from birth, and that distinction became
    load-bearing: `_confirm_started` now watches a fresh pipeline briefly and refuses a
    session whose process has already exited, because that is what "the radio could not
    be opened" looks like. A fake that was dead before it started would be refused as
    exactly that — which is right, and is why a sweep has to be modelled as something
    that ran."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self._polls = 0

    def poll(self) -> int | None:
        self._polls += 1
        return 0 if self._polls > 2 else None


def _sweep_body(**extra: Any) -> dict[str, Any]:
    """A survey request as the api sends it: a range, a WIDTH to be written at, and the
    capture that measures it. The width and the capture are different numbers on
    purpose — a survey asks to be integrated more coarsely than it is measured."""
    body: dict[str, Any] = {
        "start_hz": 144_000_000,
        "stop_hz": 144_200_000,
        "bin_hz": 25_000,
        "seconds": 2,
        **TEST_CAPTURE,
    }
    body.update(extra)
    return body


def test_a_sweep_holds_the_radio_and_returns_its_rows(sidecar: str) -> None:
    """The happy path, and the shape the api reduces.

    **The rows come from the same engine the waterfall uses now** (B2): a survey was
    never a different way of measuring, it is the same spectrum integrated for longer.
    The sidecar still does NOT draw it — the image work needs a plotting stack, which
    `Dockerfile.sdr`'s apt-only rule refuses, and the api already carries Pillow."""
    status, body = _post(sidecar, "/sweep", _sweep_body())

    assert status == 200
    assert body["complete"] is True
    assert (body["start_hz"], body["stop_hz"]) == (144_000_000, 144_200_000)
    # The width the ROWS are written at — 25 kHz asked for off a 4687.5 Hz capture is
    # five whole bins, so 23437.5, and the envelope says the same thing the rows do.
    first = [p.strip() for p in body["csv"].splitlines()[0].split(",")]
    assert len(first) > 6
    assert float(first[4]) == body["bin_hz"] == 5 * 2_400_000 / 512
    assert min(float(v) for v in first[6:]) < -20  # a real floor, in dBFS


def test_a_sweep_frees_the_radio_when_it_ends(sidecar: str) -> None:
    # The next listener must not find the radio busy with a survey that already
    # finished — and the survey is a spectrum session now, which is released rather
    # than self-terminating, so this is the assertion that the route releases it.
    _post(sidecar, "/sweep", _sweep_body())

    status, _ = _post(
        sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"}
    )
    assert status == 200


def test_a_sweep_is_refused_while_APRS_is_logging(sidecar: str, monkeypatch) -> None:
    """The refusal that matters, over HTTP this time.

    A sweep is the job an agent asks for on its own initiative, so the dangerous case is
    a week-old APRS session being taken away to look at 70cm."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs"},
    )
    status, body = _post(
        sidecar, "/sweep", _sweep_body(start_hz=440_000_000, stop_hz=440_200_000)
    )

    assert status == 409
    assert "logging APRS" in body["detail"]


def test_a_survey_reaches_shortwave_now_that_it_is_not_rtl_power(sidecar: str) -> None:
    """The floor that kept surveys above 24 MHz was the TOOL's, not the radio's:
    `rtl_power -D` hardcodes the ADC's I branch and this board wires Q, so a survey down
    there tuned something and measured nothing. B2 put the survey on the engine that
    sets the branch at runtime, and the floor went with the tool (A5)."""
    status, body = _post(
        sidecar,
        "/sweep",
        _sweep_body(
            start_hz=7_000_000,
            stop_hz=7_256_000,
            bin_hz=1_000,
            rate_hz=256_000,
            bins=1024,
        ),
    )

    assert status == 200
    assert body["csv"].strip()


def test_a_survey_below_what_the_ADC_reaches_is_still_refused(sidecar: str) -> None:
    """The radio's own floor, which is a fact about the hardware rather than a tool."""
    status, body = _post(
        sidecar, "/sweep", _sweep_body(start_hz=20_000, stop_hz=90_000)
    )

    assert status == 400
    assert "below what this radio reaches" in body["detail"]


def test_a_sweep_with_no_range_is_refused_rather_than_run(sidecar: str) -> None:
    status, _ = _post(sidecar, "/sweep", _sweep_body(stop_hz=144_000_000))

    assert status == 400


def test_a_range_whose_EDGE_is_out_of_band_is_refused(
    sidecar: str, monkeypatch
) -> None:
    """The check `listen.py` cannot make for us.

    It validates the CENTRE frequency, which for 0.02-70 MHz is 35 MHz — comfortably in
    band, while the low edge is below anything the ADC reaches. A survey that quietly
    started above where it was asked to would report the bottom of the range as
    quiet."""
    status, body = _post(
        sidecar, "/sweep", _sweep_body(start_hz=20_000, stop_hz=200_000)
    )

    assert status == 400
    assert "below what this radio reaches" in body["detail"]


def test_a_sweep_whose_radio_goes_away_keeps_what_it_measured(sidecar: str) -> None:
    """The stream ending mid-survey is not the same as a survey that finished.

    A short window reported as a full one reads as a quiet band, so the result says it
    is partial — and what was measured before the radio went is still a real measurement
    of a shorter window, which is why it is returned rather than thrown away."""
    started = _post(
        sidecar, "/listen/start", {**_sweep_body(seconds=30), "purpose": "spectrum"}
    )
    assert started[0] == 200

    # The rows a viewer would have seen, then the radio going away underneath it.
    session = server.TUNER.find(started[1]["session_id"])
    assert session is not None
    sub = session.subscribe_frames(listen.VIEW_BAND)
    rows = listen.SurveyRows(25_000)
    lines = []
    for _ in range(8):
        frame = sub.get(timeout=5)
        assert frame is not None
        lines.extend(rows.push(frame))
    lines.extend(rows.flush())
    server.TUNER.stop(started[1]["session_id"])

    assert lines, "nothing was measured before the radio went"
    assert sub.get(timeout=5) is None  # the sentinel a survey reads as "stopped early"


def test_a_sweep_that_measured_nothing_is_not_reported_as_finished(
    sidecar: str, monkeypatch
) -> None:
    """MEASURED on the box 2026-09-04, and the reason this test exists: a dongle whose
    USB descriptors had stopped answering made the sweep exit on `No matching devices
    found`, and the route answered `complete: true` with an empty CSV. A success over a
    measurement that never happened is the worst answer available — it cost a container
    log read to notice, which is exactly what the owner cannot do."""

    class _Silent(_SpectrumRadio):
        """A radio that opens and then delivers nothing, which is what a dongle in
        trouble looks like from here."""

        def read(self, samples: int) -> Any:
            self.alive = False
            raise listen.radio.RadioError("the stream stopped answering")

    monkeypatch.setattr(
        listen.radio.Radio,
        "open",
        staticmethod(
            lambda *, center_hz, rate_hz, **kw: _Silent(center_hz, rate_hz, **kw)
        ),
    )

    status, body = _post(sidecar, "/sweep", _sweep_body(seconds=1))

    # A REFUSAL, and an earlier one than before: `_confirm_started` watches a fresh
    # pipeline and a session whose radio died on the first read never becomes a lease at
    # all. Better than the 502 it used to reach — the answer names the driver's own
    # words instead of "the sweep measured nothing" after the fact.
    assert status == 400
    assert "did not start" in body["detail"]
    # ...and the radio is free regardless, because a failed sweep must not hold it.
    # Asserted on the registry rather than by starting something, since the radio this
    # fixture hands out is still the broken one.
    assert server.TUNER.sessions() == []


# --- two radios, over the wire ------------------------------------------------------


WHIP, WIRE = "09022796", "77192819"


def test_APRS_and_the_tuner_run_on_different_radios_at_once(sidecar: str) -> None:
    """The whole point of P0b. With one slot, turning APRS logging on meant the tuner
    sheet answered 409 — on a box with a second dongle sitting idle."""
    logging_ = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    listening = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )

    assert logging_[0] == 200 and listening[0] == 200
    _, health = _get(sidecar, "/healthz")
    assert {s["serial"] for s in health["sessions"]} == {WHIP, WIRE}


def test_healthz_reports_every_session_not_just_the_one_the_omnibox_draws(
    sidecar: str,
) -> None:
    """`listening` keeps its shape because the PWA's omnibox reads it and draws one
    icon. `sessions` is the whole truth beside it: a caller that asked `listening`
    whether APRS was running got the right answer only when nothing else held a radio.
    """
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )

    _, health = _get(sidecar, "/healthz")

    assert health["listening"]["serial"] == WHIP  # the tuner, not the service
    assert [s["purpose"] for s in health["sessions"]] == ["listen", "aprs"]  # by serial


def test_the_same_radio_is_still_refused_by_name(sidecar: str) -> None:
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )

    status, body = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WIRE},
    )

    assert status == 409
    assert WIRE in body["detail"] and "logging APRS" in body["detail"]


def test_releasing_one_radio_leaves_the_other_logging(sidecar: str) -> None:
    """Release is per session, and has to stay that way: the tuner sheet's Release
    button must not turn APRS logging off as a side effect."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    listening = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )[1]

    assert (
        _post(sidecar, "/listen/stop", {"session_id": listening["session_id"]})[0]
        == 200
    )

    _, health = _get(sidecar, "/healthz")
    assert [s["serial"] for s in health["sessions"]] == [WIRE]


def test_packets_come_from_the_APRS_radio_while_the_tuner_holds_the_other(
    sidecar: str,
) -> None:
    """The route used to ask for "the session" and check its purpose. With two, the one
    it happened to get was the tuner's — so the owner watching packets was told the
    radio was not logging while it was."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )

    with urllib.request.urlopen(sidecar + "/listen/packets", timeout=5) as resp:
        assert resp.status == 200


def test_a_capture_and_a_session_on_different_radios_do_not_collide(
    sidecar: str,
) -> None:
    """The capture path holds a radio without being a session. It used to take a global
    lock, so recording off one dongle refused a capture on the other — and `start` never
    consulted that lock at all, so a session could open a dongle mid-recording."""
    server.TUNER.reserve(WIRE, "recording")

    free = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )
    taken = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "serial": WIRE},
    )

    assert free[0] == 200
    assert taken[0] == 409 and "recording" in taken[1]["detail"]


def test_healthz_busy_still_means_a_capture_is_running(sidecar: str) -> None:
    """It has never meant "a session exists" — `listening` answers that — and a change
    of meaning here would read as a permanently busy radio in the PWA."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )
    assert _get(sidecar, "/healthz")[1]["busy"] is False

    assert server.TUNER.reserve(WIRE, "recording") is None
    assert _get(sidecar, "/healthz")[1]["busy"] is True


def test_an_unnamed_stop_releases_the_tuner_and_leaves_APRS_logging(
    sidecar: str,
) -> None:
    """jerv's "release the radio" and the debug console's stop both send no id. With one
    radio that could only mean one thing; with two, releasing whichever came first would
    stop a log the owner armed on a schedule — silently, and reporting success."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )

    assert _post(sidecar, "/listen/stop", {"session_id": None})[1] == {"stopped": True}

    _, health = _get(sidecar, "/healthz")
    assert [s["purpose"] for s in health["sessions"]] == ["aprs"]


def test_an_unnamed_stop_never_stops_a_SERVICE(sidecar: str) -> None:
    """Not even when it is the only session running.

    An earlier cut fell back to "the only session when there is exactly one", reasoning
    that a one-dongle box has nothing to choose between. The condition it tested was
    `len(sessions) == 1`, which is equally true of a two-dongle box running only APRS —
    so jerv's "release the radio" would have stopped a log the owner armed on a
    schedule. `holding` names what is actually on a radio so the caller can say so."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )

    _, body = _post(sidecar, "/listen/stop", {"session_id": None})

    assert body["stopped"] is False
    assert body["holding"] == [
        {"purpose": "aprs", "serial": WIRE, "session_id": mock.ANY}
    ]
    assert _get(sidecar, "/healthz")[1]["sessions"] != []


def test_an_unnamed_stop_never_picks_between_two_services(sidecar: str) -> None:
    """Nothing a person said identifies one of them, and guessing is how a control ends
    up doing something different each press."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    # Started directly: `/sweep` blocks for the length of the survey, and what matters
    # here is a second session existing, not how it got there.
    server.TUNER.start(
        146_000_000,
        "fm",
        None,
        purpose=listen.PURPOSE_SPECTRUM,
        sweep=listen.Sweep.of(
            144_000_000, 144_200_000, 4_687.5, 300, capture=(2_400_000, 512)
        ),
        serial=WHIP,
    )

    _, body = _post(sidecar, "/listen/stop", {"session_id": None})

    assert body["stopped"] is False
    # In serial order, like every other list the sidecar reports.
    assert [h["purpose"] for h in body["holding"]] == ["spectrum", "aprs"]


def test_an_unnamed_stop_with_nothing_running_is_not_an_error(sidecar: str) -> None:
    _, body = _post(sidecar, "/listen/stop", {"session_id": None})

    assert body == {"stopped": False, "holding": []}


def test_the_audio_stream_comes_from_the_LISTENING_radio(sidecar: str) -> None:
    """It used to ask for "the session" and stream whatever it got. With APRS holding a
    radio and the tuner holding another, the one it got could be the APRS lease — and
    the owner who opened the tuner sheet would get 1200-baud AFSK through their
    speakers where they expected a voice."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )

    with urllib.request.urlopen(sidecar + "/listen/audio", timeout=5) as resp:
        assert resp.status == 200
    # ...and with only an APRS session there is no audio to stream, rather than its own.
    _post(sidecar, "/listen/stop", {"session_id": server.TUNER.current(WHIP).id})
    assert _get(sidecar, "/listen/audio")[0] == 409


def test_captions_come_from_the_LISTENING_radio(sidecar: str) -> None:
    """Same argument as the audio stream: whisper transcribing packet squawk produces
    confident nonsense rather than an error."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    listening = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )[1]

    with urllib.request.urlopen(sidecar + "/listen/segments", timeout=5) as resp:
        assert resp.status == 200

    _post(sidecar, "/listen/stop", {"session_id": listening["session_id"]})
    assert _get(sidecar, "/listen/segments")[0] == 409


def test_an_unnamed_retune_moves_the_TUNER_not_a_service(sidecar: str) -> None:
    """The tuner sheet on an older client sends no session id. Resolving that to "the
    session" would retune whichever came first — moving the packet channel to whatever
    the sheet asked for, while the lease went on claiming to log APRS."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )

    status, body = _post(sidecar, "/listen/tune", {"frequency_hz": 101_100_000})

    assert status == 200 and body["serial"] == WHIP
    _, health = _get(sidecar, "/healthz")
    aprs = next(s for s in health["sessions"] if s["purpose"] == "aprs")
    assert aprs["frequency_hz"] == 144_390_000  # untouched


def test_retuning_a_session_that_has_gone_says_so(sidecar: str) -> None:
    """`find` matches on id, so a named session that is not here is a STALE id. An
    earlier cut left the "no longer the live one" check below `find`, where it could
    never fire, and this fell through to "nothing is listening" — false whenever a
    listening session existed, and not something the owner could act on."""
    stale = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )[1]["session_id"]
    _post(sidecar, "/listen/stop", {"session_id": stale})
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 146_520_000, "mode": "fm", "serial": WIRE},
    )

    status, body = _post(
        sidecar, "/listen/tune", {"session_id": stale, "frequency_hz": 101_100_000}
    )

    assert status == 409
    assert "no longer the live one" in body["detail"]


# --- /capture, which holds a radio without being a session ----------------------------


def _capture(base: str, body: dict[str, Any]) -> tuple[int, dict[str, str]]:
    """POST /capture, which answers with a WAV and its findings in HEADERS.

    `_post` cannot read this route: the body is audio, so parsing it as JSON is how
    every attempt to test capture failed before there was a helper for it."""
    req = urllib.request.Request(
        base + "/capture",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, {"detail": (json.loads(err.read() or b"{}")).get("detail", "")}


class _Recorder:
    """rtl_fm as `capture` actually drives it: it streams until it is STOPPED.

    So the first `communicate` always times out — that is the normal path, not the
    error one — and what the recording is worth depends on how it is then asked to
    stop. This fake remembers the signals in order, which is the whole of what
    `test_a_capture_closes_the_device_before_it_kills_it` can check without a radio."""

    def __init__(self, cmd, pcm: bytes, deaf: bool = False) -> None:
        self.args = cmd
        # A superset of `_FakeProc`, because patching `Popen` patches it for the whole
        # process: a test that starts a SESSION while a capture fake is installed gets
        # one of these, and its pumps read these attributes.
        self.stdout = _Empty()
        self.stderr = _Empty()
        self.stdin = _Sink()
        self.returncode = None
        self.signals: list[str] = []
        self._pcm = pcm
        self._deaf = deaf  # ignores SIGTERM, so the escalation has to happen
        self._stopped = False

    def communicate(self, timeout: float | None = None):
        if not self._stopped:
            raise server.subprocess.TimeoutExpired(self.args, timeout or 0)
        return self._pcm, b""

    def terminate(self) -> None:
        self.signals.append("terminate")
        self._stopped = not self._deaf

    def kill(self) -> None:
        self.signals.append("kill")
        self._stopped = True

    def poll(self) -> int | None:
        return 0 if self._stopped else None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _recording(
    monkeypatch, pcm: bytes = b"\x00\x10" * 8000, deaf: bool = False
) -> list[_Recorder]:
    """rtl_fm that records instantly. The real one streams until it is stopped, so a
    test driving the route would otherwise wait out the whole capture.

    Returns the list it spawns into, so a caller can inspect how it was torn down."""
    made: list[_Recorder] = []

    def popen(cmd, **_kw):
        made.append(_Recorder(cmd, pcm, deaf))
        return made[-1]

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    return made


def test_a_capture_names_the_radio_it_was_told_to_open(
    sidecar: str, monkeypatch
) -> None:
    """The serial has to reach BOTH the reservation and rtl_fm's argv. Nothing drove
    this route, so the whole serial-to-key plumbing ran only in production."""
    made = _recording(monkeypatch)

    status, _ = _capture(
        sidecar,
        {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WHIP},
    )

    assert status == 200
    cmd = made[0].args
    arg = cmd[cmd.index("-d") + 1]
    # BARE, not `serial=X`: librtlsdr's verbose_device_search has no key=value form.
    assert arg == WHIP and "=" not in arg


def test_a_capture_releases_its_radio_when_it_finishes(
    sidecar: str, monkeypatch
) -> None:
    """The `finally` is the one line whose failure strands a radio: `blocking_key` would
    then refuse it for ever and /healthz would report busy with nothing running."""
    _recording(monkeypatch)

    assert (
        _capture(
            sidecar,
            {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WHIP},
        )[0]
        == 200
    )

    assert _get(sidecar, "/healthz")[1]["busy"] is False
    assert server.TUNER.reserve(WHIP, "recording") is None


def test_a_capture_releases_its_radio_even_when_rtl_fm_produces_nothing(
    sidecar: str, monkeypatch
) -> None:
    """The failure path, which is the one a `finally` exists for."""
    _recording(monkeypatch, pcm=b"")

    status, _ = _capture(
        sidecar,
        {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WHIP},
    )

    assert status == 400
    assert _get(sidecar, "/healthz")[1]["busy"] is False


def test_a_capture_of_the_second_Nyquist_zone_is_refused_like_a_session(
    sidecar: str, monkeypatch
) -> None:
    """A capture is meant to be a sample of what a session would hear, so it has to
    refuse what a session refuses. Between 14.4 and 24 MHz the tuner is bypassed and the
    ADC's 28.8 MHz clock folds the request: this would have returned a healthy-looking
    WAV of 10.7 MHz, `peak` and all, for a request that named 18.1."""
    _recording(monkeypatch)

    status, body = _capture(
        sidecar,
        {"frequency_hz": 18_100_000, "seconds": 1, "mode": "usb", "serial": WHIP},
    )

    assert status == 400
    assert "10.700 MHz" in body["detail"]
    # And the radio it never opened is not left reserved.
    assert _get(sidecar, "/healthz")[1]["busy"] is False


def test_a_capture_is_refused_by_the_session_holding_THAT_radio(
    sidecar: str, monkeypatch
) -> None:
    _recording(monkeypatch)
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )

    status, body = _capture(
        sidecar,
        {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WIRE},
    )

    assert status == 409
    assert WIRE in body["detail"] and "logging APRS" in body["detail"]


def test_a_capture_runs_on_a_free_radio_while_another_is_held(
    sidecar: str, monkeypatch
) -> None:
    """The refusal with no physical cause, over HTTP."""
    _recording(monkeypatch)
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )

    status, _ = _capture(
        sidecar,
        {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WHIP},
    )

    assert status == 200


# --- the live spectrum ----------------------------------------------------------


class _SpectrumRadio:
    """A radio delivering noise with one carrier in it, for the I/Q spectrum path.

    There is no second engine to stand in for since B1, so the spectrum tests drive the
    real transform against a fake DEVICE rather than a fake process writing CSV. Free
    running: it hands back whatever is asked for, so rows arrive as fast as the pump
    reads and a viewer never has to be fed by hand."""

    def __init__(self, center_hz: int, rate_hz: int, **kwargs: Any) -> None:
        self.center_hz = center_hz
        self.rate_hz = rate_hz
        self.opened_with = kwargs
        self.alive = True
        self.gain_db: float | None = None
        self.closed = False
        self._phase = 0.0
        self._rng = np.random.default_rng(20260906)

    def read(self, samples: int) -> Any:
        n = int(samples)
        k = np.arange(n, dtype=np.float64) + self._phase
        self._phase += n
        wave = np.exp(2.0j * np.pi * (self.rate_hz / 8.0) * k / self.rate_hz)
        noise = self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)
        return listen.radio.Reading(
            samples=(wave + 0.03 * noise).astype(np.complex64),
            at=time.time(),
            reads=1,
            overflows=0,
            timeouts=0,
            center_hz=self.center_hz,
        )

    def set_gain(self, db: float | None) -> None:
        self.gain_db = db

    def retune(self, *, center_hz: int | None = None, **_k: Any) -> int:
        if center_hz is not None:
            self.center_hz = center_hz
        return 0

    def close(self) -> None:
        self.closed = True
        self.alive = False


#: The capture the api would name for the 2 m test range. Sent by `_start_spectrum` for
#: the reason the api sends it: the band table lives in ONE place and the sidecar
#: executes the plan it was handed, so a sweep with no capture is refused rather than
#: drawn another way (B1).
TEST_CAPTURE = {"rate_hz": 2_400_000, "bins": 512, "hops": 1}


def _start_spectrum(base: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "purpose": "spectrum",
        "start_hz": 144_000_000,
        "stop_hz": 144_200_000,
        "bin_hz": 2_400_000 / 512,
        **TEST_CAPTURE,
    }
    body.update(extra)
    return _post(base, "/listen/start", body)


def test_a_spectrum_session_is_started_by_its_range_not_a_frequency(
    sidecar: str,
) -> None:
    status, body = _start_spectrum(sidecar)

    assert status == 200
    assert body["purpose"] == "spectrum"
    assert body["sweep"]["start_hz"] == 144_000_000
    # No `frequency_hz` was sent at all: a span's centre is the only frequency it has.
    assert body["frequency_hz"] == 144_100_000


def test_a_spectrum_AND_a_survey_both_reach_shortwave_now(sidecar: str) -> None:
    """One range, two purposes, ONE answer — which is what A5 was about.

    These two spent three waves giving different answers to the same question, because
    the survey ran `rtl_power`, which hardcodes the ADC's I branch where this board
    wires Q. The picture stopped needing that tool at B1 and the survey at B2, so the
    split they were split over is gone: a survey is an accumulator over the picture."""
    shortwave = {
        "start_hz": 7_000_000,
        "stop_hz": 7_256_000,
        "bin_hz": 250,
        "rate_hz": 256_000,
        "bins": 1024,
        "hops": 1,
    }

    status, body = _start_spectrum(sidecar, **shortwave)
    assert status == 200
    _post(sidecar, "/listen/stop", {"session_id": body["session_id"]})

    status, body = _post(sidecar, "/sweep", {**shortwave, "seconds": 2})
    assert status == 200
    assert body["csv"].strip()


def test_a_spectrum_below_what_the_ADC_reaches_is_still_refused(sidecar: str) -> None:
    """The floor moved down to the board's own; it did not go away."""
    status, body = _start_spectrum(sidecar, start_hz=20_000, stop_hz=90_000)

    assert status == 400
    assert "below what this radio reaches" in body["detail"]


def test_a_spectrum_with_no_range_is_refused_rather_than_run(sidecar: str) -> None:
    status, body = _post(sidecar, "/listen/start", {"purpose": "spectrum"})

    assert status == 400
    assert body["detail"]


def test_the_waterfall_is_refused_when_the_radio_is_doing_something_else(
    sidecar: str,
) -> None:
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs"},
    )

    status, body = _get(sidecar, "/listen/spectrum")

    # Named, because "idle" and "busy logging" need opposite advice from the owner.
    assert status == 409
    assert "logging APRS" in body["detail"]


def test_the_waterfall_is_refused_when_nothing_is_watching(sidecar: str) -> None:
    status, body = _get(sidecar, "/listen/spectrum")

    assert status == 409
    assert "nothing is watching" in body["detail"]


def test_a_row_reaches_a_viewer_carrying_its_own_range(
    sidecar: str, monkeypatch
) -> None:
    """The shape the renderer depends on: a frame says where it is, so a retune needs
    no protocol event and a client that draws what each row says is already right."""
    assert _start_spectrum(sidecar)[0] == 200

    with urllib.request.urlopen(sidecar + "/listen/spectrum", timeout=10) as resp:
        row = next(
            json.loads(raw) for raw in resp if not json.loads(raw).get("keepalive")
        )

    # Centred where the range is, at the width the capture makes — `rate / bins`, not a
    # width anyone asked for.
    assert row["bin_hz"] == 2_400_000 / 512
    assert row["bins"] == 512
    assert row["start_hz"] == 144_100_000 - 256 * row["bin_hz"]
    assert row["stop_hz"] == row["start_hz"] + row["bins"] * row["bin_hz"]
    assert len(row["db"]) == 512


def test_a_row_says_which_picture_it_is(sidecar: str, monkeypatch) -> None:
    """One session now draws two pictures off one capture, so every row on the wire has
    to say which — the PWA reads it, and so does anything holding rows across time."""
    assert _start_spectrum(sidecar)[0] == 200

    with urllib.request.urlopen(sidecar + "/listen/spectrum", timeout=10) as resp:
        row = next(
            json.loads(raw) for raw in resp if not json.loads(raw).get("keepalive")
        )

    assert row["view"] == "band"


def test_a_view_the_sidecar_does_not_serve_is_refused_not_substituted(
    sidecar: str, monkeypatch
) -> None:
    """Named rather than coerced. Quietly handing a viewer the band when it asked for
    the channel is the same class of substitution as swapping the spectrum engine
    underneath a measurement: a picture that is not of what it says it is."""
    assert _start_spectrum(sidecar)[0] == 200

    status, body = _get(sidecar, "/listen/spectrum?view=sideways")

    assert status == 400
    assert "view must be one of" in body["detail"]


def test_the_view_asked_for_is_the_view_served(sidecar: str, monkeypatch) -> None:
    """`?view=` reaches `subscribe_frames`, which is the whole of the plumbing: the
    filtering itself is the session's and is tested there."""
    assert _start_spectrum(sidecar)[0] == 200

    with urllib.request.urlopen(
        sidecar + "/listen/spectrum?view=band", timeout=10
    ) as resp:
        row = next(
            json.loads(raw) for raw in resp if not json.loads(raw).get("keepalive")
        )

    assert row["view"] == "band"


def test_a_waterfall_is_moved_on_the_session_it_already_holds(sidecar: str) -> None:
    _, started = _start_spectrum(sidecar)

    status, body = _post(
        sidecar,
        "/listen/tune",
        {
            "session_id": started["session_id"],
            "start_hz": 440_000_000,
            "stop_hz": 440_200_000,
            "bin_hz": 2_400_000 / 512,
            **TEST_CAPTURE,
        },
    )

    assert status == 200
    # Same lease, same id, new band: the radio is never released in between, which is
    # what stops a retune losing the dongle to whatever asks next.
    assert body["session_id"] == started["session_id"]
    assert body["sweep"]["start_hz"] == 440_000_000
    assert body["frequency_hz"] == 440_100_000


def test_a_refused_move_costs_a_sentence_and_not_the_radio(sidecar: str) -> None:
    """The whole tap, end to end: watching 2 m, ask for a range with no capture plan,
    get a 400.

    There is one floor now — the radio's own — so the range is legal all the
    way down to the engine — which since B1 refuses what it has no plan for rather than
    reaching for a tool that measures on another scale. What the owner must be left with
    is the picture they already had: the 400 is the cheap half, and the expensive half
    is that the session, the lease and the old range are all exactly where they were."""
    _, started = _start_spectrum(sidecar)

    status, refused = _post(
        sidecar,
        "/listen/tune",
        {
            "session_id": started["session_id"],
            "start_hz": 7_125_000,
            "stop_hz": 7_300_000,
            "bin_hz": 250,
        },
    )

    assert status == 400
    assert "no capture plan" in refused["detail"]
    # Not a 409, which is what a session reaped out from under the sheet would look
    # like — and the sheet would then have to start a new one to get a picture back.
    status, health = _get(sidecar, "/healthz")
    assert status == 200
    live = health["sessions"]
    assert [s["session_id"] for s in live] == [started["session_id"]]
    assert live[0]["purpose"] == "spectrum"
    assert live[0]["sweep"]["start_hz"] == 144_000_000


def test_an_unnamed_move_with_a_range_finds_the_waterfall_not_the_tuner(
    sidecar: str,
) -> None:
    """Two radios, two jobs: a body carrying a RANGE is asking about the waterfall, and
    resolving "the session" would have moved whichever one happened to answer first."""
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": "09022796"},
    )
    _start_spectrum(sidecar, serial="77192819")

    status, body = _post(
        sidecar,
        "/listen/tune",
        {
            "start_hz": 440_000_000,
            "stop_hz": 440_200_000,
            "bin_hz": 2_400_000 / 512,
            **TEST_CAPTURE,
        },
    )

    assert status == 200
    assert body["purpose"] == "spectrum"
    # ...and the tuner is exactly where it was.
    _, health = _get(sidecar, "/healthz")
    listening = next(s for s in health["sessions"] if s["purpose"] == "listen")
    assert listening["frequency_hz"] == 99_300_000


def test_a_reset_re_enumerates_the_named_device(sidecar: str, monkeypatch) -> None:
    """The software equivalent of unplugging it, and the ONLY recovery that does not
    involve hands: nothing else clears a dongle that is on the bus but not answering —
    not a container restart, not a rebuild, not an update. The owner runs this box with
    no terminal (CLAUDE.md #10), so "go and unplug it" is not an answer."""
    hit: list[str] = []
    monkeypatch.setattr(sys.modules["usbdev"], "reset", hit.append)

    status, body = _post(
        sidecar, "/reset", {"serial": WIRE, "device_node": "/dev/bus/usb/003/010"}
    )

    assert status == 200
    assert body["reset"] is True
    assert hit == ["/dev/bus/usb/003/010"]


def test_a_reset_is_refused_while_that_radio_is_in_use(
    sidecar: str, monkeypatch
) -> None:
    """Through the LEASE, so a reset cannot be run under a session using the radio —
    re-enumerating a device mid-decode would take the log down with no explanation."""
    hit: list[str] = []
    monkeypatch.setattr(sys.modules["usbdev"], "reset", hit.append)
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )

    status, body = _post(
        sidecar, "/reset", {"serial": WIRE, "device_node": "/dev/bus/usb/003/010"}
    )

    assert status == 409
    assert "logging APRS" in body["detail"]
    assert hit == []


def test_the_other_radio_keeps_working_through_a_reset(
    sidecar: str, monkeypatch
) -> None:
    # One device is re-enumerated, not the bus. APRS on the second dongle is untouched,
    # which is the whole reason this is addressed by serial.
    monkeypatch.setattr(sys.modules["usbdev"], "reset", lambda _n: None)
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WHIP},
    )

    status, _ = _post(
        sidecar, "/reset", {"serial": WIRE, "device_node": "/dev/bus/usb/003/010"}
    )

    assert status == 200
    _, health = _get(sidecar, "/healthz")
    assert any(s["purpose"] == "aprs" for s in health["sessions"])


def test_a_reset_leaves_the_radio_free_afterwards(sidecar: str, monkeypatch) -> None:
    # The reservation is held only for the ioctl. Left behind, it would lock the radio
    # out for its full TTL with nothing running — a repair that broke the thing.
    monkeypatch.setattr(sys.modules["usbdev"], "reset", lambda _n: None)
    _post(sidecar, "/reset", {"serial": WIRE, "device_node": "/dev/bus/usb/003/010"})

    free, _ = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WIRE},
    )

    assert free == 200


def test_a_reset_that_the_kernel_refuses_says_so(sidecar: str, monkeypatch) -> None:
    def boom(_node: str) -> None:
        raise OSError(19, "No such device")

    monkeypatch.setattr(sys.modules["usbdev"], "reset", boom)

    status, body = _post(
        sidecar, "/reset", {"serial": WIRE, "device_node": "/dev/bus/usb/003/010"}
    )

    assert status == 502
    assert "would not reset" in body["detail"]
    # ...and the radio is not left reserved by a repair that failed.
    free, _ = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WIRE},
    )
    assert free == 200


def test_a_node_that_is_not_a_device_node_is_refused(sidecar: str) -> None:
    """`os.open` on a caller-supplied path inside a ROOT container is not a thing to
    leave open — even one only the api can reach today, because "only the api can reach
    it" is a property of routing rather than of this code."""
    status, _ = _post(sidecar, "/reset", {"serial": WIRE, "device_node": "/etc/passwd"})

    assert status == 400


def test_a_capture_closes_the_device_before_it_kills_it(
    sidecar: str, monkeypatch
) -> None:
    """The capture path SIGKILLed rtl_fm on every single recording, and that is the one
    thing `listen.Session._kill` exists to spell out as forbidden.

    `subprocess.run(timeout=...)` calls `process.kill()` when the timeout fires — and
    for this route the timeout IS the recording length, so the normal path was the
    fatal one. rtl_fm's SIGTERM handler cancels the pending async USB transfer and
    CLOSES the device; SIGKILL never runs it, and a dongle torn off a submitted
    transfer can stop answering the descriptor reads librtlsdr enumerates with. The
    symptom is a radio that reads as absent while sysfs still lists it by name — which
    is the state one of this box's two dongles is in."""
    made = _recording(monkeypatch)

    status, _ = _capture(
        sidecar,
        {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WHIP},
    )

    assert status == 200
    assert made[0].signals == ["terminate"], "a capture must never SIGKILL a live radio"


def test_a_capture_still_escalates_to_a_kill_for_a_wedged_tool(
    sidecar: str, monkeypatch
) -> None:
    """Politeness is not patience. SIGTERM first, but a tool that ignores it must not
    hold the radio for ever — the grace is bounded and the kill is still there."""
    made = _recording(monkeypatch, deaf=True)

    status, _ = _capture(
        sidecar,
        {"frequency_hz": 99_300_000, "seconds": 1, "mode": "wbfm", "serial": WHIP},
    )

    assert status == 200
    assert made[0].signals == ["terminate", "kill"]


# --- the in-process device handle, which the lease cannot see -------------------------


def test_a_reset_is_refused_while_a_device_handle_is_open_here(
    sidecar: str, monkeypatch
) -> None:
    """The lease knows about child processes and TTL reservations; it does not know
    about a device handle held INSIDE this process, and that is exactly the dangerous
    case: `USBDEVFS_RESET` fired from the process still holding the usbfs fd with
    interface 0 claimed re-enumerates the device at a new node, leaves the orphaned
    libusb handle at ENODEV, and nothing in `radio.py` ever learns
    (docs/plans/SDR_IQ_SPECTRUM_PLAN.md §3)."""
    hit: list[str] = []
    monkeypatch.setattr(sys.modules["usbdev"], "reset", hit.append)
    monkeypatch.setattr(sys.modules["radio"], "holders", lambda: {WIRE: "reading I/Q"})

    status, body = _post(
        sidecar, "/reset", {"serial": WIRE, "device_node": "/dev/bus/usb/003/010"}
    )

    assert status == 409
    assert "still open in this process" in body["detail"]
    assert "restart the sdr service" in body["detail"]
    assert hit == []
    # The OTHER dongle is untouched: this is one handle, not a global stop.
    status, _ = _post(
        sidecar, "/reset", {"serial": WHIP, "device_node": "/dev/bus/usb/003/011"}
    )
    assert status == 200


def test_an_unnamed_device_handle_blocks_a_reset_of_either_radio(
    sidecar: str, monkeypatch
) -> None:
    """A handle opened with no serial takes whichever device librtlsdr enumerated
    first, so nothing can prove it is not on the radio the reset is aimed at — the same
    rule the lease applies to an unnamed session, asked through the same function."""
    monkeypatch.setattr(sys.modules["usbdev"], "reset", lambda _n: None)
    monkeypatch.setattr(sys.modules["radio"], "holders", lambda: {"": "reading I/Q"})

    for serial in (WHIP, WIRE):
        status, body = _post(
            sidecar, "/reset", {"serial": serial, "device_node": "/dev/bus/usb/003/010"}
        )
        assert status == 409
        assert "unnamed" in body["detail"]


# --- the F0 probe ---------------------------------------------------------------------


def _probe_stub(monkeypatch, result: Any = None, raises: Exception | None = None):
    """Stand in for `radio.probe`, recording what the route asked and what the lease
    said while it ran. The probe itself is covered against a fake device in
    `test_sdr_radio.py`; what is under test here is the lease and the wire."""
    seen: list[dict[str, Any]] = []

    def _run(**kwargs: Any) -> dict[str, Any]:
        seen.append({**kwargs, "reserved_during": server.TUNER.reserved()})
        if raises is not None:
            raise raises
        return result or {"ok": True, "summary": "every claim held"}

    monkeypatch.setattr(sys.modules["radio"], "probe", _run)
    return seen


def test_the_probe_takes_the_radio_and_gives_it_back(sidecar: str, monkeypatch) -> None:
    """Through the LEASE, like every other holder here, so it cannot run under a live
    session and a session cannot start under it — and released in a `finally`, because
    a claim staked and not returned refuses that radio until the TTL lapses."""
    seen = _probe_stub(monkeypatch)

    status, body = _post(
        sidecar,
        "/soapy/probe",
        {"serial": WIRE, "center_hz": 10_000_000, "rate_hz": 256_000, "bins": 1024},
    )

    assert status == 200
    assert body["ok"] is True
    assert seen[0]["serial"] == WIRE
    assert seen[0]["center_hz"] == 10_000_000
    assert seen[0]["bins"] == 1024
    assert seen[0]["reserved_during"] is True, "the probe ran without holding the radio"
    assert server.TUNER.reserved() is False


def test_the_probe_is_refused_while_something_else_holds_that_radio(
    sidecar: str, monkeypatch
) -> None:
    seen = _probe_stub(monkeypatch)
    _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 144_390_000, "mode": "fm", "purpose": "aprs", "serial": WIRE},
    )

    status, body = _post(sidecar, "/soapy/probe", {"serial": WIRE})

    assert status == 409
    assert "logging APRS" in body["detail"]
    assert seen == []


def test_a_probe_that_the_radio_fails_still_releases_it(
    sidecar: str, monkeypatch
) -> None:
    """The failure path is the one that leaks a lease, so it is the one worth a test."""
    radio_mod = sys.modules["radio"]
    _probe_stub(monkeypatch, raises=radio_mod.RadioError("the radio would not open"))

    status, body = _post(sidecar, "/soapy/probe", {"serial": WHIP})

    assert status == 502
    assert "would not open" in body["detail"]
    assert server.TUNER.reserved() is False


def test_the_probe_refuses_what_the_radio_cannot_do(sidecar: str, monkeypatch) -> None:
    """Bounded here as well as in the api, for the reason `validate_serial` is: a bound
    that lives only in the caller is not a bound once there is a second caller, and
    this process has its own HTTP surface."""
    seen = _probe_stub(monkeypatch)

    for body in (
        {"center_hz": 2_000_000_000},
        {"rate_hz": 50_000},
        {"bins": 100_000},
        {"serial": {"not": "a serial"}},
    ):
        status, _ = _post(sidecar, "/soapy/probe", body)
        assert status == 400, body

    assert seen == []
    assert server.TUNER.reserved() is False


def test_the_probe_has_let_go_of_the_radio_before_it_answers(
    sidecar: str, monkeypatch
) -> None:
    """The lease must be back BEFORE the response is written, not in a `finally` after.

    Sending first leaves a window where the caller holds a 200 and the radio is still
    reserved, so anything acting on that answer meets a 409 for a hold that is already
    over. It surfaced as a test that passed alone and failed in a full run — the same
    race, won and lost by timing. `capture` has always released before answering.

    The ORDER is asserted, not the outcome. Checking `reserved()` after the response
    comes back only races the server thread again: the first version of this test passed
    against the unfixed code, because unreserve usually wins. Recording which of the two
    happened first cannot be won by being fast."""
    _probe_stub(monkeypatch)
    order: list[str] = []

    real_unreserve = server.TUNER.unreserve
    real_json = server.Handler._json

    def watched_unreserve(serial):
        order.append("released")
        return real_unreserve(serial)

    def watched_json(self, code, body):
        order.append(f"answered {code}")
        return real_json(self, code, body)

    monkeypatch.setattr(server.TUNER, "unreserve", watched_unreserve)
    monkeypatch.setattr(server.Handler, "_json", watched_json)

    status, _ = _post(
        sidecar,
        "/soapy/probe",
        {"serial": WIRE, "center_hz": 10_000_000, "rate_hz": 256_000, "bins": 1024},
    )

    assert status == 200
    assert order == ["released", "answered 200"], (
        f"the probe answered while still holding the radio: {order}"
    )


# --- F6's own probe: did the engine swap work on this radio? ------------------------


def _frame(bin_hz: int | float, bins: int, start_hz: int = 144_000_000) -> Any:
    return listen.Frame(
        at=0.0, start_hz=start_hz, bin_hz=bin_hz, db=[-44.0] * (bins - 1) + [-15.0]
    )


def _sweep_with_capture() -> Any:
    return listen.Sweep.of(
        144_000_000, 144_400_000, 600, 60, capture=(2_400_000, 4_000)
    )


def test_the_probe_reports_the_engine_that_actually_ran() -> None:
    """The sidecar drops to rtl_power at RUNTIME when a radio will not open, so the
    engine in use is a fact about this moment rather than about the request — and a
    silent downgrade is a waterfall quietly at a tenth of the rate it claims."""
    verdict = server._spectrum_verdict(
        _sweep_with_capture(), [_frame(600, 4_000)] * 30, 3.0, "rtl_power"
    )

    assert verdict["ok"] is False
    assert any("runtime fallback" in f for f in verdict["findings"])


def test_a_width_the_transform_never_used_is_a_finding() -> None:
    """The one failure nothing downstream can see: the PWA draws bin `i` at
    `start + i * bin_hz` and believes whatever the frame says."""
    verdict = server._spectrum_verdict(
        _sweep_with_capture(), [_frame(586, 4_000)] * 30, 3.0, "iq"
    )

    assert verdict["ok"] is False
    assert any("nothing computed" in f for f in verdict["findings"])


def test_a_frame_rate_no_better_than_rtl_power_is_a_finding() -> None:
    """`rtl_power` clamps its interval to `>= 1s` in its own C, and removing that
    ceiling is what this whole plan is for. One frame a second from the I/Q engine
    means the ceiling is still there, wearing the new engine's name."""
    verdict = server._spectrum_verdict(
        _sweep_with_capture(), [_frame(600, 4_000)] * 3, 3.0, "iq"
    )

    assert verdict["ok"] is False
    assert any("rtl_power's own one-second clamp" in f for f in verdict["findings"])


def test_a_healthy_run_passes_and_says_what_it_measured() -> None:
    verdict = server._spectrum_verdict(
        _sweep_with_capture(), [_frame(600, 4_000)] * 219, 3.0, "iq"
    )

    assert verdict["ok"] is True
    assert verdict["findings"] == []
    assert verdict["fps"] == 73.0
    assert verdict["frame"]["floor_db"] == -44.0
    assert verdict["frame"]["peak_db"] == -15.0


def test_no_frames_at_all_is_the_blank_picture_an_owner_would_see() -> None:
    verdict = server._spectrum_verdict(_sweep_with_capture(), [], 3.0, "iq")

    assert verdict["ok"] is False
    assert "no frames" in verdict["summary"]


def test_a_frame_with_nothing_in_it_blames_the_antenna_not_the_engine() -> None:
    """The measured HF state on this box. It is not the engine's failure and must not
    read as one, or the next reader goes looking in the wrong place."""
    dead = listen.Frame(
        at=0.0, start_hz=7_125_000, bin_hz=250, db=[iq.DB_FLOOR] * 1_024
    )
    swept = listen.Sweep.of(7_125_000, 7_381_000, 250, 60, capture=(256_000, 1_024))

    verdict = server._spectrum_verdict(swept, [dead] * 60, 3.0, "iq")

    assert verdict["ok"] is False
    assert any("antenna or the input" in f for f in verdict["findings"])


def test_the_spectrum_probe_says_what_the_rows_actually_found() -> None:
    """Peaks ride on every frame to the picture and to the agent, and until this was
    reported the only way to know whether a live band produced any was to open the PWA
    and look — the exact bind this probe exists to undo (CLAUDE.md #10).

    Counted over the whole watch as well as in the last frame, so a band whose traffic
    is intermittent is not judged by whichever row the probe happened to stop on."""
    sweep = listen.Sweep.of(
        144_000_000, 148_000_000, 9375, 3.0, capture=(2_400_000, 256)
    )
    quiet = listen.Frame(at=1.0, start_hz=144_000_000, bin_hz=9375, db=[-70.0] * 8)
    loud = listen.Frame(
        at=2.0,
        start_hz=144_000_000,
        bin_hz=9375,
        db=[-70.0] * 8,
        peaks=[{"hz": 144_390_000, "db": -50.0, "over_db": 18.0}],
    )

    verdict = server._spectrum_verdict(sweep, [quiet, loud, quiet], 3.0, "iq")

    assert verdict["signals"]["in_last_frame"] == 0
    assert verdict["signals"]["rows_with_any"] == 1


# --- the listen probe's verdict -----------------------------------------------------
#
# The probe exists so an owner with no terminal is not the test harness for their own
# box (CLAUDE.md #10), and its value is entirely in the FINDINGS: a dump of nine numbers
# leaves the reading of them to whoever remembers what each was supposed to be. So what
# is pinned here is that each failure it exists to catch produces a sentence.


class _ProbeSession:
    """Just enough session for `_listen_verdict` — it reads, it never drives."""

    def __init__(self, **over: object) -> None:
        self.engine = "iq"
        self.frequency_hz = 146_940_000
        self.mode = "fm"
        self.overflows = 0
        self.audio_peak = 0.4
        for key, value in over.items():
            setattr(self, key, value)


def _probe_frame(*, centre_hz=146_940_000, passband_hz=16_000.0, peak_at=170):
    """One tuning row: a floor with a bump in it, centred where the caller says."""
    bins, bin_hz = 341, 93.75
    db = [-78.0] * bins
    for i in range(max(0, peak_at - 70), min(bins, peak_at + 70)):
        db[i] = -34.0
    return listen.Frame(
        at=0.0,
        start_hz=int(centre_hz - (bins / 2) * bin_hz),
        bin_hz=bin_hz,
        db=db,
        passband_hz=passband_hz,
    )


def test_a_healthy_listen_probe_has_nothing_to_report() -> None:
    verdict = server._listen_verdict(
        _ProbeSession(), [_probe_frame()], [0.35, 0.41, 0.38], [0.0], [0.2], 5.0
    )
    assert verdict["ok"] is True
    assert verdict["findings"] == []
    assert verdict["engine"] == "iq"
    assert verdict["view"]["passband_hz"] == 16_000.0


def test_the_probe_names_a_silent_fallback_to_rtl_fm() -> None:
    """The fallback is right; its silence is not. On rtl_fm there is no tuning view at
    all, and nothing else on the box says why."""
    verdict = server._listen_verdict(
        _ProbeSession(engine="rtl_fm"), [], [0.4], [0.0], [0.2], 5.0
    )
    assert verdict["ok"] is False
    assert "rtl_fm" in verdict["findings"][0]


def test_the_probe_catches_silence_and_clipping_apart() -> None:
    """Both look like "it ran" from outside, and they need opposite fixes."""
    quiet = server._listen_verdict(
        _ProbeSession(), [_probe_frame()], [0.0, 0.0], [0.0], [0.0], 5.0
    )
    assert any("silence" in f for f in quiet["findings"])
    loud = server._listen_verdict(
        _ProbeSession(), [_probe_frame()], [1.0], [0.4], [0.9], 5.0
    )
    assert any("clipping" in f for f in loud["findings"])


def test_the_probe_reports_dropped_usb_buffers() -> None:
    """The one measurement no fake radio can produce, and the reason the probe runs for
    seconds rather than sampling once."""
    verdict = server._listen_verdict(
        _ProbeSession(overflows=7), [_probe_frame()], [0.4], [0.0], [0.2], 5.0
    )
    assert any("7 USB buffers" in f for f in verdict["findings"])


def test_the_probe_catches_the_mixer_and_the_tuning_disagreeing() -> None:
    """The offset-tuning failure this whole path is most exposed to: the radio sits
    above the station and the mixer takes that back out, so a row centred anywhere but
    the tuned frequency means the two are out of step — which on a narrowband channel
    is silence, from code that reads correctly in both places."""
    verdict = server._listen_verdict(
        _ProbeSession(), [_probe_frame(centre_hz=147_180_000)], [0.4], [0.0], [0.2], 5.0
    )
    assert any("disagree" in f for f in verdict["findings"])


def test_the_probe_finds_the_strongest_bin_relative_to_centre() -> None:
    """What proves the station landed where the offset says. A quarter of a megahertz
    of error is not subtle, and this is the number that would show it."""
    verdict = server._listen_verdict(
        _ProbeSession(), [_probe_frame(peak_at=170)], [0.4], [0.0], [0.2], 5.0
    )
    assert abs(verdict["view"]["strongest_offset_hz"]) < 200


def test_a_full_scale_peak_is_not_clipping_on_its_own() -> None:
    """MEASURED ON AIR, and the reason this metric changed. An FM discriminator turns
    every burst of noise that momentarily overpowers the carrier into a full-scale
    impulse — the click a weak FM signal makes — so one sample in 1600 pins the peak
    while the other 1599 are a perfectly good voice. The first version of this probe
    read `peak >= 0.999` and called NOAA weather at 17 dB SNR a clipping demodulator."""
    verdict = server._listen_verdict(
        _ProbeSession(), [_probe_frame()], [1.0, 1.0], [0.0006], [0.21], 6.0
    )
    assert verdict["findings"] == []
    assert verdict["ok"] is True
    assert verdict["audio_peak_max"] == 1.0
    assert verdict["clipped_fraction_max"] == 0.0006


def test_the_band_report_says_what_else_was_on_the_air() -> None:
    """The reading W3 exists to make possible: before it, "listen to 162.55" and "what
    is on 2 m" were two sessions and one radio, so asking the second meant giving up
    the first."""
    row = listen.Frame(
        at=1.0,
        start_hz=144_000_000,
        bin_hz=25_000,
        db=[-90.0, -50.0],
        peaks=[{"hz": 144_390_000, "db": -50.0, "over_db": 21.4}],
        view=listen.VIEW_BAND,
    )
    report = server._band_report([row, row], 30.0)

    assert report["rows"] == 2
    assert report["start_hz"] == 144_000_000
    assert report["gain_db"] == 30.0
    assert report["peaks"] == [{"mhz": 144.39, "db": -50.0, "over_db": 21.4}]
    assert "note" not in report


def test_a_band_report_under_agc_says_why_it_has_no_peaks() -> None:
    """ "No stations" and "no measurement" are opposite answers, and an empty list looks
    like the first. MEASURED ON AIR: 162.550 under AGC drove the front end to invent a
    symmetric comb of seven, so the session stops publishing peaks at all — and the
    report has to say that is what happened."""
    row = listen.Frame(at=1.0, start_hz=144_000_000, bin_hz=25_000, db=[-90.0, -50.0])
    report = server._band_report([row], None)

    assert report["gain_db"] is None
    assert report["peaks"] == []
    assert "AGC" in report["note"]


def test_a_band_report_with_no_rows_says_so_rather_than_guessing() -> None:
    """No row is not a quiet band: it is a probe that did not ask early enough, or an
    engine that draws nothing. Reporting an empty peak list with a plausible range
    would read as the first."""
    assert server._band_report([]) == {"rows": 0, "gain_db": None, "peaks": []}


def test_the_band_report_is_bounded() -> None:
    """A noisy band must not fill the verdict; the point is what the dial looks like."""
    many = [
        {"hz": 144_000_000 + i * 25_000, "db": -50.0, "over_db": 20.0}
        for i in range(40)
    ]
    row = listen.Frame(
        at=1.0, start_hz=144_000_000, bin_hz=25_000, db=[-90.0], peaks=many
    )

    assert len(server._band_report([row], 30.0)["peaks"]) == server.BAND_REPORT_PEAKS


def test_the_retune_report_proves_the_stream_was_not_rebuilt() -> None:
    """ "The session id survived" was true of a full pipeline rebuild too, by design, so
    it cannot be the evidence for A2. `setupStream` is called exactly once per `Radio`,
    so a stream token that did not change is proof the stream was never torn down —
    which is a claim no timing measurement can make."""
    rows = [
        listen.Frame(at=100.0, start_hz=1, bin_hz=1, db=[-1.0]),
        listen.Frame(at=100.1, start_hz=1, bin_hz=1, db=[-1.0]),
        listen.Frame(at=100.3, start_hz=1, bin_hz=1, db=[-1.0]),
        listen.Frame(at=100.4, start_hz=1, bin_hz=1, db=[-1.0]),
    ]
    report = server._retune_report(
        {"ok": True, "at": 100.15}, rows, 4242, 4242, 146_940_000
    )

    assert report["accepted"] is True
    assert report["stream_rebuilt"] is False
    assert report["frames_before"] == 2
    assert report["frames_after"] == 2
    assert report["worst_gap_ms"] == 200.0
    assert report["median_gap_ms"] == 100.0
    assert report["worst_gap_at_retune"] is True


def test_a_rebuilt_stream_is_reported_as_one() -> None:
    """A token that moved, and a session that has no radio at all, are the same answer:
    whatever is running now is not what was running before."""
    assert server._retune_report({}, [], 1, 2, 1)["stream_rebuilt"] is True
    assert server._retune_report({}, [], 1, 0, 1)["stream_rebuilt"] is True


def test_a_worst_gap_away_from_the_retune_says_so() -> None:
    """A gap that did not fall at the retune is an unrelated hiccup, and reading it as
    the cost of the retune is exactly the kind of mistake this file keeps making."""
    rows = [
        listen.Frame(at=100.0, start_hz=1, bin_hz=1, db=[-1.0]),
        listen.Frame(at=101.0, start_hz=1, bin_hz=1, db=[-1.0]),
        listen.Frame(at=101.1, start_hz=1, bin_hz=1, db=[-1.0]),
    ]
    report = server._retune_report({"ok": True, "at": 101.05}, rows, 7, 7, 1)

    assert report["worst_gap_ms"] == 1000.0
    assert report["worst_gap_at_retune"] is False


def test_a_retune_the_session_refused_is_reported_rather_than_swallowed() -> None:
    report = server._retune_report(
        {"ok": False, "refused": "that session has been released", "at": 1.0},
        [],
        7,
        7,
        1,
    )

    assert report["accepted"] is False
    assert "released" in report["refused"]


def test_a_spectrum_probe_finds_ITS_session_while_another_radio_listens(
    sidecar: str,
) -> None:
    """The probe used to ask for "the" session and then check the id, which on a box
    with a second radio listening was never its own — so it reported the spectrum
    session gone while that session was measuring perfectly.

    B7 is what exposed it: with the purpose ranking in the sidecar, "the" session was
    the listening one and the mismatch read as correct code. It asks by id now.
    """
    status, _ = _post(
        sidecar,
        "/listen/start",
        {"frequency_hz": 99_300_000, "mode": "wbfm", "serial": WHIP},
    )
    assert status == 200

    status, body = _post(
        sidecar,
        "/spectrum/probe",
        {
            "start_hz": 144_000_000,
            "stop_hz": 144_200_000,
            "bin_hz": 2_400_000 / 512,
            "seconds": 1,
            "serial": WIRE,
            **TEST_CAPTURE,
        },
    )

    assert status == 200
    assert "gone" not in body["summary"], body
    assert body["frames"] > 0
    # ...and the probe released only its own radio: the listener is still up.
    _, health = _get(sidecar, "/healthz")
    assert [s["serial"] for s in health["sessions"]] == [WHIP]
