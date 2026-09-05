"""The sdr sidecar's listening session — the lease, made testable.

`deploy/sdr/` is not an installed package, so it is loaded by path here. What these
cover is the arbitration and the shape the omnibox tuner reads, without a radio: the
pipeline itself needs hardware, so it is faked at the subprocess seam.
"""

from __future__ import annotations

import importlib.util
import queue
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
    def write(self, _b: bytes) -> int:
        return len(_b)

    def flush(self) -> None: ...

    def close(self) -> None: ...


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


class TestSurveySession:
    """A band sweep, as a real lease.

    The point of making this a `Session` at all is that the omnibox icon, the elapsed
    clock, Release and the 409 semantics ALL read the session's existence. A sweep that
    held the radio outside the lease would be a radio held by something invisible —
    which is the failure `PURPOSE_APRS` was introduced to prevent, one purpose earlier.
    """

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        tuner = listen.Tuner()
        yield tuner
        tuner.stop()

    def _sweep(self) -> Any:
        return listen.Sweep.of(144_000_000, 148_000_000, 5_000, 300)

    def test_a_sweep_holds_the_lease_like_any_other_session(self, tuner) -> None:
        info = tuner.start(
            144_000_000, "fm", None, purpose=listen.PURPOSE_SURVEY, sweep=self._sweep()
        )

        assert info.as_dict()["purpose"] == listen.PURPOSE_SURVEY
        # The thing the omnibox reads. Without it there is no icon, and
        # nothing for Release to target.
        assert tuner.current() is not None

    def test_a_second_session_is_refused_while_a_sweep_runs(self, tuner) -> None:
        tuner.start(
            144_000_000, "fm", None, purpose=listen.PURPOSE_SURVEY, sweep=self._sweep()
        )

        with pytest.raises(listen.SdrBusy) as refused:
            tuner.start(99_300_000, "wbfm", None)

        # And it says WHICH job has it, because the answer the owner needs differs.
        assert "sweeping" in str(refused.value)

    def test_a_sweep_cannot_STEAL_the_radio_from_APRS_logging(self, tuner) -> None:
        """The direction that matters, and the one the first test missed.

        A sweep is the newest purpose and the one an agent asks for on its own
        initiative, so the dangerous case is not "a sweep is running, refuse a listen" —
        it is "the box has been logging APRS for a week, and something quietly takes the
        radio away to look at 70cm". The lease has to refuse a survey exactly as it
        refuses everything else."""
        tuner.start(144_390_000, "fm", None, purpose=listen.PURPOSE_APRS)

        with pytest.raises(listen.SdrBusy) as refused:
            tuner.start(
                440_000_000,
                "fm",
                None,
                purpose=listen.PURPOSE_SURVEY,
                sweep=self._sweep(),
            )

        assert "logging APRS" in str(refused.value)
        # And the APRS session is untouched: a refused sweep must not disturb it.
        held = tuner.current()
        assert held is not None and held.purpose == listen.PURPOSE_APRS

    def test_the_owner_can_take_the_radio_back_mid_sweep(self, tuner) -> None:
        tuner.start(
            144_000_000, "fm", None, purpose=listen.PURPOSE_SURVEY, sweep=self._sweep()
        )

        assert tuner.stop() is True
        assert tuner.current() is None

    def test_a_survey_without_a_range_is_refused(self, tuner) -> None:
        # A survey session with no sweep would start rtl_power with no arguments and
        # hold the radio doing nothing.
        with pytest.raises(listen.SdrError):
            tuner.start(144_000_000, "fm", None, purpose=listen.PURPOSE_SURVEY)

    def test_the_command_is_rtl_power_over_the_range(self, monkeypatch) -> None:
        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _FakeProc)
        session = listen.Session(
            144_000_000, "fm", "30", purpose=listen.PURPOSE_SURVEY, sweep=self._sweep()
        )
        try:
            cmd = session._sweep_cmd()
        finally:
            session.stop()

        assert cmd[0] == "rtl_power"
        assert "144000000:148000000:5000" in cmd
        assert cmd[cmd.index("-e") + 1] == "300"
        # Fixed gain, never AGC: a floor that moves with the signal makes dB values
        # incomparable across the sweep, and every threshold built on them drifts.
        assert cmd[cmd.index("-g") + 1] == "30"


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

    def test_sweeping_opens_the_named_radio(self, monkeypatch) -> None:
        session = self._session(
            monkeypatch,
            purpose=listen.PURPOSE_SURVEY,
            sweep=self._sweep(),
            serial="77192819",
        )
        try:
            cmd = session._sweep_cmd()
        finally:
            session.stop()

        assert cmd[cmd.index("-d") + 1] == "77192819"
        # ...and the CSV path stays last, where rtl_power expects its positional.
        assert cmd[-1].endswith(".csv")

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
        sweeping = self._session(
            monkeypatch, purpose=listen.PURPOSE_SURVEY, sweep=self._sweep()
        )
        try:
            assert "-d" not in listening._rtl_cmd()
            assert "-d" not in sweeping._sweep_cmd()
        finally:
            listening.stop()
            sweeping.stop()

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
        assert tuner.for_purpose(listen.PURPOSE_SURVEY) is None

    def test_current_prefers_the_tuner_over_a_service(self, tuner) -> None:
        """The omnibox draws ONE icon, so `current` has to pick — and pick the same way
        twice, or "which session is showing" changes between two reads that changed
        nothing. Order is what a person is most likely asking about.

        The listening session is on the HIGHER serial deliberately. With it on the lower
        one, `min` by (priority, serial) and plain serial order agree, and an earlier
        cut of this test passed with the priority map deleted — proving only that
        sorting happened. This is the rule the whole PWA reads through
        `/api/sdr/status.listening`, so it has to be the thing under test."""
        tuner.start(
            144_390_000, "fm", None, purpose=listen.PURPOSE_APRS, serial="09022796"
        )
        listening = tuner.start(146_520_000, "fm", None, serial="77192819")

        assert tuner.current().id == listening.session_id
        # ...and naming a radio asks about that radio, not about the box.
        assert tuner.current("09022796").purpose == listen.PURPOSE_APRS
        assert tuner.current("nosuchserial") is None

    def test_current_breaks_a_TIE_by_serial(self, tuner) -> None:
        """Priority first, serial only to break a tie — so two sessions of the same
        purpose still answer the same way on every read."""
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

    def test_a_sweep_refuses_to_go_there_and_says_why(self) -> None:
        """Not a policy. `rtl_power -D` hardcodes direct sampling mode 1 — the I branch
        — so on this board it would tune something and measure nothing. A flat,
        plausible, meaningless waterfall is worse than a refusal."""
        with pytest.raises(listen.SdrError) as refused:
            listen.Sweep.of(7_000_000, 7_300_000, 1_000, 60)

        assert "cannot go below" in str(refused.value)
        assert "still listen" in str(refused.value)

    def test_the_SAME_range_is_accepted_for_the_engine_that_can_see_it(self) -> None:
        """The whole of F8, in one pair of calls. The refusal above is `rtl_power`'s and
        was never the radio's: the live spectrum does its own FFT off raw I/Q and sets
        direct sampling mode 2 — the ADC branch this board wires — so 40 m is a picture
        for that engine and still not a survey for the tool."""
        sweep = listen.Sweep.of(7_000_000, 7_300_000, 250, 60, direct_ok=True)

        assert sweep.start_hz == 7_000_000

    def test_even_the_direct_path_stops_at_what_the_ADC_reaches(self) -> None:
        """`direct_ok` moves the floor, it does not remove it. Below DIRECT_MIN_HZ the
        board's diplexer feeds the ADC nothing at all."""
        with pytest.raises(listen.SdrError) as refused:
            listen.Sweep.of(50_000, 200_000, 250, 60, direct_ok=True)

        assert "below what this radio reaches" in str(refused.value)

    def test_a_sweep_above_the_tuner_is_unaffected(self) -> None:
        sweep = listen.Sweep.of(144_000_000, 148_000_000, 5_000, 60)
        assert sweep.start_hz == 144_000_000


