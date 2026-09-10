"""The recordings surface of `api/sdr.py` (SDR_RECORDING_PLAN.md R1/R2).

Same shape as `test_sdr_aprs_routes.py`: no TestClient, the route functions are called
directly with the sidecar and the repo scripted, because what these routes do wrong is
not routing — it is what they say when something is missing, and what they leave on
disk afterwards.

Three properties here are load-bearing enough to be worth naming:

* **The blob is resolved from the ROW, never from the URL.** Every blob on this box
  lives in one content-addressed store, so a route that took a sha from a path segment
  would serve attachments, notes and images to anything that could reach it.
* **A trim deletes the original, and only the original.** Content-addressing means a
  full-length trim produces the SAME digest — deleting "the old blob" there would
  unlink the audio the row was just repointed at.
* **A failed cut changes nothing.** No repoint, no delete, and a sentence saying so.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import sdr as sdr_api
from jbrain.sdr.recorder import RecorderRefused
from jbrain.storage import FsBlobStore

OWNER = SimpleNamespace(id="owner", kind="owner")
CLIP = b"the-original-capture-bytes"
TRIMMED = b"the-kept-part"


class _Settings:
    sdr_url = "http://sdr:8000"
    supervisor_token = "t"


def _listening(**over: Any) -> dict[str, Any]:
    session = {
        "purpose": "listen",
        "session_id": "s1",
        "frequency_hz": 162_550_000,
        "mode": "nfm",
        "bandwidth_hz": 16_000,
        "gain": "42",
        "serial": "0092",
    }
    return {"sessions": [{**session, **over}], "listening": {**session, **over}}


_IDLE: dict[str, Any] = {"sessions": [], "listening": None}
_APRS_ONLY: dict[str, Any] = {
    "sessions": [{"purpose": "aprs", "session_id": "s-aprs", "frequency_hz": 144_390_000}],
    "listening": None,
}


class _Recorder:
    """A recorder the test scripts: what it is doing, and what stop hands back."""

    def __init__(
        self,
        *,
        active: dict[str, Any] | None = None,
        saved: dict[str, Any] | None = None,
        refused: RecorderRefused | None = None,
        broken: Exception | None = None,
    ) -> None:
        self._active = active
        self._saved = saved
        self._refused = refused
        self._broken = broken
        self.started: list[dict[str, Any]] = []
        self.stops = 0

    def state(self) -> dict[str, Any] | None:
        return self._active

    async def start(self, ctx: Any, **fields: Any) -> dict[str, Any]:
        if self._refused is not None:
            raise self._refused
        if self._broken is not None:
            raise self._broken
        self.started.append({"ctx": ctx, **fields})
        self._active = {"started_at": "2026-09-10T00:00:00+00:00", "seconds": 0.0, "bytes": 0}
        return self._active

    async def stop(self) -> dict[str, Any] | None:
        self.stops += 1
        self._active = None
        return self._saved


class _Repo:
    """An in-memory `RecordingsRepo` — the firewall is Postgres, so this just holds rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.ctxs: list[Any] = []

    def _find(self, recording_id: str) -> dict[str, Any] | None:
        self.ctxs.append(recording_id)
        return next((r for r in self.rows if r["id"] == recording_id), None)

    async def recent(self, ctx: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.rows[:limit]

    async def get(self, ctx: Any, recording_id: str) -> dict[str, Any] | None:
        row = self._find(recording_id)
        return dict(row) if row else None

    async def usage(self, ctx: Any) -> dict[str, int]:
        return {
            "count": len(self.rows),
            "bytes": sum(int(r["bytes"]) for r in self.rows),
            "reclaimed_bytes": 0,
        }

    async def retrim(
        self,
        ctx: Any,
        recording_id: str,
        *,
        duration_s: float,
        blob_sha256: str,
        bytes_: int,
        peaks: list[float],
    ) -> dict[str, Any] | None:
        row = self._find(recording_id)
        if row is None:
            return None
        row.update(duration_s=duration_s, blob_sha256=blob_sha256, bytes=bytes_, peaks=peaks)
        return dict(row)

    async def remove(self, ctx: Any, recording_id: str) -> str | None:
        row = self._find(recording_id)
        if row is None:
            return None
        self.rows.remove(row)
        return str(row["blob_sha256"])

    async def blob_in_use(self, ctx: Any, sha256: str, *, except_id: str | None = None) -> bool:
        return any(r["blob_sha256"] == sha256 and r["id"] != except_id for r in self.rows)


ROW_ID = "11111111-2222-3333-4444-555555555555"


def _row(sha: str, **over: Any) -> dict[str, Any]:
    row = {
        "id": ROW_ID,
        "started_at": datetime(2026, 9, 10, tzinfo=UTC),
        "ended_at": datetime(2026, 9, 10, 0, 0, 42, tzinfo=UTC),
        "duration_s": 42.0,
        "captured_s": 42.0,
        "frequency_hz": 162_550_000,
        "mode": "nfm",
        "bandwidth_hz": 16_000,
        "gain": None,
        "serial": "0092",
        "bytes": len(CLIP),
        "peaks": [0.1, 0.9],
        "blob_sha256": sha,
    }
    return {**row, **over}


def fake(value: object) -> Any:
    """Hand a stand-in to a route typed for the real thing.

    The fakes above are structural, and the routes take concrete types through
    `Depends`. A cast at the call keeps that one fact in one place instead of a
    per-line ignore on every call — which a multi-line call puts on the wrong line
    anyway."""
    return value


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


async def _stored(store: FsBlobStore, data: bytes) -> str:
    return await store.put(data)


def _sidecar(monkeypatch: pytest.MonkeyPatch, health: dict[str, Any] | None) -> None:
    async def _health(_base: str) -> dict[str, Any] | None:
        return health

    monkeypatch.setattr(sdr_api, "_health", _health)


def _ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cut: bytes = TRIMMED,
    measured: float | None = 9.0,
    peaks: list[float] | None = None,
) -> list[tuple[Path, float, float]]:
    """Script the two ffmpeg helpers, and record every cut asked for."""
    asked: list[tuple[Path, float, float]] = []

    async def fake_cut(source: Path, start_s: float, end_s: float) -> bytes:
        asked.append((source, start_s, end_s))
        return cut

    async def fake_levels(_path: Path) -> tuple[list[float], float | None]:
        return (peaks if peaks is not None else [0.3, 0.4]), measured

    monkeypatch.setattr(sdr_api, "cut_clip", fake_cut)
    monkeypatch.setattr(sdr_api, "levels", fake_levels)
    return asked


