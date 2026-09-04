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
from typing import Any, ClassVar
from unittest import mock

import pytest

_SDR = Path(__file__).resolve().parents[2] / "deploy/sdr"
sys.path.insert(0, str(_SDR))
_spec = importlib.util.spec_from_file_location("sdr_server", _SDR / "server.py")
assert _spec and _spec.loader
server = importlib.util.module_from_spec(_spec)
sys.modules["sdr_server"] = server
_spec.loader.exec_module(server)
listen = sys.modules["listen"]


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


def _csv_on_disk(monkeypatch, csv: str) -> None:
    """What rtl_power wrote, without a radio. Separate from `_sweeping` because the
    overrun case needs a CSV AND a process that is still running — a sweep that
    measured nothing is now a refusal, so "partial" has to be modelled as partial."""
    real_open = open

    def fake_open(path, *a, **k):
        if str(path).startswith("/tmp/sweep-"):
            import io as _io

            return _io.StringIO(csv)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)


def _sweeping(monkeypatch, csv: str = "") -> None:
    monkeypatch.setattr(listen.subprocess, "Popen", _DeadProc)
    if csv:
        _csv_on_disk(monkeypatch, csv)


def test_a_sweep_holds_the_radio_and_returns_its_rows(
    sidecar: str, monkeypatch
) -> None:
    """The happy path, and the shape the api reduces.

    The sidecar hands back the CSV rtl_power wrote and does NOT draw it: the image work
    needs a plotting stack, which `Dockerfile.sdr`'s apt-only, no-pip rule refuses, and
    the api already carries Pillow."""
    _sweeping(
        monkeypatch, csv="2026-09-03, 15:00:00, 144000000, 144005000, 5000, 12, -71.2\n"
    )

    status, body = _post(
        sidecar,
        "/sweep",
        {"start_hz": 144_000_000, "stop_hz": 148_000_000, "seconds": 2},
    )

    assert status == 200
    assert "-71.2" in body["csv"]
    assert (body["start_hz"], body["stop_hz"]) == (144_000_000, 148_000_000)
    assert body["complete"] is True


def test_a_sweep_frees_the_radio_when_it_ends(sidecar: str, monkeypatch) -> None:
    # A survey ends on its own, so nothing should be holding the tuner afterwards — the
    # next listener must not find the radio busy with a sweep that already finished.
    _sweeping(monkeypatch)

    _post(
        sidecar,
        "/sweep",
        {"start_hz": 144_000_000, "stop_hz": 148_000_000, "seconds": 2},
    )

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
    _sweeping(monkeypatch)

    status, body = _post(
        sidecar,
        "/sweep",
        {"start_hz": 440_000_000, "stop_hz": 450_000_000, "seconds": 2},
    )

    assert status == 409
    assert "logging APRS" in body["detail"]


def test_a_sweep_outside_the_tuner_s_range_is_refused(
    sidecar: str, monkeypatch
) -> None:
    # A sweep reaches 24 MHz-1.766 GHz. Below that the radio still LISTENS — it bypasses
    # the tuner — but rtl_power hardcodes the other ADC branch, so a sweep there would
    # tune something and measure nothing, and report the band as quiet.
    _sweeping(monkeypatch)

    status, body = _post(
        sidecar, "/sweep", {"start_hz": 1_000_000, "stop_hz": 2_000_000}
    )

    assert status == 400
    assert "cannot go below" in body["detail"]
    assert "still listen" in body["detail"]  # ...and say what DOES work down there


def test_a_sweep_with_no_range_is_refused_rather_than_run(
    sidecar: str, monkeypatch
) -> None:
    _sweeping(monkeypatch)

    status, _ = _post(
        sidecar, "/sweep", {"start_hz": 144_000_000, "stop_hz": 144_000_000}
    )

    assert status == 400


def test_a_range_whose_EDGE_is_out_of_band_is_refused(
    sidecar: str, monkeypatch
) -> None:
    """The check `listen.py` cannot make for us.

    It validates the CENTRE frequency, which for 20-70 MHz is 45 MHz — comfortably in
    band, while the sweep's low edge is below anything the sweep tool can reach. A sweep
    that silently started 4 MHz above where it was asked to would report the bottom of
    the range as quiet."""
    _sweeping(monkeypatch)

    status, body = _post(
        sidecar, "/sweep", {"start_hz": 20_000_000, "stop_hz": 70_000_000}
    )

    assert status == 400
    assert "cannot go below" in body["detail"]