# --- the live spectrum ----------------------------------------------------------


def _row(stamp: str, low: int, step: int, *db: float) -> str:
    """One rtl_power CSV line, in the shape the tool actually writes."""
    high = low + len(db) * step
    values = ", ".join(f"{v:.2f}" for v in db)
    return f"{stamp}, {low}, {high}, {step:.2f}, 12, {values}"


class TestStitchingRowsIntoFrames:
    """rtl_power's rows back into whole waterfall rows, with no radio in sight.

    This is where the awkward cases live, and all of them are text: a sweep wider than
    the radio's window arrives as several rows per interval, they do not arrive in band
    order, and a row can be torn or lost while the tool is still writing. `Stitch` is
    pure so every one of those can be provoked exactly rather than waited for.
    """

    def test_one_block_per_interval_is_one_frame(self) -> None:
        """The common case — a span inside the radio's own window — and the one the
        learned width pays off hardest on: once the first interval has shown that a
        frame is one row, every row after it is a finished frame the moment it lands."""
        stitch = listen.Stitch()

        first = stitch.push(
            _row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0, -71.0)
        )
        second = stitch.push(
            _row("2026-09-04, 13:00:01", 144_000_000, 25_000, -60.0, -61.0)
        )

        assert first == []  # nothing yet knows how wide a frame is
        assert [f.db for f in second] == [[-70.0, -71.0], [-60.0, -61.0]]

    def test_a_wide_sweep_is_stitched_into_one_frame_in_band_order(self) -> None:
        """The blocks do NOT arrive low-to-high, and a waterfall drawn in arrival order
        is a picture with its halves swapped."""
        stitch = listen.Stitch()

        stitch.push(_row("2026-09-04, 13:00:00", 144_100_000, 25_000, -60.0, -61.0))
        stitch.push(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0, -71.0))
        frames = stitch.push(
            _row("2026-09-04, 13:00:01", 144_000_000, 25_000, -70.0, -71.0)
        )

        assert len(frames) == 1
        assert frames[0].start_hz == 144_000_000
        assert frames[0].db == [-70.0, -71.0, -60.0, -61.0]

    def test_once_the_width_is_known_a_frame_lands_without_waiting_a_second(
        self,
    ) -> None:
        """The whole reason the width is learned: waiting for the NEXT interval to prove
        a frame complete costs every frame a second of latency."""
        stitch = listen.Stitch()
        stitch.push(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0))
        stitch.push(_row("2026-09-04, 13:00:00", 144_025_000, 25_000, -60.0))
        stitch.push(
            _row("2026-09-04, 13:00:01", 144_000_000, 25_000, -71.0)
        )  # learns 2

        # The second block of the SECOND interval completes it on arrival.
        frames = stitch.push(_row("2026-09-04, 13:00:01", 144_025_000, 25_000, -61.0))

        assert len(frames) == 1
        assert frames[0].db == [-71.0, -61.0]

    def test_a_dropped_block_costs_one_short_frame_not_every_frame_after_it(
        self,
    ) -> None:
        """`max` in `_flush`, and it is load-bearing: a plain assignment would teach the
        eager path the SHORT width, and every frame after a single lost row would be cut
        to match — a waterfall that silently loses half its band and never recovers."""
        stitch = listen.Stitch()
        stitch.push(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0))
        stitch.push(_row("2026-09-04, 13:00:00", 144_025_000, 25_000, -60.0))
        stitch.push(_row("2026-09-04, 13:00:01", 144_000_000, 25_000, -71.0))

        # Interval :01 loses its second block entirely; :02 arrives whole.
        short = stitch.push(_row("2026-09-04, 13:00:02", 144_000_000, 25_000, -72.0))
        whole = stitch.push(_row("2026-09-04, 13:00:02", 144_025_000, 25_000, -62.0))

        assert len(short) == 1 and short[0].db == [-71.0]
        assert len(whole) == 1 and whole[0].db == [-72.0, -62.0]

    def test_a_repeated_block_ends_the_frame_even_if_the_clock_did_not(self) -> None:
        """Belt and braces for a loaded box stamping two intervals alike. Without it the
        second reading would overwrite the first and the frame would never complete."""
        stitch = listen.Stitch()
        stitch.push(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0))
        frames = stitch.push(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -80.0))

        # Two readings of one block are two intervals, whatever the clock said.
        assert [f.db for f in frames] == [[-70.0], [-80.0]]

    def test_a_torn_line_is_skipped_not_raised(self) -> None:
        # This parses text a radio is still writing. One lost row must not end the
        # picture.
        stitch = listen.Stitch()
        assert stitch.push("2026-09-04, 13:00:00, 1440000") == []
        assert stitch.push("") == []
        assert stitch.push("2026-09-04, 13:00:00, x, y, z, 12, -70.0") == []

    def test_a_frame_addresses_its_own_bins(self) -> None:
        """`start_hz + i * bin_hz` has to land on bin i, whatever rtl_power reported
        about the block edges — the renderer has no other way to place a column."""
        stitch = listen.Stitch()
        stitch.push(
            _row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0, -71.0, -72.0)
        )
        frames = stitch.push(_row("2026-09-04, 13:00:01", 144_000_000, 25_000, -70.0))

        frame = frames[0]
        assert frame.stop_hz == frame.start_hz + len(frame.db) * frame.bin_hz
        assert frame.as_dict()["bins"] == 3


