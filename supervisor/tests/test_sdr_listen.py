"""The sdr sidecar's listening session — the lease, made testable.

`deploy/sdr/` is not an installed package, so it is loaded by path here. What these
cover is the arbitration and the shape the omnibox tuner reads, without a radio: the
pipeline itself needs hardware, so it is faked at the subprocess seam.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load():
    spec = importlib.util.spec_from_file_location(
        "sdr_listen", DEPLOY / "sdr/listen.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_listen"] = module
    spec.loader.exec_module(module)
    return module


listen = _load()


class _FakeProc:
    """A subprocess that is alive until killed and produces no output."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.stdout = _Empty()
        self.stdin = _Sink()
        self.stderr = _Empty()
        self._dead = False

    def poll(self) -> int | None:
        return 1 if self._dead else None

    def kill(self) -> None:
        self._dead = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _Empty:
    def read(self, _n: int = 0) -> bytes:
        return b""


class _Sink:
    def write(self, _b: bytes) -> int:
        return len(_b)

    def flush(self) -> None: ...

    def close(self) -> None: ...


@pytest.fixture
def tuner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
    return listen.Tuner()


def test_a_session_reports_what_the_tuner_ui_reads(tuner) -> None:
    info = tuner.start(99_300_000, "wbfm", None)

    body = info.as_dict()
    assert body["frequency_hz"] == 99_300_000
    assert body["mode"] == "wbfm"
    assert body["session_id"]
    assert body["listeners"] == 0
    assert "elapsed_s" in body and "peak" in body


def test_a_second_listen_is_refused_not_queued(tuner) -> None:
    # One tuner. An unknown wait on a radio someone else holds is worse than a no.
    tuner.start(99_300_000, "wbfm", None)

    with pytest.raises(listen.SdrBusy):
        tuner.start(162_550_000, "fm", None)


def test_stopping_frees_the_radio_for_the_next_caller(tuner) -> None:
    tuner.start(99_300_000, "wbfm", None)

    assert tuner.stop() is True
    assert tuner.current() is None
    tuner.start(162_550_000, "fm", None)  # must not raise


def test_stop_with_a_stale_session_id_is_refused(tuner) -> None:
    # Otherwise a client holding an old id could stop someone else's session.
    tuner.start(99_300_000, "wbfm", None)

    assert tuner.stop("not-the-live-one") is False
    assert tuner.current() is not None


def test_a_dead_pipeline_reads_as_idle(tuner) -> None:
    # rtl_fm dying (unplugged, driver reclaimed it) must report idle rather than a
    # session that can never produce audio — the icon would otherwise stay lit.
    tuner.start(99_300_000, "wbfm", None)
    session = tuner.current()
    assert session is not None
    session._rtl.kill()

    assert tuner.current() is None


def test_retuning_keeps_the_session_id(tuner) -> None:
    # The id is what the omnibox reads. A retune that changed it would flicker the
    # icon and read as the lease having been dropped and re-taken.
    info = tuner.start(99_300_000, "wbfm", None)
    session = tuner.current()
    assert session is not None

    session.tune(107_100_000, "wbfm")

    assert session.id == info.session_id
    assert session.frequency_hz == 107_100_000


def test_an_out_of_range_frequency_is_refused(tuner) -> None:
    with pytest.raises(listen.SdrError):
        tuner.start(12_000_000, "fm", None)  # below the tuner's 24 MHz floor


def test_an_unknown_mode_is_refused(tuner) -> None:
    with pytest.raises(listen.SdrError):
        tuner.start(99_300_000, "ssb-ish", None)


def test_subscribers_are_counted_and_released(tuner) -> None:
    tuner.start(99_300_000, "wbfm", None)
    session = tuner.current()
    assert session is not None

    sub = session.subscribe()
    assert session.info().listeners == 1
    session.unsubscribe(sub)
    assert session.info().listeners == 0


def test_peak_measures_the_loudest_sample() -> None:
    import struct

    quiet = struct.pack("<4h", 10, -20, 5, 0)
    loud = struct.pack("<4h", 10, -32768, 5, 0)

    assert listen._peak(quiet) < 0.01
    assert listen._peak(loud) == pytest.approx(1.0)
    assert listen._peak(b"") == 0.0
