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
    server.TUNER.stop()
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
    """A sweep that has already finished — rtl_power's exit timer fired.

    Real `_FakeProc` stays alive until killed, which models rtl_fm streaming. A sweep
    is the opposite and ENDS on its own, so `/sweep` would wait out its deadline."""

    def poll(self) -> int | None:
        return 0


def _sweeping(monkeypatch, csv: str = "") -> None:
    monkeypatch.setattr(listen.subprocess, "Popen", _DeadProc)
    if csv:
        real_open = open

        def fake_open(path, *a, **k):
            if str(path).startswith("/tmp/sweep-"):
                import io as _io

                return _io.StringIO(csv)
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", fake_open)


def test_a_sweep_holds_the_radio_and_returns_its_rows(
    sidecar: str, monkeypatch
) -> None:
    """The happy path, and the shape the api reduces.

    The sidecar hands back the CSV rtl_power wrote and does NOT draw it: the image work
    needs a plotting stack, `Dockerfile.sdr` forbids the pip install that would bring
    one, and the api already carries Pillow."""
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
    # The hardware tunes 24 MHz-1.766 GHz. Asking below it would sweep nothing and
    # report the band as quiet.
    _sweeping(monkeypatch)

    status, body = _post(
        sidecar, "/sweep", {"start_hz": 1_000_000, "stop_hz": 2_000_000}
    )

    assert status == 400
    assert "range" in body["detail"]


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
    band, while the sweep's low edge is below anything the tuner can reach. A sweep that
    silently started 4 MHz above where it was asked to would report the bottom of the
    range as quiet."""
    _sweeping(monkeypatch)

    status, body = _post(
        sidecar, "/sweep", {"start_hz": 20_000_000, "stop_hz": 70_000_000}
    )

    assert status == 400
    assert "range" in body["detail"]


def test_a_sweep_that_overruns_is_stopped_and_says_so(
    sidecar: str, monkeypatch
) -> None:
    """rtl_power's exit timer is what normally ends a sweep, so this is the case where
    it did NOT: the process is still running when the deadline passes.

    Two things have to happen. The radio is released — otherwise a wedged rtl_power
    holds the tuner until somebody notices — and the result says it is partial, because
    a short window reported as a full one reads as a quiet band."""
    monkeypatch.setattr(server, "SWEEP_SETTLE_S", 0)
    # The default fake stays alive until killed, which IS the overrun case.

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
