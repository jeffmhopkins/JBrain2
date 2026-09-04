"""The sdr sidecar's listening session — the lease, made testable.

`deploy/sdr/` is not an installed package, so it is loaded by path here. What these
cover is the arbitration and the shape the omnibox tuner reads, without a radio: the
pipeline itself needs hardware, so it is faked at the subprocess seam.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

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
