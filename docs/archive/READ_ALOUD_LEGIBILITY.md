# Read-aloud legibility — split the `tts` service, normalize the text, ramp the chunks

> **Status:** Shipped · **Last verified:** 2026-07-06 · **Waves:** 1✅ 2✅ 0✅ — all
> landed; archived. Sequence was **reordered** (owner call): CI-verifiable legibility
> (W1) + fluid streaming (W2) shipped first (PR #793), then the infra split/colocate
> (W0). W1+W2 **merged** (coupled through the auto-play cursor): the shared `speakable`
> normalizer, a stream-safe `chunkStream` (complete-unit extraction, property-tested), and
> a prefetch pump for gapless playback. W0 split piper out of the wall into an always-on
> `tts-stt` service (colocated with whisper, which became default-on) with a warm
> `PiperVoice` cache (load once ~8 s, then ~0.05–0.3 s/clip vs ~1.5 s subprocess-per-clip);
> `server-brain` → `wall`. **Not built** (deliberately dropped): the wall's own adoption of
> the shared JS module (it still uses `mdToPlain` — its read-aloud renders through the
> tts-stt piper via the `/tts` forward, so token-level legibility already applies; structural
> parity on the wall can be a follow-up) and **ramp sizing** (warm piper + prefetch already
> give gapless playback, so packing sentences buys little — revisit only if round-trips bite).
> The container/compose runtime verifies on the box at `jbrain update`.

Read-aloud today feeds piper near-raw markdown, so emoji, tables, line breaks,
numbers and symbols garble; and the box renders one piper subprocess per clip
with a fully serial pump, so speech is gappy. This plan (a) moves piper out of
the wall display and colocates it with whisper as one always-on **`tts-stt`**
speech service with a warm model cache, (b) normalizes answer text into
speakable prose before it reaches piper, and (c) reworks streaming into an
adaptive, pipelined chunker so playback stays fluid. Prior-art research (ChatGPT
read-aloud, ElevenLabs, Open WebUI, pipecat, Inworld) all converge on the same
plain-text pre-processing pass — the regime piper lives in, since it has no SSML.

Peer to `reference/DESIGN.md` (the read-aloud UX spec) and
`deploy/server-brain/README.md` (the wall/TTS runbook, which this plan renames).

## 0. Framing — what's wrong and why

**Two problems, independent:**

1. **Legibility.** piper is a plain-text neural voice (espeak-ng phonemes, **no
   SSML, no markdown**, only `[[ ipa ]]` overrides). Both read-aloud paths strip
   markdown emphasis to prose but stop there: emoji, tables, numbers, currency,
   symbols and URLs reach piper raw, and — the sharpest bug — **line structure
   is destroyed before it can create a pause**. `speakableText` collapses all
   whitespace (`\s+ → " "`) *before* the chunker looks for `\n` boundaries
   (`frontend/src/agent/useReadAloud.ts:39`), so a bullet list with no periods
   is spoken as one breathless run.

2. **Fluidity + wrong home.** The box renders **one piper subprocess per clip**,
   cold-loading the model every time (`deploy/server-brain/serve.py` `tts_wav`),
   and `pumpPiper` is **fully serial** — it fetches a clip, plays it, *then*
   fetches the next (`useReadAloud.ts:293`), so every clip carries a render gap
   in front of it. And piper lives *inside the wall display* even though TTS is
   a **shared service**: the PWA read-aloud (via the api proxy `brain.py`), the
   wall's own read-aloud, and the Settings sample all use it. It is not a wall
   feature.

**Two drifted implementations.** The PWA (`speakableText` + `chunkSentences` +
`pumpPiper`, `useReadAloud.ts`) and the wall (`mdToPlain` + `chunkForTTS`,
`deploy/server-brain/index.html`) are separate copies that have already diverged
(the wall packs to 220 chars and strips citations; the PWA is one-sentence,
uncapped). A fix added to one does not reach the other unless it lives at a
shared point.

## 1. Target architecture — `wall` display + a shared `tts-stt` speech service

Two moves: piper leaves the wall, and the box's speech I/O (STT + TTS)
consolidates into one always-on service.

```
PWA  ──▶ api /api/brain/tts     ──▶ http://tts-stt:8801/tts ──▶ piper (warm cache)
PWA  ──▶ api transcribe          ──▶ http://tts-stt:8080     ──▶ whisper.cpp (llama-swap)
Wall ──▶ wall:8800/tts (forward) ──▶ http://tts-stt:8801/tts ──▶ piper (warm)
```

- **`wall`** (renamed from `server-brain`) — the LAN kiosk only: neural-brain
  viz + JPet + host vitals + `/event` + `/stats`, published on `:8800` (it is a
  browser kiosk, needs the LAN port). **No piper baked** → smaller image. Keeps
  a thin `/tts` + `/tts/voices` + `/tts/silence` **forward** to `tts-stt`, so
  the kiosk browser still fetches audio same-origin (it cannot reach a
  docker-internal name or the authed api).
- **`tts-stt`** (renamed from `whisper`) — the box's speech service, **internal-
  only**. Hosts **whisper.cpp** (STT, via llama-swap on `:8080`,
  load/unload-on-idle — unchanged) **and** **piper** (TTS, a warm `PiperVoice`
  cache on `:8801`, serving `/tts`, `/tts/voices`, `/healthz`, and the
  `tts_debug` latch). Reached by the api (both STT and TTS) and the wall (TTS
  forward).

**Both speech services default-on.** `whisper` loses `profiles: [whisper]`;
`enable-whisper` becomes provisioning rather than an opt-in gate. Consequence
accepted: every stock deploy now provisions the whisper GGML model + llama-swap
config (fold `scripts/whisper-setup.sh`'s download/config into the default
setup/build), and STT is always available. TTS + STT share one image, one Ops
row, and one lifecycle — the box's "speech" service. (Colocation is viable
*because* both are now always-on; whisper stays GPU/load-on-demand while piper
stays CPU/resident within the same container.)

