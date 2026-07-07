# Read-aloud, audiobook grade — Kokoro pronunciation, pacing, and narrator voice

> **Status:** In progress · **Last verified:** 2026-07-07 · **Waves:** W0✅ W1✅ W2◻️ W3◻️ W4◻️

**A scheduled, multi-wave build plan** (per `docs/DOC_LIFECYCLE.md`), governed by
`docs/reference/PROCESS.md`. It builds directly on the shipped
`archive/READ_ALOUD_LEGIBILITY.md` (the `speakable` normalizer + warm `tts-stt`
service) and the shipped Kokoro-82M engine (`deploy/tts-stt/piper_server.py`,
`deploy/Dockerfile.tts-stt`). Peer to `reference/DESIGN.md` (the read-aloud UX spec).

Two target use cases on **one** pipeline, **single narrator** (no per-character
voices this plan): (1) reading **short stories** aloud at audiobook quality, and
(2) reading **LLM Markdown answers** well. Owner calls, already made: **Kokoro-only,
on-box**; **single narrator**; **misaki G2P is approved** even though it grows RAM
(disk/CPU are free) — RAM is the one budget to measure, §6.

---

## 1. Framing — what's good, what's missing

Kokoro already sounds markedly better than piper, and the `speakable` normalizer
already turns Markdown into clean prose (strips markup, linearizes tables,
verbalizes numbers/currency/percent/fractions, authors pauses by terminal
punctuation, maps symbols/emoji, converts dashes to comma beats). Three gaps stand
between that and audiobook grade:

1. **Pronunciation is espeak-tuned and coarse.** Kokoro (in our build) phonemizes
   through **espeak-ng + phonemizer-fork**, and `speakable`'s abbreviation rules were
   written for espeak. Acronyms/initialisms (`FBI` vs `NASA` vs `API`), homographs
   (`lead`, `bass`, `read`), and domain terms (`SQL`, `GIF`, `kubectl`) are read wrong,
   and there is no way to *fix a specific word* today.
2. **No audiobook pacing.** Kokoro has **no SSML and no `<break>`** — pacing comes only
   from punctuation, chunking, speed, and inserted silence. We render sentence clips
   with a small silence *lead* but no shaped **sentence / paragraph / scene** pauses, so
   prose reads at a flat, uniform cadence — fine for a chat answer, not for a story.
3. **One fixed timbre, one speed.** Every voice is a canned style vector at speed 1.0.
   No narrator timbre, no story-vs-answer pacing.

The good news (from research, §2): Kokoro exposes exactly the levers to close all
three, all on-box, most at near-zero RAM.

## 2. The levers Kokoro gives us (research-grounded)

- **Direct phoneme input** — `create(text, voice, …, is_phonemes=True)` accepts phonemes,
  so we can override any word's pronunciation exactly.
- **misaki G2P** — Kokoro's recommended grapheme→phoneme engine: `num2words`,
  POS-based homograph handling, **espeak fallback for OOV**, and an **inline override
  syntax** `[word](/ipˈaphonemes/)` for precise per-word control. Cost: `misaki[en]`
  pulls **spaCy + spacy-curated-transformers** — the RAM line item (§6).
- **Voice blending** — a voice is a 256-d style vector (`.npz`); a weighted average of
  two (e.g. `af_nicole:50 + am_michael:50`) is a valid custom voice, computed once,
  ~free. This is the timbre/"tune" lever.
- **Silence insertion** — the audiobook-pacing technique every Kokoro reader uses
  (audiblez, epub2tts-kokoro, Kokoro-TTS-Pause): concatenate exact numpy silence
  arrays between rendered clips. We already pad a lead; this generalizes it.
- **Speed** — `create(speed=)` for story-vs-answer pacing.

## 3. Target architecture — a two-layer text pipeline + a Kokoro render path

Today `speakable(md)` does everything in one pass and is espeak-tuned. Split it so the
engine-agnostic work is shared and the phoneme/pacing work is per-engine:

```
markdown ──▶ toProse(md)            ── engine-agnostic: strip markup, tables→sentences,
                 │                     numbers/currency/emoji/symbols, structural pauses
                 ▼
            toUtterance(prose, engine)   ── per-engine: pronunciation (acronyms, lexicon,
                 │                          misaki inline overrides), pacing marks
                 ▼
   chunkStream → clips ──▶ /tts?voice&speed ──▶ kokoro.create(is_phonemes?) 
                                                   └─▶ concat with shaped silence
```