# --- POST /record --------------------------------------------------------------------


async def test_recording_with_nothing_listening_is_a_409_with_a_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no audio to record, and the owner's next move is to press Listen.

    A silent no-op here is the failure that matters: the tape deck would show a
    recording that does not exist, and stop would save nothing."""
    _sidecar(monkeypatch, _IDLE)
    recorder = _Recorder()

    with pytest.raises(HTTPException) as refused:
        await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert refused.value.status_code == 409
    assert "listening" in str(refused.value.detail).lower()
    assert recorder.started == []


async def test_an_aprs_lease_is_not_something_to_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/listen/audio` serves the LISTENING session specifically, so a packet lease on
    another dongle is not audio anyone asked to keep — recording it would spool 1200-baud
    AFSK into the library."""
    _sidecar(monkeypatch, _APRS_ONLY)

    with pytest.raises(HTTPException) as refused:
        await sdr_api.record(_Settings(), OWNER, _Recorder(), True)  # type: ignore[arg-type]

    assert refused.value.status_code == 409


async def test_record_stores_the_settings_in_force_when_it_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retune does not restart the pipeline, so the row keeps where the clip began."""
    _sidecar(monkeypatch, _listening())
    recorder = _Recorder()

    out = await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert out["recording"]["seconds"] == 0.0
    (started,) = recorder.started
    assert started["frequency_hz"] == 162_550_000
    assert started["mode"] == "nfm"
    assert started["bandwidth_hz"] == 16_000
    assert (started["gain"], started["serial"]) == ("42", "0092")
    assert started["base_url"] == "http://sdr:8000"
    # The row is written under the OWNER's scope, from the principal on the request.
    assert started["ctx"].principal_kind == "owner"


async def test_a_session_with_no_filter_records_a_null_bandwidth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sidecar reports 0 for "no channel filter"; a library saying "0 Hz wide" under
    a clip is worse than saying nothing."""
    _sidecar(monkeypatch, _listening(bandwidth_hz=0))
    recorder = _Recorder()

    await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert recorder.started[0]["bandwidth_hz"] is None


