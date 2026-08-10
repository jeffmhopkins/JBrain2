# JBrain2 — Kokoro TTS Consolidation

> **Status:** In progress · **Last verified:** 2026-08-10 · **Waves:** W1✅ W2◻️ W3◻️ W4◻️

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
- **One normalizer, on the box.** `piper_server.py` becomes the single source of
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
   `deploy/tts-stt/piper_server.py::KOKORO_LEXICON` seeds `"titusville":
   "tˈItəsvɪl"` — but it applies **only on the misaki path**. If the box silently
   fell back to espeak, the override is inert and the mispronunciation returns.
   Titusville-still-wrong is itself the symptom of an espeak fallback.
4. **Three normalizers.** `speakable.js` (PWA, full: numbers/dates/currency/
   symbols/tables), `piper_server.py::_speakable_text` (box, partial: dates/units/
   states/acronyms, **no number verbalization**, runs for *every* render), and
   `deploy/wall/index.html::mdToPlain` (wall, strip-only). Number verbalization
   works only on the PWA path; dates are handled twice, differently; the wall gets
   neither. Coverage gaps fall out of this split (phone numbers read as arithmetic
   ranges, `3:30pm`/`2026-08-10`/`v2.3.1` mangled, `<`/`>`/`≤` dropped,
   `snake_case` underscores eaten).
5. **Path/parity.** Every audio path already flows through `piper_server.py` (PWA
   proxy, wall, pet), which is why the box is the right single home — it is
   colocated with the phonemizer and the lexicon, the one place pronunciation truth
   lives. The PWA's chunker must stay client-side (it drives progressive playback),
   but it can operate on raw markdown boundaries without verbalizing.

Full research notes: see the commit history of this branch; findings are folded
into the wave tasks below rather than kept as a separate dossier.

## Target architecture

```
PWA answer/markdown ──chunkStream (raw-boundary split, no verbalize)──▶ /api/brain/tts
wall answer/markdown ──(raw)──────────────────────────────────────────▶ /tts (box)
                                                                         │
                          api injects per-owner respelling map ─────────┤ (brain.py, has the principal)
                                                                         ▼
                                          piper_server.py: _speakable_text (THE normalizer)
                                            markdown→prose + verbalize + U.S./heading/coverage fixes
                                                                         ▼
                                            KOKORO_LEXICON phoneme overrides (misaki path)
                                                                         ▼
                                              Kokoro (misaki G2P; espeak = degraded, surfaced)
```

- **Engine-agnostic structural + verbalization rules** live once, in
  `_speakable_text`. Because the box is DB-free by design, the **per-owner
  respelling map** is injected by `brain.py` (which holds the principal) as a plain
  whole-word text substitution *before* forwarding — so it works regardless of
  engine and independent of the DB-free box.
- **Phoneme overrides** (`KOKORO_LEXICON`) stay on the box, misaki-only, for
  power-user exactness; the owner-facing feature is the respelling map.
- **`speakable.js`** shrinks to `chunkStream`/`committedLen`/`splitClips` +
  `reportToSpeech` (a cheap report-structure preprocessor). It keeps the
  abbreviation-aware boundary guard so a dotted initialism never splits a clip.

## Waves

### W1 — Phonemizer-path observability ✅ (shipped on-branch)

- `GET /tts/health` on the box → `{kokoro_available, g2p:
  "misaki"|"espeak"|"unavailable", lexicon_entries, voice_count}`; loading the G2P
  also pre-warms misaki. `GET /api/brain/tts/health` proxy on the api.
- Tests: box (`test_piper_server.py`) misaki/espeak/unavailable; api proxy
  (`test_brain_proxy.py`) passthrough + auth + 503.
