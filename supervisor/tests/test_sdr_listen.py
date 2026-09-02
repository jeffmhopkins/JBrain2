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

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
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
        got = sub.get(timeout=8)
    finally:
        tuner.stop()
        server.close()

    assert got is not None
    assert (got.source, got.info) == ("KE8XYZ-9", "GATE 7K2M9")