async def test_an_unreachable_sidecar_is_a_502_not_a_started_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sidecar(monkeypatch, None)
    recorder = _Recorder()

    with pytest.raises(HTTPException) as broken:
        await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert broken.value.status_code == 502
    assert recorder.started == []


async def test_a_race_with_the_sidecar_keeps_its_own_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session ended between the health read and the stream open.

    409 with the sidecar's own words, exactly as `_post` maps a refusal: wrapping it in
    a 502 would tell the owner the box is broken when the fix is to press Listen."""
    _sidecar(monkeypatch, _listening())
    recorder = _Recorder(refused=RecorderRefused("nothing is listening", status=409))

    with pytest.raises(HTTPException) as refused:
        await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert refused.value.status_code == 409
    assert refused.value.detail == "nothing is listening"


async def test_an_unexpected_sidecar_status_is_a_502_naming_the_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sidecar(monkeypatch, _listening())
    recorder = _Recorder(refused=RecorderRefused("boom", status=500))

    with pytest.raises(HTTPException) as broken:
        await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert broken.value.status_code == 502
    assert str(broken.value.detail).startswith("sdr sidecar:")


async def test_a_slow_sidecar_is_a_504(monkeypatch: pytest.MonkeyPatch) -> None:
    _sidecar(monkeypatch, _listening())
    recorder = _Recorder(broken=httpx.ConnectTimeout("too slow"))

    with pytest.raises(HTTPException) as slow:
        await sdr_api.record(_Settings(), OWNER, recorder, True)  # type: ignore[arg-type]

    assert slow.value.status_code == 504


async def test_stopping_returns_the_saved_row_without_its_blob_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that held the sha would eventually ask for a blob BY it, and that
    request cannot be scoped to a row the caller was allowed to read."""
    saved = _row("f" * 64)
    recorder = _Recorder(saved=saved)

    def _boom(_base: str) -> None:
        raise AssertionError("stopping must not need the sidecar")

    monkeypatch.setattr(sdr_api, "_health", _boom)

    out = await sdr_api.record(_Settings(), OWNER, recorder, False)  # type: ignore[arg-type]

    assert out["recording"] is None
    assert out["saved"]["id"] == ROW_ID
    assert "blob_sha256" not in out["saved"]
    assert recorder.stops == 1


async def test_stopping_when_nothing_is_recording_is_not_an_error() -> None:
    recorder = _Recorder()

    out = await sdr_api.record(_Settings(), OWNER, recorder, False)  # type: ignore[arg-type]

    assert out == {"recording": None, "saved": None}


# --- GET /sdr/status -----------------------------------------------------------------


async def test_status_carries_the_recording_so_the_deck_needs_no_second_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {"started_at": "2026-09-10T00:00:00+00:00", "seconds": 12.0, "bytes": 96_000}
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(sdr_recorder=_Recorder(active=live)))
    )

    async def _status_of(_settings: Any, recording: Any = None) -> Any:
        return SimpleNamespace(recording=recording)

    monkeypatch.setattr(sdr_api, "status_of", _status_of)
    out = await sdr_api.status(request, _Settings(), OWNER)  # type: ignore[arg-type]

    assert out.recording == live