class _Pipe:
    """One process's stdout. Ends when the process is killed, as a real pipe does."""

    def __init__(self) -> None:
        self._q: queue.Queue[bytes | None] = queue.Queue()

    def put(self, line: str) -> None:
        self._q.put(line.encode() + b"\n")

    def close(self) -> None:
        self._q.put(None)

    def read(self, _n: int = 0) -> bytes:
        return b""

    def __iter__(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item


class _Feed:
    """rtl_power's stdout, driven by the test: `emit()` a line, the pump reads it.

    A queue rather than a fixed list of rows, so a test can attach a viewer BEFORE any
    row exists and then provoke exactly the one it wants — no sleeps anywhere.

    A FRESH pipe per launch, and `emit` always writes to the newest. A retune relaunches
    the process, and a single shared queue would leave the old pump thread sitting on it
    beside the new one, splitting the rows between them at random."""

    def __init__(self) -> None:
        self.pipes: list[_Pipe] = []

    def open(self) -> _Pipe:
        pipe = _Pipe()
        self.pipes.append(pipe)
        return pipe

    def emit(self, line: str) -> None:
        self.pipes[-1].put(line)

    def close(self) -> None:
        for pipe in self.pipes:
            pipe.close()


class TestTheLiveSpectrum:
    """A waterfall is a radio held open, not a measurement that ends.

    That is the whole difference from `survey`, and every test here is about a
    consequence of it: no exit timer, rows on stdout instead of a file read back, and a
    session that has to be released like a listening one rather than freeing itself.
    """

    @pytest.fixture
    def feed(self) -> Any:
        return _Feed()

    @pytest.fixture
    def tuner(self, monkeypatch: pytest.MonkeyPatch, feed) -> Any:
        launched: list[list[str]] = []

        class _Fed(_FakeProc):
            def __init__(self, argv, *a: Any, **k: Any) -> None:
                launched.append(list(argv))
                super().__init__(argv, *a, **k)
                self.stdout = feed.open()

            def kill(self) -> None:
                # A killed process closes its pipe, and the reader ends. Without this
                # the pump thread would block on the queue for ever and a retune would
                # sit out `_restart`'s join timeout on every test that provokes one.
                super().kill()
                self.stdout.close()

        _instant(monkeypatch)
        monkeypatch.setattr(listen.shutil, "which", lambda _n: "/usr/bin/fake")
        monkeypatch.setattr(listen.subprocess, "Popen", _Fed)
        tuner = listen.Tuner()
        tuner._launched_argv = launched  # type: ignore[attr-defined]
        yield tuner
        feed.close()
        tuner.stop()

    def _sweep(self, start: int = 144_000_000, stop: int = 144_200_000) -> Any:
        return listen.Sweep.of(start, stop, 25_000, 60)

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
        assert body["sweep"] == {
            "start_hz": 144_000_000,
            "stop_hz": 144_200_000,
            "bin_hz": 25_000,
            "seconds": 60.0,
        }
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

    # ---- the command ----------------------------------------------------------

    def test_the_command_never_carries_an_exit_timer(self, tuner) -> None:
        """THE defining difference from a survey. `-e` would make the picture stop on
        its own after a minute, and the radio free itself under a viewer still watching
        — with nothing but a frozen waterfall to say so."""
        session = self._start(tuner, gain="30")
        cmd = session._spectrum_cmd()

        assert "-e" not in cmd
        assert cmd[-1] == "-"  # stdout, so rows can be fanned out as they are measured
        assert "144000000:144200000:25000" in cmd
        assert cmd[cmd.index("-i") + 1] == "1"
        assert cmd[cmd.index("-g") + 1] == "30"

    def test_the_rows_are_line_buffered_when_stdbuf_is_here(self, tuner) -> None:
        session = self._start(tuner)
        assert session._spectrum_cmd()[:3] == ["stdbuf", "-oL", "rtl_power"]

    def test_and_the_tool_still_runs_when_it_is_not(self, tuner, monkeypatch) -> None:
        # `stdbuf` is insurance, not a dependency: an image without it must still sweep.
        monkeypatch.setattr(listen, "_LINE_BUFFERED", False)
        session = self._start(tuner)
        assert session._spectrum_cmd()[0] == "rtl_power"

    def test_it_opens_the_radio_it_was_told_to(self, tuner) -> None:
        session = self._start(tuner, serial="77192819")
        cmd = session._spectrum_cmd()
        assert cmd[cmd.index("-d") + 1] == "77192819"

    # ---- frames reaching viewers ----------------------------------------------

    def test_a_viewer_is_handed_the_rows_as_they_are_measured(
        self, tuner, feed
    ) -> None:
        session = self._start(tuner)
        sub = session.subscribe_frames()

        feed.emit(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0, -71.0))
        feed.emit(_row("2026-09-04, 13:00:01", 144_000_000, 25_000, -60.0, -61.0))

        frame = sub.get(timeout=5)
        assert frame is not None and frame.db == [-70.0, -71.0]

    def test_a_viewer_arriving_late_is_not_shown_a_blank_canvas(
        self, tuner, feed
    ) -> None:
        """The seeded row. Without it a waterfall opens on nothing for up to a whole
        interval, which reads as a radio that did not start."""
        session = self._start(tuner)
        early = session.subscribe_frames()
        feed.emit(_row("2026-09-04, 13:00:00", 144_000_000, 25_000, -70.0))
        feed.emit(_row("2026-09-04, 13:00:01", 144_000_000, 25_000, -60.0))
        early.get(timeout=5)  # the pump has certainly published by now

        late = session.subscribe_frames()

        assert late.get_nowait() is not None

    def test_a_backed_up_viewer_loses_rows_and_never_wedges_the_pump(
        self, tuner, feed
    ) -> None:
        """A phone that stopped reading must cost that phone its picture, not everyone
        else's — the same backpressure live audio takes, and for the same reason: a
        waterfall row drawn late is drawn in the wrong place."""
        session = self._start(tuner)
        stalled = session.subscribe_frames()
        reading = session.subscribe_frames()

        for interval in range(listen.SPECTRUM_QUEUE + 6):
            feed.emit(
                _row(f"2026-09-04, 13:00:{interval:02d}", 144_000_000, 25_000, -70.0)
            )
            if interval:
                assert reading.get(timeout=5) is not None

        assert stalled.qsize() == listen.SPECTRUM_QUEUE

    def test_releasing_the_radio_closes_every_viewers_stream(self, tuner, feed) -> None:
        """Without the sentinel a released session leaves a server thread per viewer
        blocked for ever, each still reporting a healthy picture of a radio nothing is
        watching — the same leak the packet readers had."""
        session = self._start(tuner)
        sub = session.subscribe_frames()

        tuner.stop(session.id)

        assert sub.get(timeout=5) is None

    # ---- moving it ------------------------------------------------------------

    def test_moving_the_range_keeps_the_session_and_its_viewers(
        self, tuner, feed
    ) -> None:
        session = self._start(tuner)
        sub = session.subscribe_frames()
        was = session.id

        session.resweep(self._sweep(440_000_000, 440_200_000))

        assert session.id == was
        assert session.info().as_dict()["sweep"]["start_hz"] == 440_000_000
        # The omnibox names the new centre, not the old one.
        assert session.frequency_hz == 440_100_000
        # And the viewer was never told anything — no sentinel, no reconnect. The next
        # row simply describes the new band, which is what every frame carrying its own
        # range buys.
        feed.emit(_row("2026-09-04, 13:01:00", 440_000_000, 25_000, -70.0))
        feed.emit(_row("2026-09-04, 13:01:01", 440_000_000, 25_000, -71.0))
        frame = sub.get(timeout=5)
        assert frame is not None and frame.start_hz == 440_000_000

    def test_a_refused_shortwave_retune_leaves_the_picture_running(
        self, tuner, feed
    ) -> None:
        """The tap that must not cost the owner their radio.

        F8 opened the ten HF rows one wave before F6 replaces the engine, so "watching
        2 m, tap 40 m" is now one tap: the band sheet offers it, the route passes it
        through, and the sidecar refuses it. Refusing from inside the relaunch would
        refuse AFTER `_restart` had killed the pipeline and written the new range —
        `alive` false, the next `Tuner._reap` stopping the session and dropping the
        lease, and the error toast landing on a screen that had just lost the waterfall
        AND the radio. Validated before anything is destroyed, the tap costs a sentence.
        """
        session = self._start(tuner)
        sub = session.subscribe_frames()

        with pytest.raises(listen.SdrError) as refused:
            session.resweep(
                listen.Sweep.of(7_125_000, 7_300_000, 250, 0, direct_ok=True)
            )

        assert "I/Q engine" in str(refused.value)
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
        feed.emit(_row("2026-09-04, 13:01:00", 144_000_000, 25_000, -70.0))
        feed.emit(_row("2026-09-04, 13:01:01", 144_000_000, 25_000, -71.0))
        frame = sub.get(timeout=5)
        assert frame is not None and frame.start_hz == 144_000_000

    def test_moving_a_released_spectrum_is_refused_not_relaunched(self, tuner) -> None:
        """The race `Session._restart` exists for: a relaunch here spawns an rtl_power
        for a session no longer in the registry — unreapable, and holding the dongle
        until the container restarts."""
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

    def test_a_spectrum_is_refused_the_same_way(self, tuner) -> None:
        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                144_000_000,
                "fm",
                None,
                purpose=listen.PURPOSE_SPECTRUM,
                sweep=listen.Sweep.of(144_000_000, 144_200_000, 25_000, 60),
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


