# JBrain2 — Kokoro TTS Consolidation

> **Status:** In progress · **Last verified:** 2026-08-11 · **Waves:** W1✅ W2✅ W3✅ W4◻️

Standardize read-aloud on **Kokoro only**, collapse the three overlapping text
normalizers into **one on the box**, make the misaki-vs-espeak phonemizer path
**visible**, and give the owner a **plain-respelling pronunciation list** — fixing
the reported symptoms (an unwanted pause after "U.S.", flat/uncharacteristic
headings, and "Titusville" mispronounced) at their root rather than per-word.

Owner decisions ratified up front (this is the binding spec, not the open menu):

- **Remove Piper entirely.** Kokoro is the sole on-box engine.
- **Fallback = browser-native only.** When the box can't run Kokoro, read-aloud
  falls back to the device's Web Speech voice (needs no box). No internal Piper
  safety net. Kokoro provisioning is made **loud** (a failed weight/venv build is
  surfaced, never a silent degrade) so an outage is observable, not guessed.
- **One normalizer, on the box.** `tts_server.py` becomes the single source of
  truth for text normalization; the PWA keeps only the streaming chunker; the wall
  drops its own `mdToPlain`.
- **Pronunciation UI = plain respelling.** The owner types a word + how to say it
  ("Titusville → Tight us ville"); engine-agnostic, no phonemes to learn.

## Why (the diagnosis)

Five research passes over the read-aloud stack established:

1. **"U.S." pause.** `frontend/src/agent/speakable.js` deliberately preserves the
   interior periods in `U.S.` (to protect single-initial names like "Dennis E."),
   but nothing removes them, so `splitClips` and the streaming committer
   `intraLineSafe` read `U.S. ` as a sentence end and cut a sentence into two
   separate renders — the audible gap. espeak's per-dot falling intonation adds to
   it. The same bug hits `U.K./U.N./E.U./D.C./a.m./p.m./Ph.D.`.
2. **Flat headings.** `toProse` strips the `#`, then pause-authoring appends a `.`,
   so a heading becomes an isolated 1–4-word sentence rendered as its own clip with
   zero co-text — neural voices have no contour to give it. A **colon** terminal
   (a pause cue that is *not* a clip boundary) lets a heading flow into the next
   sentence with lead-in intonation.
3. **Titusville.** A phoneme override already exists —
   `deploy/tts-stt/tts_server.py::KOKORO_LEXICON` seeds `"titusville":
   "tˈItəsvɪl"` — but it applies **only on the misaki path**. If the box silently
   fell back to espeak, the override is inert and the mispronunciation returns.
   Titusville-still-wrong is itself the symptom of an espeak fallback.
4. **Three normalizers.** `speakable.js` (PWA, full: numbers/dates/currency/
   symbols/tables), `tts_server.py::_speakable_text` (box, partial: dates/units/
   states/acronyms, **no number verbalization**, runs for *every* render), and
   `deploy/wall/index.html::mdToPlain` (wall, strip-only). Number verbalization
   works only on the PWA path; dates are handled twice, differently; the wall gets
   neither. Coverage gaps fall out of this split (phone numbers read as arithmetic
   ranges, `3:30pm`/`2026-08-10`/`v2.3.1` mangled, `<`/`>`/`≤` dropped,
   `snake_case` underscores eaten).
5. **Path/parity.** Every audio path already flows through `tts_server.py` (PWA
   proxy, wall, pet), which is why the box is the right single home — it is
   colocated with the phonemizer and the lexicon, the one place pronunciation truth
   lives. The PWA's chunker must stay client-side (it drives progressive playback),
   but it can operate on raw markdown boundaries without verbalizing.

Full research notes: see the commit history of this branch; findings are folded
into the wave tasks below rather than kept as a separate dossier.

## Target architecture (as built)

```
PWA answer/markdown ──speakable.js (normalize + chunk)──▶ /api/brain/tts ──┐
wall answer/markdown ──mdToPlain──▶ /tts (box) ────────────────────────────┤
                                                                           ▼
                api injects per-owner respelling map (brain.py, has the principal) [W4]
                                                                           ▼
             box tts_server.py: _speakable_text (thin engine-agnostic mirror, for the wall)
                                                                           ▼
                            KOKORO_LEXICON phoneme overrides (misaki path)
                                                                           ▼
                    Kokoro (misaki G2P; espeak = degraded, surfaced by /tts/health)
```