def test_status_on_a_box_that_has_never_recorded_reports_nothing() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    assert sdr_api.recording_now(request) is None  # type: ignore[arg-type]


def test_the_box_keeps_one_recorder_so_two_presses_cannot_open_two_streams() -> None:
    state = SimpleNamespace(blob_store=object(), session_maker=object())
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    first = sdr_api.get_recorder(request)  # type: ignore[arg-type]
    second = sdr_api.get_recorder(request)  # type: ignore[arg-type]

    assert first is second is state.sdr_recorder


# --- GET /recordings -----------------------------------------------------------------


async def test_the_library_lists_clips_with_the_disk_line_beside_them() -> None:
    repo = _Repo([_row("a" * 64), _row("b" * 64, id="other")])

    out = await sdr_api.recordings(OWNER, repo, 50)  # type: ignore[arg-type]

    assert [r["id"] for r in out["recordings"]] == [ROW_ID, "other"]
    assert out["usage"] == {"count": 2, "bytes": 2 * len(CLIP), "reclaimed_bytes": 0}
    # Not one row leaks where the bytes live.
    assert all("blob_sha256" not in r for r in out["recordings"])


# --- GET /recordings/{id} ------------------------------------------------------------


async def test_one_recording_carries_the_waveform_the_list_leaves_out() -> None:
    """The whole reason this route exists. `peaks` is what the trim sheet draws, and
    without it the sheet is two handles over an empty picture — the shape's argument
    (you can SEE the dead air at each end) missing entirely."""
    repo = _Repo([_row("a" * 64)])

    out = await sdr_api.recording(ROW_ID, OWNER, fake(repo))  # type: ignore[arg-type]

    assert out["peaks"] == [0.1, 0.9]


async def test_one_recording_still_withholds_where_the_bytes_live() -> None:
    """Carrying more than the list does is not carrying everything: a client holding a
    digest would eventually ask for a blob BY it, and that request cannot be scoped to
    the row the caller was allowed to read."""
    out = await sdr_api.recording(ROW_ID, OWNER, fake(_Repo([_row("a" * 64)])))  # type: ignore[arg-type]

    assert "blob_sha256" not in out


async def test_a_recording_that_is_not_yours_reads_as_missing() -> None:
    """RLS answers a non-owner with an empty result, and this reports that as a 404
    rather than a 403 — whether the row exists is not something a caller who cannot
    read it should be able to learn."""
    with pytest.raises(HTTPException) as missing:
        await sdr_api.recording(ROW_ID, OWNER, fake(_Repo()))  # type: ignore[arg-type]

    assert missing.value.status_code == 404


# --- GET /recordings/{id}/audio ------------------------------------------------------


async def test_the_audio_route_resolves_the_blob_from_the_row(blobs: FsBlobStore) -> None:
    """The sha is never taken from the URL: the store holds attachments, notes and
    images too, so a path-supplied digest would serve any of them."""
    sha = await _stored(blobs, CLIP)
    repo = _Repo([_row(sha)])

    out = await sdr_api.recording_audio(ROW_ID, OWNER, repo, blobs)  # type: ignore[arg-type]

    assert Path(out.path) == blobs.path_for(sha)
    assert out.media_type == "audio/mpeg"


async def test_an_unknown_recording_is_a_404(blobs: FsBlobStore) -> None:
    with pytest.raises(HTTPException) as missing:
        await sdr_api.recording_audio(ROW_ID, OWNER, _Repo(), blobs)  # type: ignore[arg-type]

    assert missing.value.status_code == 404


async def test_a_row_whose_audio_is_gone_is_a_404_not_a_500(blobs: FsBlobStore) -> None:
    """A database restored without the blob volume. A 404 with a sentence beats the 500
    a missing file becomes once the response has started."""
    repo = _Repo([_row("c" * 64)])

    with pytest.raises(HTTPException) as missing:
        await sdr_api.recording_audio(ROW_ID, OWNER, repo, blobs)  # type: ignore[arg-type]

    assert missing.value.status_code == 404
    assert "missing" in str(missing.value.detail).lower()