def test_an_hf_spectrum_is_refused_while_rtl_power_is_still_the_engine(tuner) -> None:
    """F8 opened the HF band rows one wave before F6 replaces this engine.

    `rtl_power -D` hardcodes direct sampling mode 1 — the I branch — and this hardware
    wires Q, so the tool would tune something and measure nothing. The refusal has to
    live on the ENGINE rather than in the route: the same range becomes viewable the
    moment the I/Q engine lands, and a floor in the route would have to be hunted down
    and removed again. This test is the reminder to delete both together."""
    with pytest.raises(listen.SdrError) as refused:
        tuner.start(
            7_200_000,
            "fm",
            None,
            purpose=listen.PURPOSE_SPECTRUM,
            sweep=listen.Sweep.of(7_125_000, 7_300_000, 250, 0, direct_ok=True),
        )

    assert "I/Q engine" in str(refused.value)
    # Above the tuner floor is untouched: this guard is about the ENGINE, not the band.
    assert tuner.current() is None


# --- F6: the I/Q engine, and the fallback that must survive it ----------------------


def test_a_named_capture_keeps_its_exact_width_through_the_clamp() -> None:
    """`MIN_SWEEP_BIN_HZ` is rtl_power's floor, and clamping an I/Q width to it would
    make the frame declare a width the transform never used — invisibly, because
    nothing downstream can tell (§6.14). 250 Hz is under that floor and is exactly what
    256 kS/s over 1024 bins produces."""
    swept = listen.Sweep.of(
        7_125_000, 7_300_000, 250, 60, direct_ok=True, capture=(256_000, 1_024)
    )

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
    swept = listen.Sweep.of(
        7_125_000, 7_300_000, 250, 60, direct_ok=True, capture=(256_000, 1_024)
    )

    assert listen.spectrum_engine_refusal(swept) is None


