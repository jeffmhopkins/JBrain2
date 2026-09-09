"""The sdr sidecar's listening session — the lease, made testable.

`deploy/sdr/` is not an installed package, so it is loaded by path here. What these
cover is the arbitration and the shape the omnibox tuner reads, without a radio: the
pipeline itself needs hardware, so it is faked at the subprocess seam.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import numpy as np
import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load():
    # The sidecar's modules import each other by bare name, which is how they resolve
    # in the image (all copied to one WORKDIR). Loading one by path here needs the same
    # directory importable, or `import packets` inside listen.py fails.
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location(
        "sdr_listen", DEPLOY / "sdr/listen.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_listen"] = module
    spec.loader.exec_module(module)
    return module


listen = _load()
usbdev = importlib.import_module("usbdev")
demod = importlib.import_module("demod")


class _FakeProc:
    """A subprocess that is alive until killed and produces no output."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.stdout = _Empty()
        self.stdin = _Sink()
        self.stderr = _Empty()
        self._dead = False

    def poll(self) -> int | None:
        return 1 if self._dead else None

    def terminate(self) -> None:
        # A real Popen has both, and `_kill` now asks politely first so rtl_fm and
        # rtl_power get to close the USB device rather than being torn off it.
        self._dead = True

    def kill(self) -> None:
        self._dead = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _Empty:
    def read(self, _n: int = 0) -> bytes:
        return b""

    def __iter__(self):
        # rtl_fm's stderr is ITERATED by the log drain, not read: an unread pipe fills
        # at 64 KB and blocks the tuner mid-session. The fake has to be iterable or it
        # only pretends to cover that path.
        return iter(())


class _Sink:
    def __init__(self) -> None:
        self.closed = False
        self.written = 0

    def write(self, _b: bytes) -> int:
        self.written += len(_b)
        return len(_b)

    def flush(self) -> None: ...

    def close(self) -> None:
        self.closed = True


def _instant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start sessions without waiting to see whether they die.

    `_confirm_started` watches a fresh pipeline for `STARTUP_GRACE_S` before believing
    it, which is what turns "the radio could not be opened" from a session that silently
    vanishes into a refusal. A fake process is alive the instant it exists, so that wait
    buys these tests nothing and costs every one of them four tenths of a second."""
    monkeypatch.setattr(listen, "STARTUP_GRACE_S", 0)


@pytest.fixture
def tuner(monkeypatch: pytest.MonkeyPatch):
    _instant(monkeypatch)
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
    assert "elapsed_s" in body and "audio_peak" in body


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
    # 50 kHz is below the ADC itself. 12 MHz used to fail here and no longer does: it
    # is below the TUNER, but the radio reaches it by bypassing one (TestShortwave).
    with pytest.raises(listen.SdrError):
        tuner.start(50_000, "fm", None)


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


# --- live-caption segmenting ----------------------------------------------------
# Whisper is not a streaming model, so captions are chunks of live audio. What must
# hold is that a chunk is worth sending: cut between words, and never over noise.


def _loud(seconds: float) -> bytes:
    """PCM at roughly half scale — a talking channel."""
    return b"\x00\x40" * int(listen.AUDIO_RATE * seconds)


def _quiet(seconds: float) -> bytes:
    """PCM near the floor — an empty channel's hiss."""
    return b"\x00\x01" * int(listen.AUDIO_RATE * seconds)


def _session(tuner):
    tuner.start(frequency_hz=162_550_000, mode="fm", gain=None)
    return tuner.current()


def test_nothing_is_segmented_until_someone_is_captioning(tuner) -> None:
    session = _session(tuner)

    session._accumulate(_loud(1.0), 0.5)

    # Captioning is opt-in and costs a resident model, so a session nobody is
    # captioning must do no extra work and hold no extra audio.
    assert session._seg == []


def test_a_segment_is_cut_on_the_gap_after_speech(tuner) -> None:
    session = _session(tuner)
    sub = session.subscribe_segments()

    session._accumulate(_loud(3.5), 0.5)
    for _ in range(listen.SEGMENT_GAP_CHUNKS):
        session._accumulate(_quiet(0.1), 0.01)

    # Cutting on a quiet gap rather than a clock is what keeps words whole: a boundary
    # through the middle of a word garbles the audio on both sides of it.
    started, pcm = sub.get_nowait()
    assert started > 0
    assert len(pcm) > listen.AUDIO_RATE


def test_a_quiet_segment_is_never_sent(tuner) -> None:
    session = _session(tuner)
    sub = session.subscribe_segments()

    # An empty channel: rtl_fm emits loud hiss into it, and whisper answers noise with
    # fluent invented sentences. The squelch lives at the audio so noise never leaves
    # this process — the alternative is captions that confidently make things up.
    session._accumulate(_quiet(4.0), 0.02)
    for _ in range(listen.SEGMENT_GAP_CHUNKS):
        session._accumulate(_quiet(0.1), 0.01)

    assert sub.empty()


def test_a_segment_is_cut_at_the_ceiling_when_nobody_pauses(tuner) -> None:
    session = _session(tuner)
    sub = session.subscribe_segments()

    # A continuous talker never hands us a gap, so the ceiling has to end the segment
    # or captions would never appear at all.
    session._accumulate(_loud(listen.SEGMENT_MAX_S + 0.5), 0.5)

    assert not sub.empty()


def test_releasing_the_last_captioner_drops_the_held_audio(tuner) -> None:
    session = _session(tuner)
    sub = session.subscribe_segments()
    session._accumulate(_loud(1.0), 0.5)

    session.unsubscribe_segments(sub)

    # Turning captions off must not leave a half-segment in memory to be prepended to
    # whatever the next captioner hears, minutes later and on another frequency.
    assert session._seg == []


def test_a_backed_up_captioner_loses_the_oldest_segment_not_the_newest(tuner) -> None:
    session = _session(tuner)
    sub = session.subscribe_segments()

    # Fill past the queue's depth. `put_nowait` on a full queue discards what you are
    # ADDING, which would leave a captioner grinding through stale audio forever while
    # every fresh segment was dropped — the lag would never close.
    for i in range(listen.SEGMENT_QUEUE + 3):
        session._seg = [_loud(4.0)]
        session._seg_peak_seen = 0.5
        session._seg_started = float(i)
        session._cut()

    held = []
    while not sub.empty():
        held.append(sub.get_nowait()[0])

    assert len(held) == listen.SEGMENT_QUEUE
    # The NEWEST cut must have survived; the oldest are the ones given up.
    assert held[-1] == float(listen.SEGMENT_QUEUE + 2)


# --- the lease's purpose ------------------------------------------------------------
# APRS logging is not a background daemon but a SESSION holding the same one-tuner
# lease with a different job. What makes that usable rather than merely correct is that
# a refusal NAMES the holder — see deploy/sdr/listen.py for why.


def test_a_session_says_what_it_is_holding_the_radio_for(tuner) -> None:
    info = tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    assert info.purpose == listen.PURPOSE_APRS
    assert info.as_dict()["purpose"] == listen.PURPOSE_APRS


def test_listening_is_what_a_caller_that_says_nothing_means(tuner) -> None:
    # Every caller predating purposes meant listening, so the default keeps them
    # byte-identical rather than making them declare something they never knew about.
    info = tuner.start(99_300_000, "wbfm", None)

    assert info.purpose == listen.PURPOSE_LISTEN


def test_logging_refuses_a_listener_by_name(tuner) -> None:
    tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    with pytest.raises(listen.SdrBusy) as busy:
        tuner.start(162_550_000, "fm", None)

    # The owner has to learn WHICH switch to throw. "Busy" does not tell them.
    assert "logging APRS" in str(busy.value)


def test_listening_refuses_a_logger_by_name(tuner) -> None:
    tuner.start(162_550_000, "fm", None)

    with pytest.raises(listen.SdrBusy) as busy:
        tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    assert "listening" in str(busy.value)


def test_an_unknown_purpose_is_refused(tuner) -> None:
    # A typo'd purpose must not silently become a listening session that produces
    # audio nobody asked for, on a radio somebody wanted for packets.
    with pytest.raises(listen.SdrError):
        tuner.start(144_390_000, "fm", None, purpose="transmit")


def test_releasing_a_logging_session_frees_the_radio(tuner) -> None:
    tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    assert tuner.stop() is True
    assert tuner.start(99_300_000, "wbfm", None).purpose == listen.PURPOSE_LISTEN


def test_a_session_bounds_its_own_purpose(tuner) -> None:
    # Not only Tuner.start. This module's own rule is that a bound living in the caller
    # is not a bound once there is a second caller, and `mode` is validated here for
    # exactly that reason — `purpose` has to be too.
    with pytest.raises(listen.SdrError):
        listen.Session(144_390_000, "fm", None, "sweep")


def test_a_purpose_with_no_phrase_cannot_turn_a_refusal_into_a_crash(
    tuner, monkeypatch
) -> None:
    # The label map is read while HOLDING the tuner lock, on the contention path. A
    # purpose added without a phrase used to raise KeyError there — a 500 with a
    # traceback where the caller needed a 409 telling them the radio was busy.
    monkeypatch.setattr(listen, "PURPOSES", (*listen.PURPOSES, "sweep"))
    tuner.start(144_390_000, "fm", None, purpose="sweep")

    with pytest.raises(listen.SdrBusy) as busy:
        tuner.start(99_300_000, "wbfm", None)

    assert "in use" in str(busy.value)


def test_every_purpose_has_a_phrase_to_explain_itself(tuner) -> None:
    # The runtime degrades safely (above), but a purpose shipped without a phrase would
    # tell the owner "in use" and nothing more — the generic P0 exists to delete.
    assert set(listen.PURPOSES) == set(listen.PURPOSE_LABEL)


# --- the packet pipeline ------------------------------------------------------------
# An independent review mutation-tested this wave and found 13 of 26 mutants surviving,
# including the one that defines it: an `aprs` session running the AUDIO pipeline. These
# are the assertions that kill them.


def _argv_of(session, which: int) -> list[str]:
    """The argv of one of the session's two processes, as launched."""
    return session._launched[which]


@pytest.fixture
def recording_tuner(monkeypatch: pytest.MonkeyPatch):
    """A tuner whose sessions remember the argv they launched."""
    launched: list[list[str]] = []

    class _Recorder(_FakeProc):
        def __init__(self, argv, *a, **k):
            launched.append(list(argv))
            super().__init__(argv, *a, **k)

    _instant(monkeypatch)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _Recorder)
    tuner = listen.Tuner()
    tuner._launched_argv = launched  # type: ignore[attr-defined]
    return tuner


def test_a_logging_session_runs_direwolf_and_never_the_encoder(recording_tuner) -> None:
    # THE defining behaviour of the wave, and it was asserted nowhere: a mutant that ran
    # ffmpeg for an APRS session passed the whole suite.
    recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    argvs = recording_tuner._launched_argv
    assert any(a[0] == "rtl_fm" for a in argvs)
    assert any(a[0] == "direwolf" for a in argvs)
    assert not any(a[0] == "ffmpeg" for a in argvs), (
        "a packet session has no audio to encode"
    )


def test_a_listening_session_runs_the_encoder_and_never_direwolf(
    recording_tuner,
) -> None:
    recording_tuner.start(99_300_000, "wbfm", None)

    argvs = recording_tuner._launched_argv
    assert any(a[0] == "ffmpeg" for a in argvs)
    assert not any(a[0] == "direwolf" for a in argvs)


def test_direwolf_is_pointed_at_the_tuners_sample_rate(recording_tuner) -> None:
    recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    argv = next(a for a in recording_tuner._launched_argv if a[0] == "direwolf")
    # Feeding it the wrong rate decodes nothing while reporting healthy.
    assert argv[argv.index("-r") + 1] == str(listen.AUDIO_RATE)
    assert argv[argv.index("-B") + 1] == "1200"


def test_the_generated_direwolf_config_carries_this_sessions_port(
    recording_tuner,
) -> None:
    info = recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
    session = recording_tuner.current()
    assert session is not None

    conf = Path(f"/tmp/direwolf-{session.id}.conf").read_text()

    # A shared or hardcoded port means a second session decodes into the first's socket.
    assert f"KISSPORT {session.kiss_port}" in conf
    assert info.purpose == listen.PURPOSE_APRS


def test_two_sessions_do_not_share_a_config_file(recording_tuner) -> None:
    first = recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
    recording_tuner.stop()
    second = recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

    assert first.session_id != second.session_id


def test_the_config_is_cleaned_up_when_the_session_ends(recording_tuner) -> None:
    recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
    session = recording_tuner.current()
    assert session is not None
    path = Path(f"/tmp/direwolf-{session.id}.conf")
    assert path.exists()

    recording_tuner.stop()

    # One file per lease taken would otherwise accumulate for the life of the container.
    assert not path.exists()


def test_a_missing_direwolf_refuses_the_session_rather_than_pretending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(
        listen.shutil, "which", lambda n: None if n == "direwolf" else "/x"
    )
    tuner = listen.Tuner()

    with pytest.raises(listen.SdrError):
        tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)


def test_a_mode_that_cannot_carry_packet_is_refused(recording_tuner) -> None:
    # 1200-baud AFSK arrives on narrowband FM. `usb` or `wbfm` would start a radio that
    # reports a healthy logging session and can never decode a thing.
    for mode in ("usb", "wbfm", "am"):
        with pytest.raises(listen.SdrError):
            recording_tuner.start(144_390_000, mode, None, purpose=listen.PURPOSE_APRS)


def test_a_packet_reader_is_released_when_the_session_stops(recording_tuner) -> None:
    recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
    session = recording_tuner.current()
    assert session is not None
    sub = session.subscribe_packets()

    recording_tuner.stop()

    # Without the end-of-stream sentinel every reader blocked for ever, still emitting
    # keep-alives the api reads as "logging is healthy", pinning a dead session apiece.
    assert sub.get(timeout=2) is None


def test_a_backed_up_reader_loses_the_OLDEST_packet_not_the_newest(
    recording_tuner,
) -> None:
    recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
    session = recording_tuner.current()
    assert session is not None
    sub = session.subscribe_packets()

    sent = []
    for i in range(listen.PACKET_QUEUE + 10):
        packet = _packet(f"INFO{i}")
        sent.append(packet.info)
        session._publish_packet(packet)

    got = []
    while not sub.empty():
        item = sub.get_nowait()
        if item is not None:
            got.append(item.info)

    # Dropping the NEWEST — catching Full and never retrying the put — lost every other
    # frame while a reader lagged. This is a log: late still counts, missing does not.
    assert got[-1] == sent[-1], "the most recent packet must survive"
    assert got == sent[-len(got) :], (
        "the surviving packets must be the most recent, in order"
    )


def test_a_session_whose_decoder_died_is_not_alive(recording_tuner) -> None:
    recording_tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
    session = recording_tuner.current()
    assert session is not None

    session._enc.kill()  # direwolf crashed, failed to bind, or was killed

    # It used to keep reporting a healthy `aprs` lease while decoding nothing, and the
    # owner's only clue would have been a log that stopped growing.
    assert session.alive is False
    assert recording_tuner.current() is None


def _packet(info: str):
    return listen.packets.Packet(
        source="KE8XYZ-9", destination="APDW17", path=[], info=info, raw="00"
    )


def test_a_frame_on_the_KISS_socket_reaches_a_subscriber(monkeypatch) -> None:
    """The real reader path: socket -> KissStream -> subscriber.

    Every other packet test injects a `Packet` at the fan-out, which leaves the socket,
    the deframer and the reader thread untested — a mutant that never started the reader
    survived the whole suite. This one binds a fake direwolf on the port the session
    will dial and pushes REAL captured KISS bytes at it.
    """
    import socket as _socket
    import threading as _threading

    # One port, so the fixture can bind before the session picks it.
    monkeypatch.setattr(listen, "KISS_PORT_SPAN", 1)
    monkeypatch.setattr(listen, "KISS_PORT_BASE", 8231)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)

    fixture = Path(__file__).parent / "fixtures/aprs_kiss_frames.hex"
    frame = bytes.fromhex(
        next(
            ln
            for ln in fixture.read_text().splitlines()
            if ln and not ln.startswith("#")
        )
    )
    server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 8231))
    server.listen(1)

    # The reader connects the moment the session starts, so without this gate the
    # frame can be fanned out BEFORE `subscribe_packets` attaches — and a packet with
    # no subscribers is dropped, by design. That race made this test fail about three
    # runs in four while testing nothing about the code.
    subscribed = _threading.Event()

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            assert subscribed.wait(timeout=8)
            conn.sendall(
                bytes([listen.packets.FEND]) + frame + bytes([listen.packets.FEND])
            )
            time.sleep(1.0)

    _threading.Thread(target=serve, daemon=True).start()
    tuner = listen.Tuner()
    try:
        tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
        session = tuner.current()
        assert session is not None
        sub = session.subscribe_packets()
        subscribed.set()
        got = sub.get(timeout=8)
    finally:
        tuner.stop()
        server.close()

    assert got is not None
    assert (got.source, got.info) == ("KE8XYZ-9", "GATE 7K2M9")