## 2. Decisions (settled with the owner)

| # | Decision | Choice |
|---|---|---|
| 1 | Where normalization lives | Hybrid: a shared framework-free `speakable` module pre-chunk, plus a thin defensive scrub in the tts server |
| 2 | The two drifted pipelines | De-dup: extract `speakable`, the PWA imports it, and the **wall loads the same file** via `<script src>` served by the wall — one source of truth |
| 3 | piper server deps | A small Python TTS server + piper's in-process `PiperVoice`; heavy number-to-words stays in the frontend module |
| 4 | Emoji | Strip by default; tiny allow-list verbalized (✅→"check", ⚠️→"warning", ❌→"cross") |
| 5 | Code blocks | Announce "code block." — never read contents |
| 6 | Service names | `wall` (display + pet) and `tts-stt` (speech: whisper STT + piper TTS); keep the `BRAIN_*` env prefix — the *visualization* is still a brain |
| 7 | Warm piper home | Colocated with whisper in the `tts-stt` service |
| 8 | whisper profile | **Default-on** (drop `profiles: [whisper]`); both speech services always available; provision the STT model by default |

## 3. Wave 0 — `wall`/`tts-stt` split + whisper default + warm piper

The structural wave; do it **first** so features land on the clean layout.

**Compose + images**
- Rename service `server-brain` → `wall` (`deploy/docker-compose.yml`); update
  its network alias and comment. Keep the `:8800` LAN publish. **No piper** in
  its image.
- Rename service `whisper` → `tts-stt`; **drop `profiles: [whisper]`** so it is
  default-on. Keep `/dev/dri` + the video/render GIDs (whisper needs the iGPU;
  piper ignores it). Add the piper TTS port to the compose (internal only).
- The container now runs **two processes** — llama-swap (whisper, `:8080`) and a
  small piper TTS server (`:8801`). Replace the hardcoded single-binary
  entrypoint with a tiny launcher (a shell script or `tini`/`s6`) that starts
  the piper server in the background and execs llama-swap in the foreground (or
  vice-versa), so a crash of either is visible.
- `Dockerfile.whisper` → `Dockerfile.tts-stt`: keep the llama-swap + whisper.cpp
  layers; **add** a Python runtime + `piper-tts` + espeak-ng + the baked voices
  (`/opt/piper-voices`) + the piper server script. (Fatter image — accepted.)