def test_a_sweep_that_overruns_is_stopped_and_says_so(
    sidecar: str, monkeypatch
) -> None:
    """rtl_power's exit timer is what normally ends a sweep, so this is the case where
    it did NOT: the process is still running when the deadline passes.

    Two things have to happen. The radio is released — otherwise a wedged rtl_power
    holds the tuner until somebody notices — and the result says it is partial, because
    a short window reported as a full one reads as a quiet band."""
    monkeypatch.setattr(server, "SWEEP_SETTLE_S", 0)
    # The default fake stays alive until killed, which IS the overrun case. It still has
    # to have MEASURED something: a sweep with no rows at all is a different answer now
    # (see the test below), and conflating the two is what let an unopenable radio
    # report `complete: true`.
    _csv_on_disk(
        monkeypatch, "2026-09-04, 13:00:00, 144000000, 144100000, 25000.00, 12, -70.0\n"
    )

    status, body = _post(
        sidecar,
        "/sweep",
        {"start_hz": 144_000_000, "stop_hz": 148_000_000, "seconds": 1},
    )

    assert status == 200
    assert body["complete"] is False
    # And the next caller finds the radio free.
    free, _ = _post(
        sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"}
    )
    assert free == 200


def test_a_sweep_that_measured_nothing_is_not_reported_as_finished(
    sidecar: str, monkeypatch
) -> None:
    """MEASURED on the box 2026-09-04, and the reason this test exists: a dongle whose
    USB descriptors had stopped answering made rtl_power exit on `No matching devices
    found`, and the route answered `complete: true` with an empty CSV. A success over a
    measurement that never happened is the worst answer available — it cost a container
    log read to notice, which is exactly what the owner cannot do."""
    monkeypatch.setattr(server, "SWEEP_SETTLE_S", 0)
    _sweeping(monkeypatch)  # a process that ends at once and writes nothing

    status, body = _post(
        sidecar,
        "/sweep",
        {"start_hz": 144_000_000, "stop_hz": 148_000_000, "seconds": 1},
    )

    assert status == 502
    assert "measured nothing" in body["detail"]
    # ...and the radio is free regardless, because a failed sweep must not hold it.
    free, _ = _post(
        sidecar, "/listen/start", {"frequency_hz": 99_300_000, "mode": "wbfm"}
    )
    assert free == 200


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
    # Started directly: `/sweep` blocks until rtl_power exits, and what matters here is
    # a second session existing, not how it got there.
    server.TUNER.start(
        146_000_000,
        "fm",
        None,
        purpose=listen.PURPOSE_SURVEY,
        sweep=listen.Sweep.of(144_000_000, 148_000_000, 5_000, 300),
        serial=WHIP,
    )

    _, body = _post(sidecar, "/listen/stop", {"session_id": None})

    assert body["stopped"] is False
    # In serial order, like every other list the sidecar reports.
    assert [h["purpose"] for h in body["holding"]] == ["survey", "aprs"]


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


def _spectrum_row(stamp: str, low: int, step: int, *db: float) -> bytes:
    high = low + len(db) * step
    values = ", ".join(f"{v:.2f}" for v in db)
    return f"{stamp}, {low}, {high}, {step:.2f}, 12, {values}\n".encode()


class _Rows(_FakeProc):
    """rtl_power streaming two intervals of one block, then holding the pipe open.

    Two, because a frame is only known to be complete when the next interval starts or
    the learned width is reached — so one row proves nothing about what a viewer sees.
    """

    # The same readings twice, so the assertion does not depend on WHICH frame the
    # viewer catches: a viewer attaching after both were published is seeded with the
    # newer one, which is right, and is a race a fixed expectation would lose at random.
    LINES: ClassVar[list[bytes]] = [
        _spectrum_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0, -71.0),
        _spectrum_row("2026-09-04, 13:00:01", 144_000_000, 25_000, -70.0, -71.0),
    ]

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.stdout = _Lines(self.LINES)


class _Lines:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self._done = threading.Event()

    def read(self, _n: int = -1) -> bytes:
        return b""

    def __iter__(self) -> Iterator[bytes]:
        yield from self._lines
        # Held open, as a live rtl_power's pipe is: a stream that ended would send the
        # viewer an end-of-stream sentinel and hide whether the rows arrived at all.
        self._done.wait(timeout=5)

    def close(self) -> None:
        self._done.set()