class _Piped(_FakeProc):
    """A fake process whose stdout yields the lines direwolf actually wrote.

    Subclasses `_FakeProc` so it is still killable — the session's own teardown reaps
    whatever is in `_enc`."""

    def __init__(self, lines: list[bytes]) -> None:
        super().__init__()
        self.stdout = iter(lines)


class TestAudioLevelPairing:
    """Pairing direwolf's audio level to the frame it belongs to.

    The level arrives on stdout and the frame over a KISS socket, so this is a
    correlation between two streams, and the failure it has to rule out is a level from
    an EARLIER transmission attaching to a later one. A plausible wrong number is worse
    than a blank here: nothing else on screen would contradict it."""

    @pytest.fixture
    def session(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """A real Session over the faked subprocess seam, STOPPED afterwards.

        The teardown is not tidiness. Constructing one starts a reader thread that dials
        the KISS port in a retry loop, so a leaked session goes on to connect to the
        fake direwolf that `test_a_frame_on_the_KISS_socket_reaches_a_subscriber` binds
        — and consumes the single frame that test is waiting for."""
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        session = listen.Session(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)
        yield session
        session.stop()

    def test_the_level_direwolf_announced_lands_on_the_next_frame(
        self, session
    ) -> None:
        session._level = (time.monotonic(), 50)

        assert session._take_audio_level() == 50

    def test_a_level_is_claimed_once_and_not_by_the_frame_after(self, session) -> None:
        """The off-by-one that would put one station's signal on the next station's row,
        every row, for as long as the channel stayed busy."""
        session._level = (time.monotonic(), 50)

        assert session._take_audio_level() == 50
        assert session._take_audio_level() is None

    def test_a_stale_level_is_dropped_rather_than_attached(self, session) -> None:
        # A frame whose own level line never arrived must not inherit the last one that
        # did — that is how a strong station's number ends up on a marginal station.
        session._level = (time.monotonic() - listen._LEVEL_WINDOW_S - 0.1, 50)

        assert session._take_audio_level() is None

    def test_a_frame_with_no_level_reports_unknown(self, session) -> None:
        assert session._take_audio_level() is None

    def test_the_decoder_log_still_reaches_the_container_log(self, session) -> None:
        """Parsing the level must not turn the drain into a filter. An unread pipe
        blocks direwolf at 64 KB and stops it decoding permanently, so every line still
        has to be read AND printed — the reason this thread exists at all."""
        printed: list[str] = []
        session._enc = _Piped(
            [
                b"Dire Wolf version 1.7\n",
                b"N0CALL-9 audio level = 50(14/14)    _||||||__\n",
                b"[0] N0CALL-9>APDW17,WIDE1-1:!2837.27N\n",
            ]
        )

        with mock.patch("builtins.print", lambda *a, **k: printed.append(str(a[0]))):
            session._drain_decoder_log()

        assert session._level is not None and session._level[1] == 50
        # Every line, not just the ones that parsed.
        assert len(printed) == 3

    def test_the_heard_line_is_not_suppressed_by_the_quiet_flag(self, session) -> None:
        """`-q h` means exactly "suppress the heard line with the audio level". Shipping
        `hd` is what made signal level look unrecoverable for a whole wave."""
        quiet = session._direwolf_cmd()
        flag = quiet[quiet.index("-q") + 1]

        assert "h" not in flag
        assert "d" in flag


class TestAddressingOneRadioOfSeveral:
    """Both pipelines must open the radio they were TOLD to, not the first one.

    MEASURED 2026-09-03: two NESDR SMArt v5s attached (09022796 on bus 1-1, 77192819 on
    bus 3-4). Neither `rtl_fm` nor `rtl_power` was passed `-d`, so both opened whichever
    librtlsdr enumerated first — a property of USB bus order, not of anything the owner
    chose. With one radio on a desk whip and one on a long wire, that is how APRS moves
    to the wrong antenna on a re-plug with no symptom but worse reception.
    """

    def _sweep(self):
        return listen.Sweep.of(144_000_000, 148_000_000, 5_000, 300)

    def _session(self, monkeypatch, **kwargs):
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        return listen.Session(144_390_000, "fm", None, **kwargs)

    def test_listening_opens_the_named_radio(self, monkeypatch) -> None:
        session = self._session(monkeypatch, serial="77192819")
        try:
            cmd = session._rtl_cmd()
        finally:
            session.stop()

        assert cmd[cmd.index("-d") + 1] == "77192819"

    def test_the_argument_is_what_librtlsdr_can_actually_match(
        self, monkeypatch
    ) -> None:
        """The form matters, and asserting a formatted string does not check it.

        rtl_fm and rtl_power pass `-d` straight to librtlsdr's `verbose_device_search`,
        which tries a raw index, an exact serial, a serial prefix and a serial suffix —
        and has NO key=value form. `serial=09022796` is SoapySDR syntax; here it matches
        nothing and the tool exits(1) before opening the device, while `Tuner.start` has
        already returned a lease that looks live. An earlier cut of this shipped exactly
        that, and the test that was supposed to catch it only proved an f-string ran."""
        session = self._session(monkeypatch, serial="09022796")
        try:
            arg = session._rtl_cmd()[session._rtl_cmd().index("-d") + 1]
        finally:
            session.stop()

        assert "=" not in arg
        assert arg == "09022796"

    def test_naming_no_radio_stays_byte_identical_to_before(self, monkeypatch) -> None:
        """A one-dongle box must not change behaviour. `-d` absent means librtlsdr's
        own choice, which is exactly right when there is only one thing to choose."""
        listening = self._session(monkeypatch)
        try:
            assert "-d" not in listening._rtl_cmd()
        finally:
            listening.stop()

    def test_a_serial_that_is_not_one_is_refused_rather_than_passed_along(self) -> None:
        """`serial` is the only field in a start body that becomes a subprocess argv
        token, and it was the only one taken raw off the JSON while its neighbours were
        each cast and validated. A dict here became the argv token `serial={'a': 1}`
        and a dict in the `/healthz` payload typed `str | None`."""
        for bad in ({"a": 1}, "has space", "semi;colon", "x" * 65, 12345):
            with pytest.raises(listen.SdrError):
                listen.validate_serial(bad)

    def test_naming_nothing_stays_legal(self) -> None:
        # The one-dongle case, which must not become an error.
        assert listen.validate_serial(None) is None
        assert listen.validate_serial("") is None
        assert listen.validate_serial("09022796") == "09022796"

    def test_the_lease_says_which_radio_it_holds(self, tuner) -> None:
        """The omnibox and /health read this. "Something is using the radio" is not an
        answer on a box with two of them."""
        info = tuner.start(144_390_000, "fm", None, serial="09022796")

        assert info.serial == "09022796"
        assert info.as_dict()["serial"] == "09022796"


class TestSweepBounds:
    """The caps, enforced where the sweep is BUILT rather than at each caller.

    An agent will ask for an hour, because nothing in its training says the radio is
    scarce — and a survey holds the tuner for every second of it."""

    def test_a_long_request_is_clamped_not_refused(self) -> None:
        # Clamped rather than refused: the caller gets a shorter sweep and a result,
        # instead of an error and nothing.
        assert (
            listen.Sweep.of(144e6, 148e6, 5000, 99_999).seconds
            == listen.MAX_SWEEP_SECONDS
        )

    def test_a_bin_finer_than_the_hardware_can_mean_is_clamped(self) -> None:
        assert listen.Sweep.of(144e6, 148e6, 1, 60).bin_hz == listen.MIN_SWEEP_BIN_HZ

    def test_a_span_wider_than_the_cap_is_refused(self) -> None:
        # Refused rather than clamped: silently sweeping a different range than asked
        # for would report the wrong band as quiet.
        with pytest.raises(listen.SdrError):
            listen.Sweep.of(24e6, 1_700e6, 25_000, 60)

    def test_a_backwards_range_is_read_the_right_way_round(self) -> None:
        swept = listen.Sweep.of(148_000_000, 144_000_000, 5_000, 60)

        assert (swept.start_hz, swept.stop_hz) == (144_000_000, 148_000_000)

    def test_a_single_frequency_is_not_a_sweep(self) -> None:
        with pytest.raises(listen.SdrError):
            listen.Sweep.of(144_390_000, 144_390_000, 5_000, 60)

    def test_the_centre_is_what_the_omnibox_shows(self) -> None:
        # A range has no one frequency, and showing the low edge would read as a tuner
        # parked somewhere it is not.
        assert listen.Sweep.of(144e6, 148e6, 5000, 60).centre_hz == 146_000_000


class TestOneSessionPerRadio:
    """Two dongles means two radios, and the box should use both at once.

    MEASURED 2026-09-04: with 09022796 on a desk whip and 77192819 on a long wire,
    turning APRS logging on took the only session slot — so opening the tuner got a 409
    naming a radio it was not asking for, and the second dongle sat idle. The slot was
    the box's, not the radio's, which is only the same thing when there is one.
    """

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        tuner = listen.Tuner()
        yield tuner
        tuner.stop()

    def test_two_radios_run_at_once(self, tuner) -> None:
        logging_ = tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="77192819"
        )
        listening = tuner.start(146_520_000, "fm", None, serial="09022796")

        assert {s.serial for s in tuner.sessions()} == {"77192819", "09022796"}
        assert logging_.session_id != listening.session_id

    def test_the_same_radio_twice_is_still_refused(self, tuner) -> None:
        tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="77192819"
        )

        with pytest.raises(listen.SdrBusy) as refused:
            tuner.start(146_520_000, "fm", None, serial="77192819")

        # The message names the radio, because on a two-dongle box "the radio is busy"
        # is a sentence the owner cannot act on.
        assert "77192819" in str(refused.value)
        assert "logging APRS" in str(refused.value)

    def test_an_unnamed_session_blocks_every_radio(self, tuner) -> None:
        """The load-bearing case. A session started with no `-d` opens whatever
        librtlsdr enumerates first, so nothing can prove it is NOT on the radio the
        next caller wants. Letting the named one through would put two processes on one
        dongle, which fails as garbled audio rather than as an error."""
        tuner.start(146_520_000, "fm", None)

        with pytest.raises(listen.SdrBusy):
            tuner.start(144_390_000, "fm", None, serial="77192819")

    def test_an_unnamed_request_is_blocked_by_any_named_session(self, tuner) -> None:
        """And the other direction, which is the same argument read backwards."""
        tuner.start(144_390_000, "fm", None, serial="77192819")

        with pytest.raises(listen.SdrBusy):
            tuner.start(146_520_000, "fm", None)

    def test_a_one_dongle_box_still_allows_exactly_one_session(self, tuner) -> None:
        """No serial anywhere is what every box did before this existed, and it must
        keep behaving identically."""
        tuner.start(146_520_000, "fm", None)

        with pytest.raises(listen.SdrBusy):
            tuner.start(162_400_000, "fm", None)

    def test_releasing_one_radio_leaves_the_other_running(self, tuner) -> None:
        keep = tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="77192819"
        )
        drop = tuner.start(146_520_000, "fm", None, serial="09022796")

        assert tuner.stop(drop.session_id) is True

        assert [s.id for s in tuner.sessions()] == [keep.session_id]

    def test_stopping_with_no_id_stops_EVERY_radio(self, tuner) -> None:
        """It used to mean "the one". With several, an arbitrary pick would be a
        control that does something different each time it is pressed."""
        tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="77192819"
        )
        tuner.start(146_520_000, "fm", None, serial="09022796")

        assert tuner.stop() is True
        assert tuner.sessions() == []

    def test_for_purpose_finds_the_radio_doing_that_job(self, tuner) -> None:
        """What the packets and captions routes need: "the session" is no longer one
        thing, so a purpose-specific stream has to ask for its own."""
        aprs = tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="77192819"
        )
        tuner.start(146_520_000, "fm", None, serial="09022796")

        assert tuner.for_purpose(listen.PURPOSE_APRS).id == aprs.session_id
        assert tuner.for_purpose(listen.PURPOSE_SPECTRUM) is None

    def test_current_means_the_LISTENING_session(self, tuner) -> None:
        """`current()` with no serial is how this process says "the tuner", so a radio
        holding a service must not answer it.

        The listening session is on the HIGHER serial deliberately. With it on the lower
        one, "listening first" and plain serial order agree, and an earlier cut of this
        test passed with the purpose filter deleted — proving only that sorting
        happened.

        This is no longer a presentation ranking: B7 moved "which session an owner SEES"
        to the api (`health.shown`), which holds every session anyway. What is left here
        is a deterministic answer for routes that mean the tuner."""
        tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="09022796"
        )
        listening = tuner.start(146_520_000, "fm", None, serial="77192819")

        assert tuner.current().id == listening.session_id
        # ...and naming a radio asks about that radio, not about the box.
        assert tuner.current("09022796").purpose == listen.PURPOSE_APRS
        assert tuner.current("nosuchserial") is None

    def test_current_breaks_a_TIE_by_serial(self, tuner) -> None:
        """Serial only breaks a tie — two reads that changed nothing must not disagree
        about which session is "the tuner"."""
        tuner.start(146_520_000, "fm", None, serial="77192819")
        tuner.start(99_300_000, "wbfm", None, serial="09022796")

        assert tuner.current().serial == "09022796"

    def test_a_released_session_cannot_relaunch_itself(self, tuner) -> None:
        """The retune race. `/listen/tune` resolves the Session under the tuner's lock
        and calls `tune` outside it, so a stop landing in between used to spawn a fresh
        rtl_fm for a session no longer in the registry — invisible to `blocking_key`,
        never reaped, holding the dongle until the container restarts, after which the
        next caller for that serial is let through and two processes fight over one
        radio."""
        info = tuner.start(146_520_000, "fm", None, serial="77192819")
        session = tuner.find(info.session_id)
        tuner.stop(info.session_id)

        with pytest.raises(listen.SessionGone):
            session.tune(146_940_000)

        # ...and the radio really is free, rather than held by a process nothing tracks.
        assert tuner.start(146_940_000, "fm", None, serial="77192819") is not None

    def test_sessions_are_listed_in_a_stable_order(self, tuner) -> None:
        """Sorted by serial rather than by whoever started first, so a list the owner
        reads twice does not reorder itself."""
        for serial in ("77192819", "09022796", "40000123"):
            tuner.start(146_520_000, "fm", None, serial=serial)

        assert [s.serial for s in tuner.sessions()] == [
            "09022796",
            "40000123",
            "77192819",
        ]

    def test_a_dead_session_stops_holding_its_radio(self, tuner) -> None:
        """Unplugged, or the driver reclaimed it. A dead session must not hold a radio
        against the next caller — and with per-radio keys the reap has to drop the
        right key rather than the only one."""
        held = tuner.start(144_390_000, "fm", None, serial="77192819")
        keep = tuner.start(146_520_000, "fm", None, serial="09022796")
        tuner.find(held.session_id)._rtl.kill()

        fresh = tuner.start(145_000_000, "fm", None, serial="77192819")

        assert {s.id for s in tuner.sessions()} == {keep.session_id, fresh.session_id}