- Split `deploy/server-brain/` → `deploy/wall/` (serve.py display bits +
  `index.html` + `pet.html` + README). The piper code moves into the `tts-stt`
  image (a `deploy/tts-stt/` dir for the TTS server + `install-tts.sh` +
  voices), alongside the existing whisper assets.

**Move the TTS code** out of the wall `serve.py` into the piper server:
`tts_wav`, `_resolve_voice`/`_voices`/`_speaker_map`, `piper_voices`, the `/tts`,
`/tts/voices`, `/tts/silence` routes, `_pad_lead`, `CURATED_SPEAKERS`, and the
`_tts_debug` latch + the `[tts] …` render trace (shipped in #792). The wall's
`serve.py` keeps display/pet/stats/event and gains a thin proxy for `/tts*` →
`http://tts-stt:8801`.

**Warm piper** — replace subprocess-per-clip with an in-process cache:
- Load each voice's `.onnx` once via `piper.voice.PiperVoice.load(...)`, keyed by
  model path, cached in a module dict; synthesize in-process. Guard synthesis
  with a `threading.Lock`. Removes the ~1 s cold-load per clip (the dominant
  render cost).
- Lazy-load on first use; optional idle-evict (mirrors whisper's TTL) only if
  resident RAM (~60–78 MB/voice) becomes a concern.
- Keep the failure logging + tunable timeout (now a synth-call guard, not a
  subprocess timeout) and the `PIPER_LEAD_MS` behaviour.

**whisper default-on provisioning**
- Fold `scripts/whisper-setup.sh`'s work (download the GGML model, write the
  one-model llama-swap config) into the **default** setup/build path so a stock
  deploy has STT ready; `jbrain enable-whisper` becomes a no-op / provisioning
  helper, not a profile flip. Verify the model volume (`./whisper-models`) is
  populated on first run (guard against a crash-loop when absent).

**Rewire**
- api config: `JBRAIN_BRAIN_EVENTS_URL → http://wall:8800/event`; add
  `JBRAIN_BRAIN_TTS_URL → http://tts-stt:8801`. Point the whisper/transcribe
  config at `http://tts-stt:8080` (renamed from `whisper:8080`). `brain.py`
  `/brain/tts` + `/brain/voices` target the TTS base; `_brain_base` splits into
  an events base (wall) and a TTS base.
- The per-turn `tts_debug` flag push (`api/agent.py`) now targets `tts-stt`.

**Tests / docs**
- `test_server_brain_tts.py` → move/rename to the piper server; add a warm-cache
  test (model loaded once across N synth calls; lock serializes).
- `test_brain_proxy.py`: assert `/brain/*` hit the TTS base, `/event` the wall;
  transcribe tests point at the renamed STT host.
- Rewrite `deploy/server-brain/README.md` → `deploy/wall/README.md` +
  `deploy/tts-stt/README.md`; update `reference/DESIGN.md`, the whisper runbook,
  and the Ops-screen grouping (`frontend/src/screens/OpsScreen.tsx`
  `SERVICE_GROUPS`: `wall` under Display, `tts-stt` under AI). Note the
  whisper-is-now-default change in `ROADMAP`/setup docs.

## 4. Wave 1 — Legibility (the visible win)

A shared, framework-free `speakable` module run **before** chunking.

**Pause authoring (fixes the line-break bug).** Convert each line / list item /
heading / paragraph to end in terminal punctuation **before** collapsing
whitespace. Paragraph break → a stronger boundary. This is the single
highest-leverage change.

**Structural linearization.** Tables → one sentence per row pairing header with
cell (`Row one: Name, Alice. Age, thirty.`); lists → a sentence each; headings →
`Setup.`; fenced code → `code block.`; links → anchor text; images / blockquote
markers / HR removed.

**Token normalization.** Emoji (strip, or verbalize the allow-list); a symbol
map (`&`→and, `%`→percent, `km/h`→kilometers per hour, `→`→to, `$5`→five
dollars, `°`→degrees, `@`→at); numbers / dates / currency (a JS number-to-words
lib or hand-rolled); URLs → registrable domain only (`github.com`), path
dropped.

**Wiring.** PWA replaces `speakableText` with the module; the wall's `serve.py`
serves the module file and `index.html` `<script src>`s it (kills the drift).
The tts service keeps a **stdlib defensive scrub** (emoji + residual markdown
symbols per clip) as a safety net for the sample button and any un-normalized
text — no number-to-words there. Add piper `--sentence-silence`-equivalent
(inter-sentence gap in the `SynthesisConfig`) for breathing room.

**Tests.** `speakable` vector table (before→after per element); the tts scrub;
extend `useReadAloud.test.ts`.

## 5. Wave 2 — Fluid streaming (adaptive, pipelined)

Two coupled changes in the PWA pump, then ported to the wall via the shared path.

**A. Pipeline the pump (prefetch).** Decouple fetch from play: keep **depth-2**
rendered clips ready so clip N+1 renders while clip N plays. This alone removes
today's per-clip gap.

**B. Ramped chunk sizing.** First **1–2 clips = a single short sentence** (fast
time-to-first-audio + a cushion for the cold start), then pack whole sentences
into a **growing** budget (≈80 → 160 → 320, capped ~400 chars ≤ the 1000
transport cap). Never split mid-sentence (preserves prosody and the decimal rule
in `chunkSentences`). Fewer, larger later clips also mean fewer piper synth calls.

**The "don't run out of audio" math.** Measured on these voices: speech ≈ **12
chars/sec**; render ≈ **~1.0 s cold-load + L/70** per clip (the warm cache of
Wave 0 largely removes the 1.0 s after the first). Playtime(L) = L/12, so a clip
renders faster than it plays once **L ≳ 15 chars** — after the first clip the
buffer only grows. The only risk is the first clip's latency, which is why the
first 1–2 clips stay small and we prefetch depth-2. Optional refinement: measure
each clip's real render time + `audio.duration` at runtime and size the next
clip so buffered-audio-seconds ≥ next render time × safety (self-correcting on a
slow box).

**Tests.** Chunker: ramp schedule, no mid-sentence split, caps, decimal rule.
Pump: prefetch depth, no underrun, stop/replace mid-stream, box-failure →
native fallback (existing behaviour preserved). Fake `brainTts`/`Audio`.

## 6. Test plan (summary)

- Wave 0: tts service unit tests (warm cache, lock, voice resolution incl.
  libritts `#3922` → speaker 0, render-failure logging, `tts_debug` latch);
  `test_brain_proxy.py` routing (tts vs wall bases); wall `/tts` forward.
- Wave 1: `speakable` vectors; tts defensive scrub; `useReadAloud.test.ts`.
- Wave 2: chunker + pump tests as above.
- Real Postgres for any api-side auth touchpoints; piper faked in unit tests, a
  smoke render kept behind the integration marker.

## 7. Non-goals / open

- **No SSML / prosody tags** — piper has none; pauses come only from punctuation
  + sentence segmentation + the synth-config gap. `[[ ipa ]]` pronunciation
  overrides are out of scope for v1 (could fix mangled acronyms later).
- **No LLM-side rewrite** — normalization is deterministic and testable, not a
  prompt to the answer model (kept as a possible future safety net).
- A standalone LAN-published tts port is intentionally avoided; the wall forward
  keeps the kiosk browser same-origin and tts internal.

## 8. Prior art (research, 2026-07-06)

Every product that reads LLM/markdown aloud runs a plain-text pre-processing pass
(strip markdown → verbalize symbols → author pauses with punctuation); the modern
LLM-native engines (OpenAI `gpt-4o-mini-tts`, ElevenLabs v3) dropped SSML too, so
piper's plain-text regime is the mainstream, not a workaround.

- [piper1-gpl (CLI + Python API)](https://github.com/OHF-Voice/piper1-gpl)
- [pipecat MarkdownTextFilter](https://docs.pipecat.ai/server/utilities/text/markdown-text-filter)
- [ElevenLabs TTS best practices (pauses, normalization)](https://elevenlabs.io/docs/overview/capabilities/text-to-speech/best-practices)
- [OpenAI TTS — instructions, no SSML](https://developers.openai.com/api/docs/guides/text-to-speech)
- [Open WebUI — markdown → TTS](https://github.com/open-webui/open-webui/discussions/7758)
- [Inworld — prompting for TTS (no markdown/emoji)](https://docs.inworld.ai/tts/best-practices/prompting-for-tts)
- [num2words](https://pypi.org/project/num2words/) · [emoji-regex](https://www.npmjs.com/package/emoji-regex)
