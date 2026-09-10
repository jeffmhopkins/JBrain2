"""`jbrain.sdr.recorder` — what happens to the bytes between the radio and the row.

The recorder's whole job is to not lose audio, and every interesting case here is a way
the stream can end that is NOT the owner pressing Stop: the sidecar dropping the session,
a connection breaking mid-clip, a socket that goes quiet and stays open. Each of those
must still finalize the blob and write the row — a raised exception anywhere in the
chunk generator unlinks `put_stream`'s spool file instead, and the recording is gone with
nothing on any screen to say so (CLAUDE.md #10).

The blob store is the REAL `FsBlobStore` over a tmp_path, because the property being
tested is exactly the one a fake would paper over: that a `return` out of the generator
finalizes the spool and hands back a digest.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jbrain.db.session import SessionContext
from jbrain.sdr import recorder as recorder_mod
from jbrain.sdr.recorder import RecorderRefused, SdrRecorder
from jbrain.storage import FsBlobStore

OWNER = SessionContext(principal_id="owner", principal_kind="owner")

# What the fake decoder reports about every finished clip, so no test needs ffmpeg.
PEAKS = [0.25, 0.75]
MEASURED_S = 3.5


class _Response:
    """The sidecar's audio stream, scripted."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        status_code: int = 200,
        body: bytes = b"",
        silent: bool = False,
        boom: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks or []
        self._body = body
        self._silent = silent
        self._boom = boom
        self.closed = False
        #: Set once every scripted chunk has been handed over. Tests that assert on the
        #: WHOLE clip wait for it first: `stop()` racing the reader is the recorder's
        #: correct behaviour, so a test that stopped mid-script would be asserting on a
        #: coin toss.
        self.drained = asyncio.Event()

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk
            # A pause between chunks, so the recorder's stop event has somewhere to win.
            await asyncio.sleep(0)
        self.drained.set()
        if self._boom is not None:
            raise self._boom
        if self._silent:
            # A socket held open with nothing coming down it: the wedged-sidecar case.
            await asyncio.Event().wait()

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Repo:
    """A RecordingsRepo stand-in that remembers what it was asked to write."""

    def __init__(self, *, boom: Exception | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self._boom = boom

    async def add(self, ctx: SessionContext, **fields: Any) -> dict[str, Any]:
        if self._boom is not None:
            raise self._boom
        row = {"id": f"r{len(self.rows)}", "ctx": ctx, **fields}
        self.rows.append(row)
        return row


def _install(monkeypatch: pytest.MonkeyPatch, response: _Response) -> tuple[_Client, list[str]]:
    """Answer `open_audio_stream` with this response, and record every call."""
    client = _Client()
    opened: list[str] = []

    async def opener(base_url: str) -> tuple[Any, Any]:
        opened.append(base_url)
        return client, response

    monkeypatch.setattr(recorder_mod, "open_audio_stream", opener)

    async def fake_levels(_path: Any) -> tuple[list[float], float | None]:
        return PEAKS, MEASURED_S

    monkeypatch.setattr(recorder_mod, "levels", fake_levels)
    return client, opened


def _recorder(tmp_path: Any, repo: _Repo, *, blobs: Any = None) -> SdrRecorder:
    return SdrRecorder(blobs or FsBlobStore(tmp_path), repo)  # type: ignore[arg-type]


class _Store(FsBlobStore):
    """A real store on a volume the test can starve. Everything about the bytes is the
    real thing; only how much room is left is scripted."""

    def __init__(self, root: Any, free: int = 1 << 40) -> None:
        super().__init__(root)
        self.free = free

    def free_bytes(self) -> int:
        return self.free


#: Event-loop turns a test will give the recorder's background task before giving up.
#: Generous — the finalize is several awaits deep — and finite, so a property that never
#: becomes true fails with a sentence rather than hanging the suite.
_TURNS = 1_000


async def _until(done: Any) -> None:
    """Let the recorder's background task run until `done()`.

    A yield-and-recheck loop rather than an event, because what is being waited for is
    the recorder's own state changing — the whole point is that nothing signals it."""
    for _ in range(_TURNS):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("the recorder never got there")


def _ended_elsewhere() -> Any:
    """An `_Active` the recorder does not hold — a recording whose stream has ended."""
    return recorder_mod._Active(
        ctx=OWNER,
        started_at=datetime.now(tz=UTC),
        frequency_hz=1,
        mode="nfm",
        bandwidth_hz=None,
        gain=None,
        serial=None,
    )


async def _start(rec: SdrRecorder, **kw: Any) -> dict[str, Any]:
    return await rec.start(
        OWNER,
        base_url="http://sdr:8000",
        frequency_hz=kw.pop("frequency_hz", 162_550_000),
        mode=kw.pop("mode", "nfm"),
        **kw,
    )


async def test_a_stopped_recording_becomes_a_blob_and_a_row(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = [b"ID3frame-one", b"frame-two", b"frame-three"]
    response = _Response(audio)
    _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec, bandwidth_hz=16_000, gain="42", serial="0092")
    await response.drained.wait()
    saved = await rec.stop()

    assert saved is not None
    whole = b"".join(audio)
    assert saved["blob_sha256"] == hashlib.sha256(whole).hexdigest()
    assert FsBlobStore(tmp_path).path_for(saved["blob_sha256"]).read_bytes() == whole
    assert saved["bytes_"] == len(whole)
    # The DECODED length, not the wall clock: a clip's real duration is what the trim
    # sheet places its handles against.
    assert saved["duration_s"] == MEASURED_S
    assert saved["peaks"] == PEAKS
    assert (saved["frequency_hz"], saved["mode"], saved["bandwidth_hz"]) == (
        162_550_000,
        "nfm",
        16_000,
    )
    assert (saved["gain"], saved["serial"]) == ("42", "0092")
    # `ended_at` follows from the measured length, so the row cannot claim a span its
    # own duration disagrees with.
    assert (saved["ended_at"] - saved["started_at"]).total_seconds() == pytest.approx(MEASURED_S)
    # Both ends of the connection are released, or the next Record leaks a socket.
    assert response.closed and rec.state() is None


async def test_the_sidecar_ending_the_session_still_writes_the_row(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's §2: an interrupted recording is still a recording.

    The stream ends on its own — a retune, an Ops → Update, the owner stopping the radio
    from another tab — and the audio captured so far must land in the library rather than
    being discarded because nobody pressed Stop."""
    response = _Response([b"caught-this-much"])
    _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec)
    await response.drained.wait()
    saved = await rec.stop()

    assert len(repo.rows) == 1
    assert saved is not None and saved["bytes_"] == len(b"caught-this-much")
    assert rec.state() is None


async def test_a_broken_stream_keeps_what_it_captured(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read error mid-clip is the same story as a clean end.

    This is the case that punishes a `raise`: propagated out of the generator it reaches
    `put_stream`, whose `finally` unlinks the spool file — so the recording disappears
    because the last few hundred milliseconds of it did."""
    response = _Response([b"the-first-minute"], boom=ConnectionResetError("gone"))
    _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec)
    await response.drained.wait()
    saved = await rec.stop()

    assert saved is not None and saved["bytes_"] == len(b"the-first-minute")


async def test_stop_answers_even_when_the_stream_has_gone_quiet(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged sidecar holds the socket open and sends nothing.

    Checking the stop flag only BETWEEN chunks would block here for ever, on a request
    the owner is waiting on with no terminal to escape to. The read is raced against the
    event instead, so Stop finalizes what was captured and answers."""
    response = _Response([b"then-silence"], silent=True)
    _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec)
    await response.drained.wait()
    saved = await asyncio.wait_for(rec.stop(), timeout=2.0)

    assert saved is not None and saved["bytes_"] == len(b"then-silence")


async def test_a_second_start_does_not_open_a_second_stream(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Record presses a moment apart both see "not recording".

    A second stream would spool a byte-identical second copy, which content-addressing
    dedupes into ONE blob with two rows pointing at it — and then deleting either
    recording takes the other's audio."""
    response = _Response([b"a"], silent=True)
    _, opened = _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    first = await _start(rec)
    again = await _start(rec, frequency_hz=7_200_000, mode="lsb")

    assert len(opened) == 1
    assert again["started_at"] == first["started_at"]
    assert again["frequency_hz"] == 162_550_000  # the running recording, not the new ask
    await response.drained.wait()
    await rec.stop()
    # One stream, one blob, one row — the property the single active slot exists for.
    assert len(repo.rows) == 1


async def test_two_presses_that_actually_RACE_still_open_one_stream(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sequential case above proves nothing about a race: its fake opener never
    yields, so the second press cannot arrive inside the first one's connect.

    Opening the stream is a real socket. `if self._active is None` followed by `await
    open_audio_stream(...)` is a check with a yield point inside it, so two presses both
    pass it, both connect, and the loser is orphaned — an `_Active` with a stop event
    nobody holds, spooling 28.8 MB/hour that no button can stop. This opener awaits, the
    way a connect does."""
    opened: list[_Response] = []

    async def opener(base_url: str) -> tuple[Any, Any]:
        await asyncio.sleep(0)  # the connect the check-then-set used to straddle
        response = _Response([b"one"], silent=True)
        opened.append(response)
        return _Client(), response

    monkeypatch.setattr(recorder_mod, "open_audio_stream", opener)

    async def fake_levels(_path: Any) -> tuple[list[float], float | None]:
        return PEAKS, MEASURED_S

    monkeypatch.setattr(recorder_mod, "levels", fake_levels)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    both = await asyncio.gather(_start(rec), _start(rec, frequency_hz=7_200_000, mode="lsb"))

    assert len(opened) == 1
    # Both presses answer about the SAME recording, so the tape deck cannot draw two.
    assert both[0]["started_at"] == both[1]["started_at"]
    await opened[0].drained.wait()
    saved = await rec.stop()
    assert saved is not None and len(repo.rows) == 1
    assert rec.state() is None


async def test_a_recording_that_ends_cannot_clear_a_live_ones_slot(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown clears the slot only if it still OWNS it.

    An unconditional `self._active = None` in the finalize is the second half of the same
    bug: any recording that ends — an orphan, or a clip whose stream dropped while the
    next one was starting — wipes the live recording's slot. After that `/sdr/status`
    reports nothing recording, Stop finds no stop event to set and waits for ever, and
    the shutdown finalize burns its whole timeout and then cancels the save it exists to
    perform. Here the ended recording is planted directly, because with the lock in place
    the race that used to create one no longer can."""
    live = _Response([b"still-going"], silent=True)
    _install(monkeypatch, live)
    rec = _recorder(tmp_path, _Repo())

    started = await _start(rec)
    await live.drained.wait()

    # An earlier recording finishing its teardown, now that it no longer owns the slot.
    await rec._run(_ended_elsewhere(), _Client(), _Response([]))  # type: ignore[arg-type]

    state = rec.state()  # what /sdr/status reports
    assert state is not None and state["started_at"] == started["started_at"]
    saved = await asyncio.wait_for(rec.stop(), timeout=2.0)  # ...and Stop still answers
    assert saved is not None and saved["bytes_"] == len(b"still-going")


async def test_a_stop_returns_the_row_of_the_recording_it_stopped(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved row belongs to a recording, not to the recorder.

    A clip whose stream ended on its own finishes writing its row LATE — the slot is
    cleared the moment the stream ends, but measuring the audio takes as long as ffmpeg
    takes. On one shared `_saved` slot the next Record press clears it, that late write
    then fills it in, and the next stop hands back the PREVIOUS recording's row as what
    it just saved: the tape deck reports the wrong clip, and the owner is told a
    recording was saved that they did not just make.

    Here the first clip's measuring pass is held open across the second Record press,
    which is exactly the window that used to swap them."""
    measuring = asyncio.Event()
    finished_late = asyncio.Event()

    first = _Response([b"first-clip"])
    _install(monkeypatch, first)

    async def slow_levels(_path: Any) -> tuple[list[float], float | None]:
        # Only the first clip is held: the second must be free to finish under stop().
        if not measuring.is_set():
            measuring.set()
            await finished_late.wait()
        return PEAKS, MEASURED_S

    monkeypatch.setattr(recorder_mod, "levels", slow_levels)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec)
    await measuring.wait()  # the first clip's stream has ended; its row is mid-write
    assert rec.state() is None

    second = _Response([b"second-clip"])

    async def opener(_base_url: str) -> tuple[Any, Any]:
        return _Client(), second

    monkeypatch.setattr(recorder_mod, "open_audio_stream", opener)
    await _start(rec)
    await second.drained.wait()
    await _until(lambda: len(repo.rows) == 1)  # the SECOND clip's row is written first...
    finished_late.set()
    await _until(lambda: len(repo.rows) == 2)  # ...and the first one's lands after it

    saved = await rec.stop()

    assert saved is not None
    assert saved["bytes_"] == len(b"second-clip")
    # Both clips are in the library — the first was never lost, only never reported as
    # the second one's result.
    assert sorted(row["bytes_"] for row in repo.rows) == [len(b"first-clip"), len(b"second-clip")]


async def test_a_capture_stops_itself_at_the_bound_and_keeps_what_it_has(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing expires by design (the plan's §1), so a Record press nobody releases is
    the one thing here that grows without limit — on a box whose owner has no terminal to
    clear it from (CLAUDE.md #10). The bound ends the capture the way the sidecar dropping
    the session does: the blob is finalized and the row written, so four hours land in the
    library instead of a day going missing."""
    monkeypatch.setattr(recorder_mod, "MAX_CAPTURE_BYTES", 10)
    response = _Response([b"12345", b"67890", b"never-read"], silent=True)
    _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec)
    # It ends itself; nobody presses Stop.
    await _until(lambda: rec.state() is None)
    saved = await asyncio.wait_for(rec.stop(), timeout=2.0)

    assert saved is not None and saved["bytes_"] == 10
    assert len(repo.rows) == 1
    assert rec.state() is None


async def test_a_capture_stops_itself_when_the_disk_is_nearly_full(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full volume is the failure the owner cannot undo from the PWA: Postgres stops
    accepting writes and Ops → Update cannot pull an image. So the spool watches the
    floor as it goes, rather than only at the start — a four-hour capture runs beside
    everything else the box is doing."""
    monkeypatch.setattr(recorder_mod, "_FREE_CHECK_EVERY_BYTES", 1)
    response = _Response([b"first", b"second"], silent=True)
    _install(monkeypatch, response)
    store = _Store(tmp_path)
    rec = _recorder(tmp_path, _Repo(), blobs=store)

    await _start(rec)
    store.free = 1 << 20  # the volume fills under us
    await _until(lambda: rec.state() is None)
    saved = await asyncio.wait_for(rec.stop(), timeout=2.0)

    # Stopped early, and what it caught was kept.
    assert saved is not None and 0 < saved["bytes_"] <= len(b"firstsecond")


async def test_record_is_refused_with_a_sentence_when_there_is_no_room(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal the owner can act on, before a stream is opened — not a recording that
    fills the last of the disk and takes the box down with it."""
    _, opened = _install(monkeypatch, _Response([b"x"]))
    rec = _recorder(tmp_path, _Repo(), blobs=_Store(tmp_path, free=240 << 20))

    with pytest.raises(RecorderRefused) as refused:
        await _start(rec)

    assert refused.value.status == 400
    assert "240 MB" in refused.value.detail
    assert opened == [] and rec.state() is None


async def test_nothing_listening_is_refused_with_the_sidecars_own_sentence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _Response(status_code=409, body=b'{"detail": "nothing is listening"}')
    client, _ = _install(monkeypatch, response)
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    with pytest.raises(RecorderRefused) as refused:
        await _start(rec)

    assert refused.value.status == 409
    assert refused.value.detail == "nothing is listening"
    # Nothing is left recording, and neither end of the refused connection is left open.
    assert rec.state() is None and response.closed and client.closed
    assert await rec.stop() is None


async def test_a_stream_that_gave_no_audio_writes_no_row(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-second entry that plays silence and cannot be trimmed is not a recording."""
    _install(monkeypatch, _Response([]))
    repo = _Repo()
    rec = _recorder(tmp_path, repo)

    await _start(rec)
    saved = await rec.stop()

    assert saved is None
    assert repo.rows == []


async def test_stop_with_nothing_recording_is_not_an_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _Response([b"x"]))
    rec = _recorder(tmp_path, _Repo())

    assert await rec.stop() is None


async def test_state_reports_elapsed_time_and_running_size(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the tape deck draws off the 1 Hz status poll."""
    response = _Response([b"12345678"], silent=True)
    _install(monkeypatch, response)
    rec = _recorder(tmp_path, _Repo())

    started = await _start(rec, serial="0092")
    await response.drained.wait()
    began = datetime.fromisoformat(started["started_at"])
    state = rec.state(now=began + timedelta(seconds=5))

    assert state is not None
    assert state["seconds"] == pytest.approx(5.0)
    assert state["bytes"] == 8  # counted as the bytes go past, not after the fact
    assert state["serial"] == "0092"
    await rec.stop()


async def test_a_failed_row_write_does_not_take_the_task_down(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blob is on disk either way, and a background task that raises is a recorder
    that never records again."""
    _install(monkeypatch, _Response([b"audio"]))
    rec = _recorder(tmp_path, _Repo(boom=RuntimeError("no database")))

    await _start(rec)
    assert await rec.stop() is None
    # ...and the next recording still starts.
    assert await _start(rec) is not None