class TestCaptureAndSessionsShareOneRegistry:
    """A one-shot capture holds a radio without being a session, and the two used to be
    tracked separately — a global `threading.Lock` beside the tuner's single slot.

    That seam leaked in BOTH directions: the lock was per box, so a capture on the long
    wire refused while something recorded off the desk whip; and `Tuner.start` never
    consulted it at all, so a listening session could open a dongle mid-recording. One
    registry under one lock is what makes both true, and makes them true atomically —
    checking the tuner and then taking a separate lock leaves a window between.
    """

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        tuner = listen.Tuner()
        yield tuner
        tuner.stop()

    def test_a_reservation_blocks_a_session_on_that_radio(self, tuner) -> None:
        """The direction that was simply missing before."""
        assert tuner.reserve("77192819", "recording") is None

        with pytest.raises(listen.SdrBusy) as refused:
            tuner.start(144_390_000, "fm", None, serial="77192819")

        assert "recording" in str(refused.value)

    def test_a_session_blocks_a_reservation_on_that_radio(self, tuner) -> None:
        tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="77192819"
        )

        busy = tuner.reserve("77192819", "recording")

        assert isinstance(busy, listen.SdrBusy)
        assert "logging APRS" in str(busy)

    def test_another_radio_is_free_during_a_capture(self, tuner) -> None:
        """The refusal with no physical cause: one radio recording is not a reason to
        refuse the other, whether the other caller wants to record or to listen."""
        tuner.reserve("77192819", "recording")

        assert tuner.start(146_520_000, "fm", None, serial="09022796") is not None
        assert tuner.reserve("40000123", "recording") is None

    def test_an_unnamed_capture_holds_everything(self, tuner) -> None:
        assert tuner.reserve(None, "recording") is None

        assert tuner.reserve("77192819", "recording") is not None
        with pytest.raises(listen.SdrBusy):
            tuner.start(146_520_000, "fm", None, serial="09022796")

    def test_releasing_the_reservation_frees_the_radio(self, tuner) -> None:
        tuner.reserve("77192819", "recording")
        tuner.unreserve("77192819")

        assert tuner.start(144_390_000, "fm", None, serial="77192819") is not None

    def test_a_stranded_reservation_lapses_instead_of_holding_a_radio_for_ever(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session is reaped by asking its process whether it is alive. A reservation
        has no process to ask — it is a claim staked by a `capture` that PROMISES to
        release it in a `finally`, and a promise is not a reap: a signal between taking
        the claim and entering that `try`, or a worker killed outright, would strand the
        key for the life of the process. `blocking_key` then refuses that radio for
        ever, and `/healthz` reports busy with nothing running."""
        assert tuner.reserve("77192819", "recording") is None
        assert tuner.reserve("77192819", "recording") is not None  # still held
        assert tuner.reserved() is True

        later = time.monotonic() + listen.RESERVATION_TTL_S + 1
        monkeypatch.setattr(listen.time, "monotonic", lambda: later)

        assert tuner.reserve("77192819", "recording") is None
        assert tuner.start(146_520_000, "fm", None, serial="09022796") is not None

    def test_a_reservation_does_not_lapse_while_a_capture_could_still_be_running(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline is longer than any capture the sidecar will run, so an expiry is
        always a leak rather than a slow caller. A TTL below that would hand the radio
        away underneath a recording that is still going."""
        tuner.reserve("77192819", "recording")

        soon = time.monotonic() + listen.RESERVATION_TTL_S - 1
        monkeypatch.setattr(listen.time, "monotonic", lambda: soon)

        assert tuner.reserve("77192819", "recording") is not None

    def test_stopping_every_session_does_NOT_free_a_capture_s_radio(
        self, tuner
    ) -> None:
        """Stated in prose by the sidecar's test fixture and unpinned until now.

        An in-flight `rtl_fm` still has the device open; releasing its key because a
        person pressed Release would let the next session open a dongle a running
        process holds — the "garbled audio rather than an error" outcome. The capture
        frees it in its own `finally`, seconds later."""
        tuner.start(146_520_000, "fm", None, serial="09022796")
        tuner.reserve("77192819", "recording")

        assert tuner.stop() is True

        assert tuner.sessions() == []
        assert tuner.reserved() is True
        with pytest.raises(listen.SdrBusy):
            tuner.start(144_390_000, "fm", None, serial="77192819")

    def test_reserved_is_what_healthz_calls_busy(self, tuner) -> None:
        """`/healthz`'s `busy` has always meant "a capture is running" — not "a session
        exists", which `listening` already answers."""
        assert tuner.reserved() is False
        tuner.start(146_520_000, "fm", None, serial="09022796")
        assert tuner.reserved() is False

        tuner.reserve("77192819", "recording")
        assert tuner.reserved() is True


class TestBlockingKey:
    """The rule itself, stated once because two things hold radios.

    A capture and a session both open a device, and a rule they could disagree about is
    a rule that eventually puts both of them on one dongle.
    """

    def test_the_same_radio_blocks(self) -> None:
        assert listen.blocking_key({"a", "b"}, "a") == "a"

    def test_a_free_radio_does_not(self) -> None:
        assert listen.blocking_key({"a"}, "b") is None

    def test_an_unnamed_holder_blocks_a_named_request(self) -> None:
        assert listen.blocking_key({listen.ANY_DEVICE}, "b") == listen.ANY_DEVICE

    def test_a_named_holder_blocks_an_unnamed_request(self) -> None:
        assert listen.blocking_key({"b"}, listen.ANY_DEVICE) == "b"

    def test_nothing_held_blocks_nothing(self) -> None:
        assert listen.blocking_key(set(), listen.ANY_DEVICE) is None
        assert listen.blocking_key(set(), "a") is None

    def test_which_holder_is_named_does_not_depend_on_insertion_order(self) -> None:
        """Enough entries that a set's iteration order is not sorted by luck: with two
        it can be, and a test that passes by luck is one that stops catching this."""
        held = {"h", "c", "f", "a", "d", "g", "b", "e"}

        assert listen.blocking_key(held, listen.ANY_DEVICE) == "a"


class TestHowASignalIsDemodulated:
    """`demod_args` — the flags that decide what a station SOUNDS like.

    Built in two places until now (a live session and the one-shot capture), which is
    how `-d serial=` shipped wrong in both at once. These pin the three settings that
    research showed were measurably wrong, each so that reverting it fails here.
    """

    def _args(
        self, mode: str, gain: str | None = None, hz: int = 146_940_000
    ) -> list[str]:
        return listen.demod_args(mode, gain, hz)

    def test_wide_FM_gets_enough_bandwidth_for_the_station_it_is_tuned_to(self) -> None:
        """A broadcast station deviates ±75 kHz and carries audio to 53 kHz, so Carson
        puts it near 190 kHz wide. rtl_fm's documented 171 kHz gives the demodulator
        ±85.5 kHz and clips the station's OWN sidebands — distortion introduced in the
        signal path, where nothing downstream can undo it."""
        args = self._args("wbfm")

        assert args[args.index("-s") + 1] == "192000"
        # Carson, for ±75 kHz deviation plus 53 kHz of audio.
        assert int(args[args.index("-s") + 1]) >= 190_000

    def test_the_wide_FM_rates_divide_EXACTLY(self) -> None:
        """rtl_fm resamples with `low_pass_real`, whose factor is the INTEGER
        `rate_in / rate_out`. 171000/16000 = 10.6875 truncates to 10: the output runs
        6.9% fast and each sample averages a count that alternates between 10 and 11 —
        a per-sample gain wobble on every wide-FM capture ever taken here."""
        args = self._args("wbfm")
        rate_in = int(args[args.index("-s") + 1])
        rate_out = int(args[args.index("-r") + 1])

        assert rate_in % rate_out == 0, f"{rate_in}/{rate_out} does not divide exactly"

    def test_AM_strips_the_carrier_pedestal(self) -> None:
        """An AM carrier is a DC offset after envelope detection. Left in, it eats
        headroom and reaches whisper as a bias on every sample."""
        assert "-E" in self._args("am") and "dc" in self._args("am")

    def test_FM_does_NOT_get_the_DC_filter(self) -> None:
        """A discriminator's output is already centred, so this would be a filter with
        nothing to remove — and `-E dc` is not free: it is a one-pole high-pass that
        would eat the low end of voice."""
        for mode in ("fm", "nfm", "wbfm"):
            assert "dc" not in self._args(mode)

    def test_every_mode_gets_the_real_decimation_filter(self) -> None:
        """rtl_fm's default is a BOXCAR — an unweighted sum whose first sidelobe is only
        ~13 dB down — so a strong neighbour a few channels away leaks into whatever you
        are tuned to. `-F 9` is cascaded half-bands at the SAME bandwidth, which makes
        it the one filter improvement that costs no selectivity to buy."""
        for mode in sorted(listen.MODES):
            args = self._args(mode)
            assert args[args.index("-F") + 1] == "9", mode

    def test_gain_is_passed_through_and_omitting_it_means_AGC(self) -> None:
        """No `-g` is not "default gain" — it is `rtlsdr_set_tuner_gain_mode(0)`, a
        different operating point where the tuner runs its own loop."""
        assert "-g" not in self._args("fm")
        assert self._args("fm", "36.4")[-2:] == ["-g", "36.4"]

    def test_the_live_path_and_a_capture_ask_for_the_SAME_demodulation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason this function exists. A capture is meant to be a sample of what
        a session would hear, and two builders that can drift apart make that false."""
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        session = listen.Session(92_300_000, "wbfm", None)
        try:
            live = session._rtl_cmd()
        finally:
            session.stop()

        shared = listen.demod_args("wbfm", None, 92_300_000)

        # The shared flags appear in the live command CONTIGUOUSLY and in order, so this
        # fails if the session ever starts building its own variant beside them.
        joined = "\x00".join(live)
        assert "\x00".join(shared) in joined


class TestShortwave:
    """Below 24 MHz the tuner is BYPASSED, not merely tuned lower.

    The NESDR SMArt v5 routes HF through an on-board diplexer into the RTL2832U's Q
    branch — printed in Nooelec's own datasheet block diagram, no hardware mod. Three
    things follow, and each is a way the software could lie about the hardware.
    """

    def test_it_selects_the_branch_this_board_actually_wires(self) -> None:
        """`direct` is the I branch and `direct2` is Q. This board wires Q, so the wrong
        one produces silence from hardware that looks entirely healthy — no error, no
        log line, just a dead band."""
        args = listen.demod_args("am", None, 10_000_000)

        assert args[args.index("-E") + 1] == "direct2"

    def test_it_is_not_used_above_the_tuner(self) -> None:
        assert "direct2" not in listen.demod_args("fm", None, 146_940_000)

    def test_gain_is_dropped_below_the_tuner(self) -> None:
        """`rtlsdr_set_direct_sampling` calls the tuner's own exit(), so the R820T2 is
        powered down and out of the signal path. Passing `-g` there writes to a chip
        that is not listening — a control that appears to work and does nothing."""
        assert "-g" not in listen.demod_args("am", "36.4", 10_000_000)
        assert "-g" in listen.demod_args("fm", "36.4", 146_940_000)

    def test_a_shortwave_frequency_is_accepted(self) -> None:
        assert listen.validate(10_000_000, "am") == "am"
        assert listen.validate(530_000, "am") == "am"

    def test_below_the_ADC_is_still_refused(self) -> None:
        with pytest.raises(listen.SdrError):
            listen.validate(50_000, "am")

    def test_the_second_Nyquist_zone_is_refused_rather_than_mis_tuned(self) -> None:
        """The gap between the two floors, and the reason this is a refusal.

        `demod_args` bypasses the tuner for everything below 24 MHz, but the ADC clocks
        at 28.8 MHz — so the honest range down there stops at 14.4 MHz. Ask for 18.1 and
        the radio hands back 10.7, mirrored: the request succeeds, the session reports
        healthy, the level meter moves, and the owner is listening to a different
        station with nothing anywhere saying so. The PWA's own floor used to hide this
        by refusing everything below 24 MHz; F8 lowered it to 0.1 and left the hole
        (docs/plans/SDR_IQ_SPECTRUM_PLAN.md §8)."""
        with pytest.raises(listen.SdrError) as refused:
            listen.validate(18_100_000, "usb")

        # The sentence has to carry the number the owner would otherwise be hearing,
        # because "out of range" is not true — the radio tunes it, at the wrong place.
        assert "10.700 MHz" in str(refused.value)
        assert "14.4 MHz" in str(refused.value)

    def test_the_zone_boundaries_themselves_stay_legal(self) -> None:
        """The first zone ends AT 14.4 MHz and the tuner comes back AT 24, so both
        edges are reachable and neither folds. A guard that took one of them would cost
        a real band for nothing."""
        assert listen.validate(listen.NYQUIST_HZ, "usb") == "usb"
        assert listen.validate(listen.MIN_HZ, "fm") == "fm"

    def test_shortwave_is_accepted_now_that_one_engine_serves_both(self) -> None:
        """This pair of tests spent three waves asserting the opposite, and the reason
        was never the radio's: `rtl_power -D` hardcodes direct sampling mode 1 — the
        ADC's I branch — where this board wires Q, so a survey below 24 MHz tuned
        something and measured nothing. B1 removed the tool from the picture and B2 from
        the survey, and the floor went with it."""
        sweep = listen.Sweep.of(7_000_000, 7_300_000, 250, 60)

        assert sweep.start_hz == 7_000_000

    def test_even_the_direct_path_stops_at_what_the_ADC_reaches(self) -> None:
        """The floor that remains is the RADIO's, not a tool's: below DIRECT_MIN_HZ the
        board's diplexer feeds the ADC nothing at all."""
        with pytest.raises(listen.SdrError) as refused:
            listen.Sweep.of(50_000, 200_000, 250, 60)

        assert "below what this radio reaches" in str(refused.value)

    def test_a_sweep_above_the_tuner_is_unaffected(self) -> None:
        sweep = listen.Sweep.of(144_000_000, 148_000_000, 5_000, 60)
        assert sweep.start_hz == 144_000_000


# --- the live spectrum ----------------------------------------------------------


class TestTheLiveSpectrum:
    """A waterfall is a radio held open, not a measurement that ends.

    That is the whole difference from `survey`, and every test here is about a
    consequence of it: no exit timer, rows fanned out as they are transformed, and a
    session that has to be released like a listening one rather than freeing itself.

    **There is one engine now (B1).** The tests that asserted `rtl_power`'s argv are
    gone with the tool; what is left is what a picture has to do whoever draws it, and
    it is checked against a fake RADIO rather than a fake process.
    """

    CAPTURE = (2_400_000, 512)

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        opened: list[Any] = []

        def _open(*, center_hz: int, rate_hz: int, **kwargs: Any) -> Any:
            made = _FakeRadio(center_hz, station_hz=center_hz, rate_hz=rate_hz)
            made.opened_with = kwargs  # type: ignore[attr-defined]
            opened.append(made)
            return made

        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(listen.radio.Radio, "open", staticmethod(_open))
        tuner = listen.Tuner()
        tuner.opened = opened  # type: ignore[attr-defined]
        yield tuner
        tuner.stop()

    def _sweep(
        self, start: int = 144_000_000, stop: int = 144_200_000, **kw: Any
    ) -> Any:
        kw.setdefault("capture", self.CAPTURE)
        return listen.Sweep.of(start, stop, 25_000, 60, **kw)

    def _start(self, tuner, **kw: Any) -> Any:
        tuner.start(
            144_000_000,
            "fm",
            kw.pop("gain", None),
            purpose=listen.PURPOSE_SPECTRUM,
            sweep=kw.pop("sweep", None) or self._sweep(),
            **kw,
        )
        return tuner.for_purpose(listen.PURPOSE_SPECTRUM)

    # ---- the lease ------------------------------------------------------------

    def test_a_spectrum_holds_the_lease_and_says_what_for(self, tuner) -> None:
        info = tuner.start(
            144_000_000,
            "fm",
            None,
            purpose=listen.PURPOSE_SPECTRUM,
            sweep=self._sweep(),
        )

        body = info.as_dict()
        assert body["purpose"] == listen.PURPOSE_SPECTRUM
        # The range, so the waterfall can label its own axis. `frequency_hz` can only
        # carry the midpoint, which reads as a tuner parked somewhere it is not.
        assert body["sweep"]["start_hz"] == 144_000_000
        assert body["sweep"]["stop_hz"] == 144_200_000
        assert body["sweep"]["seconds"] == 60.0
        # ...and the capture that draws it, which IS the engine choice.
        assert (body["sweep"]["rate_hz"], body["sweep"]["bins"]) == self.CAPTURE
        assert body["frequency_hz"] == 144_100_000

    def test_a_spectrum_cannot_steal_the_radio_from_APRS(self, tuner) -> None:
        tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

        with pytest.raises(listen.SdrBusy) as refused:
            tuner.start(
                144_000_000,
                "fm",
                None,
                purpose=listen.PURPOSE_SPECTRUM,
                sweep=self._sweep(),
            )

        assert "logging APRS" in str(refused.value)

    def test_a_spectrum_without_a_range_is_refused(self, tuner) -> None:
        with pytest.raises(listen.SdrError):
            tuner.start(144_000_000, "fm", None, purpose=listen.PURPOSE_SPECTRUM)

    def test_a_frame_wider_than_the_stream_carries_is_refused(self, tuner) -> None:
        """Refused at the lease rather than discovered on the wire: 60 MHz at 100 Hz
        bins is 600,000 numbers a second per viewer, and the honest place to say no is
        before a radio is held for it."""
        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                144_000_000,
                "fm",
                None,
                purpose=listen.PURPOSE_SPECTRUM,
                sweep=listen.Sweep.of(144_000_000, 154_000_000, 100, 60),
            )

        assert "coarser bins" in str(refused.value)

    def test_a_range_with_no_capture_plan_is_refused_not_drawn_another_way(
        self, tuner
    ) -> None:
        """B1. There is no second engine, and that is the point rather than a gap:
        `rtl_power` measured on a scale of its own, both engines landed on the same
        `Frame.db`, and `peaks.find` over it reaches the agent's tools as fact. A
        picture that is a different QUANTITY depending on who drew it is worse than no
        picture, so a range with no plan is a sentence."""
        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                144_000_000,
                "fm",
                None,
                purpose=listen.PURPOSE_SPECTRUM,
                sweep=listen.Sweep.of(144_000_000, 144_200_000, 25_000, 60),
            )

        assert "no capture plan" in str(refused.value)
        assert tuner.sessions() == []  # and no radio was taken for it

    # ---- the radio ------------------------------------------------------------

    def test_a_live_spectrum_fixes_the_gain_even_when_nobody_asked(self, tuner) -> None:
        """A waterfall whose dB scale is a property of whatever the tuner's AGC was
        doing has no two rows that mean the same thing, and no two runs either."""
        self._start(tuner)

        assert tuner.opened[0].gain_db == listen.MEASURING_GAIN_DB

    def test_the_owners_gain_wins_when_they_name_one(self, tuner) -> None:
        self._start(tuner, gain="30")

        assert tuner.opened[0].gain_db == 30.0

    def test_it_opens_the_radio_it_was_told_to(self, tuner) -> None:
        self._start(tuner, serial="77192819")

        assert tuner.opened[0].opened_with["serial"] == "77192819"

    def test_it_captures_at_the_rate_the_plan_named(self, tuner) -> None:
        """The band table lives in ONE place and the sidecar executes what it was
        handed: a rate chosen here would be a second table that disagrees."""
        self._start(tuner)

        assert tuner.opened[0].rate_hz == self.CAPTURE[0]
        assert tuner.opened[0].center_hz == 144_100_000

    # ---- frames reaching viewers ----------------------------------------------

    def test_a_viewer_is_handed_the_rows_as_they_are_measured(self, tuner) -> None:
        session = self._start(tuner)
        sub, _ = session.subscribe_frames()

        frame = sub.get(timeout=5)

        assert frame is not None
        assert frame.view == listen.VIEW_BAND
        assert len(frame.db) == self.CAPTURE[1]
        assert frame.bin_hz == self.CAPTURE[0] / self.CAPTURE[1]

    def test_a_stitched_row_STOPS_where_the_band_asked_for_does(self, tuner) -> None:
        """Whole hops cannot tile an arbitrary span, so the plan rounds UP and the
        capture reaches past the stop. That is right for the capture — the alternative
        is a gap at the top of the band — and wrong for the row.

        Publishing the overshoot presented 1.86 MHz of out-of-band noise as part of the
        FM dial, and `peaks.find` judges each bin against its neighbours and cannot
        know the band ended: it reported stations at 108.3, 108.7, 109.1, 109.4 and
        109.7, where by law there are none. MEASURED on the box: a row requested as
        88-108 came back stopping at 109.8625.
        """
        rate_hz, bins = self.CAPTURE
        usable = listen.hop_usable_bins(bins)
        bin_hz = rate_hz / bins
        # Two and a half hops' worth: a span whole hops cannot tile, which is the case.
        stop = 144_000_000 + int(usable * 2.5 * bin_hz)
        session = self._start(tuner, sweep=self._sweep(144_000_000, stop, hops=3))
        sub, _ = session.subscribe_frames()

        frame = sub.get(timeout=5)

        assert frame is not None
        assert frame.stop_hz >= stop, "the row must still cover the whole band"
        # ...and not a whole channel past it. Untrimmed this row is three full hops.
        assert frame.stop_hz < stop + bin_hz
        assert len(frame.db) < usable * 3

    def test_a_viewer_arriving_late_is_not_shown_a_blank_canvas(self, tuner) -> None:
        """The seed. Without it a waterfall opens on nothing for up to a whole interval,
        which reads as a radio that did not start.

        It comes back BESIDE the queue rather than in it: the queue is four deep on
        purpose, so a seed of any size would silently drop most of itself."""
        session = self._start(tuner)
        early, _ = session.subscribe_frames()
        early.get(timeout=5)  # the pump has certainly published by now

        _late, seed = session.subscribe_frames()

        assert seed and seed[-1] is not None

    def test_a_viewer_can_ask_for_the_rows_already_drawn(self, tuner) -> None:
        """What the owner saw: switching away from a running waterfall and back showed
        one strip at the bottom of an empty box, because the picture restarted from
        nothing at two rows a second."""
        session = self._start(tuner)
        warm, _ = session.subscribe_frames()
        for _ in range(4):
            warm.get(timeout=5)

        _sub, seed = session.subscribe_frames(backfill=4)

        assert len(seed) >= 3
        # Oldest first, so a waterfall draws them in the order they were measured.
        assert [frame.at for frame in seed] == sorted(frame.at for frame in seed)

    def test_the_history_it_keeps_is_a_RING_and_not_a_log(self, tuner) -> None:
        """A session runs for days on a box with 512 MB, and a row is thousands of
        floats. The bound is the deque's own — asserted here rather than by publishing
        past it, which would take minutes of real capture to reach and would pass
        vacuously at any size below it."""
        session = self._start(tuner)
        sub, _ = session.subscribe_frames()
        sub.get(timeout=5)

        kept = list(session._history.values())

        assert kept and all(ring.maxlen == listen.HISTORY_ROWS for ring in kept)

    def test_asking_for_more_history_than_exists_gets_what_exists(self, tuner) -> None:
        session = self._start(tuner)
        sub, _ = session.subscribe_frames()
        sub.get(timeout=5)

        _late, seed = session.subscribe_frames(backfill=listen.HISTORY_ROWS * 4)

        assert 0 < len(seed) <= listen.HISTORY_ROWS

    def test_a_backed_up_viewer_loses_rows_and_never_wedges_the_pump(
        self, tuner
    ) -> None:
        """A phone that stopped reading must cost that phone its picture, not everyone
        else's — the same backpressure live audio takes, and for the same reason: a
        waterfall row drawn late is drawn in the wrong place."""
        session = self._start(tuner)
        stalled, _ = session.subscribe_frames()
        reading, _ = session.subscribe_frames()

        for _ in range(listen.SPECTRUM_QUEUE + 6):
            assert reading.get(timeout=5) is not None

        assert stalled.qsize() == listen.SPECTRUM_QUEUE

    def test_releasing_the_radio_closes_every_viewers_stream(self, tuner) -> None:
        """Without the sentinel a released session leaves a server thread per viewer
        blocked for ever, each still reporting a healthy picture of a radio nothing is
        watching — the same leak the packet readers had."""
        session = self._start(tuner)
        sub, _ = session.subscribe_frames()

        tuner.stop(session.id)

        while sub.get(timeout=5) is not None:
            pass  # the rows already queued, then the sentinel behind them

    # ---- moving it ------------------------------------------------------------

    def test_moving_the_range_keeps_the_session_and_its_viewers(self, tuner) -> None:
        session = self._start(tuner)
        sub, _ = session.subscribe_frames()
        was = session.id

        session.resweep(self._sweep(440_000_000, 440_200_000))

        assert session.id == was
        assert session.info().as_dict()["sweep"]["start_hz"] == 440_000_000
        # The omnibox names the new centre, not the old one.
        assert session.frequency_hz == 440_100_000
        # And the viewer was never told anything — no sentinel, no reconnect. The next
        # row simply describes the new band, which is what every frame carrying its own
        # range buys.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = sub.get(timeout=5)
            assert frame is not None
            if frame.start_hz > 400_000_000:
                break
        else:  # pragma: no cover - the loop above returns on the first new-band row
            raise AssertionError("no row described the new band")

    def test_a_retune_with_no_capture_plan_leaves_the_picture_running(
        self, tuner
    ) -> None:
        """The tap that must not cost the owner their radio.

        Refusing from inside the relaunch would refuse AFTER `_restart` had killed the
        pipeline and written the new range — `alive` false, the next `Tuner._reap`
        stopping the session and dropping the lease, and the error toast landing on a
        screen that had just lost the waterfall AND the radio. Validated before anything
        is destroyed, the tap costs a sentence."""
        session = self._start(tuner)
        sub, _ = session.subscribe_frames()

        with pytest.raises(listen.SdrError) as refused:
            session.resweep(listen.Sweep.of(7_125_000, 7_300_000, 250, 0))

        assert "no capture plan" in str(refused.value)
        # Still running, still leased, and still the session the omnibox names —
        # `current()` takes the lock and reaps, so this is the reaper's own verdict.
        assert session.alive
        assert tuner.current() is not None
        assert tuner.for_purpose(listen.PURPOSE_SPECTRUM) is session
        # And still pointed where it was. A refused move moves NOTHING: a session left
        # holding the range it was refused would draw 2 m rows under a 40 m label.
        assert session.sweep is not None and session.sweep.start_hz == 144_000_000
        assert session.frequency_hz == 144_100_000
        # The viewer was never disconnected either — no sentinel, and the picture it is
        # already watching keeps arriving.
        frame = sub.get(timeout=5)
        assert frame is not None and frame.start_hz < 200_000_000

    def test_shortwave_with_a_capture_plan_is_drawn_rather_than_refused(
        self, tuner
    ) -> None:
        """F6's gain, kept: the I/Q engine sets `direct_samp` at runtime and reaches the
        branch this board wires, so a shortwave range ONE capture covers is the engine's
        to draw. Whether anything arrives there is the antenna's business and shows up
        as an empty picture rather than a sentence about software."""
        session = self._start(tuner)

        session.resweep(
            listen.Sweep.of(7_125_000, 7_175_000, 250, 0, capture=(256_000, 1024))
        )

        assert session.sweep is not None and session.sweep.start_hz == 7_125_000
        assert tuner.opened[-1].opened_with["direct"] is True

    def test_moving_a_released_spectrum_is_refused_not_relaunched(self, tuner) -> None:
        """The race `Session._restart` exists for: a relaunch here builds a pipeline for
        a session no longer in the registry — unreapable, and holding the dongle until
        the container restarts."""
        session = self._start(tuner)
        tuner.stop(session.id)

        with pytest.raises(listen.SessionGone):
            session.resweep(self._sweep(440_000_000, 440_200_000))

    def test_a_listening_session_has_no_range_to_move(self, tuner) -> None:
        tuner.start(99_300_000, "wbfm", None)
        session = tuner.for_purpose(listen.PURPOSE_LISTEN)

        with pytest.raises(listen.SdrError):
            session.resweep(self._sweep())