- **`speakable.js` is the single source of truth** for the text-normalization rules
  (numbers/dates/currency/symbols/tables/abbreviations + the W3 fixes). It stays
  client-side because the streaming chunker (`chunkStream`/`committedLen`/
  `splitClips`) needs normalized clip boundaries to drive progressive playback — see
  the W3 architecture note for why a literal box-side single-normalizer was rejected.
- **The box `_speakable_text`** carries a *thin mirror* of the engine-agnostic fixes
  (dates/states/acronyms/dotted-initialisms/relations) for the **wall** path, which
  reaches the box as near-raw text; it is redundant-but-idempotent for the PWA path
  (which arrives already normalized).
- **Per-owner respelling map** [W4] is injected by `brain.py` (which holds the
  principal) as a plain whole-word substitution *before* forwarding — engine-agnostic
  and independent of the DB-free box.
- **Phoneme overrides** (`KOKORO_LEXICON`) stay on the box, misaki-only, for
  power-user exactness; the owner-facing feature is the respelling map.

## Waves

### W1 — Phonemizer-path observability ✅ (shipped on-branch)

- `GET /tts/health` on the box → `{kokoro_available, g2p:
  "misaki"|"espeak"|"unavailable", lexicon_entries, voice_count}`; loading the G2P
  also pre-warms misaki. `GET /api/brain/tts/health` proxy on the api.
- Tests: box (`test_tts_server.py`) misaki/espeak/unavailable; api proxy
  (`test_brain_proxy.py`) passthrough + auth + 503.
