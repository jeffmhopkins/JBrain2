# Recording — keeping what the radio heard (GUI-gate mockups)

> **Status:** Living · **Last verified:** 2026-09-09

The radio gains the ability to **keep** what it hears. These mockups are the
`docs/reference/PROCESS.md` GUI gate for that surface; the feature itself is waves **S1**
(start/stop record) and **S2** (the library) of `../../plans/SDR_RADIO_PLAN.md`.

The launcher's shape was already settled (`Radios | APRS | Recordings`, shape A,
`../sdr-launcher/`). What had never been designed is the Recordings tab itself — a
placeholder line in `RadioScreen.tsx` — and the capture control, a disabled `Record`
button in `SdrTunerControls.tsx`.

Two rounds ran. **Round 1 settled what a recording is. Round 2 is open, and settles how
you trim one.**

## What the box can actually do, measured

None of this is aspirational. Every size in every mock is computed from these.

| Fact | Where |
| --- | --- |
| Demodulated audio is **mono int16 at 16 kHz** | `deploy/sdr/demod.py` `AUDIO_RATE` |
| It is already encoded to **64 kbps MP3**, with a subscriber fan-out | `deploy/sdr/listen.py` `_enc_cmd`, `_pump_audio` |
| So a recording costs **8 kB/s — 480 kB/minute, 28.8 MB/hour** | arithmetic |
| Every PCM byte passes one chokepoint on both engines | `listen.py` `_record` |
| An MP3 frame is 1152 samples — **72 ms** at 16 kHz | why a lossless cut is only frame-accurate |

Recording is therefore not a new pipeline; it is one more subscriber on a stream that
already exists.

### Three constraints that decide the build

1. **The recording cannot live on the sidecar.** `deploy/docker-compose.yml` gives the
   `sdr` service **no volume** and `mem_limit: 512m`. Anything it writes dies on the next
   Ops → Update. The bytes go to the api and through the storage abstraction into the
   `blobs` volume — which CLAUDE.md #2 requires anyway, and where retention, RLS and
   byte-serving already work.
2. **Nothing in this repo deletes a blob.** `backend/src/jbrain/storage.py`'s `BlobStore`
   has `put` / `get` / `exists` / `usage` and **no `delete`**. Every "frees 1.1 MB" in
   these mocks is contingent on that method being added — a trim that keeps the original
   costs disk rather than saving it.
3. **A trim is a new blob, not an edit.** Blobs are content-addressed by sha256, so the
   trimmed clip is a fresh key and the row repoints at it. `ffmpeg -c copy -ss -to` cuts
   MP3 on a frame boundary: **lossless and instant, accurate to 72 ms**. Re-encoding
   would be sample-exact and lossy; frame-accurate is the right trade for a radio clip,
   and it is why every mock offers a nudge rather than pretending to millisecond
   precision. The transcript has to be re-cut alongside it, with word times rebased.

Playback is nearly free in the other direction: Starlette's `FileResponse` does HTTP
Range natively, so scrubbing needs no range code — `api/chat_attachments.py`'s
owner-gated blob download is the template.

## Round 1 — what a recording is. **Chosen: A** (2026-09-09)

**A — `a-tape-deck.html`. A recording is a file.** Record sits in the transport next to
the radio it records; the library is a reverse-chronological list grouped by day, rows
expanding to a transcript with download / send-to-chat / delete. The owner picked it, and
asked for one thing it did not have: **trim**. That request is exactly right, and it
repairs A's one real weakness — a tape deck captures what you decided to keep *before* it
happened, so every clip has dead air at both ends and the good part is somewhere in the
middle. Trim turns a rough take into the thing worth keeping, and is the only feature here
that gives disk back.

Rivals, retained, not built:

**B — `b-rolling-buffer.html`.** The box always holds the last two minutes; the control is
"save what just happened", dragged over a drawn level strip. Costs 960 kB of RAM. Still the
best answer to "the interesting thing was already over", and the natural follow-on if
capture ever needs revisiting — the trim surface chosen below is the same surface a buffer
save would land in.

**C — `c-the-log.html`.** The tab is a search over what was said, and a watched channel
fills the log unattended. Its transcript-first stance survives into round 2 as shape F.

## Round 2 — how you trim. **Open.**

All three carry A's settled capture unchanged; the Listen tab is identical in each. They
differ only in where the trim lives. Each shows the same library, including one clip that
is **already trimmed** and one with **no speech at all**, because both are cases the
chosen shape has to handle.

**D — `d-trim-sheet.html`. Trim in a sheet.** The scissors on a row opens a dedicated
sheet: the whole clip as a waveform, two handles, per-frame nudge buttons, Preview that
plays only the selection, and a "keep the full capture as well" escape hatch. *The
waveform is the argument — radio has real silence in it, so the part worth keeping is
visible.* One place to be careful in, and the only shape with room for Preview.
**Costs a modal**, on a surface that is otherwise flat.

**E — `e-trim-inline.html`. Trim is the scrub bar.** No mode, no second surface: a row
expands into a player whose two end caps are the handles. Trimming and listening become
one activity on the same 58 pixels — scrub to where it starts, pull the cap to the
playhead. *Apply stays disabled until a handle actually moves*, so a fumbled scrub can
never silently shorten a recording (there is a test for exactly that). **Costs precision**
— a cap is a small target beside a big one, and there is no room for a preview.

**F — `f-trim-by-transcript.html`. Trim by what was said.** Tap the first word to keep,
then the last; the cut lands on those word timestamps, and the waveform below is a readout
rather than a control. *Nobody thinks about a recording in seconds — they think "the bit
where it gives the solar flux"*, and because the transcript feeds hybrid search, the words
you trim **to** are the words you later search **for**. **Costs a fallback**: the 121.500
clip has no speech, and on shortwave a great many captures are tones, silence, or a signal
too weak to copy — so this shape still needs D's or E's handles underneath it, which is
visible on that row rather than hidden.

All are single-file and fully offline, dark-first with a working light/dark toggle,
phone-framed, tokens-only (no raw hex outside the token sheet), ≥44px targets,
`prefers-reduced-motion` honoured, keyboard-operable (handles are `role="slider"` with
arrow keys and Shift for a coarse step; words in F are focusable buttons), and Lucide-style
inline icons rather than glyphs. Each was driven end to end in Chromium — drag, apply, and
the disk meter moving — with no console errors in either theme.

## Still open alongside the shape

1. **Does a trim discard the original?** The mocks default to yes, with D offering a
   "keep the full capture as well" checkbox. Keeping both is the safe answer and the one
   that saves nothing.
2. **Is Record really arm-then-confirm?** `../sdr-tuner/a-tuner-sheet.html` is a binding
   spec and says yes. All mocks honour it, but starting a recording destroys nothing —
   worth deciding explicitly rather than by inheritance. Trim, by contrast, genuinely is
   destructive, and gets the ceremony in every shape here.
3. **What is the retention?** The mocks show "oldest kept 90 days" as a placeholder, which
   the disk meter replaces the moment anything is trimmed. A clip the owner deliberately
   kept and trimmed probably should not expire at all.