class TestARadioThatWillNotOpen:
    """A pipeline that dies on the spot must be a refusal, not a session.

    MEASURED on the box 2026-09-04. One dongle's USB descriptors stopped answering, so
    librtlsdr enumerated it with blank strings and every `-d <serial>` lookup failed:

        Found 2 device(s):
          0:  , , SN:
          1:  Nooelec, NESDR SMArt v5, SN: 09022796
        No matching devices found.

    rtl_power printed that and exited in milliseconds. `start` returned 200 anyway, the
    api reported a session, and a second later the tuner reaped a dead one — with the
    only explanation in a container log the owner has no way to read (CLAUDE.md #10).
    """

    class _Stillborn(_FakeProc):
        """A tool that printed its complaint and exited before anything read it."""

        SAID = b"Found 2 device(s):\n  0:  , , SN:\nNo matching devices found.\n"

        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self._dead = True
            self.stderr = _Lines(self.SAID.splitlines())

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", self._Stillborn)
        # NOT `_instant`: the grace is the mechanism under test, and it has to be long
        # enough to read a pipe that is already closed.
        monkeypatch.setattr(listen, "STARTUP_GRACE_S", 0.05)
        return listen.Tuner()

    def test_starting_is_refused_rather_than_answered_with_a_session(
        self, tuner
    ) -> None:
        with pytest.raises(listen.SdrError) as refused:
            tuner.start(99_300_000, "wbfm", None)

        assert "did not start" in str(refused.value)

    def test_the_refusal_carries_the_tool_s_own_words(self, tuner) -> None:
        # Which is what names the radio it could not find. "The radio did not start" on
        # its own sends the owner to look at everything.
        with pytest.raises(listen.SdrError) as refused:
            tuner.start(99_300_000, "wbfm", None)

        assert "No matching devices found" in str(refused.value)

    def test_the_radio_is_left_free_for_the_next_caller(self, tuner) -> None:
        """The half that would bite hardest. A refused session that stayed in the
        registry would hold the radio against every later caller, and nothing would
        ever release it — the lease would be held by something that never ran."""
        with pytest.raises(listen.SdrError):
            tuner.start(99_300_000, "wbfm", None)

        assert tuner.current() is None
        assert tuner.sessions() == []

    class _Complaining(_FakeProc):
        """A tool that SAYS it cannot have the radio and then carries on.

        MEASURED, and it is why matching the words beats waiting for an exit: rtl_power
        printed `No matching devices found` and went straight on to its hop plan, still
        running well past the grace. A check that only watched for a dead process saw a
        healthy session and let the caller build a sweep on it."""

        SAID: ClassVar[list[bytes]] = [
            b"Found 2 device(s):",
            b"  0:  , , SN:",
            b"No matching devices found.",
            b"Number of frequency hops: 1",
            b"Dongle bandwidth: 2000000Hz",
        ]

        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.stderr = _Lines(self.SAID)

    def test_a_tool_that_complains_and_keeps_running_is_still_refused(
        self, monkeypatch
    ) -> None:
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", self._Complaining)
        monkeypatch.setattr(listen, "STARTUP_GRACE_S", 0.2)
        tuner = listen.Tuner()

        with pytest.raises(listen.SdrError) as refused:
            tuner.start(99_300_000, "wbfm", None)

        # The process never died, so only its own words could have caught this.
        assert "No matching devices found" in str(refused.value)
        # And it leads with that line rather than the hop plan behind it.
        assert "Dongle bandwidth" not in str(refused.value)
        assert tuner.sessions() == []

    def test_a_spectrum_is_refused_the_same_way(self, tuner, monkeypatch) -> None:
        """A radio that will not open is a refusal naming the driver's own words, on
        the picture path as on the audio one — and since B1 there is no second engine
        to quietly draw it on a different scale instead."""

        def _no(**_k: Any):
            raise listen.radio.RadioError("No matching devices found")

        monkeypatch.setattr(listen.radio.Radio, "open", staticmethod(_no))
        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                144_000_000,
                "fm",
                None,
                purpose=listen.PURPOSE_SPECTRUM,
                sweep=listen.Sweep.of(
                    144_000_000, 144_200_000, 25_000, 60, capture=(2_400_000, 512)
                ),
            )

        assert "No matching devices found" in str(refused.value)