- *Deferred to W4:* surface `g2p` in the Settings panel (rides the pronunciation
  UI's GUI gate).

### W2 — Remove Piper ✅

- **Box:** delete the piper voice resolver, warm cache, curated multi-speaker
  machinery, and `_prewarm`; `tts_wav` renders Kokoro only; `piper_voices()` →
  Kokoro ids. Rename left only where it doesn't churn callers. Make the Kokoro
  weight fetch + misaki venv build **loud but NON-FATAL** in `Dockerfile.tts-stt`
  (keep the `|| …` non-fatal structure; only the message goes loud — an
  unmistakable multi-line stderr banner) so a transient weight-fetch blip can't
  abort an otherwise-fine `jbrain update`, yet a real outage is greppable in the
  build/`docker logs`. Browser-native is the fallback when the box has no Kokoro,
  so the build must never be fatal on a Kokoro hiccup.
- **Backend:** `brain.py` voices/speakers reflect Kokoro-only; `settings_store`
  `brain_answer_voice` default → a Kokoro voice; `brain_read_aloud_engine` domain
  → `kokoro | native`.
- **Frontend:** `engineForVoice` retired (always Kokoro); Settings engine control
  → Kokoro | Native; voice picker Kokoro-only.
- **Wall:** read-aloud uses a Kokoro voice (`kokoro-af_heart` / `kokoro-am_michael`);
  drop baked piper voices.
- **Docs/setup:** `install-tts.sh` deleted, `deploy/tts-stt/README.md`,
  `SERVICES.md`, `DESIGN.md` reconciled. Tests updated across all four surfaces.
- *Risk:* Kokoro is now the only box voice — W1 health + the loud (non-fatal)
  provisioning are the mitigations; browser-native covers box-unreachable and a
  box that built without Kokoro.

### W3 — Read-aloud fixes + wall parity ✅

**Architecture note (a deviation from the original "collapse to the box" target,
ratified during build):** the literal single-normalizer-on-the-box target fights the
streaming design. The PWA read-aloud chunker (`chunkStream`/`committedLen`/
`splitClips`) MUST run client-side — it decides clip boundaries to drive
progressive per-clip `/tts` calls — and it needs *normalized* boundaries, so the
normalizer naturally lives with it in `speakable.js`. A true single-file merge would
require either a normalize-and-split round-trip endpoint (extra latency, lost
sentence-granularity streaming) or maintaining **two byte-identical copies** of
`speakable.js` (PWA + a no-build-step wall adoption) behind a parity guard — *more*
fragility, not less. So the consolidation is: **`speakable.js` is the single source
of truth** for the rules; the box `tts_server.py::_speakable_text` carries a *thin
mirror* of the engine-agnostic fixes for the wall path (which reaches the box as
near-raw text). This delivers the symptom fixes + correctness on every path without
the risky, untestable wall rewrite.

- **Symptom fixes (speakable.js):** dotted-initialism collapse (`U.S.`→`U S`) before
  pause-authoring, the clip splitter, AND the streaming committer's `ABBREV_NO_BREAK`
  guard, so "The U.S. economy" is one clip/one render; heading terminal `:` instead
  of `.` (a pause cue `splitClips` doesn't cut on) so a heading leads into the next
  sentence, with `committedLen` holding a heading with its block so streaming ==
  one-shot.
- **Coverage fixes (speakable.js):** inequalities (`<`,`>`,`<=`,`>=`,`!=`,`±` —
  dropping them inverts meaning), `snake_case`, clock times (`3:30pm`/`14:00`), phone
  numbers (digit-by-digit, not arithmetic ranges), multi-part versions (`v2.3.1`),
  ISO dates (`2026-08-10`). Full unit + golden coverage.
- **Wall parity (box `_speakable_text`):** mirrored the two engine-agnostic headline
  fixes — the dotted-initialism collapse and the inequality/relation map — so the
  wall path is also correct. (Heading-colon needs the `#` marker, which the wall's
  `mdToPlain` strips before the box sees it, so it stays PWA-only; the wall is a
  secondary display.)
- **Rename:** `piper_server.py` → `tts_server.py` (+ `test_tts_server.py`) now that
  Piper is gone — entrypoint, compose, Dockerfile, and docs updated.
- *Deferred (see Open questions):* the literal reduction to ONE normalizer (wall
  adopts `speakable.js`; box `_speakable_text` deleted). Low user value (wall is
  secondary), higher risk (untestable no-build-step display), and it trades one
  normalizer for two byte-identical copies — revisit only if the wall path needs the
  full verbalization set.

### W4 — Owner-editable respelling lexicon + Settings panel ◻️ (built on-branch; marker flips on merge)

- `settings_store` key `pronunciation_lexicon` (`{word: respelling}`, sanitized +
  bounded: non-dict/blank/over-long dropped, ≤ 200 entries), exposed via
  `SettingsOut`/`SettingsPatch` in `api/settings.py` (owner-only, RLS — invariant #3,
  no migration: constant, not a row seed). ✅ shipped on-branch.
- `brain.py /brain/tts` reads the map and applies a whole-word, case-insensitive
  substitution to `text` before forwarding — engine-agnostic (fixes Titusville on the
  misaki OR espeak path), best-effort (a settings-read hiccup skips respelling, never
  fails read-aloud). Covers every PWA read-aloud path (chat/report/custom-text) since
  all route through this proxy. ✅ shipped on-branch.
- **GUI gate (PROCESS.md):** three interactive mocks landed in
  `docs/mocks/pronunciations/` (a-inline-list, b-edit-sheet, c-add-first-chips); the
  owner chose **A — inline list**, now the binding spec. ✅
- **Panel (mock A):** an inline word → say-it-like list in the read-aloud settings
  card with a per-row Test ▷ / remove ✕, an "Add a pronunciation" expander, and a
  phonemizer health chip (misaki ✓ / espeak "pronunciations still apply, quality
  limited") fed by a new `brainTtsHealth()` over the W1 `/api/brain/tts/health`.
  Edits PUT the full map (replace semantics); Test plays the respelling through the
  box. Gated to the on-box (Kokoro) model. ✅ shipped on-branch.
- Tests: store sanitize + round-trip (unit + Postgres integration), `/api/settings`
  round-trip, the brain.py substitution + read-failure fallback, and the panel
  (render/add/delete/health). ✅
- *Note:* per `DOC_LIFECYCLE.md` the W4 header marker + this plan's archival happen in
  the PR that MERGES the wave; everything above is complete on the branch.

## Non-negotiables touched

- **#2** (storage abstraction): respelling map via the settings store, not raw
  paths. **#3** (RLS): new settings key is owner-only; an RLS isolation test lands
  with it. **#5** (tests same PR, 80%/security-100%). **#8** (`dev-setup.sh`
  updated with any tool/step change in W2). **#9** (docs travel: this plan flips
  per wave and archives at W4; `SERVICES.md`/`DEVELOPMENT.md`/READMEs reconciled).

## Open questions / deferrals

- Whether to keep the `KOKORO_LEXICON` phoneme map as a documented power-user
  escape hatch alongside the owner respelling map (leaning yes — Titusville stays
  seeded there; respelling is the friendly layer on top).
- **Deferred: the literal single-normalizer merge.** The original "collapse to ONE
  normalizer on the box" was reframed in W3 (see the W3 architecture note):
  `speakable.js` is the single source of truth, with a thin box mirror for the wall.
  Fully deleting the box `_speakable_text` would require the wall to adopt
  `speakable.js` verbatim (a no-build-step display, untestable here) and a byte-parity
  guard between two copies — *more* fragility for a secondary surface. Revisit only if
  the wall needs the full verbalization set (numbers/times/etc.), or if the wall is
  ever rebuilt with a bundler.