- *Deferred to W4:* surface `g2p` in the Settings panel (rides the pronunciation
  UI's GUI gate).

### W2 — Remove Piper ◻️

- **Box:** delete the piper voice resolver, warm cache, curated multi-speaker
  machinery, and `_prewarm`; `tts_wav` renders Kokoro only; `piper_voices()` →
  Kokoro ids. Rename left only where it doesn't churn callers. Make the Kokoro
  weight fetch + misaki venv build **fatal/loud** in `Dockerfile.tts-stt` (drop the
  non-fatal `|| echo` swallow) so a box always has Kokoro or the build fails.
- **Backend:** `brain.py` voices/speakers reflect Kokoro-only; `settings_store`
  `brain_answer_voice` default → a Kokoro voice; `brain_read_aloud_engine` domain
  → `kokoro | native`.
- **Frontend:** `engineForVoice` retired (always Kokoro); Settings engine control
  → Kokoro | Native; voice picker Kokoro-only.
- **Wall:** read-aloud uses a Kokoro voice; drop baked piper voices.
- **Docs/setup:** `install-tts.sh`, `deploy/tts-stt/README.md`, `SERVICES.md`,
  `dev-setup.sh` reconciled. Tests updated across all four surfaces.
- *Risk:* Kokoro is now the only box voice — W1 health + the loud provisioning are
  the mitigations; browser-native covers box-unreachable.

### W3 — Collapse to one box normalizer ◻️

- Port `speakable.js`'s `toProse` + `toUtterance` semantics into `_speakable_text`
  (single misaki-targeted profile): markdown→prose (citations, code, tables→
  sentences, heading/quote/bullet markers, emphasis), then verbalize (numbers,
  dates, currency, percent, fractions, ranges, symbols, emoji, URLs,
  abbreviations, dashes/parentheticals).
- **Fold in the symptom fixes:** dotted-initialism collapse (`U.S.`→`U S`) before
  boundary detection; heading terminal `:` instead of `.`.
- **Fold in coverage fixes** (highest-impact first): phone numbers (digit-by-digit,
  not a range), times (`3:30pm`, `14:00`), ISO/slash dates, version numbers
  (`v2.3.1`), numbers glued to units, inequalities (`<`/`>`/`≤`/`≠`), `snake_case`,
  roman numerals, common abbreviations (`etc./St./Inc./No.`).
- **Shrink `speakable.js`** to the chunker (+ `reportToSpeech`); PWA sends raw
  markdown clips; the box normalizes each. Keep the abbreviation-aware boundary
  guard client-side so `U.S. ` never splits a clip.
- **Wall** drops `mdToPlain`; sends raw text; box normalizes.
- Port `speakable.golden.test.ts` cases to `test_piper_server.py`; keep a thin JS
  suite for the chunker only.

### W4 — Owner-editable respelling lexicon + Settings panel ◻️

- `settings_store` key `pronunciation_lexicon` (`{word: respelling}`, sanitized:
  non-dict/blank dropped, bounded size), exposed via `SettingsOut`/`SettingsPatch`
  in `api/settings.py` (owner-only, RLS — invariant #3, no migration: constant, not
  a row seed).
- `brain.py /brain/tts` reads the map and applies a whole-word, case-insensitive
  substitution to `text` before forwarding (engine-agnostic; also makes Titusville
  robust on the espeak path). Applies on the report/custom-text paths too.
- **GUI gate (PROCESS.md):** the Settings "Pronunciations" surface is a new GUI
  surface → **three interactive HTML mocks** to `docs/mocks/`, owner picks before
  build; also surfaces the W1 `g2p` health state ("Voice engine: misaki ✓" /
  "espeak — pronunciations limited"). A "Test" button reuses `/api/brain/tts`.
- Tests: store sanitizer, settings round-trip, the brain.py injection, RLS
  isolation for the new key.

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
- Quick interim relief: the Titusville respelling and the U.S./heading fixes could
  land ahead of the full W3 port if the owner wants symptom relief sooner. Sequenced
  into W3 by default to avoid rework against code being removed.