class _Lines:
    """A stderr pipe holding lines a dead process already wrote."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def read(self, _n: int = 0) -> bytes:
        return b""

    def __iter__(self):
        return iter(self._lines)


class TestClosingTheRadioPolitely:
    """SIGTERM before SIGKILL, and it is not politeness.

    `rtl_fm` and `rtl_power` install a handler that cancels the pending async USB
    transfer and CLOSES the device. SIGKILL never runs it, so the RTL2832U is left with
    transfers submitted — and a device torn down that way can stop answering the
    descriptor reads librtlsdr uses to enumerate it, after which it looks absent while
    sysfs still lists it. That is the state one dongle was found in.
    """

    class _Recorder(_FakeProc):
        signals: ClassVar[list[str]] = []

        def terminate(self) -> None:
            self.signals.append("term")
            self._dead = True

        def kill(self) -> None:
            self.signals.append("kill")
            self._dead = True

    class _Stubborn(_Recorder):
        """A tool that ignores SIGTERM — the case the kill is still there for."""

        def terminate(self) -> None:
            self.signals.append("term")

        def wait(self, timeout: float | None = None) -> int:
            if not self._dead:
                raise listen.subprocess.TimeoutExpired("x", timeout or 0)
            return 0

    def _run(self, monkeypatch: pytest.MonkeyPatch, proc: Any) -> list[str]:
        proc.signals = []
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", proc)
        tuner = listen.Tuner()
        tuner.start(99_300_000, "wbfm", None)
        tuner.stop()
        return list(proc.signals)

    def test_a_tool_that_goes_quietly_is_never_killed(self, monkeypatch) -> None:
        assert "kill" not in self._run(monkeypatch, self._Recorder)

    def test_a_tool_that_will_not_go_still_gets_killed(self, monkeypatch) -> None:
        # The device is worse off either way, but a wedged tool holding the radio for
        # ever is worse still.
        sent = self._run(monkeypatch, self._Stubborn)

        assert sent.index("term") < sent.index("kill")


class TestTheUsbReset:
    """The ioctl itself. Pure enough to check without a device: the number, and the one
    guard that stops a caller-supplied path reaching `os.open` in a root container."""

    def test_it_is_the_kernel_s_own_reset_number(self) -> None:
        # _IO('U', 20) from <linux/usbdevice_fs.h>: direction 0, type 'U', number 20.
        assert (ord("U") << 8) | 20 == usbdev.USBDEVFS_RESET

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "/dev/bus/usb/003/010/../../../../etc/shadow",
            "/dev/bus/usb/3/10",
            "/dev/null",
            "",
        ],
    )
    def test_only_a_device_node_is_ever_opened(self, path: str) -> None:
        with pytest.raises(ValueError):
            usbdev.reset(path)

    def test_a_real_node_shape_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[tuple[str, int]] = []
        monkeypatch.setattr(
            usbdev.os, "open", lambda p, f: (opened.append((p, f)), 7)[1]
        )
        monkeypatch.setattr(usbdev.os, "close", lambda _fd: None)
        monkeypatch.setattr(usbdev.fcntl, "ioctl", lambda *_a: 0)

        usbdev.reset("/dev/bus/usb/003/010")

        assert opened == [("/dev/bus/usb/003/010", usbdev.os.O_WRONLY)]

    def test_the_handle_is_closed_even_when_the_ioctl_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A leaked fd on a USB device node would keep the device open against the very
        # tools the reset exists to unblock.
        closed: list[int] = []
        monkeypatch.setattr(usbdev.os, "open", lambda _p, _f: 7)
        monkeypatch.setattr(usbdev.os, "close", closed.append)

        def boom(*_a: object) -> int:
            raise OSError(19, "No such device")

        monkeypatch.setattr(usbdev.fcntl, "ioctl", boom)

        with pytest.raises(OSError):
            usbdev.reset("/dev/bus/usb/003/010")

        assert closed == [7]


class _Tracked(_FakeProc):
    """A fake process that records itself, so a test can ask what is still running."""

    made: ClassVar[list[_Tracked]] = []

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        _Tracked.made.append(self)

    @property
    def running(self) -> bool:
        return not self._dead


@pytest.fixture
def tracking(monkeypatch: pytest.MonkeyPatch):
    _instant(monkeypatch)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    _Tracked.made = []
    monkeypatch.setattr(listen.subprocess, "Popen", _Tracked)
    return listen.Tuner()


def test_a_stop_during_a_retune_does_not_strand_the_relaunched_radio(tracking) -> None:
    """The bug this closes, seen on the box on 2026-09-04.

    `_restart` checked `_released` under the lock and then let go of it to do the work.
    A `stop()` landing in that window killed the OLD pipeline (already down) and popped
    the session — and the pipeline `_start_pipeline` spawned a moment later held the
    dongle with nothing left in the program pointing at it. It ran for hours, printing
    `[rtl_fm] Error: dropped samples` because `_stopping` was already true when its
    pumps started so nothing drained its stdout, while `/healthz` and the PWA both said
    the radio was idle. The only cure was restarting the container."""
    tracking.start(99_300_000, "wbfm", None)
    session = tracking.current()
    assert session is not None
    spawn = session._start_pipeline

    def stop_lands_mid_relaunch() -> None:
        # Exactly the interleaving: the guard has passed, the old pipeline is down, and
        # the release happens before the new one exists to be killed.
        session.stop()
        spawn()

    session._start_pipeline = stop_lands_mid_relaunch  # type: ignore[method-assign]

    with pytest.raises(listen.SessionGone):
        session.tune(107_100_000, "wbfm")

    still_up = [p for p in _Tracked.made if p.running]
    assert not still_up, "a released session left a process holding the radio"


def test_a_process_that_survives_its_kill_is_retried_not_forgotten(tracking) -> None:
    """`_kill` clears `_rtl`/`_enc` so `alive` can go false, which is right for the
    SESSION and wrong for the process: a survivor holds the radio while every
    `blocking_key` believes it is free, and nothing left can still reach it."""

    class _Unkillable(_Tracked):
        def terminate(self) -> None:
            self.signals = getattr(self, "signals", 0) + 1

        kill = terminate

    unkillable = _Unkillable()
    listen._park(unkillable)

    assert listen.reap_survivors() == 1, "a survivor must stay parked, not be dropped"

    unkillable._dead = True  # it finally went

    assert listen.reap_survivors() == 0
    assert listen._survivors == []


def test_reaping_sessions_also_retries_stranded_processes(tracking) -> None:
    """Wired onto the session sweep deliberately: a parked process is one holding a
    radio the sweep is about to report as free, so that is exactly when to try again."""
    tracking.start(99_300_000, "wbfm", None)
    stranded = _Tracked()
    listen._park(stranded)

    tracking.current()  # takes the lock and reaps

    assert not stranded.running and listen._survivors == []


def test_a_spectrum_with_no_capture_plan_is_refused_wherever_it_is(tuner) -> None:
    """This test used to be about SHORTWAVE, and about `rtl_power -D` hardcoding the
    ADC's I branch where this board wires Q. Both halves of that are gone: the I/Q
    engine sets the branch at runtime, and B1 deleted the tool.

    What survives is the shape of the rule, which is why the refusal was put on the
    ENGINE rather than in the route in the first place — a range with no capture plan
    has nothing that can honestly draw it, at any frequency, and the sidecar says so
    rather than reaching for something that measures on another scale."""
    with pytest.raises(listen.SdrError) as refused:
        tuner.start(
            7_200_000,
            "fm",
            None,
            purpose=listen.PURPOSE_SPECTRUM,
            sweep=listen.Sweep.of(7_125_000, 7_300_000, 250, 0),
        )

    assert "no capture plan" in str(refused.value)
    assert tuner.current() is None


# --- F6: the I/Q engine, and the fallback that must survive it ----------------------


def test_a_named_capture_keeps_its_exact_width_through_the_clamp() -> None:
    """`MIN_SWEEP_BIN_HZ` is rtl_power's floor, and clamping an I/Q width to it would
    make the frame declare a width the transform never used — invisibly, because
    nothing downstream can tell (§6.14). 250 Hz is under that floor and is exactly what
    256 kS/s over 1024 bins produces."""
    swept = listen.Sweep.of(7_125_000, 7_300_000, 250, 60, capture=(256_000, 1_024))

    assert swept.bin_hz == 250
    assert swept.capture == (256_000, 1_024)
    assert swept.as_dict()["rate_hz"] == 256_000
    assert swept.as_dict()["bins"] == 1_024


def test_a_sweep_with_no_capture_still_gets_rtl_powers_floor() -> None:
    """The clamp is not gone, it is scoped: where the width is a REQUEST to a tool
    rather than a fact about a transform, it still applies."""
    swept = listen.Sweep.of(144_000_000, 148_000_000, 1, 60)

    assert swept.bin_hz == listen.MIN_SWEEP_BIN_HZ
    assert swept.capture is None
    assert "rate_hz" not in swept.as_dict()


def test_shortwave_with_a_capture_is_no_longer_refused() -> None:
    """F6. The refusal belonged to the ENGINE: `rtl_power -D` hardcodes the I branch
    and this board wires Q. The I/Q engine sets mode 2 at runtime, so a range it can
    draw in one capture is its to draw."""
    swept = listen.Sweep.of(7_125_000, 7_300_000, 250, 60, capture=(256_000, 1_024))

    assert listen.spectrum_engine_refusal(swept) is None


def test_a_range_with_no_capture_plan_is_refused_and_says_what_to_ask_for() -> None:
    """The sentence has to name what to do instead, because the owner has no terminal
    to look with (CLAUDE.md #10) — and since B1 there is no second engine to name, only
    a narrower request."""
    swept = listen.Sweep.of(3_000_000, 8_000_000, 25_000, 60)

    refusal = listen.spectrum_engine_refusal(swept)

    assert refusal is not None
    assert "no capture plan" in refusal
    assert "band section" in refusal


def test_a_frame_carries_a_fractional_width_without_rounding_it() -> None:
    """`rate / bins` is exact for every pairing in the band table and a float for
    anything else. Rounding 585.9375 to 586 puts the top of the frame 256 Hz out with
    nothing able to tell, and the PWA compares this value exactly (§6.13)."""
    frame = listen.Frame(at=0.0, start_hz=100_000_000, bin_hz=585.9375, db=[-40.0] * 4)

    assert frame.bin_hz == 585.9375
    assert frame.stop_hz == 100_000_000 + 4 * 585.9375
    assert frame.as_dict()["bin_hz"] == 585.9375


class TestIQSpectrumEngine:
    """F6: the sidecar transforms the samples itself, and falls back when it cannot.

    The fallback is the load-bearing part. An owner with no terminal must not need a
    revert and a rebuild to get a picture back if the I/Q engine will not open a radio
    on their box (CLAUDE.md #10), so a failure drops to rtl_power wherever rtl_power
    can serve the range — and keeps the failure only where it cannot, because there the
    alternative is not a worse picture but a false one."""

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        tuner = listen.Tuner()
        yield tuner
        tuner.stop()

    @staticmethod
    def _refuse_to_open(monkeypatch: pytest.MonkeyPatch, why: Exception) -> None:
        def _open(**_kwargs: Any) -> Any:
            raise why

        monkeypatch.setattr(listen.radio.Radio, "open", staticmethod(_open))

    def test_a_radio_that_will_not_open_is_a_refusal_not_a_second_engine(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B1 reversed this test, and the reasoning is worth keeping in one place.

        It used to assert that VHF fell back to `rtl_power` when the radio would not
        open, on CLAUDE.md #10's grounds: an owner with no terminal must not need a
        revert and a rebuild to get a picture back. **The trade was the wrong way
        round.** Both engines landed on the same `Frame.db`, the same colour map and the
        same `peaks.find`, whose output reaches the agent's tools as a MEASUREMENT — and
        `iq.py` emits true dBFS where `rtl_power` emitted its own uncalibrated scale.
        What #10 requires is that the owner is never left guessing, and a refusal naming
        the driver does that better than a picture whose decibels are a different
        quantity.

        LISTENING keeps its `rtl_fm` fallback, deliberately: there the fallback degrades
        the FEATURE, not the meaning of a number."""
        self._refuse_to_open(monkeypatch, listen.radio.RadioError("no such device"))
        swept = listen.Sweep.of(
            144_000_000, 144_400_000, 600, 300, capture=(2_400_000, 4_000)
        )

        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                146_000_000, "fm", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
            )

        assert "no such device" in str(refused.value)
        assert tuner.sessions() == []

    def test_shortwave_is_refused_the_same_way_as_everything_else(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It always was refused here; what changed is that it is no longer the ONE
        range where refusing was right."""
        self._refuse_to_open(monkeypatch, listen.radio.RadioError("no such device"))
        swept = listen.Sweep.of(
            7_125_000, 7_300_000, 250, 300, capture=(256_000, 1_024)
        )

        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                7_212_500, "usb", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
            )

        assert "no such device" in str(refused.value)

    def test_a_busy_radio_is_a_refusal_too_and_not_a_crash(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`RadioBusy` is a different exception from `RadioError` and would have escaped
        an `except RadioError` written from the happy path."""
        self._refuse_to_open(monkeypatch, listen.radio.RadioBusy("held by aprs"))
        swept = listen.Sweep.of(
            144_000_000, 144_400_000, 600, 300, capture=(2_400_000, 4_000)
        )

        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                146_000_000, "fm", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
            )

        assert "held by aprs" in str(refused.value)

    def test_the_engine_publishes_frames_it_transformed_itself(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The happy path, end to end through the real `iq.Spectrometer`: samples in,
        one self-describing row out, at the width the capture makes and not the width
        anyone asked for."""
        rate_hz_outer = rate_hz = 2_400_000
        fft_bins = 4_000
        opened: dict[str, Any] = {}

        class _OneFrameRadio:
            alive = True
            center_hz = 146_000_000
            # The frame budget is derived from the rate now (`segments_for`), so a fake
            # radio has to have one — a row is sized to last 1/TARGET_FPS.
            rate_hz = rate_hz_outer

            def read(self, samples: int) -> Any:
                tone = np.exp(
                    2.0j * np.pi * (rate_hz / 8.0) * np.arange(samples) / rate_hz
                )
                noise = np.random.default_rng(7).standard_normal(samples)
                return listen.radio.Reading(
                    samples=(tone + 1e-3 * noise).astype(np.complex64),
                    at=1234.5,
                    reads=1,
                    overflows=0,
                    timeouts=0,
                    center_hz=self.center_hz,
                )

            def set_gain(self, db: float | None) -> None:
                opened["gain_db"] = db

            def close(self) -> None:
                opened["closed"] = True

        def _open(**kwargs: Any) -> Any:
            opened.update(kwargs)
            return _OneFrameRadio()

        monkeypatch.setattr(listen.radio.Radio, "open", staticmethod(_open))
        swept = listen.Sweep.of(
            144_000_000, 144_400_000, 600, 300, capture=(rate_hz, fft_bins)
        )

        tuner.start(
            146_000_000, "fm", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
        )
        session = tuner.current()
        assert session is not None
        frame = session.subscribe_frames()[0].get(timeout=5)

        assert frame is not None
        # 2.4 MS/s over 4000 bins is 600 Hz EXACTLY, which is why this pairing is in
        # the table: an inexact one would put a float on the wire (§6.13).
        assert frame.bin_hz == 600
        assert len(frame.db) == fft_bins
        # Self-describing: the row says where it is, so a retune needs no
        # protocol event at all.
        assert frame.start_hz == 146_000_000 - (rate_hz // 2)
        # And it opened the radio the capture named, not a rate of its own choosing.
        assert opened["rate_hz"] == rate_hz
        assert opened["direct"] is False
        # AND IT SET THE GAIN. `Radio.open` does not touch it, so without this the
        # picture ran at whatever librtlsdr was left at by the previous session — a dB
        # scale that is a property of history rather than of the band. The fake had no
        # `set_gain` at all when this was written, which is why the omission survived:
        # the call is suppressed broadly, so a radio that cannot take a gain still
        # draws, and a test whose fake cannot take one asserts nothing.
        assert opened["gain_db"] == listen.MEASURING_GAIN_DB


def test_the_audio_level_is_named_for_what_it_measures() -> None:
    """F9. Two numbers in one surface, both once called `peak`, in different units,
    measuring different things: this one is the loudest sample of the DEMODULATED AUDIO
    after AGC, squelch and de-emphasis, and the spectrum path's is true dBFS per bin.
    It was drawn as a signal meter once, which is the reading it cannot support — idle
    airband AM sits at 0.21 off the tuner's AGC amplifying its own noise. The name on
    the wire is what stops it being re-surfaced as one."""
    info = listen.SessionInfo(
        session_id="abc",
        frequency_hz=99_300_000,
        mode="wbfm",
        gain=None,
        started_at=time.time(),
        audio_peak=0.42,
        listeners=0,
    )

    body = info.as_dict()

    assert body["audio_peak"] == 0.42
    assert "peak" not in body


# --- F11: wide bands stitched from hops, and the wire held to 10 fps ----------------


def test_a_row_is_sized_to_the_wire_budget_not_to_the_radio() -> None:
    """The engine measured 31.9 fps on the box, and everything downstream was sized for
    ten: the plan's own budget is 5,704 B gzipped per frame, so 31.9 fps is ~1.45 Mbit/s
    per viewer against ~0.46, plus thirty 4096-element JSON parses a second on a phone.

    Slowed by AVERAGING MORE, not by discarding: the samples arrive either way, so
    throwing them away buys nothing and costs the noise floor."""
    # 2.4 MS/s over 4000 bins is 600 raw rows/s; ten a second needs 60 of them.
    assert listen.segments_for(4_000, 2_400_000) == 60
    # 256 kS/s over 1024 is 250/s; 25 segments is ten rows a second.
    assert listen.segments_for(1_024, 256_000) == 25


def test_the_averaging_never_drops_below_what_it_was_before() -> None:
    """A very slow capture would otherwise compute fewer than 8 segments and report a
    noisier floor than the engine did before this cap existed."""
    assert listen.segments_for(4_096, 256_000) == listen.IQ_SEGMENTS


def test_the_averaging_is_bounded_so_one_row_cannot_swallow_a_burst() -> None:
    assert listen.segments_for(64, 3_200_000) == listen.MAX_IQ_SEGMENTS


def test_hops_tile_their_trusted_middles_with_no_gap_and_no_overlap() -> None:
    """Each hop contributes only the middle of its capture, because a capture's edges
    sit in the tuner's IF rolloff — drawing them puts a dip at every seam and shows the
    receiver's own filter shape as if it were the band."""
    rate_hz, bins, hops = 2_400_000, 256, 11
    usable = listen.hop_usable_bins(bins)
    width = listen.iq.bin_width_hz(rate_hz, bins)

    centres = listen.hop_centres(88_000_000, rate_hz, bins, hops)

    assert len(centres) == hops
    # Each hop's window starts exactly where the previous one ended.
    for index, centre in enumerate(centres):
        low = centre - (usable / 2) * width
        assert low == pytest.approx(88_000_000 + index * usable * width, abs=1.0)


def test_the_usable_middle_is_always_even() -> None:
    """The hop's centre sits on the boundary between its two middle bins, so an odd
    count cannot be placed symmetrically — and half a bin per hop accumulates into an
    axis that drifts from its own label."""
    for bins in (64, 128, 256, 512, 1024, 2048, 4096):
        assert listen.hop_usable_bins(bins) % 2 == 0


def test_a_sweep_carries_its_hop_count_on_the_wire() -> None:
    """Absent means one, which is what every caller before F11 meant. A sidecar reading
    it as more would sweep a band nobody asked for."""
    swept = listen.Sweep.of(
        88_000_000, 108_000_000, 9375, 60, capture=(2_400_000, 256), hops=11
    )

    assert swept.hops == 11
    assert swept.as_dict()["hops"] == 11
    assert listen.Sweep.of(144e6, 148e6, 5000, 60).hops == 1


# --- the I/Q listening path: one radio, audio AND the tuning view --------------------
#
# The pipeline is faked at the RADIO seam here rather than the subprocess one, because
# that is the seam this path actually has. `radio.py` and `demod.py` are each tested
# against their own fakes and synthetic signals; what is left for these — and what
# nothing else can catch — is the WIRING: that the radio is opened above the station by
# the offset the demodulator snapped to, that the audio reaches the encoder, that the
# tuning row is centred on the station rather than on the radio, and that a box where
# the I/Q engine will not start still gets sound.


class _FakeRadio:
    """A radio that hands back an FM carrier, and remembers how it was tuned.

    `rate_hz` because ONE fake serves both engines now that the spectrum path has no
    subprocess to stand in for: a listening capture is always `LISTEN_CAPTURE_HZ` and a
    spectrum session captures at whatever rate the api's plan named."""

    def __init__(
        self,
        center_hz: int,
        *,
        station_hz: int,
        rate_hz: int = listen.LISTEN_CAPTURE_HZ,
    ) -> None:
        self.rate_hz = rate_hz
        self.center_hz = center_hz
        self.alive = True
        self.closed = False
        self.reads = 0
        self.gain_db: float | None = "unset"  # type: ignore[assignment]
        self.gain_calls = 0
        self.retunes = 0
        #: The `settle_s` each retune was asked for, so a caller paying a settle this
        #: radio does not need is visible rather than merely slow (C29).
        self.settles: list[float | None] = []
        # The station sits `center - station` BELOW the radio's centre, which is what
        # the demodulator's mixer has to take back out.
        self._offset = station_hz - center_hz
        self._phase = 0.0
        # A NOISE FLOOR, 30 dB down, and it is load-bearing rather than realism.
        # Without it this radio hands over a bare carrier, and a bare carrier makes
        # every content test in this file unfalsifiable: an FM discriminator is blind
        # to amplitude, so a station the chain has attenuated by FIFTY-FIVE decibels —
        # tuned to the wrong side of the offset, say, and surviving only as filter
        # leakage — still demodulates to a perfect, full-scale 1 kHz tone. Noise is
        # what makes a signal that has been thrown away sound like a signal that has
        # been thrown away.
        self._rng = np.random.default_rng(20260906)

    def read(self, samples: int):
        self.reads += 1
        n = int(samples)
        t = (np.arange(n, dtype=np.float64) + self._phase) / self.rate_hz
        self._phase += n
        # A carrier at the station, deviated by a 1 kHz tone: something the
        # discriminator can actually recover, so silent audio means a broken chain.
        freq = self._offset + 3_000.0 * np.sin(2.0 * np.pi * 1_000.0 * t)
        phase = 2.0 * np.pi * np.cumsum(freq) / self.rate_hz
        noise = self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)
        wave = np.exp(1j * phase) + 0.0316 * noise  # -30 dB
        return listen.radio.Reading(
            samples=wave.astype(np.complex64),
            at=time.time(),
            reads=1,
            overflows=0,
            timeouts=0,
            center_hz=self.center_hz,
        )

    def set_gain(self, db: float | None) -> None:
        self.gain_db = db
        self.gain_calls += 1

    def retune(self, *, center_hz: int | None = None, **kwargs: Any) -> int:
        """Move in place, as `radio.Radio.retune` does — the whole of A2.

        The fake follows the real one in the property that matters: the STREAM is not
        rebuilt, so `closed` stays false and the carrier simply arrives at a new offset
        from the centre."""
        self.retunes += 1
        self.settles.append(kwargs.get("settle_s"))
        if center_hz is not None:
            self._offset += self.center_hz - center_hz
            self.center_hz = center_hz
        return 0

    def close(self) -> None:
        self.closed = True
        self.alive = False


@pytest.fixture
def iq_tuner(monkeypatch: pytest.MonkeyPatch):
    """A Tuner whose listening sessions get the I/Q engine and a synthetic radio."""
    _instant(monkeypatch)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
    opened: list[_FakeRadio] = []

    def _open(*, center_hz: int, **kwargs: Any) -> _FakeRadio:
        # 146.94 is what every test here tunes to; the fake needs to know where the
        # station is to put a carrier there.
        made = _FakeRadio(center_hz, station_hz=kwargs.pop("station_hz", 146_940_000))
        opened.append(made)
        return made

    monkeypatch.setattr(listen.radio.Radio, "open", staticmethod(_open))
    tuner = listen.Tuner()
    tuner.opened = opened  # type: ignore[attr-defined]
    return tuner


def _wait_for(predicate, timeout: float = 4.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_iq_engine_takes_the_listen_session(iq_tuner) -> None:
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        assert info.engine == "iq"
        assert info.as_dict()["engine"] == "iq"
    finally:
        iq_tuner.stop()


def test_the_radio_is_tuned_below_the_station_by_the_snapped_offset(iq_tuner) -> None:
    """BELOW, and this test asserted "above" while the code did the same — so both
    were wrong together and the pair looked like a check.

    The mixer shifts the spectrum DOWN by `offset_hz`, so what reaches DC is what sat
    ABOVE the tuned centre. Tuning above the station instead put it at `-offset`, which
    the mixer moved to `-2 * offset`: 480 kHz from DC, past every filter in the chain.
    MEASURED ON AIR 2026-09-06 — asking to hear 99.3 read 3.7 dB over the noise, and
    asking for 99.3 minus 480 kHz read 21.8 dB with four times the audio.

    The offset asserted is the one the MIXER settled on, not the one requested:
    `_Mixer` rounds to a whole division of the sample rate so its tone is a short
    repeating table, and opening the radio at the requested offset while mixing by the
    snapped one leaves the station a few kHz off centre — which on a narrowband channel
    is silence, from code that looks right in both places."""
    iq_tuner.start(146_940_000, "fm", None)
    try:
        chain = demod.Demodulator(
            "fm", listen.LISTEN_CAPTURE_HZ, offset_hz=float(listen.LISTEN_OFFSET_HZ)
        )
        assert iq_tuner.opened[0].center_hz == 146_940_000 - int(chain.offset_hz)
    finally:
        iq_tuner.stop()


def test_audio_reaches_the_encoder(iq_tuner) -> None:
    """The whole reason the output format was kept: ffmpeg is unchanged."""
    written: list[bytes] = []

    class _Recorder(_Sink):
        def write(self, b: bytes) -> int:
            written.append(bytes(b))
            return len(b)

    class _RecordingProc(_FakeProc):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.stdin = _Recorder()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(listen.subprocess, "Popen", _RecordingProc)
        iq_tuner.start(146_940_000, "fm", None)
        try:
            assert _wait_for(lambda: sum(len(c) for c in written) > 3_000)
            # int16 mono: an odd byte count would mean a sample was cut in half.
            assert sum(len(c) for c in written) % 2 == 0
        finally:
            iq_tuner.stop()


def test_every_engine_says_which_one_it_is(tuner, monkeypatch) -> None:
    """`SessionInfo.engine` is a documented part of the PWA contract and drives a
    banner, and it was set on the two LISTENING paths only — so a live spectrum running
    our own I/Q engine reported `rtl_fm`, and `server._watch_spectrum` worked around it
    by reading `session._radio` through a `noqa`.

    Two of the three engines it could name are gone now: B1 deleted the `rtl_power`
    spectrum and W5 the survey. `rtl_fm` remains, deliberately, as the LISTENING
    fallback — and it is the one case where the field still has work to do, because on
    that engine there is no tuning view and nothing else says why."""
    _instant(monkeypatch)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)

    # No SoapySDR in the test image, so the listening path falls back and says so.
    info = tuner.start(146_940_000, "fm", None)
    try:
        assert info.engine == "rtl_fm"
        assert info.as_dict()["engine"] == "rtl_fm"
    finally:
        tuner.stop()


def test_the_audio_carries_the_station_and_not_noise(iq_tuner) -> None:
    """The recovered audio must contain the tone the fake radio is transmitting.

    THIS IS THE TEST THE I/Q LISTEN PATH SHIPPED WITHOUT, and its absence is what let
    a sign error in the offset tuning live in the owner's radio: the station sat
    480 kHz from DC, the demodulator spent its life on empty spectrum, and every other
    check in this file still passed.

    They passed because they measure LEVEL, and level is the one thing an FM
    discriminator cannot report honestly. It differentiates phase and is completely
    blind to amplitude, so fed nothing at all it emits noise AT FULL SCALE — a peak of
    1.0, an RMS of 0.4, a busy level meter, a moving tape and a plausible waterfall.
    `test_the_level_meter_hears_the_carrier` asserts `audio_peak > 0.1`, which pure
    noise satisfies with room to spare.

    The only question that separates a working chain from a dead one is WHAT the audio
    contains. So: a 1 kHz tone, where the fake radio put it."""
    written: list[bytes] = []

    class _Recorder(_Sink):
        def write(self, b: bytes) -> int:
            written.append(bytes(b))
            return len(b)

    class _RecordingProc(_FakeProc):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.stdin = _Recorder()

    rate = demod.AUDIO_RATE
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(listen.subprocess, "Popen", _RecordingProc)
        iq_tuner.start(146_940_000, "fm", None)
        try:
            assert _wait_for(lambda: sum(len(c) for c in written) >= rate * 2)
        finally:
            iq_tuner.stop()
    audio = np.frombuffer(b"".join(written), dtype=np.int16).astype(np.float64)
    audio = audio[len(audio) // 4 :]  # past the filters' start-up transient
    magnitude = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    freqs = np.fft.rfftfreq(audio.size, 1.0 / rate)
    assert freqs[int(np.argmax(magnitude))] == pytest.approx(1_000.0, abs=40.0)
    # ...and it must DOMINATE. An argmax alone can land on the loudest bin of noise.
    tone = magnitude[np.abs(freqs - 1_000.0) < 60.0]
    assert float((tone**2).sum() / (magnitude**2).sum()) > 0.5


def test_the_audio_tap_collects_what_the_demodulator_produced(iq_tuner) -> None:
    """`listen-probe --transcribe` is only worth anything if the tap holds real audio.

    Same assertion as `test_the_audio_carries_the_station_and_not_noise`, through the
    path the probe actually uses: the tap must carry the station, not a plausible
    quantity of bytes."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert session.taken_audio() == b""  # nothing is kept until a probe asks
        session.tap_audio(2.0)
        rate = demod.AUDIO_RATE
        assert _wait_for(lambda: len(session._tap or ()) * listen._CHUNK >= rate)
        pcm = session.taken_audio()
    finally:
        iq_tuner.stop()
    assert len(pcm) % 2 == 0
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    audio = audio[len(audio) // 4 :]
    magnitude = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    freqs = np.fft.rfftfreq(audio.size, 1.0 / rate)
    assert freqs[int(np.argmax(magnitude))] == pytest.approx(1_000.0, abs=40.0)


def test_the_tap_is_bounded_and_off_by_default(iq_tuner) -> None:
    """A tap a failed probe left on must not grow for the life of the session."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert session._tap is None
        session.tap_audio(1.0)
        held = session._tap
        assert held is not None and held.maxlen is not None
        # ~1 s of audio, not unbounded.
        assert held.maxlen * listen._CHUNK <= 4 * demod.AUDIO_RATE * 2
        assert _wait_for(lambda: len(held) == held.maxlen, timeout=6.0)
        # It stops growing rather than eating the session.
        assert len(held) == held.maxlen
    finally:
        iq_tuner.stop()


def test_the_level_meter_hears_the_carrier(iq_tuner) -> None:
    """A silent meter on a fully-modulated carrier is the symptom of every wiring
    mistake in this path at once — wrong offset, dead mixer, filters not connected."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert _wait_for(lambda: session.audio_peak > 0.1)
        assert session.audio_peak <= 1.0
    finally:
        iq_tuner.stop()


def test_the_tuning_row_is_centred_on_the_station(iq_tuner) -> None:
    """Centred on what is being LISTENED to, not on where the radio is pointed.

    The radio sits 240 kHz above the station; the mixer takes that back out, so the
    baseband — and every row measured from it — is about the station."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        sub, _ = session.subscribe_frames()
        frame = sub.get(timeout=5)
        assert frame is not None
        middle = frame.start_hz + (frame.stop_hz - frame.start_hz) / 2
        assert middle == pytest.approx(146_940_000, abs=frame.bin_hz)
    finally:
        iq_tuner.stop()


def test_the_tuning_row_is_cropped_to_twice_the_passband(iq_tuner) -> None:
    """The span the mock settled on, and the passband the picture shades.

    A narrowband channel is 16 kHz of a 48 kHz IF, so the row is the middle 32 kHz —
    which makes the shaded band exactly the middle half, whatever the mode."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        frame = session.subscribe_frames()[0].get(timeout=5)
        assert frame is not None
        assert frame.passband_hz == pytest.approx(16_000.0)
        span = frame.stop_hz - frame.start_hz
        assert span == pytest.approx(2 * frame.passband_hz, rel=0.05)
        assert frame.as_dict()["passband_hz"] == pytest.approx(16_000.0)
    finally:
        iq_tuner.stop()


def test_a_listening_session_is_what_the_frames_route_finds(iq_tuner) -> None:
    """Before this the frames route only looked for a spectrum session, so the one
    surface that most wants a picture was the one that could not be given one."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None and session.draws_frames
        assert iq_tuner.drawing() is session
    finally:
        iq_tuner.stop()


def test_a_box_where_the_iq_engine_will_not_start_still_gets_audio(
    monkeypatch: pytest.MonkeyPatch, tuner
) -> None:
    """CLAUDE.md #10 in a test. The owner has no terminal, so a build where SoapySDR
    cannot open the dongle must not need a revert to get sound back — it drops to
    rtl_fm, keeps working, and SAYS which engine it is on."""
    info = tuner.start(146_940_000, "fm", None)
    try:
        assert info.engine == "rtl_fm"
        assert tuner.drawing() is None  # rtl_fm draws nothing, and does not pretend to
    finally:
        tuner.stop()


def test_the_gain_reaches_the_tuner(iq_tuner) -> None:
    """The defect this path shipped with: it opened the radio and never touched the
    gain, so a listening session ran at whatever librtlsdr left the dongle at —
    MEASURED on the box as 0.0 dB, the bottom of a 0-49.6 dB range. `rtl_fm` hands the
    tuner to its own AGC when no `-g` is given, so anything else is a silent
    regression against the engine this replaces."""
    iq_tuner.start(146_940_000, "fm", None)
    try:
        assert iq_tuner.opened[0].gain_calls == 1
        assert iq_tuner.opened[0].gain_db is None  # None means the radio's own loop
    finally:
        iq_tuner.stop()


def test_a_chosen_gain_wins_over_the_radios_own_loop(iq_tuner) -> None:
    """AGC is the DEFAULT, not the policy: an owner who names a gain gets it, which is
    what makes a weak band workable and what `-g` did before."""
    iq_tuner.start(146_940_000, "fm", "30.0")
    try:
        assert iq_tuner.opened[0].gain_db == 30.0
    finally:
        iq_tuner.stop()


def test_a_retune_never_reopens_the_radio(iq_tuner) -> None:
    """A2, and the strongest form of the claim: the window is GONE, not narrowed.

    The regression the owner hit was a retune while listening kicking the radio to
    idle. `alive` reads the radio when there is one and otherwise falls through to
    `self._rtl.poll()` — and on the I/Q path there is no rtl_fm process to fall through
    to, so between `_kill()` clearing `_radio` and `Radio.open` returning it answered
    False. `_reap` believes that answer, so the status poll the PWA runs every second
    deleted a session that was merely between pipelines. From the box's own log: the
    device reopened cleanly and the next request came back "that session is no longer
    the live one", with the threads still running and the dongle still held.

    Every guard written for that window is still in `_restart` and still needed on the
    engine that still uses it. This path simply does not go there: the radio moves, the
    stream stays, and there is no moment at which this session has no radio."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        held = iq_tuner.opened[0]
        opens = len(iq_tuner.opened)

        session.tune(146_950_000)

        assert len(iq_tuner.opened) == opens, "the retune reopened the radio"
        assert held.retunes == 1
        assert held.closed is False
        assert session.frequency_hz == 146_950_000
        # ...and the session was never for one instant reapable.
        assert iq_tuner.find(info.session_id) is session
        assert session.alive
    finally:
        iq_tuner.stop()


def test_a_retune_moves_the_radio_by_the_offset_it_snapped_to(iq_tuner) -> None:
    """The same subtraction `_start_iq_listen` makes, and it has to be the same one:
    the mixer shifts the spectrum DOWN, so the station has to sit ABOVE the centre. Any
    other sign here and a retune is silence from code that reads correctly."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        chain = listen.demod.Demodulator(
            "fm", listen.LISTEN_CAPTURE_HZ, offset_hz=float(listen.LISTEN_OFFSET_HZ)
        )

        session.tune(145_000_000)

        assert iq_tuner.opened[0].center_hz == 145_000_000 - int(chain.offset_hz)
    finally:
        iq_tuner.stop()


def test_a_retune_into_shortwave_switches_the_branch_without_reopening(
    iq_tuner,
) -> None:
    """Crossing `DIRECT_MAX_HZ` changes the ADC branch AND drops the offset to zero —
    the tuner is powered down there, so there is no LO spike to dodge. Both are
    `Radio.retune` arguments; neither needs a new stream."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        opens = len(iq_tuner.opened)

        session.tune(7_200_000, "am")

        assert len(iq_tuner.opened) == opens
        assert iq_tuner.opened[0].center_hz == 7_200_000  # no offset on the direct path
        assert session.mode == "am"
    finally:
        iq_tuner.stop()


def test_a_retune_that_cannot_be_demodulated_leaves_the_session_alone(
    iq_tuner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is the reverse of `_restart`'s and that is the whole gain: everything
    that can fail happens BEFORE the radio moves. `_restart` kills first and applies
    second, so anything it raises kills a session that was working."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None

        def _no(*_a: Any, **_k: Any):
            raise listen.demod.DemodError("not in this build")

        monkeypatch.setattr(listen.demod, "Demodulator", _no)
        with pytest.raises(listen.RadioUnavailable):
            session.tune(145_000_000)

        assert session.frequency_hz == 146_940_000
        assert iq_tuner.opened[0].retunes == 0
        assert session.alive
    finally:
        iq_tuner.stop()


def test_a_released_session_still_refuses_to_retune(iq_tuner) -> None:
    """`_restart`'s load-bearing guard, kept on the path that no longer goes through it:
    the route resolves the Session outside the tuner's lock, so a `/listen/stop` landing
    in between must not move a radio this session no longer owns."""
    info = iq_tuner.start(146_940_000, "fm", None)
    session = iq_tuner.find(info.session_id)
    assert session is not None
    iq_tuner.stop()

    with pytest.raises(listen.SessionGone):
        session.tune(145_000_000)


def test_a_retune_drops_the_old_stations_rows(iq_tuner) -> None:
    """A viewer attaching after a retune is seeded with "the most recent row" so its
    picture does not open blank. One from before the retune is a picture of somewhere
    else with a plausible axis on it."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        warm, _ = session.subscribe_frames(listen.VIEW_CHANNEL)
        assert warm.get(timeout=5) is not None
        assert session._history

        session.tune(145_000_000)

        assert session._history == {}
    finally:
        iq_tuner.stop()


def test_a_listening_session_draws_the_band_it_is_sitting_in(iq_tuner) -> None:
    """A1/A3: the whole point of the wave. The radio has always captured 2.4 MHz and
    thrown all but 32 kHz of it away; "listen to a station OR look at the band, pick
    one" was a property of the code, not of the hardware.

    `view="all"` because the two pictures now share one stream, and a row says which
    it is rather than a reader guessing from `passband_hz`."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        sub, _ = session.subscribe_frames(listen.VIEW_ALL)
        seen: dict[str, Any] = {}
        end = time.monotonic() + 6.0
        while time.monotonic() < end and len(seen) < 2:
            frame = sub.get(timeout=5)
            assert frame is not None
            seen[frame.view] = frame

        assert set(seen) == {listen.VIEW_BAND, listen.VIEW_CHANNEL}
        band, channel = seen[listen.VIEW_BAND], seen[listen.VIEW_CHANNEL]
        # The band row is the WHOLE capture, centred where the radio is pointed — which
        # is `LISTEN_OFFSET_HZ` below the station, and the row says so rather than
        # pretending to be symmetric about the tuning.
        assert len(band.db) == listen.LISTEN_BAND_BINS
        assert band.stop_hz - band.start_hz == pytest.approx(
            listen.LISTEN_CAPTURE_HZ, rel=1e-6
        )
        middle = band.start_hz + (band.stop_hz - band.start_hz) / 2
        assert middle == pytest.approx(
            146_940_000 - listen.LISTEN_OFFSET_HZ, abs=2 * band.bin_hz
        )
        # ...and the channel row is still the narrow one, off the demodulator's own
        # baseband: 93.75 Hz bins against the band's 586.
        assert channel.bin_hz < band.bin_hz
        assert channel.passband_hz > 0
    finally:
        iq_tuner.stop()


def test_a_band_row_carries_peaks_and_a_channel_row_does_not(iq_tuner) -> None:
    """`peaks.find` answers "what stands above the noise across this BAND", and its
    rolling baseline is meaningless on a 32 kHz row a station fills 40% of. Which row
    gets measured is now decided by what the row IS rather than by whether anyone
    happened to set `passband_hz`.

    At a FIXED gain, which is the other half of the rule — see
    `test_a_band_row_measured_under_agc_carries_no_peaks`."""
    info = iq_tuner.start(146_940_000, "fm", "30")
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        sub, _ = session.subscribe_frames(listen.VIEW_ALL)
        seen: dict[str, Any] = {}
        end = time.monotonic() + 6.0
        while time.monotonic() < end and len(seen) < 2:
            frame = sub.get(timeout=5)
            assert frame is not None
            seen[frame.view] = frame

        assert seen[listen.VIEW_CHANNEL].peaks == []
        # The fake radio puts one carrier on the air, and the band sink is the only
        # thing on this session that can see it as a station among others.
        assert seen[listen.VIEW_BAND].peaks
    finally:
        iq_tuner.stop()


def test_a_viewer_is_handed_only_the_picture_it_asked_for(iq_tuner) -> None:
    """One capture publishing two pictures is only useful if a viewer can take one.
    Anything else and the tuning strip draws band rows half the time."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        band, _ = session.subscribe_frames(listen.VIEW_BAND)
        channel, _ = session.subscribe_frames(listen.VIEW_CHANNEL)
        got_band = [band.get(timeout=5) for _ in range(3)]
        got_channel = [channel.get(timeout=5) for _ in range(3)]

        assert {f.view for f in got_band} == {listen.VIEW_BAND}
        assert {f.view for f in got_channel} == {listen.VIEW_CHANNEL}
    finally:
        iq_tuner.stop()


def test_the_default_view_is_the_one_the_session_always_published(iq_tuner) -> None:
    """A client that never learns about views must see no change at all. That is the
    whole reason `default_view` exists rather than a constant."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert session.default_view == listen.VIEW_CHANNEL
        sub, _ = session.subscribe_frames()
        assert {sub.get(timeout=5).view for _ in range(3)} == {listen.VIEW_CHANNEL}
    finally:
        iq_tuner.stop()


def test_a_spectrum_session_still_defaults_to_the_band(tuner, monkeypatch) -> None:
    swept = listen.Sweep.of(
        144_000_000, 144_400_000, 600, 300, capture=(2_400_000, 4_000)
    )
    session = listen.Session.__new__(listen.Session)
    session.purpose = listen.PURPOSE_SPECTRUM
    session.sweep = swept
    assert session.default_view == listen.VIEW_BAND


def test_a_fresh_viewer_is_seeded_with_the_latest_row_of_its_own_view(
    iq_tuner,
) -> None:
    """Attaching mid-stream used to hand over "the most recent row", which on a session
    drawing two pictures is the wrong one half the time — and a tuning strip opening on
    a 2.4 MHz band row is a blank canvas that looks like a radio that did not start."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        warm, _ = session.subscribe_frames(listen.VIEW_ALL)
        seen = set()
        end = time.monotonic() + 6.0
        while time.monotonic() < end and len(seen) < 2:
            seen.add(warm.get(timeout=5).view)
        assert len(seen) == 2

        # Both pictures have been published, so a seed can now be wrong.
        _late, seed = session.subscribe_frames(listen.VIEW_CHANNEL)
        assert seed and {frame.view for frame in seed} == {listen.VIEW_CHANNEL}
    finally:
        iq_tuner.stop()


def test_a_listening_capture_asks_for_a_deeper_ring_than_a_hopping_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`radio.QUEUE_BUFFERS = 4` was measured for a HOPPING spectrum, where a shallow
    ring is the whole point: whatever is left in it after a retune is pre-retune data.
    A listening session never hops, so the shallow ring buys it nothing and costs it
    41 ms of grace against an ffmpeg stall — and SoapyRTLSDR discards its ENTIRE fifo
    on one overflow event (C9)."""
    _instant(monkeypatch)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
    seen: dict[str, Any] = {}

    def _open(*, center_hz: int, **kwargs: Any) -> _FakeRadio:
        seen.update(kwargs)
        return _FakeRadio(center_hz, station_hz=146_940_000)

    monkeypatch.setattr(listen.radio.Radio, "open", staticmethod(_open))
    tuner = listen.Tuner()
    try:
        tuner.start(146_940_000, "fm", None)
        assert seen["stream_args"] == {"buffers": str(listen.LISTEN_QUEUE_BUFFERS)}
        assert listen.LISTEN_QUEUE_BUFFERS > listen.radio.QUEUE_BUFFERS
    finally:
        tuner.stop()


def test_the_band_is_transformed_only_while_someone_is_watching_it(iq_tuner) -> None:
    """A listening session holds the radio to make AUDIO. The band row is ~11% of one
    core, which is worth paying for a picture the owner is looking at and is pure waste
    for one nobody asked for — so the sink asks before it transforms."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        # A channel viewer is attached, and the band still costs nothing.
        channel, _ = session.subscribe_frames(listen.VIEW_CHANNEL)
        for _ in range(3):
            assert channel.get(timeout=5).view == listen.VIEW_CHANNEL
        assert listen.VIEW_BAND not in session._history

        band, _ = session.subscribe_frames(listen.VIEW_BAND)
        assert band.get(timeout=5).view == listen.VIEW_BAND

        # ...and it stops again when the last band viewer goes.
        session.unsubscribe_frames(band)
        assert not session._wants_view(listen.VIEW_BAND)
    finally:
        iq_tuner.stop()


def test_a_retune_keeps_the_very_same_encoder(
    iq_tuner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2's payoff, stated as the thing the owner actually experiences.

    MP3 is a sequence of self-describing frames and every mode demodulates to the same
    `AUDIO_RATE`, so the encoder does not care that the station changed — and a listener
    already attached hears a click rather than the silence of a relaunch. Nothing about
    the capture changes either: 2 400 000 samples a second for every mode is the
    property `demod.IF_RATE_HZ` was chosen around."""
    made: list[Any] = []
    real = listen.subprocess.Popen

    def _watched(*a: Any, **k: Any) -> Any:
        proc = real(*a, **k)
        made.append(proc)
        return proc

    monkeypatch.setattr(listen.subprocess, "Popen", _watched)
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert _wait_for(lambda: made and made[0].stdin.written > 0)
        before = made[0].stdin.written

        session.tune(146_520_000, "wbfm")

        assert len(made) == 1, "the retune relaunched the encoder"
        assert made[0].stdin.closed is False
        # ...and it is still being fed, through a demodulator built for the new mode.
        assert _wait_for(lambda: made[0].stdin.written > before)
        assert session.mode == "wbfm"
    finally:
        iq_tuner.stop()


def test_rtl_fm_still_rebuilds_because_it_really_cannot_be_retuned(
    tuner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scope boundary of A2, asserted rather than assumed.

    `rtl_fm` takes its frequency on the command line and has no control channel, so a
    retune there is a new process — and `_restart`, with every guard its docstring
    records, is what this engine still needs. Keeping the SESSION across it is the part
    that has to hold either way: listeners stay attached, and the end-of-stream sentinel
    means the session ended, never that its pipeline was replaced."""
    made: list[Any] = []
    real = listen.subprocess.Popen

    def _watched(*a: Any, **k: Any) -> Any:
        proc = real(*a, **k)
        made.append(proc)
        return proc

    monkeypatch.setattr(listen.subprocess, "Popen", _watched)
    info = tuner.start(146_940_000, "fm", None)
    try:
        session = tuner.find(info.session_id)
        assert session is not None
        assert session.engine == "rtl_fm"
        assert session._capture is None  # nothing to move
        listener = session.subscribe()
        made.clear()

        session.tune(146_520_000)

        assert len(made) >= 2, "rtl_fm and its encoder are both replaced"
        assert session.frequency_hz == 146_520_000
        assert tuner.find(info.session_id) is session
        # The listener was never told the stream ended.
        assert None not in list(listener.queue)
    finally:
        tuner.stop()


def test_a_listening_session_leaves_the_tuner_to_its_own_loop(iq_tuner) -> None:
    """Loudness is the point when someone is listening, and a gain that moves costs
    nothing there — which is exactly why it is not the same answer a measuring session
    needs."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert session.tuner_gain_db is None
        assert iq_tuner.opened[0].gain_db is None
    finally:
        iq_tuner.stop()


def test_the_owners_gain_wins_on_every_purpose(iq_tuner) -> None:
    info = iq_tuner.start(146_940_000, "fm", "40")
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        assert session.tuner_gain_db == 40.0
    finally:
        iq_tuner.stop()


def test_a_band_row_measured_under_agc_carries_no_peaks(iq_tuner) -> None:
    """MEASURED ON AIR 2026-09-06, the first hour this file could draw a band while
    listening: 162.550 with the tuner on its own loop ran to -7.0 dBFS, and the R820T2's
    front end answered with a symmetric spur comb at ±55.5, 111, 166 and 222 kHz.
    `peaks.find` did its job perfectly and reported SEVEN stations, none of them on
    NOAA's 25 kHz raster. The same radio pinned at 30 dB found one, agreeing with a
    spectrum session over the same span to within 1.5 dB.

    The picture is still worth drawing — its levels are relative and it says so. Its
    PEAKS are a measurement that reaches the agent's tools as fact, and under a moving
    gain they are a reading of the receiver rather than of the air."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        sub, _ = session.subscribe_frames(listen.VIEW_BAND)
        row = sub.get(timeout=5)
        assert row is not None
        assert row.view == listen.VIEW_BAND
        assert row.db  # the picture is there...
        assert row.peaks == []  # ...and the measurement is not
    finally:
        iq_tuner.stop()


def test_a_band_row_at_a_fixed_gain_is_measured(iq_tuner) -> None:
    """The other half: pinning the tuner is what turns the same row into a reading."""
    info = iq_tuner.start(146_940_000, "fm", "30")
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        sub, _ = session.subscribe_frames(listen.VIEW_BAND)
        row = sub.get(timeout=5)
        assert row is not None
        assert row.peaks
    finally:
        iq_tuner.stop()


def test_a_measuring_session_is_always_pinned_even_with_no_gain_asked_for(
    tuner, monkeypatch
) -> None:
    """A picture whose dB scale is a property of whatever the last session left behind
    is one where no two rows, and no two runs, mean the same thing."""
    swept = listen.Sweep.of(
        144_000_000, 144_400_000, 600, 300, capture=(2_400_000, 4_000)
    )
    session = listen.Session.__new__(listen.Session)
    session.purpose = listen.PURPOSE_SPECTRUM
    session.gain = None
    session.sweep = swept

    assert session.tuner_gain_db == listen.MEASURING_GAIN_DB


def test_a_retune_the_radio_refuses_ends_the_session_rather_than_mistuning_it(
    iq_tuner,
) -> None:
    """`Radio.retune` sets rate, branch and frequency IN ORDER, so a failure partway
    leaves the radio somewhere nobody asked for. A receiver that keeps demodulating a
    frequency it is not on is the silent failure this whole wave has been peeling —
    `_restart` ends the session when its relaunch fails, and so does this."""
    info = iq_tuner.start(146_940_000, "fm", None)
    session = iq_tuner.find(info.session_id)
    assert session is not None
    held = iq_tuner.opened[0]

    def _no(**_k: Any) -> int:
        raise listen.radio.RadioError("the radio stopped answering")

    held.retune = _no  # type: ignore[method-assign]

    with pytest.raises(listen.RadioUnavailable, match="stopped answering"):
        session.tune(145_000_000)

    assert held.closed is True
    assert iq_tuner.find(info.session_id) is None


def test_an_SSB_row_shades_the_sideband_it_can_actually_HEAR(iq_tuner) -> None:
    """C14. `usb` hears +300..+3400 Hz and `lsb` hears -3400..-300, but the strip drew
    `2 * channel_half_hz` — symmetric ±3400 — so half the shaded box was the sideband
    the back end rejects and the top 300 Hz of the real one fell outside it. Someone
    centring a signal in that box put half of it where nothing can hear it.

    The CROP stays centred on the dial, because "am I centred?" is a question about the
    dial. Only the shading moves."""
    for mode, want_centre in (("usb", 1_850.0), ("lsb", -1_850.0)):
        info = iq_tuner.start(14_250_000, mode, None)
        try:
            session = iq_tuner.find(info.session_id)
            assert session is not None
            frame = session.subscribe_frames(listen.VIEW_CHANNEL)[0].get(timeout=5)
            assert frame is not None
            # 3100 Hz wide, not 6800: the two edges, not a half-width doubled.
            assert frame.passband_hz == pytest.approx(3_100.0)
            assert frame.passband_centre_hz == pytest.approx(want_centre)
            wire = frame.as_dict()
            assert wire["passband_centre_hz"] == pytest.approx(want_centre)
            # ...and the row itself still straddles the tuned frequency.
            middle = frame.start_hz + (frame.stop_hz - frame.start_hz) / 2
            assert middle == pytest.approx(14_250_000, abs=frame.bin_hz)
        finally:
            iq_tuner.stop()


def test_a_symmetric_mode_shades_exactly_where_it_always_did(iq_tuner) -> None:
    """The other half of C14, and what makes it safe: every mode but SSB reports a zero
    centre, so a strip that adds the field draws the same picture it drew before — and a
    PWA that has not been updated yet reads no field and does the same."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        frame = session.subscribe_frames(listen.VIEW_CHANNEL)[0].get(timeout=5)
        assert frame is not None
        assert frame.passband_hz == pytest.approx(16_000.0)
        assert frame.passband_centre_hz == 0.0
        assert (frame.stop_hz - frame.start_hz) == pytest.approx(32_000.0, rel=0.05)
    finally:
        iq_tuner.stop()


class TestTheWireFormIsBuiltOnce:
    """C23. `as_dict` rounds every bin in a Python comprehension, and the frames route
    called it PER SUBSCRIBER PER FRAME — the exact per-row cost `iq.py` says it removed
    with `np.round`, still paid here and multiplied by however many people are watching.
    A frame is frozen and every subscriber gets the same bytes."""

    def _frame(self) -> Any:
        return listen.Frame(
            at=1.0, start_hz=144_000_000, bin_hz=93.75, db=[-70.04] * 512
        )

    def test_two_readers_get_the_same_object_rather_than_two_equal_ones(self) -> None:
        frame = self._frame()

        assert frame.as_dict() is frame.as_dict()

    def test_the_answer_is_the_same_one_it_always_was(self) -> None:
        """A cache that changed the answer would be a worse defect than the cost."""
        wire = self._frame().as_dict()

        assert wire["db"][0] == -70.0
        assert wire["bins"] == 512
        assert wire["start_hz"] == 144_000_000

    def test_a_frame_derived_from_another_does_not_inherit_its_cache(self) -> None:
        """`_publish_frame` uses `dataclasses.replace` to add peaks to a frame that has
        often already been serialised. Carrying the cache across would publish the
        PRE-PEAKS answer with the peaks silently missing — a row that says a quiet band,
        which is a real answer and so unfalsifiable from outside."""
        before = self._frame()
        before.as_dict()

        after = dataclasses.replace(before, peaks=[{"hz": 144_000_000, "db": -30.0}])

        assert after.as_dict()["peaks"] == [{"hz": 144_000_000, "db": -30.0}]
        assert before.as_dict()["peaks"] == []


def test_every_row_says_what_GAIN_it_was_measured_at(iq_tuner) -> None:
    """C22. `db` is dBFS, and dBFS is comparable only against the same gain and the same
    `bin_hz`. `bin_hz` was always on the row; the gain was not, so a floor from an older
    run was silently incomparable with this one — the same class as C18, one field up.

    None is a real answer and not an absence: it means the radio's own loop was running,
    where the absolute level means nothing between rows at all."""
    info = iq_tuner.start(146_940_000, "fm", "24.0")
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        frame = session.subscribe_frames()[0].get(timeout=5)
        assert frame is not None

        assert frame.gain_db == pytest.approx(24.0)
        assert frame.as_dict()["gain_db"] == pytest.approx(24.0)
    finally:
        iq_tuner.stop()


def test_a_row_measured_under_the_radios_OWN_loop_says_so(iq_tuner) -> None:
    """`None`, not a number: a listening session leaves the gain automatic, and
    reporting whatever the driver last stored would be the C26 mistake one layer up."""
    info = iq_tuner.start(146_940_000, "fm", None)
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        frame = session.subscribe_frames()[0].get(timeout=5)
        assert frame is not None

        assert frame.gain_db is None
        assert frame.as_dict()["gain_db"] is None
    finally:
        iq_tuner.stop()


def test_a_hop_does_not_pay_a_settle_this_radio_does_not_need(iq_tuner) -> None:
    """C29. At the top of the hop ladder a 30 MHz row took ~1 s — 1.0 fps, which is
    exactly `rtl_power`'s own clamp, the ceiling this engine exists to remove. The
    cost is per-RETUNE: sixteen `setFrequency` + settle pairs, the settle 30 ms each.

    MEASURED ON THE BOX at a FIXED gain, seven trials: settle 0.0 ms, worst 0.0 ms,
    steady level holding to 0.091 dB. A spectrum session runs at a fixed gain BY
    CONSTRUCTION, so that is the reading that applies — and 16 x 30 ms of the second was
    being discarded for a transient this radio does not have."""
    info = iq_tuner.start(
        144_000_000,
        "fm",
        None,
        purpose=listen.PURPOSE_SPECTRUM,
        sweep=listen.Sweep.of(
            144_000_000, 148_000_000, 9_375, 60, capture=(2_400_000, 256), hops=2
        ),
    )
    try:
        session = iq_tuner.find(info.session_id)
        assert session is not None
        session.subscribe_frames()[0].get(timeout=5)
    finally:
        iq_tuner.stop()

    asked = [s for made in iq_tuner.opened for s in made.settles]
    assert asked, "the hop never retuned"
    assert all(s == listen.radio.HOP_SETTLE_S for s in asked), asked
    # ...and the LISTENING path keeps the number that was measured under its own AGC,
    # which is what moves the level there (1.47 dB sigma against 0.09 fixed).
    assert listen.radio.SETTLE_S > listen.radio.HOP_SETTLE_S


def test_a_hop_integrates_long_enough_to_see_past_the_MODULATION() -> None:
    """The owner's report, as arithmetic. `HOP_SEGMENTS` decides how much signal each
    slice of a stitched row is made of, and at four segments it was 0.43 ms — while a
    wideband-FM carrier sweeps its own ±75 kHz of deviation continuously.

    A look that short does not measure where a station is; it measures where the
    modulation happened to be. On the dial that drew a striped waterfall and put peaks
    ±60 kHz off their channels, so one station arrived as several signals and the held
    list filled with pills that went stale as fast as they appeared.

    The number that matters is the DWELL, not the segment count, so that is what this
    checks — a future change to the bin count must not quietly undo it."""
    rate_hz, bins = 2_400_000, 256
    dwell_ms = bins * listen.HOP_SEGMENTS / rate_hz * 1000.0

    assert dwell_ms >= 5.0, f"{dwell_ms:.2f} ms is a snapshot of the modulation"
    # ...and bounded, because a row is still a row: past this the hop is a long enough
    # exposure that a burst inside it is smeared rather than seen.
    assert listen.HOP_SEGMENTS <= listen.MAX_IQ_SEGMENTS


def test_the_encoded_read_is_sized_in_TIME_and_not_in_pcm_bytes() -> None:
    """`read()` blocks until its buffer is FULL, so the chunk size IS the delay.

    It was `_CHUNK` — 4096 bytes, sized as 128 ms of signed-16-bit 16 kHz PCM, which is
    a sensible granularity — reused on the compressed side, where the same 4096 bytes
    is 512 ms at 64 kbps. Every listener therefore waited half a second for audio the
    encoder had already finished with, on top of whatever the browser buffered. The
    number looked like the quantity and was not.
    """
    per_read_s = listen.AUDIO_CHUNK / (listen.AUDIO_BITRATE_BPS / 8)

    assert per_read_s == pytest.approx(listen.AUDIO_CHUNK_S, abs=0.005)
    assert per_read_s <= 0.1
    # The trap this replaces, still true of the PCM chunk: the SAME byte count is a
    # different duration on each side of the encoder.
    assert listen._CHUNK / (listen.AUDIO_BITRATE_BPS / 8) > 0.4


def test_a_stalled_listener_is_dropped_after_seconds_not_half_a_minute() -> None:
    """The queue is documented as holding a subscriber that is BRIEFLY behind.

    At 64 chunks of half a second it held 32 s, which is not brief by any reading of
    "live audio is worthless late" — and a phone coming back from a lock screen would
    play out half a minute of stale radio before reaching the air.
    """
    held_s = listen._SUB_QUEUE_CHUNKS * listen.AUDIO_CHUNK_S

    assert 2.0 <= held_s <= 8.0


class TestWhichRadioIsDrawing:
    """`drawing()` picks the session a viewer is attached to, and on a two-dongle box
    picking it by PREFERENCE rather than by name is a bug the owner sees as a dead
    surface: with a spectrum on one radio and the tuner on the other, every viewer —
    including the tuner's own channel strip — was handed the spectrum session and waited
    for rows it does not draw."""

    class _Drawing:
        """A session as `drawing()` reads it: a serial, a purpose, and whether it
        draws anything at all."""

        def __init__(self, serial: str, purpose: str, draws: bool = True) -> None:
            self.serial = serial
            self.purpose = purpose
            self.draws_frames = draws

    def _tuner(self, monkeypatch, sessions):
        tuner = listen.Tuner.__new__(listen.Tuner)
        monkeypatch.setattr(type(tuner), "sessions", lambda _self: sessions)
        return tuner

    def test_a_named_radio_gets_ITS_session_and_not_the_preferred_one(
        self, monkeypatch
    ) -> None:
        watching = self._Drawing("BBB", listen.PURPOSE_SPECTRUM)
        listening = self._Drawing("AAA", listen.PURPOSE_LISTEN)
        tuner = self._tuner(monkeypatch, [listening, watching])

        assert tuner.drawing(serial="AAA") is listening
        assert tuner.drawing(serial="BBB") is watching

    def test_a_named_radio_drawing_nothing_is_None_rather_than_someone_elses_picture(
        self, monkeypatch
    ) -> None:
        # The wrong picture is worse than none: every row would draw correctly, at
        # frequencies the owner is not listening to, with nothing saying so.
        watching = self._Drawing("BBB", listen.PURPOSE_SPECTRUM)
        idle = self._Drawing("AAA", listen.PURPOSE_APRS, draws=False)
        tuner = self._tuner(monkeypatch, [idle, watching])

        assert tuner.drawing(serial="AAA") is None

    def test_with_no_name_it_still_prefers_the_spectrum_session(
        self, monkeypatch
    ) -> None:
        # Unchanged for every caller that predates two radios.
        watching = self._Drawing("BBB", listen.PURPOSE_SPECTRUM)
        listening = self._Drawing("AAA", listen.PURPOSE_LISTEN)
        tuner = self._tuner(monkeypatch, [listening, watching])

        assert tuner.drawing() is watching


# -- filter bandwidth ----------------------------------------------------------------


def _idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Session that never opens a radio: the fields under test are set before any
    engine starts, and starting one here would only test the fake."""
    _instant(monkeypatch)
    monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
    monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)


def test_a_session_defaults_to_the_modes_widest_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that never mentions bandwidth gets exactly what it always got."""
    _idle(monkeypatch)
    session = listen.Session(5_000_000, "am", None)
    session.stop()
    assert session.bandwidth_hz == listen.bandwidths_for("am")[0] == 8_000
    info = session.info()
    assert info.bandwidth_hz == 8_000
    # The ladder travels WITH the session: a PWA holding its own copy would offer
    # widths a redeployed box had stopped accepting.
    assert info.bandwidths_hz == (8_000, 6_000, 4_000, 3_000)
    assert info.as_dict()["bandwidths_hz"] == [8_000, 6_000, 4_000, 3_000]


def test_a_width_that_is_not_on_the_ladder_is_refused() -> None:
    """Refused, never clamped.

    Clamping would leave the radio listening at a width other than the one on screen,
    and a filter doing something other than what the owner believes is exactly the
    failure this control exists to end."""
    with pytest.raises(listen.SdrError) as bad:
        listen.Session(5_000_000, "am", None, bandwidth_hz=5_000)
    assert "8000" in str(bad.value)
    # Anything that is not a number: the value comes off a JSON body, so a list or a
    # dict reaches this function as readily as an int does, and `int()` of one raises
    # TypeError rather than the sentence the owner needs.
    for junk in ("wide", [8000], {"hz": 8000}, object()):
        with pytest.raises(listen.SdrError):
            listen.Session(5_000_000, "am", None, bandwidth_hz=junk)  # type: ignore[arg-type]
    # `True` is an `int` to Python and would otherwise sail through as 1 Hz, refused for
    # the wrong reason and with a message naming a width nobody asked for.
    with pytest.raises(listen.SdrError, match="not a filter width"):
        listen.Session(5_000_000, "am", None, bandwidth_hz=True)  # type: ignore[arg-type]


def test_a_spectrum_session_reports_no_bandwidth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stare tunes to a SPAN, so there is no channel for a filter to be.

    Zero rather than the mode's default, so a client can tell "no filter here" from
    "the narrowest one" and put no bandwidth control on a screen that cannot use it."""
    _idle(monkeypatch)
    sweep = listen.Sweep.of(
        144_000_000, 144_200_000, 25_000, 60, capture=(2_400_000, 512)
    )
    # The engine stubbed out: a spectrum session needs SoapySDR, and what is under test
    # is what `info()` REPORTS, which is decided before anything opens a radio.
    monkeypatch.setattr(listen.Session, "_start_pipeline", lambda self: None)
    # ...and the health check that follows it, which has no engine to confirm.
    monkeypatch.setattr(listen.Session, "_confirm_started", lambda self: None)
    session = listen.Session(
        0, "fm", None, purpose=listen.PURPOSE_SPECTRUM, sweep=sweep
    )
    info = session.info()
    assert info.bandwidth_hz == 0
    assert info.bandwidths_hz == ()


def test_the_width_survives_a_retune_and_resets_on_a_mode_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrow filter is a decision about a crowded BAND, not about one station.

    Re-picking it at every step of the dial would make it useless exactly where it is
    needed. But the ladders differ per mode, so carrying a width into a mode with no
    such rung would refuse an ordinary mode-button press — which is not where the owner
    asked for anything about bandwidth — so a mode change falls back to that mode's
    default instead."""
    _idle(monkeypatch)
    session = listen.Session(5_000_000, "am", None, bandwidth_hz=4_000)
    # `_restart` stubbed to just apply: this is about which width the rules choose, and
    # rebuilding a pipeline around it would only exercise the fake process.
    with mock.patch.object(listen.Session, "_restart", lambda self, apply: apply()):
        session.tune(5_010_000)
        assert session.bandwidth_hz == 4_000, "the width should follow the dial"
        session.tune(5_010_000, "usb")
        assert session.bandwidth_hz == listen.bandwidths_for("usb")[0] == 3_100
        # ...and an explicit width still wins over both rules.
        session.tune(5_010_000, "usb", 1_800)
        assert session.bandwidth_hz == 1_800


def test_bandwidths_for_comes_from_the_demodulator() -> None:
    """One source of truth. A copy in `listen` would be a second place to forget a rung,
    and the failure would be a control offering a width the box then refuses."""
    for mode, ladder in demod.BANDWIDTH_HZ.items():
        assert listen.bandwidths_for(mode) == tuple(ladder)
