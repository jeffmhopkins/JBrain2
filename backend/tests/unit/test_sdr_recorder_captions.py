"""`kind='captions'` — the same reception kept as words instead of as a file.

The audio recorder's whole job is to not lose audio; this one's is to not lose SENTENCES
and, just as importantly, to not pretend to have kept anything else. A captions row has
no blob, and the columns that describe a file must be ABSENT rather than zeroed — this
repo's recurring failure is a value shaped like a measurement that is in fact fiction
(SDR_RECORDING_PLAN.md §7), and `bytes = 0` under a recording that was never measured in
bytes is exactly that.

The other half of what is pinned here is that adding a second kind did not move the
first one. The audio path is asserted byte-for-byte in the same terms it was before, in
this file as well as its own, because a refactor that parameterised the source is
precisely the change that can quietly reroute it.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import wave
from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain.db.session import SessionContext
from jbrain.sdr import recorder as recorder_mod
from jbrain.sdr.recorder import RecorderRefused, SdrRecorder
from jbrain.storage import FsBlobStore
from jbrain.transcribe import Transcript, Word

OWNER = SessionContext(principal_id="owner", principal_kind="owner")

#: Long enough that the sidecar's own segmenter would have sent it (`SEGMENT_MIN_S`),
#: and short enough that a whole test's worth fits in a `Backlog`.
CLIP_S = 4.0


def _wav(seconds: float = CLIP_S, rate: int = 16_000) -> bytes:
    """A real 16-bit mono WAV. Real because `Backlog` reads its frame count to decide
    what to give up, and a stub would make that decision on nothing."""
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(b"\x00\x00" * int(seconds * rate))
    return out.getvalue()


def _frame(started: float, wav: bytes) -> bytes:
    """One `/listen/segments` frame: a JSON header line, then exactly that many bytes."""
    head = json.dumps({"started_at": started, "bytes": len(wav)}).encode()
    return head + b"\n" + wav


class _Stream:
    """The sidecar's stream, scripted — segments for captions, chunks for audio."""

    def __init__(
        self,
        blocks: list[bytes] | None = None,
        *,
        status_code: int = 200,
        silent: bool = False,
        hold_after: int | None = None,
    ) -> None:
        self.status_code = status_code
        self._blocks = blocks or []
        self._silent = silent
        self._hold_after = hold_after
        self.closed = False
        #: Set once every scripted block has been handed over, so a test that asserts on
        #: the WHOLE capture waits for it rather than racing its own Stop.
        self.drained = asyncio.Event()
        #: Released by a test that needs the next frame to arrive AFTER the previous one
        #: was transcribed. Without it every scripted frame lands before the loop takes
        #: any of them, and `Backlog` correctly merges them into one clip — which is the
        #: real behaviour, but not the one a test about two separate transmissions means.
        self.resume = asyncio.Event()

    async def aiter_bytes(self):
        for index, block in enumerate(self._blocks):
            if self._hold_after is not None and index == self._hold_after:
                await self.resume.wait()
            yield block
            await asyncio.sleep(0)
        self.drained.set()
        if self._silent:
            await asyncio.Event().wait()  # a socket held open with nothing coming down it

    async def aread(self) -> bytes:
        return b'{"detail": "nothing is listening"}'

    async def aclose(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Repo:
    """Remembers what it was asked to write, and by which door."""

    def __init__(self) -> None:
        self.audio: list[dict[str, Any]] = []
        self.captions: list[dict[str, Any]] = []

    async def add(self, ctx: SessionContext, **fields: Any) -> dict[str, Any]:
        row = {"id": f"a{len(self.audio)}", "ctx": ctx, **fields}
        self.audio.append(row)
        return row

    async def add_captions(self, ctx: SessionContext, **fields: Any) -> dict[str, Any]:
        row = {"id": f"c{len(self.captions)}", "ctx": ctx, **fields}
        self.captions.append(row)
        return row


class _Whisper:
    """A transcriber that answers from a script and counts what it was handed."""

    def __init__(self, replies: list[Transcript | Exception] | None = None) -> None:
        self._replies = replies or []
        self.calls: list[bytes] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls.append(audio)
        reply: Transcript | Exception
        if self._replies:
            reply = self._replies.pop(0)
        else:
            reply = Transcript(text=f"said {len(self.calls)}")
        if isinstance(reply, Exception):
            raise reply
        return reply


def _openers(
    monkeypatch: pytest.MonkeyPatch, stream: _Stream
) -> tuple[list[str], _Client, _Stream]:
    """Answer BOTH openers with this stream, and record which one was asked."""
    client = _Client()
    opened: list[str] = []

    def _opener(name: str):
        async def open_it(base_url: str) -> tuple[Any, Any]:
            opened.append(name)
            return client, stream

        return open_it

    monkeypatch.setattr(recorder_mod, "open_audio_stream", _opener("audio"))
    monkeypatch.setattr(recorder_mod, "open_caption_stream", _opener("captions"))

    async def fake_levels(_path: Any) -> tuple[list[float], float | None]:
        return [0.25, 0.75], 3.5

    monkeypatch.setattr(recorder_mod, "levels", fake_levels)
    return opened, client, stream


def _recorder(tmp_path: Any, repo: _Repo) -> SdrRecorder:
    return SdrRecorder(FsBlobStore(tmp_path), repo)  # type: ignore[arg-type]


async def _start(rec: SdrRecorder, **kw: Any) -> dict[str, Any]:
    return await rec.start(
        OWNER,
        base_url="http://sdr:8000",
        frequency_hz=kw.pop("frequency_hz", 162_550_000),
        mode=kw.pop("mode", "nfm"),
        **kw,
    )


_TURNS = 1_000
_DEADLINE_S = 10.0


async def _until(done: Any) -> None:
    """Let the recorder's background task run until `done()` — the same yield-and-recheck
    wait `test_sdr_recorder.py` uses, and for its reason: nothing signals this."""
    for _ in range(_TURNS):
        if done():
            return
        await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DEADLINE_S
    while loop.time() < deadline:
        if done():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the recorder never got there")


# --- the captions capture ------------------------------------------------------------


async def test_a_captions_recording_writes_a_transcript_and_no_blob(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's decision, in one assertion: captions INSTEAD of audio."""
    now = time.time()
    stream = _Stream([_frame(now, _wav()), _frame(now + CLIP_S, _wav())], silent=True, hold_after=1)
    _openers(monkeypatch, stream)
    repo = _Repo()
    whisper = _Whisper([Transcript(text="net control, K7XYZ"), Transcript(text="go ahead")])
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=whisper, serial="0092")
    await _until(lambda: len(whisper.calls) == 1)
    stream.resume.set()
    await _until(lambda: len(whisper.calls) == 2)
    saved = await rec.stop()

    assert repo.audio == []  # not one row through the audio door
    assert saved is not None
    assert saved["transcript"]["text"] == "net control, K7XYZ\ngo ahead"
    assert [s["text"] for s in saved["transcript"]["segments"]] == [
        "net control, K7XYZ",
        "go ahead",
    ]
    # The settings travel with a transcript for the same reason they travel with a clip:
    # words with no frequency under them are a page about nothing.
    assert (saved["frequency_hz"], saved["mode"], saved["serial"]) == (162_550_000, "nfm", "0092")
    assert saved["transcribed_at"] is not None
    # Nothing was written to the store at all — not an empty blob, not a zero-byte file.
    assert list(tmp_path.rglob("*.blob")) == []


async def test_the_columns_that_only_mean_something_for_audio_are_never_written(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL, not zero. `bytes=0` and `peaks=[]` on a row nothing measured read as "no
    bytes" and "a flat waveform" — two measurements of a file that does not exist. The
    captions INSERT must not name the columns at all, which is what this asserts: the
    repo's door for this kind has no parameter to put a fiction in."""
    now = time.time()
    stream = _Stream([_frame(now, _wav())], silent=True)
    _openers(monkeypatch, stream)
    repo = _Repo()
    whisper = _Whisper()
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=whisper)
    await _until(lambda: whisper.calls != [])
    await rec.stop()

    assert len(repo.captions) == 1
    written = repo.captions[0]
    for column in ("blob_sha256", "bytes_", "bytes", "peaks"):
        assert column not in written, f"a captions row must not carry {column}"


async def test_word_times_are_offsets_into_the_recording_not_into_the_clip(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whisper times a word from the start of the clip it was handed, and those clips are
    minutes apart in a capture. Left unshifted, every transmission in a four-hour net
    claims to have happened in its first few seconds."""
    now = time.time()
    late = now + 600.0
    stream = _Stream([_frame(now, _wav()), _frame(late, _wav())], silent=True, hold_after=1)
    _openers(monkeypatch, stream)
    repo = _Repo()
    whisper = _Whisper(
        [
            Transcript(text="first", words=(Word("first", 100, 400, 0.9),)),
            Transcript(text="later", words=(Word("later", 100, 400, 0.8),)),
        ]
    )
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=whisper)
    await _until(lambda: len(whisper.calls) == 1)
    stream.resume.set()
    await _until(lambda: len(whisper.calls) == 2)
    saved = await rec.stop()

    assert saved is not None
    words = saved["transcript"]["words"]
    assert [w["text"] for w in words] == ["first", "later"]
    # The first segment starts when the recording does, give or take the moment between
    # `datetime.now()` and the scripted stamp; the second is ten minutes further on.
    assert words[0]["start_ms"] == pytest.approx(100, abs=2_000)
    assert words[1]["start_ms"] == pytest.approx(600_100, abs=2_000)
    assert words[1]["confidence"] == 0.8


async def test_segments_that_piled_up_while_whisper_worked_become_one_caption(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a recording keeps is what CC would have SHOWN, which is the owner's ask.

    A transcription costs about ten seconds whatever the clip holds, so segments arriving
    behind one are transcribed together as a single merged clip rather than one at a time
    (`sdr/captions.py` `Backlog`). Recording inherits that deliberately: a recorder that
    transcribed every segment separately would fall a little further behind on every one
    and never catch up, and the transcript would stop being the captions it is named for.
    """
    now = time.time()
    stream = _Stream(
        [_frame(now + i * CLIP_S, _wav()) for i in range(3)],
        silent=True,
    )
    _openers(monkeypatch, stream)
    whisper = _Whisper([Transcript(text="the whole exchange")])
    rec = _recorder(tmp_path, _Repo())

    await _start(rec, kind="captions", transcriber=whisper)
    await stream.drained.wait()
    saved = await rec.stop()

    assert saved is not None
    assert len(whisper.calls) == 1  # three segments, one model call
    assert saved["transcript"]["segments"] == [
        {"at_s": pytest.approx(0.0, abs=2.0), "text": "the whole exchange"}
    ]


async def test_a_captions_capture_that_heard_nothing_writes_no_row(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet band is squelched at the sidecar, so a capture pointed at one produces no
    segments at all. An empty transcript row is the captions twin of a clip that plays
    silence: it reads in the library as a recording and holds nothing."""
    _openers(monkeypatch, _Stream([], silent=True))
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=_Whisper())
    saved = await rec.stop()

    assert saved is None
    assert repo.captions == [] and repo.audio == []


async def test_stopping_keeps_the_last_thing_that_was_said(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seconds before Stop are exactly what Record was pressed for. The backlog
    merges, so finishing it is ONE bounded whisper call rather than a queue drained to
    the end — which is why the wait is affordable."""
    now = time.time()
    stream = _Stream([_frame(now, _wav()), _frame(now + CLIP_S, _wav())], silent=True)
    _openers(monkeypatch, stream)
    repo = _Repo()
    # One reply only would be enough for a loop that dropped the tail; the point is that
    # everything waiting at Stop is transcribed too.
    whisper = _Whisper()
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=whisper)
    await stream.drained.wait()
    saved = await rec.stop()

    assert saved is not None
    assert saved["transcript"]["text"] != ""
    assert whisper.calls != []


async def test_a_failed_transcription_ends_the_clip_not_the_recording(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording that stopped itself because whisper hiccuped would lose the hour after
    the hiccup as well as the sentence during it."""
    now = time.time()
    stream = _Stream([_frame(now, _wav()), _frame(now + CLIP_S, _wav())], silent=True, hold_after=1)
    _openers(monkeypatch, stream)
    repo = _Repo()
    whisper = _Whisper([RuntimeError("whisper fell over"), Transcript(text="still here")])
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=whisper)
    await _until(lambda: len(whisper.calls) == 1)
    stream.resume.set()
    await _until(lambda: len(whisper.calls) == 2)
    saved = await rec.stop()

    assert saved is not None
    assert "still here" in saved["transcript"]["text"]


async def test_a_captions_capture_stops_itself_at_its_own_bound(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MAX_CAPTURE_BYTES` says "four hours" through a bitrate this kind does not have,
    so captions carry their own wall-clock bound. What a forgotten captions press costs
    is whisper staying resident beside the chat model, not disk — and on a quiet band it
    accumulates nothing, so a bound on transcript size would never fire for the very
    recording that most needs ending."""
    monkeypatch.setattr(recorder_mod, "MAX_CAPTION_CAPTURE_S", 0.15)
    monkeypatch.setattr(recorder_mod, "_CAPTION_TICK_S", 0.01)
    now = time.time()
    stream = _Stream([_frame(now, _wav())], silent=True)
    _openers(monkeypatch, stream)
    repo = _Repo()
    whisper = _Whisper()
    rec = _recorder(tmp_path, repo)

    await _start(rec, kind="captions", transcriber=whisper)
    # It ends itself; nobody presses Stop.
    await _until(lambda: rec.state() is None)
    saved = await asyncio.wait_for(rec.stop(), timeout=2.0)

    # ...and what it heard before the bound is kept, exactly as the audio bound keeps
    # what it spooled.
    assert saved is not None and saved["transcript"]["text"] != ""
    assert len(repo.captions) == 1


async def test_the_audio_bound_does_not_apply_to_captions(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A captions capture counts no bytes, so a byte bound set to nothing at all must not
    end it — the two bounds are separate numbers about separate things."""
    monkeypatch.setattr(recorder_mod, "MAX_CAPTURE_BYTES", 1)
    now = time.time()
    stream = _Stream([_frame(now, _wav())], silent=True)
    _openers(monkeypatch, stream)
    whisper = _Whisper()
    rec = _recorder(tmp_path, _Repo())

    await _start(rec, kind="captions", transcriber=whisper)
    await _until(lambda: whisper.calls != [])

    assert rec.state() is not None  # still running, a byte bound away from nothing
    assert await rec.stop() is not None


# --- what it opens, and what it leaves alone ------------------------------------------


async def test_captions_start_their_own_stream_rather_than_needing_cc_on_first(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner has no terminal, and "turn CC on, then press Record" is a worse answer
    than doing it (CLAUDE.md #10). The recorder subscribes to `/listen/segments` itself —
    the sidecar fans segments out per subscriber — so Record works with the live caption
    stream open or closed, and closes only what it opened, leaving CC exactly as found."""
    now = time.time()
    stream = _Stream([_frame(now, _wav())], silent=True)
    opened, client, _ = _openers(monkeypatch, stream)
    whisper = _Whisper()
    rec = _recorder(tmp_path, _Repo())

    await _start(rec, kind="captions", transcriber=whisper)
    await _until(lambda: whisper.calls != [])
    await rec.stop()

    assert opened == ["captions"]  # its own subscription, nothing borrowed
    assert stream.closed and client.closed  # and only its own released at the end


async def test_recording_audio_still_opens_the_audio_stream_and_never_whisper(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio path, bit for bit, after the source was parameterised. The digest is the
    assertion that matters: the same bytes in the same order into the same store."""
    chunks = [b"ID3frame-one", b"frame-two", b"frame-three"]
    stream = _Stream(chunks)
    opened, _client, _ = _openers(monkeypatch, stream)
    repo = _Repo()
    whisper = _Whisper()
    rec = _recorder(tmp_path, repo)

    await _start(rec, bandwidth_hz=16_000, serial="0092")
    await stream.drained.wait()
    saved = await rec.stop()

    assert opened == ["audio"]
    assert whisper.calls == []  # no model is loaded for a clip
    whole = b"".join(chunks)
    assert saved is not None
    assert saved["blob_sha256"] == hashlib.sha256(whole).hexdigest()
    assert FsBlobStore(tmp_path).path_for(saved["blob_sha256"]).read_bytes() == whole
    assert saved["bytes_"] == len(whole)
    assert repo.captions == []  # and not one row through the captions door


async def test_a_sidecar_that_refuses_the_segments_keeps_its_own_sentence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """409 is the owner-fixable one — press Listen — and must not be buried in a 502."""
    _openers(monkeypatch, _Stream(status_code=409))
    rec = _recorder(tmp_path, _Repo())

    with pytest.raises(RecorderRefused) as refused:
        await _start(rec, kind="captions", transcriber=_Whisper())

    assert refused.value.status == 409
    assert "nothing is listening" in refused.value.detail


async def test_captions_without_a_whisper_gateway_are_refused_with_a_sentence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused BEFORE the stream is opened: a capture with nothing to transcribe into
    would hold a subscription for four hours and write nothing."""
    opened, _client, _ = _openers(monkeypatch, _Stream([], silent=True))
    rec = _recorder(tmp_path, _Repo())

    with pytest.raises(RecorderRefused) as refused:
        await _start(rec, kind="captions")

    assert refused.value.status == 503
    assert "whisper" in refused.value.detail
    assert opened == []


# --- the gate, and what the deck is told ----------------------------------------------


async def test_the_gate_is_box_wide_across_kinds(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One radio, one listen session, and `/sdr/status` carries ONE recording — so the
    long press SWAPS what Record does rather than adding a second thing it can do. A
    captions press while audio is recording gets back the recording already running, and
    opens nothing."""
    stream = _Stream([b"audio-bytes"], silent=True)
    opened, _client, _ = _openers(monkeypatch, stream)
    rec = _recorder(tmp_path, _Repo())

    first = await _start(rec)
    second = await _start(rec, kind="captions", transcriber=_Whisper())

    assert opened == ["audio"]  # the second press opened nothing
    assert second["started_at"] == first["started_at"]
    assert second["kind"] == "audio"
    await rec.stop()


async def test_state_reports_a_caption_count_and_no_byte_count(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the tape deck draws off the 1 Hz poll. `bytes: 0` under a captions capture
    would put a measurement on screen for something nothing is measuring, and the running
    figure is the owner's argument for stopping — so it has to be the real one."""
    now = time.time()
    stream = _Stream([_frame(now, _wav())], silent=True)
    _openers(monkeypatch, stream)
    whisper = _Whisper()
    rec = _recorder(tmp_path, _Repo())

    started = await _start(rec, kind="captions", transcriber=whisper, serial="0092")
    assert started["kind"] == "captions"
    await _until(lambda: (rec.state() or {}).get("captions") == 1)

    state = rec.state(now=datetime.fromisoformat(started["started_at"]))
    assert state is not None
    assert state["bytes"] is None
    assert state["captions"] == 1
    assert state["serial"] == "0092"
    await rec.stop()


async def test_an_audio_capture_reports_a_size_and_no_caption_count(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _Stream([b"12345678"], silent=True)
    _openers(monkeypatch, stream)
    rec = _recorder(tmp_path, _Repo())

    await _start(rec)
    await stream.drained.wait()

    state = rec.state(now=datetime.now(tz=UTC))
    assert state is not None
    assert (state["kind"], state["bytes"], state["captions"]) == ("audio", 8, None)
    await rec.stop()