def test_shortwave_too_wide_for_one_capture_is_still_refused_and_says_why() -> None:
    """Several hops is rtl_power's job, and rtl_power cannot see down there at all. The
    sentence has to name WHICH limit it hit, because the owner has no terminal to look
    with (CLAUDE.md #10)."""
    swept = listen.Sweep.of(3_000_000, 8_000_000, 25_000, 60, direct_ok=True)

    refusal = listen.spectrum_engine_refusal(swept)

    assert refusal is not None
    assert "several hops" in refusal
    assert "Listening there works" in refusal


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

    def test_a_radio_that_will_not_open_falls_back_to_rtl_power(
        self, tuner, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """VHF: rtl_power can serve it, so the picture comes back rather than the
        session failing. And it SAYS so, because a silent downgrade is a waterfall
        that is quietly a tenth of the frame rate it claims."""
        self._refuse_to_open(monkeypatch, listen.radio.RadioError("no such device"))
        swept = listen.Sweep.of(
            144_000_000, 144_400_000, 600, 300, capture=(2_400_000, 4_000)
        )

        info = tuner.start(
            146_000_000, "fm", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
        )

        assert info.as_dict()["purpose"] == listen.PURPOSE_SPECTRUM
        assert "falling back to rtl_power" in capsys.readouterr().out

    def test_shortwave_keeps_the_failure_instead_of_drawing_a_lie(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one range where falling back is worse than failing: `rtl_power -D`
        hardcodes the ADC's I branch and this board wires Q, so it would tune something
        and measure nothing — a flat, plausible, meaningless waterfall."""
        self._refuse_to_open(monkeypatch, listen.radio.RadioError("no such device"))
        swept = listen.Sweep.of(
            7_125_000, 7_300_000, 250, 300, direct_ok=True, capture=(256_000, 1_024)
        )

        with pytest.raises(listen.SdrError) as refused:
            tuner.start(
                7_212_500, "usb", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
            )

        assert "no such device" in str(refused.value)

    def test_a_busy_radio_is_a_fallback_too_not_a_crash(
        self, tuner, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """`RadioBusy` is a different exception from `RadioError` and would have escaped
        an `except RadioError` written from the happy path."""
        self._refuse_to_open(monkeypatch, listen.radio.RadioBusy("held by aprs"))
        swept = listen.Sweep.of(
            144_000_000, 144_400_000, 600, 300, capture=(2_400_000, 4_000)
        )

        tuner.start(
            146_000_000, "fm", None, purpose=listen.PURPOSE_SPECTRUM, sweep=swept
        )

        assert "falling back to rtl_power" in capsys.readouterr().out

    def test_the_engine_publishes_frames_it_transformed_itself(
        self, tuner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The happy path, end to end through the real `iq.Spectrometer`: samples in,
        one self-describing row out, at the width the capture makes and not the width
        anyone asked for."""
        rate_hz, fft_bins = 2_400_000, 4_000
        opened: dict[str, Any] = {}

        class _OneFrameRadio:
            alive = True
            center_hz = 146_000_000

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
                )

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
        frame = session.subscribe_frames().get(timeout=5)

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