def _start_spectrum(base: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    body = {
        "purpose": "spectrum",
        "start_hz": 144_000_000,
        "stop_hz": 144_200_000,
        "bin_hz": 25_000,
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


def test_a_spectrum_reaches_shortwave_where_a_SURVEY_of_it_does_not(
    sidecar: str, monkeypatch
) -> None:
    """One range, two engines, two answers — which is the point of splitting the guard
    per purpose rather than per frequency. `rtl_power` hardcodes the ADC branch this
    board does not wire and can never see 40 m; the live spectrum does its own FFT and
    sets direct sampling mode 2, so the same numbers are a picture there.

    **Both are refused today, and the two refusals are the assertion.** F8 opened the
    band before F6 replaced the spectrum engine, so what a shortwave spectrum meets is
    no longer the tuner floor — it is the engine saying it is still rtl_power. That
    distinction is the whole wave: the guards now answer per PURPOSE, and the last
    thing in the way is a fact about the tool rather than about the radio. When F6
    lands this becomes a 200 and the second half of the test stands unchanged."""
    status, spectrum = _start_spectrum(sidecar, start_hz=7_000_000, stop_hz=7_300_000)

    assert status == 400
    assert "I/Q engine" in spectrum["detail"], "the band guard, not the engine, refused"
    assert "cannot go below" not in spectrum["detail"]

    _sweeping(monkeypatch)
    status, refused = _post(
        sidecar, "/sweep", {"start_hz": 7_000_000, "stop_hz": 7_300_000}
    )

    assert status == 400
    assert "cannot go below" in refused["detail"]


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
    monkeypatch.setattr(listen.subprocess, "Popen", _Rows)
    assert _start_spectrum(sidecar)[0] == 200

    with urllib.request.urlopen(sidecar + "/listen/spectrum", timeout=10) as resp:
        row = next(
            json.loads(raw) for raw in resp if not json.loads(raw).get("keepalive")
        )

    assert row["start_hz"] == 144_000_000
    assert row["bin_hz"] == 25_000
    assert row["stop_hz"] == row["start_hz"] + row["bins"] * row["bin_hz"]
    assert row["db"] == [-70.0, -71.0]


def test_a_waterfall_is_moved_on_the_session_it_already_holds(sidecar: str) -> None:
    _, started = _start_spectrum(sidecar)

    status, body = _post(
        sidecar,
        "/listen/tune",
        {
            "session_id": started["session_id"],
            "start_hz": 440_000_000,
            "stop_hz": 440_200_000,
            "bin_hz": 25_000,
        },
    )

    assert status == 200
    # Same lease, same id, new band: the radio is never released in between, which is
    # what stops a retune losing the dongle to whatever asks next.
    assert body["session_id"] == started["session_id"]
    assert body["sweep"]["start_hz"] == 440_000_000
    assert body["frequency_hz"] == 440_100_000


def test_a_refused_shortwave_move_costs_a_sentence_and_not_the_radio(
    sidecar: str,
) -> None:
    """The whole tap, end to end: watching 2 m, ask for 40 m, get a 400.

    `_resweep` passes `direct_ok=True` unconditionally, so the range is legal all the
    way down to the engine — which is still `rtl_power` until F6, and cannot serve it.
    What the owner must be left with is the picture they already had: the 400 is the
    cheap half, and the expensive half is that the session, the lease and the old range
    are all exactly where they were. When F6 lands this becomes a 200."""
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
    assert "I/Q engine" in refused["detail"]
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
        {"start_hz": 440_000_000, "stop_hz": 440_200_000, "bin_hz": 25_000},
    )

    assert status == 200
    assert body["purpose"] == "spectrum"
    # ...and the tuner is exactly where it was.
    _, health = _get(sidecar, "/healthz")
    listening = next(s for s in health["sessions"] if s["purpose"] == "listen")
    assert listening["frequency_hz"] == 99_300_000


def test_a_survey_is_never_moved_mid_measurement(sidecar: str, monkeypatch) -> None:
    """A survey's rows are a measurement someone is waiting on, and moving it halfway
    through would hand them a CSV of two different bands with nothing to say so."""
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
    _, started = _post(
        sidecar,
        "/listen/start",
        {
            "purpose": "survey",
            "start_hz": 144_000_000,
            "stop_hz": 144_200_000,
            "bin_hz": 25_000,
            "seconds": 60,
        },
    )

    status, body = _post(
        sidecar,
        "/listen/tune",
        {
            "session_id": started["session_id"],
            "start_hz": 440_000_000,
            "stop_hz": 440_200_000,
            "bin_hz": 25_000,
        },
    )

    assert status == 409
    assert "sweeping the band" in body["detail"]


# --- resetting a radio that has stopped answering -----------------------------------


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
