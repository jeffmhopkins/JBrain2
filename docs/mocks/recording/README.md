# Recording — keeping what the radio heard (GUI-gate mockups)

> **Status:** Living · **Last verified:** 2026-09-09

The radio gains the ability to **keep** what it hears. These three mockups are the
`docs/reference/PROCESS.md` GUI gate for that surface; the feature itself is waves **S1**
(start/stop record) and **S2** (the library) of `../../plans/SDR_RADIO_PLAN.md`.

**The launcher's shape is already settled and is not being re-litigated.** Tabs are
`Radios | APRS | Recordings` (shape A, `../sdr-launcher/`). What has never been designed
is the Recordings tab itself — today it is a one-line placeholder in `RadioScreen.tsx`
— and the capture control that fills it, which ships as a disabled `Record` button in
`SdrTunerControls.tsx`. That hole is what these mocks are for.

## What the box can actually do, measured

None of this is aspirational. The numbers below are read off the shipped code, and every
size in every mock is computed from them.

| Fact | Where |
| --- | --- |
| Demodulated audio is **mono int16 at 16 kHz** | `deploy/sdr/demod.py` `AUDIO_RATE` |
| It is already encoded to **64 kbps MP3** by ffmpeg, with a subscriber fan-out | `deploy/sdr/listen.py` `_enc_cmd`, `_pump_audio` |
| So a recording costs **8 kB/s — 480 kB/minute, 28.8 MB/hour** | arithmetic |
| Every PCM byte passes one chokepoint on both engines | `listen.py` `_record` |
| A retune **does not** restart the pipeline on the I/Q engine | `listen.py` `Session.tune` |

Recording is therefore not a new pipeline. It is one more subscriber on a stream that
already exists — which is why the interesting question here is a design question, not an
engineering one.

### Two constraints that decide the architecture

1. **The recording cannot live on the sidecar.** `deploy/docker-compose.yml` gives the
   `sdr` service **no volume** and `mem_limit: 512m`. Anything it writes to disk dies on
   the next Ops → Update. So the bytes go to the api and through the storage abstraction
   into the `blobs` volume — which is what CLAUDE.md #2 requires anyway, and where
   retention, RLS and byte-serving already work.
2. **Nothing in this repo deletes a blob.** `backend/src/jbrain/storage.py`'s `BlobStore`
   has `put` / `get` / `exists` / `usage` and **no `delete`**. Every mock here shows a
   Delete action and a retention line, and both of them require that method to be added.
   It is named here rather than discovered in the middle of S2.

Playback is nearly free in the other direction: Starlette's `FileResponse` implements
HTTP Range natively, so scrubbing inside a recording needs no range code —
`api/chat_attachments.py`'s owner-gated blob download is the template.

## What all three share

- **The clip carries its radio settings** — frequency, mode, bandwidth — so a recording
  of 5 MHz at 6 kHz is still legible as that a year later.
- **Sizes are always shown**, because the owner runs this box remotely and a library
  that quietly fills a disk is a support call they cannot answer from a phone.
- **Transcription on arrival**, via the existing `transcribe_audio_chunked`, landing in
  the external corpus and hybrid search per the plan's §4.3. The mocks differ in how
  much they *lean* on that, not in whether it happens.
- Playback reuses `AudioTranscript.tsx`, which the plan already names.

## The shapes

**A — `a-tape-deck.html`. A recording is a file.** Record sits in the transport next to
the radio it records; the library is a reverse-chronological list grouped by day, each
row expanding to a transcript and download / send-to-chat / delete. This is the shape
`SDR_RADIO_PLAN.md` already assumes and the one nobody has to be taught. *Its weakness is
the one the hobby actually has: it only captures what you decided to keep **before** it
happened, and on shortwave the interesting thing is usually over by the time your thumb
arrives.*

**B — `b-rolling-buffer.html`. The box is always holding the last two minutes.** The
primary control is not "start recording" but **"save what just happened"** — and because
the buffer is drawn as an audio-level strip, you can *see* the transmission and drag a
selection around it. A "Keep rolling" toggle is the ordinary record button for when you
do know in advance. **It costs about a megabyte**: two minutes of the existing 64 kbps
stream is 960 kB of RAM, which the 512 MB sidecar limit swallows without noticing.
*Its weakness is that it is a concept — one screenshot does not explain it, and a buffer
that only fills while you are listening may not be where the owner expects it to be.*

**C — `c-the-log.html`. A recording is something the box heard.** The Recordings tab is
a **search over what was said** rather than a column of timestamps, and the capture
control grows a **Watch this channel** toggle that records whenever the squelch opens —
so the box fills the log while nobody is there. This is the shape that treats the radio
as a knowledge source feeding hybrid search, which is what the plan's title promises.
*Its weakness is honesty: narrowband radio voice transcribes badly, and a garbled line in
a searchable log is worse than no line. It also implies unattended capture, which is the
one shape here that can fill a disk while the owner is asleep.*

All three are single-file and fully offline, dark-first with a working light/dark toggle,
phone-framed, tokens-only (no raw hex outside the token sheet), ≥44px targets,
`prefers-reduced-motion` honoured, keyboard-operable, and Lucide-style inline icons
rather than glyphs. Each renders clean in both themes with no console errors.

## Open questions for the owner, alongside the shape

1. **Is Record really arm-then-confirm?** `../sdr-tuner/a-tuner-sheet.html` is a binding
   spec and it says yes ("tap, 'Tap again', 2.6s window, per the destructive-action
   doctrine"). All three mocks honour it. But starting a recording destroys nothing —
   the ceremony may be a mis-inherited rule, and on B it actively fights the point, since
   the whole idea is to catch something that is *already ending*. Worth deciding
   explicitly rather than by inheritance.
2. **What is the retention?** The mocks show "oldest kept 90 days" as a placeholder.
   APRS chose 14 days for a log of strangers' traffic; a recording the owner deliberately
   kept is a different thing and probably should not expire at all, with the disk meter
   doing the arguing instead.
3. **B and C are not exclusive.** A rolling buffer and a watched channel are both answers
   to "the box should catch what I would have missed", and the library underneath them is
   the same library. If the answer is "B now, C later", the buffer is the smaller build
   and the one that needs no squelch tuning.