# --- POST /recordings/{id}/trim ------------------------------------------------------


async def test_a_trim_repoints_the_row_and_deletes_the_original(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete is the point: a trim that keeps the original adds a blob and frees
    nothing, which inverts the feature."""
    old = await _stored(blobs, CLIP)
    repo = _Repo([_row(old)])
    asked = _ffmpeg(monkeypatch, measured=9.072)

    out = await sdr_api.trim_recording(
        ROW_ID, sdr_api.TrimIn(start_s=4.0, end_s=13.0), fake(OWNER), fake(repo), blobs
    )

    new = hashlib.sha256(TRIMMED).hexdigest()
    assert asked == [(blobs.path_for(old), 4.0, 13.0)]
    assert repo.rows[0]["blob_sha256"] == new
    assert await blobs.exists(new)
    assert not await blobs.exists(old)
    assert not blobs.path_for(old).exists()
    # What was ACTUALLY cut, measured from the result — the cut lands on a frame
    # boundary, so echoing the request back would be a lie by up to 72 ms.
    assert out["recording"]["duration_s"] == 9.072
    assert out["cut"] == {"start_s": 4.0, "end_s": 13.072}
    assert out["recording"]["bytes"] == len(TRIMMED)
    # `captured_s` is untouched: `duration_s < captured_s` is what makes a row trimmed.
    assert out["recording"]["captured_s"] == 42.0
    assert "blob_sha256" not in out["recording"]


async def test_a_full_length_trim_does_not_delete_its_own_audio(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable, not paranoia: trimming a clip to its whole extent produces identical
    bytes and therefore the identical digest. Deleting "the old blob" there unlinks the
    audio the row was just repointed at, and the recording plays silence for ever."""
    old = await _stored(blobs, CLIP)
    repo = _Repo([_row(old)])
    _ffmpeg(monkeypatch, cut=CLIP, measured=42.0)

    out = await sdr_api.trim_recording(
        ROW_ID, sdr_api.TrimIn(start_s=0.0, end_s=42.0), fake(OWNER), fake(repo), blobs
    )

    assert out["recording"]["duration_s"] == 42.0
    assert await blobs.exists(old)
    assert blobs.path_for(old).read_bytes() == CLIP


async def test_a_blob_another_recording_still_uses_survives_a_trim(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rows with identical bytes are ONE file. Unlinking on the first trim would
    silently empty the second."""
    shared = await _stored(blobs, CLIP)
    repo = _Repo([_row(shared), _row(shared, id="twin")])
    _ffmpeg(monkeypatch)

    await sdr_api.trim_recording(
        ROW_ID, sdr_api.TrimIn(start_s=1.0, end_s=5.0), fake(OWNER), fake(repo), blobs
    )

    assert await blobs.exists(shared)


async def test_a_failed_cut_changes_nothing(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await _stored(blobs, CLIP)
    repo = _Repo([_row(old)])
    _ffmpeg(monkeypatch, cut=b"")

    with pytest.raises(HTTPException) as failed:
        await sdr_api.trim_recording(
            ROW_ID, sdr_api.TrimIn(start_s=1.0, end_s=5.0), fake(OWNER), fake(repo), blobs
        )

    assert failed.value.status_code == 500
    assert "unchanged" in str(failed.value.detail)
    assert repo.rows[0]["blob_sha256"] == old
    assert repo.rows[0]["duration_s"] == 42.0
    assert await blobs.exists(old)


async def test_a_recording_deleted_mid_trim_leaves_no_orphan_blob(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted from another tab between the read and the write. The cut blob would
    otherwise sit on disk for ever with nothing pointing at it."""
    old = await _stored(blobs, CLIP)
    repo = _Repo([_row(old)])
    _ffmpeg(monkeypatch)

    async def vanish(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(repo, "retrim", vanish)

    with pytest.raises(HTTPException) as gone:
        await sdr_api.trim_recording(
            ROW_ID, sdr_api.TrimIn(start_s=1.0, end_s=5.0), fake(OWNER), fake(repo), blobs
        )

    assert gone.value.status_code == 404
    assert not await blobs.exists(hashlib.sha256(TRIMMED).hexdigest())


async def test_trimming_an_unknown_recording_never_reaches_ffmpeg(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _ffmpeg(monkeypatch)

    with pytest.raises(HTTPException) as missing:
        await sdr_api.trim_recording(
            ROW_ID, sdr_api.TrimIn(start_s=1.0, end_s=5.0), fake(OWNER), fake(_Repo()), blobs
        )

    assert missing.value.status_code == 404
    assert asked == []


@pytest.mark.parametrize(
    ("start_s", "end_s", "says"),
    [
        (10.0, 10.0, "after the start"),
        (10.0, 5.0, "after the start"),
        (10.0, 10.05, "at least"),
        (50.0, 60.0, "only 42.0 seconds"),
        (1.0, 90.0, "only 42.0 seconds"),
    ],
)
async def test_an_impossible_trim_is_refused_with_a_sentence(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch, start_s: float, end_s: float, says: str
) -> None:
    """A sentence and a 400, never a 422 validation blob: these numbers come from two
    draggable handles, and every refusal is something the owner can act on there
    (CLAUDE.md #10)."""
    old = await _stored(blobs, CLIP)
    repo = _Repo([_row(old)])
    asked = _ffmpeg(monkeypatch)

    with pytest.raises(HTTPException) as refused:
        await sdr_api.trim_recording(
            ROW_ID, sdr_api.TrimIn(start_s=start_s, end_s=end_s), fake(OWNER), fake(repo), blobs
        )

    assert refused.value.status_code == 400
    assert says in str(refused.value.detail)
    # Nothing ran and nothing changed.
    assert asked == [] and repo.rows[0]["blob_sha256"] == old


async def test_a_trim_whose_result_cannot_be_measured_uses_what_was_asked_for(
    blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg could not decode the cut clip. A missing waveform must not lose the
    recording — the row keeps the requested length and an empty envelope."""
    old = await _stored(blobs, CLIP)
    repo = _Repo([_row(old)])
    _ffmpeg(monkeypatch, measured=None, peaks=[])

    out = await sdr_api.trim_recording(
        ROW_ID, sdr_api.TrimIn(start_s=2.0, end_s=8.0), fake(OWNER), fake(repo), blobs
    )

    assert out["recording"]["duration_s"] == 6.0
    assert out["recording"]["peaks"] == []


# --- DELETE /recordings/{id} ---------------------------------------------------------


async def test_deleting_takes_the_row_and_the_audio(blobs: FsBlobStore) -> None:
    sha = await _stored(blobs, CLIP)
    repo = _Repo([_row(sha)])

    out = await sdr_api.delete_recording(ROW_ID, OWNER, repo, blobs)  # type: ignore[arg-type]

    assert out["deleted"] is True
    assert out["usage"]["count"] == 0
    assert repo.rows == []
    assert not await blobs.exists(sha)


async def test_deleting_one_of_two_identical_clips_keeps_the_audio(
    blobs: FsBlobStore,
) -> None:
    """Content-addressed storage means identical clips are one file."""
    sha = await _stored(blobs, CLIP)
    repo = _Repo([_row(sha), _row(sha, id="twin")])

    await sdr_api.delete_recording(ROW_ID, OWNER, repo, blobs)  # type: ignore[arg-type]

    assert await blobs.exists(sha)
    assert [r["id"] for r in repo.rows] == ["twin"]


async def test_deleting_an_unknown_recording_is_a_404(blobs: FsBlobStore) -> None:
    with pytest.raises(HTTPException) as missing:
        await sdr_api.delete_recording(ROW_ID, OWNER, _Repo(), blobs)  # type: ignore[arg-type]

    assert missing.value.status_code == 404
