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
from datetime import datetime, timedelta
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