- **`toProse`** — the shared layer (all of today's Markdown→prose work). Piper output
  stays byte-identical.
- **`toUtterance(prose, engine)`** — the per-engine layer. Piper keeps the current
  espeak ruleset; Kokoro gets the acronym rules, the pronunciation lexicon, and pacing
  marks. This is the seam every later wave hangs off.
- **Kokoro render path** (`piper_server.py`) gains: misaki phonemization (with espeak
  fallback), inter-clip silence shaping, and a `speed` param.

The `readAloudBus` fix (Settings changes reach the mounted chat hook live) is a
prerequisite and is **already landed on the branch** — it is what makes a chosen
Kokoro voice actually take effect.

---

## 4. The waves

Each wave follows `PROCESS.md`: parallel tasks off a `wave-N` branch, an **independent
adversarial per-task review** (reviewer ≠ builder), a **per-wave review**, then **one PR
per wave**, CI green, merge, next wave. Tests land with the code (§5). No wave touches
RLS / the domain firewall / principal scope, so no red-team gate — except where a
**GUI surface** appears, which triggers the **three-mock GUI gate** (W4).

### W0 — Two-layer split + golden fixtures (foundation, behavior-preserving)

- **T0.1** Split `frontend/src/agent/speakable.js` into `toProse(md)` +
  `toUtterance(prose, engine)`, with `speakable(md, engine = "piper")` composing them.
  Piper output **byte-identical** (guarded by the existing suite + a new parity test);
  Kokoro currently routes through the same espeak ruleset → no audible change yet.
  Preserve the dependency-free ESM shape (the wall loads the module verbatim).
- **T0.2** A **golden read-aloud corpus**: fixtures for a short-story excerpt and a
  Markdown LLM answer, with snapshot expectations for `toProse`/`toUtterance`, so every
  later prosody/pronunciation change is regression-visible.
- **Verify:** existing `speakable`/`useReadAloud`/`SettingsScreen` suites unchanged;
  new split + parity + golden tests. No new deps, no GUI.

### W1 — Kokoro pronunciation (misaki + acronym rules + lexicon)

- **T1.1** Bake `misaki[en]` into `deploy/Dockerfile.tts-stt` (adds spaCy +
  spacy-curated-transformers + num2words). **Non-fatal**, like the Kokoro steps: if the
  install or model load fails, the Kokoro path falls back to the current espeak-ng g2p —
  a build hiccup can never break read-aloud. Dockerfile bake-guard test.
- **T1.2** `piper_server.py` Kokoro path phonemizes via misaki (espeak fallback for OOV)
  and feeds Kokoro phonemes; resident like the model, under the existing warm-load lock.
- **T1.3** A **pronunciation lexicon**, `KOKORO_LEXICON` in `piper_server.py` (server-side,
  co-located with misaki — avoids emitting misaki markup across the api/tts boundary where an
  espeak fallback couldn't read it): known-hard words → misaki phonemes, emitted as inline
  overrides `[word](/…/)` on the misaki path only. Ships **empty** (nothing guessed); the owner
  adds entries after a listen, deriving phonemes on the box (see `deploy/tts-stt/README.md`).
  *(Refinement vs the original plan, which put this in the frontend `toUtterance`.)*
- **Verify:** fake-misaki unit tests (misaki path feeds phonemes with `is_phonemes=True`; the
  degrade-to-espeak path; lexicon → inline override); Dockerfile bake-guard. RAM measured on the
  box (§6). No GUI. **(Landed.)**

### W2 — Audiobook pacing (shaped silence + prosody marks)

- **T2.1** Server-side **silence shaping** in the Kokoro render/concatenation path:
  tunable sentence / paragraph / scene-break gaps (numpy silence arrays), generalizing
  today's lead-pad. Bounded, gapless.
- **T2.2** `toUtterance` pacing marks: paragraph & scene-break detection → longer
  pauses; colon/ellipsis dwell; **quote/dialogue micro-pauses** for a single narrator
  (set dialogue off by pacing, not a second voice).
- **T2.3** Plumb a **`speed`** param end-to-end (`/tts?speed=` → `create(speed=)`).
- **Verify:** silence-gap durations asserted on rendered WAV frame counts; prosody rules
  on the golden story fixture; property tests for chunk/pacing stability. No new deps, no GUI.

### W3 — Narrator timbre (voice blending)

- **T3.1** Voice **blending** in the Kokoro path: weighted style-vector average; expose
  one or more curated blended narrator ids (e.g. `kokoro-narrator`) through the existing
  `/tts/voices` list — they appear in the **existing** picker (data-driven), so **no new
  GUI surface**.
- **Verify:** blend-math + listing + render tests. No new deps, no GUI gate.

### W4 — Reading modes (story vs answer) — **GUI gate**

- **T4.1** A **reading-style profile** — story (narrator blend, slower speed, generous
  paragraph/scene silence) vs answer (clear voice, snappy, minimal silence) — wiring the
  W1–W3 levers into two presets.
- **Decision (owner):** whether the mode is **automatic by context** (chat turn = answer;
  a long-form "read this note/story aloud" entry = story) or a **user control**. A user
  control (and/or a new long-form read-aloud entry point) is a **GUI surface** →
  `PROCESS.md`'s **three interactive mock artifacts, chosen before implementation**, landing
  in `docs/mocks/`. This wave is deliberately last and may split once the mode surface is chosen.
- **Verify:** preset application through the pipeline; whatever the chosen surface needs.

---

## 5. Non-negotiables & verification (`CLAUDE.md`, `PROCESS.md`)

- **LLM adapter / storage / RLS:** not touched — TTS is not an LLM completion, the
  `tts-stt` server holds no user data and no DB session, and no table changes. No RLS
  isolation test applies.
- **Tests with the code:** frontend (biome + tsc + vitest) and the `piper_server`
  unit suite (fakes for misaki/kokoro, no weights) land in the same PR as each wave; the
  golden corpus is the prosody regression net.
- **`scripts/dev-setup.sh`:** misaki lives **only in the `tts-stt` image**, not the dev
  venv — confirm no dev-setup change is needed per wave (flag if it is).
- **Docs travel:** `deploy/tts-stt/README.md` and `reference/SERVICES.md` reconciled as
  the engine gains misaki/pacing/blending; this plan's `Waves:` header flips per wave and
  the plan **archives when W4 lands**.
- **Conventional Commits; one PR per wave; CI green before merge.** CI does **not** build
  the `tts-stt` image, so the misaki install + RAM are proven on the box's `jbrain update`.

## 6. Risks & the RAM budget

- **misaki RAM (the one real cost, owner-accepted).** `misaki[en]` loads a spaCy English
  pipeline (POS tagging for homographs) plus its lexicon — order a few hundred MB resident
  on top of Kokoro's ~310 MB. **Measure on the box in W1** (`docker stats tts-stt`), record
  it in the runbook, and keep the espeak-fallback path so a lean deploy can skip it. Disk/CPU
  are non-issues per the owner.
- **Non-fatal everywhere.** misaki install + model load follow the Kokoro precedent: any
  failure degrades to espeak-ng, never a broken read-aloud or a blocked `jbrain update`.
- **Quality is ear-judged.** Pacing, dialogue micro-pauses, and the narrator blend need a
  **listen on the box** (I can't hear from CI); each wave ends with a box-listen checkpoint
  on the golden fixtures before its PR is called done.
- **espeak/misaki phoneme mismatch.** Feeding phonemes bypasses Kokoro's own text handling;
  W1 keeps espeak fallback and a small, tested lexicon rather than owning all of G2P.

## 7. Rollout

Everything rides the normal `jbrain update` (image rebuild bakes misaki; the bind-mounted
`piper_server.py`/`speakable` changes need no rebuild). Order W0→W4; each wave is usable on
its own (W0 invisible, W1 fixes pronunciation, W2 adds pacing, W3 adds a narrator voice, W4
adds modes). No migrations, no data changes, no new ports.

## 8. Out of scope (named follow-ons)

- **Multi-voice / per-character dialogue** (narrator + distinct character voices) — a real
  audiobook lever, deliberately deferred; single narrator this plan.
- **Non-English voices / languages** — English roster only, matching the shipped set.
- **Cloud/hosted TTS** — excluded by the on-box constraint.
- **The wall display's own `mdToPlain`** — still separate (per `READ_ALOUD_LEGIBILITY.md`);
  structural parity there stays a follow-on.
