# SDR recording — capture, library, trim

> **Status:** In progress · **Last verified:** 2026-09-10 · **Waves:** R0✅ R1◻️ R2◻️ R3◻️
> (R0 — the two-round GUI gate — is closed: capture is `docs/mocks/recording/a-tape-deck.html`,
> trim is `docs/mocks/recording/d-trim-sheet.html`, both binding.)

The radio can hear but not keep. This adds the third tab of the Radio launcher: press
Record while listening, and the clip lands in a library you can play, trim and delete.
It is waves S1 (start/stop record) and S2 (the library) of `SDR_RADIO_PLAN.md`, built
against the settled mocks.

## 1. The decisions already taken

From the GUI gate (`docs/mocks/recording/README.md`), binding:

- **Capture is a tape deck.** Record in the tuner's action row, arm-then-confirm, carrying
  its own elapsed time and running size. A recording is a file.
- **Trim is a sheet** with a waveform, two handles, per-frame nudges and Preview.
- **A trim discards the original.** No keep-both. This is why `BlobStore` grows a
  `delete()` and why Preview is mandatory rather than a nicety.
- **Nothing expires.** No retention prune. The disk meter argues instead; trim and delete
  are the only things that remove audio.

## 2. Why the api is the recorder, not the sidecar

`deploy/docker-compose.yml` gives the `sdr` service **no volume** and `mem_limit: 512m`,
so anything it writes dies on the next Ops → Update. It also cannot hold the owner's data
under RLS. So the sidecar is unchanged by this plan: it already serves
`GET /listen/audio` as chunked 64 kbps MP3 with a subscriber fan-out
(`deploy/sdr/listen.py`), and the api already proxies that at `GET /api/sdr/audio`.

**Recording is one more subscriber.** The api opens its own stream to the sidecar and
spools it straight into `blobs.put_stream(...)` — never buffering the clip in memory —
then writes a row. That satisfies CLAUDE.md #2 (storage abstraction) and #3 (RLS) without
the sidecar learning about disks at all.

Consequences to respect:

- A retune **does not** restart the pipeline on the I/Q engine, so a recording may span a
  frequency change. The row stores the settings at the moment recording *started*, and
  that is what the library shows.
- The sidecar ending a session closes the stream. The recorder must finalize the blob and
  write the row it has, not discard it — an interrupted recording is still a recording.

### The arithmetic, measured

64 kbps mono MP3 = **8 kB/s, 480 kB/minute, 28.8 MB/hour**. An MP3 frame is 1152 samples,
which at the sidecar's 16 kHz is **72 ms** — the granularity of a lossless cut.

## 3. Data model

One new table, `app.sdr_recordings`, **owner-only** (`app.is_owner()`, `FORCE ROW LEVEL
SECURITY`, plus the `GRANT` — see `migrations/versions/0180_aprs_packets.py`, which is the
template) with the isolation test CLAUDE.md #3 requires. No `domain_code`: like the APRS
log, what the radio overheard is not domain-scoped data, it is simply the owner's.

| Column | Why |
| --- | --- |
| `id uuid` | |
| `started_at`, `ended_at timestamptz` | |
| `duration_s double precision` | what the clip IS now |
| `captured_s double precision` | what was originally recorded — `duration_s < captured_s` is what makes a row "trimmed", so the library can say so without a flag that can drift |
| `frequency_hz bigint`, `mode text`, `bandwidth_hz int`, `gain text?`, `serial text?` | the settings at the moment Record was pressed |
| `blob_sha256 text`, `bytes bigint` | the audio, through the storage abstraction |
| `peaks jsonb` | the level envelope the trim sheet draws, computed once at stop and again after a trim (see §5) |
| `transcript jsonb?`, `transcribed_at timestamptz?` | R3; the shape `AudioTranscript.tsx` consumes |

Index on `started_at DESC` — the only order the library is read in.

## 4. The API contract

All routes are `OwnerDep` under `/api/sdr`, and follow `api/sdr.py`'s existing error
mapping (409 busy, 400 refusal-with-a-sentence, 502 `sdr sidecar: …`, 504 timeout).

| Route | Does |
| --- | --- |
| `POST /record?on=true\|false` | Start/stop against the live listen session. Idempotent both ways, like `POST /sdr/aprs`. Starting with nothing listening is a **409 with a sentence**. Returns `{recording, saved?}`. |
| `GET /recordings?limit=` | `{recordings: [...], usage: {bytes, count, reclaimed_bytes}}` — newest first. |
| `GET /recordings/{id}/audio` | `FileResponse(blobs.path_for(sha), media_type="audio/mpeg")` — Range comes free from Starlette, which is what makes the trim sheet's Preview and scrubbing work. Resolve the sha **from the RLS-scoped row**, never from the URL. |
| `POST /recordings/{id}/trim` | Body `{start_s, end_s}`. Cuts, repoints, **deletes the old blob**. Returns the row. |
| `DELETE /recordings/{id}` | Row and blob. |

`GET /sdr/status` gains a `recording` object (or null) so the tuner can draw its elapsed
time and size from the same 1 Hz poll everything else uses — no second timer.

## 5. Trim

`ffmpeg -ss {start} -to {end} -i {blob} -c copy -f mp3 {out}` — **lossless and instant**,
because the frames are copied rather than re-encoded. The cost is that the cut lands on a
frame boundary, within 72 ms of where the handle was. That is why the sheet offers a
per-frame nudge and does not imply millisecond precision.

Then: `put` the result (a new content-addressed blob), update the row, `delete()` the old
blob. **The delete is the point** — a trim that keeps the original adds a blob and frees
nothing, which inverts the feature.

`peaks` is recomputed from the trimmed audio in the same pass, so the sheet never draws a
waveform that disagrees with the clip.

**`BlobStore` gains `delete(sha256)`** (Protocol + `FsBlobStore`). It is the first thing
in the repo to delete a blob, so it needs its own test: deleting one blob leaves the
others, and deleting a missing one is not an error.

## 6. Waves

### R1 — the recorder and the library
Migration + RLS isolation test. `sdr/recorder.py` (the streaming recorder, one active at a
time), `sdr/recordings.py` (reader/repo). `BlobStore.delete()`. Routes: `record`,
`recordings`, `recordings/{id}/audio`, `DELETE`. Peaks at stop. `recording` on status.

### R2 — trim
`POST /recordings/{id}/trim`, the ffmpeg copy-cut, blob repoint + delete, peak recompute.
Frame-boundary rounding is server-side truth: the client asks for seconds, the server
answers with what it actually cut.

### R3 — the PWA
Record wired in `SdrTunerControls` (the disabled button is already there). The Recordings
tab replacing the placeholder in `RadioScreen`. The trim sheet on the shared `Sheet`
shell. `api/client.ts` methods + `api/mock.ts` fixtures for default/empty/error/offline,
which DESIGN.md makes part of done.

### R4 — transcription *(deferred, not in the first PR)*
`transcribe_audio_chunked` over the stored clip, corpus persistence + embedding enqueue
per `SDR_RADIO_PLAN.md` §4.3, transcript re-cut on trim. The library and trim both work
without it; the rows simply have no transcript preview yet.

## 7. Open

- **Is Record really arm-then-confirm?** Inherited from `docs/mocks/sdr-tuner/a-tuner-sheet.html`
  and honoured, but starting a recording destroys nothing. Trim has earned the ceremony;
  Record may not have.
- **A recording spanning a retune** keeps its start-time settings in the row. If that
  proves confusing on air, the alternative is a settings-changed marker rather than a
  second row.
