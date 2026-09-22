# Talking to the panel — press, hold, ask, be answered

> **Status:** Proposed · **Last verified:** 2026-09-22

The owner, while a display fix was deploying: *"how do we put it in a mode where we can have
bidirectional conversation with the AI box via tts stt?"* — and then, unprompted, the exact
interaction: *"press and hold, it records and sends, and then we get a thinking bubble until
the reply?"*

That gesture is already what `ROOM_ENDPOINT_PLAN.md` settled on for the voice path, and this
document exists because the research question behind it has a surprising answer.

## The surprise: both hard halves already ship

Speech-to-text and text-to-speech are **not** new work. They are in production on this box
today, in one always-on container, `tts-stt`:

| | what it is | where it runs | cost per call |
|---|---|---|---|
| **STT** | whisper.cpp server, GGML `large-v3-turbo` | iGPU, Vulkan | **~9.8 s, flat** |
| **TTS** | Kokoro-82M ONNX, held warm | **CPU only** | **~0.1 s** |

`WhisperCppClient.transcribe(audio, ...)` (`backend/src/jbrain/transcribe.py:85`) is fully
general — four unrelated consumers already share it (SDR captions, the SDR recorder, the
agent's `transcribe` tool, video analysis). Kokoro already has an authenticated proxy for the
PWA at `GET /brain/tts` (`backend/src/jbrain/api/brain.py:96`), with the owner's respelling
lexicon applied on the way through.

**And a complete voice loop already works** — just not on the panel. The wall kiosk runs
browser speech recognition with the wake word "robot" → `POST /internal/pet/say`
(`backend/src/jbrain/api/pet.py:379`) → a keyword classifier with an LLM fallback → Kokoro.
That endpoint is the shape to copy, including its guards: rate-limited, memory-free, and
restricted to non-sensitive domains.

So this feature is **not "build STT and TTS"**. It is "get audio off the panel and audio back
onto it", plus one latency problem that has to be solved or the whole thing is unusable.

## The latency problem, which is the actual work

**Whisper takes ~9.8 seconds per call on this box** with the model resident — measured, and
recorded at `backend/src/jbrain/api/sdr.py:1412-1416`. It is flat regardless of clip length,
because whisper.cpp pads every clip to a 30-second window. A four-year-old will not wait nine
seconds, and no amount of thinking-bubble covers it.

Everything else in the chain is cheap. A 3-second utterance is 96 KB of 16 kHz mono s16 —
about 50 ms over the LAN. Kokoro is a tenth of a second and doesn't touch the GPU.

Three things to try, cheapest first:

1. **A smaller model on the voice path.** `scripts/whisper-setup.sh:5` already makes the model
   operator-selectable and `base.en` is one of the options. `base.en` is roughly an order of
   magnitude smaller than `large-v3-turbo`; the 30 s padding still applies, so the win is the
   model, not the clip. **This number has to be measured on the box before anything else is
   designed around it.**
2. **A second whisper instance**, so the voice path gets `base.en` while SDR captions keep
   `large-v3-turbo`. They are already separate llama-swap instances from the chat LLM, so
   this is configuration rather than architecture.
3. **The keyword classifier first**, exactly as `_say_router` does for the wall
   (`api/pet.py:~285-352`). A large share of what a four-year-old says to a pet — "jump",
   "what's your name", "are you hungry" — never needs the LLM at all. This is a latency win
   *and* a robustness win when the box is busy.

**Contention is real and partly unaccounted.** Whisper shares the iGPU and the unified memory
pool with the chat model, and `llm/ledger.py:153-157` says plainly that the ledger cannot see
it — whisper is a second llama-swap, and "Kokoro holds a model with no accounting in
`backend/src` at all". A voice turn that runs whisper and the chat model back to back is
exactly the pattern that accounting does not cover. Kokoro being CPU-only is the one piece of
good luck here.

## What the box actually measured, 2026-09-22

The chain shipped, the twins used it, and the numbers moved the bottleneck twice. Both
answers are the opposite of the guess above.

**Whisper: the window was the cost, not the model.** `base.en` was never needed.
whisper.cpp takes `audio_ctx` as a per-request form field (`server.cpp:418` →
`wparams.audio_ctx`), so the encoder window can be sized to the clip instead of padded to
thirty seconds — ~50 encoder frames per second of audio against 1500 for the full window.
`api/endpoint.py` now sizes it per turn with half again for margin and a floor of 256.
Measured on the same `large-v3-turbo` that took 9.55 s warm: **1,296–1,319 ms** on a short
clip, 2,262–2,382 ms on a six-second one. A 7.3× cut with no second instance, no second
model and no quality loss. Option 1 and option 2 above are both moot.

**The LLM: the problem is one slot, not a big model.** `gpt-oss-120b` serves
`total_slots = 1` (`props gpt-oss-120b`). `pet.turn` and `agent.turn` are routed to it
together, so a child's turn and the owner's assistant contend for the same slot, and the
thrash is mutual and expensive. From the 12:31–12:35 window, with an agent research loop
running its `web_search`/`web_fetch` turns at a 32k–45k-token context:

| what | tokens out | elapsed | rate |
|---|---|---|---|
| `pet.turn` | 46 | **43,216 ms** | 1.1 tok/s |
| `pet.turn` | 46 | 12,250 ms | 3.8 tok/s |
| `pet.turn` | 37 | 14,036 ms | 2.6 tok/s |
| `pet.turn` (slot clear) | 46 | **815–997 ms** | ~50 tok/s |
| `agent.turn` (slot clear) | 200–824 | 5.5–10.5 s | 19–29 tok/s |
| `agent.turn` (panel interleaved) | 69–176 | 36–40 s | **1.7–2.3 tok/s** |

The `kv_prefix.restore_waited_for_slot` and `kv_prefix.restored tokens: 32366 slot: 0`
lines in `logs api` are the mechanism: every panel turn evicts the agent's 32k-token
prefix from the one slot, and the agent pays a full restore to get it back. The panel
makes the agent slow and the agent makes the panel slow. A steady-state turn is
2.6–4.3 s end to end; a contended one is 12–52 s. **That variance is what "still really
slow" means** — the toy is fast until someone else is using the box, which on this box is
most of the time.

The fix is not a faster 120B. It is to stop `pet.turn` sharing a slot with the assistant:
give it its own resident model. `qwen3.5-4b` (Q8, 4.3 GB, `enabled`, on disk) co-resides —
the residency coordinator evicts only to hold the free-RAM floor (5% of 121 GB ≈ 6 GB) and
the box has ~32 GB available. Its catalogue default of a 131,072-token window costs 10.7 GB
of KV for a prompt that is 190 tokens, so the served `-c` wants trimming to a few thousand
first; footprint then lands near 5 GB. Effort is already `none` on this task, so no thinking
tokens. Routing is live-settable (`llm-set pet.turn qwen3.5-4b none`) and revertible in one
command, which makes it a measurement rather than a commitment — the quality question for a
four-year-old's turn is answerable in five minutes on the real prompt.

Unchanged and still true: Kokoro is ~450 ms and CPU-only, and the whisper call is still
outside the ledger.

## What the panel cannot do, and therefore where the work is

On-panel open-vocabulary speech is **off the table**, permanently. MultiNet7 resolves a fixed
list of pre-registered phrases and does not transcribe; Whisper-tiny-int8 is ~75 MB and does
not fit in 8 MB of PSRAM (`firmware/main/speech.h:15-17`). The panel's entire job is capture,
send, receive, play.

Three firmware gaps, and the second is the awkward one:

**1. There is no audio transport.** The panel speaks to exactly three routes today —
`/endpoint/settings`, `/endpoint/telemetry`, `/endpoint/firmware` (`firmware/main/ota.c`) —
all plain HTTPS with a bearer device key. `PanelDep` (`api/deps.py:129-162`) already
authenticates it. There is no audio upload endpoint and no WebSocket.

**2. The panel cannot play arbitrary PCM, and playback blocks capture.** `audio_beep()` is the
only playback that exists: a single pre-computed 880 Hz tone written with
`esp_codec_dev_write` (`audio.c:250`). The generic call is one line away, but there is no ring
buffer and no streaming source — and worse, **the blocking `esp_codec_dev_read` is the render
loop's clock** (`audio.c:254-257`). A 90 ms beep already stalls capture for 90 ms; a
multi-second reply would starve the microphone and the ESP-SR feed outright.

This is survivable *because* the interaction is press-to-talk. The panel listens or it speaks,
never both, so the audio task can legitimately stop feeding the recogniser while a reply
plays. It is a restructure of `audio_task`, not a new subsystem.

**And it must stay one task.** `audio.h:16-29` is emphatic: `esp_codec_dev` has no lock of any
kind, and a control write from the main task racing the audio task's capture **panicked a live
panel on 2026-09-21** (§10.4al). Everything else stays a request flag.

**3. Sample rates do not match.** The panel is 16 kHz mono s16, chosen for ESP-SR rather than
the vendor's 22050 (`audio.h:6-9`). Kokoro's WAV is not.

**The box should do every format conversion.** It has CPU to spare and the panel has 31 KB of
contiguous internal RAM on a good day. The reply should arrive as raw 16 kHz mono s16 that the
panel writes straight to the codec — no decoder, no resampler, no WAV parser in firmware. This
is the single decision that keeps the firmware side small.

## The interaction

Press and hold anywhere on the screen. Release to send.

**`gesture.h` says why the gesture needs care**: 4–5 year olds were measured producing ordinary
presses lasting **up to 4.2 seconds**. That measurement is why the maintenance gestures stopped
being a bare hold — the old one fired 1,937 times per 20,000 simulated child presses, the
rhythm-guarded one fires 12. A bare press-and-hold to talk *will* trigger during ordinary play.

Two mitigations, both shipped from the start:

- **A VAD gate on the upload.** No speech in the clip, nothing sent. An accidental lean costs a
  listening face and zero bytes — which disposes of the privacy problem for the common case,
  and the annoyance with it.
- **A listening state that is never ambiguous.** The mic meter already exists and already
  works; while held it is the honest indicator that it is hearing you.

If accidental triggering still proves annoying in the twins' room, the fallback is cheap:
`gesture.h` already implements "N short taps in rhythm, then hold", and **slot 2 is unused**
(3 = reboot, 4 = swap body, 5 = calibrate). "Tap tap and hold" reuses a mechanism that is
already measured and tested, with recording beginning as the hold begins rather than after the
5 s maintenance timeout.

**No wake word.** WakeNet is disabled because it does not fit, and re-enabling it costs
internal RAM this panel has spent four versions fighting over. Press-to-talk also bounds the
privacy question by construction, which is the better reason.

**No barge-in.** There is one microphone and no ES7210 reference channel, so there is no AEC
input; `ROOM_ENDPOINT_PLAN.md:58-62` already doubts barge-in on this board. Half-duplex is not
a limitation to apologise for here — it is what makes the audio task tractable.

### The state machine

| state | what the panel shows | what it is doing |
|---|---|---|
| held | listening face, mic meter live | capturing to a PSRAM buffer |
| released, no speech | back to idle immediately | discards; sends nothing |
| released, speech | **thinking bubble**, dots cycling | POST, awaiting reply |
| reply arriving | pet speaks — a nod or neck bob per phrase | writing PCM to the codec |
| no reply in N seconds | **a visible failure face** | gives up, says so |

The thinking bubble is not decoration — it is the latency budget. The moment the child lets
go, the pet is visibly thinking, and that buys a second or more of real round-trip for free.
It is cheap to draw with primitives `face.c` already has (a rounded rect and three dots).

The last row matters most. On a box its owner cannot get a terminal to, a hang that looks
identical to "it didn't hear you" is the failure mode this entire feature sequence has been
made of (§10.4bc: *the nothing in the log was the symptom*). It must say it failed.

## Suggested shape

1. **Measure `base.en` on the box first.** If the voice path cannot get under ~2 s of
   transcription, none of the rest of this is worth building as designed, and the answer is a
   different STT rather than a cleverer panel. This is one configuration change and one timing
   run — it should happen before any firmware is written.
2. **`POST /endpoint/converse`**, `PanelDep` auth, raw 16 kHz mono s16 in, raw 16 kHz mono s16
   out, streamed so playback can start on the first sentence. Guards copied from
   `/internal/pet/say`: rate limit, non-sensitive domains only, no memory writes. RLS scoping
   per `CLAUDE.md` #3 — the panel is a principal and the twins are not the owner.
3. **Restructure `audio_task`** for a playback mode that stops feeding ESP-SR while writing,
   with a PSRAM ring buffer. One task still owns the codec.
4. **The gesture, the capture buffer and the VAD gate** in firmware.
5. **The thinking bubble, the speaking animation and the failure face** in `face.c` — the
   cheapest part, and the part that decides whether it feels alive.

A PWA switch for the whole mode, per `CLAUDE.md` #10, since the owner cannot edit a config
file on the box.

## Voice notes between the twins

The owner: *"since we're sending audio I think we should be able to say a command like
'send to Elora helloooo' and her audio play on Elora when they click on a bubble that shows.
Essentially, a bubble shows up as a notification that a message came in and then when it's
touched it'll play the audio message."*

**The best part of this is free, and it decides the design.** `WhisperCppClient` already
returns **word-level timings** — `Word.start_ms` / `Word.end_ms`
(`backend/src/jbrain/transcribe.py:34`). So the box transcribes *"send to Elora helloooo"*,
finds where the addressee's name ends, and **cuts the original recording at that offset**.

That means the message that arrives is **the sibling's own voice**, not a text-to-speech
rendition of it — and without the "send to Elora" preamble attached. For twins this is the
whole feature: a four-year-old wants to hear their sister, not a robot quoting her. Synthesis
would have been the obvious implementation and much the worse one.

### The shape

1. A child holds the panel and says *"send to Elora, …"*. It uploads as any other turn.
2. The box transcribes. A classifier — the jpet's `classify()` shape — matches
   `send to <name>` against the known panel names before anything reaches an LLM.
3. The audio is trimmed at the end of the name using the word timings, stored, and queued for
   the named panel.
4. That panel shows a **bubble**, and a tap plays it.

### What it needs that does not exist

- **Panels have to be named, and reliably.** `cfg->name` exists ("which twin's endpoint this
  is") and the unit on the bench is provisioned as `''`. A `send to` router matching against
  an empty string is a message that silently goes nowhere.
- **A message store.** Audio blobs through the storage abstraction (`CLAUDE.md` #2), never a
  raw path, with a per-panel queue and a retention rule — a voice note is a recording of a
  child's bedroom and should not accumulate forever.
- **Delivery inside a child's attention span.** The panel polls settings and the manifest
  every fifteen minutes. That is fine for firmware and useless for a message; this needs a
  short poll or an SSE channel, and the plan already recommends one WebSocket per endpoint.
- **Playback, which the conversation path needs anyway.** The same PCM sink, so this costs
  nothing extra once press-and-hold works.
- **The bubble, which is mostly drawn.** `display.c` already has `bubble()` and a tap already
  resolves to a zone; a notification bubble is that plus a queue count.

### Three recipients, not two

The owner, extending it: *"Another route should be 'tell Dad' where my pwa can play them. And
pwa gets 'tell Elora' and 'Lydian' buttons to send them messages."*

So the directory is **Elora's panel, Lydian's panel, and Dad's PWA**, and messages move in
every direction between them. That is a better feature than panel-to-panel and it is also a
different one, because the third recipient is not a device.

| | Elora / Lydian | Dad |
|---|---|---|
| what it is | a panel, a `device_key` principal | the owner, a session — and possibly no session at all when the message is sent |
| how it arrives | a bubble on the glass, tapped to play | a list in the PWA |
| how it is sent | *"tell Dad …"*, spoken | a **button**, because Dad is not going to hold a phone down and say "tell Elora" |

**The asymmetry is the point.** A panel is always on and always the same person; a PWA is a
session that may not exist when the message is sent, on a device that may be asleep. A message
to Dad has to survive nobody being there, which a queue does anyway — but it means "delivered"
and "played" are different states for Dad and effectively the same for a panel.

### What the third recipient adds

- **A format conversion the panels do not need.** The PWA records through the browser, which
  means webm/opus, not 16 kHz mono s16. The box must transcode before a panel can play it —
  and the `tts-stt` container already carries ffmpeg for Kokoro's effects pipeline, so this is
  configuration rather than a new dependency.
- **A route the owner can reach.** `/endpoint/*` is `PanelDep`; a PWA list and a send button
  are `OwnerDep`. Same store, two doors.
- **Names that are load-bearing.** Already flagged, and now worse: a directory of three makes
  *"tell Dad"* vs *"tell Elora"* a routing decision taken from a transcript, and the bench
  panel is still provisioned as `''`. A name that does not match is a message that silently
  goes nowhere, which is the worst possible failure for a four-year-old who thinks they just
  spoke to their sister.
- **A recogniser question.** MultiNet resolves a fixed phrase list and cannot hear a name it
  was not given; whisper can. So *"tell Dad"* is a transcript-side match on the box, not a
  command on the panel — which is already how this design works, but it means the panel cannot
  confirm the recipient before the audio leaves. The bubble that says "sent to Dad" has to come
  back from the box.

### The part worth arguing about before it is built

**A voice note is a recording of one child's room that surfaces in another child's room** —
and now in the owner's pocket, and the owner's voice in a child's bedroom. It is the right
feature and it is also the first thing here that moves audio between people rather than to a
model and back. Retention, a way for the owner to see what has been sent, and a cap on queued
messages belong in the first version rather than a later one — the same reasoning that put a
VAD gate on the upload so an accidental hold sends nothing.

The PWA side makes that easier rather than harder: the owner's list of received notes is also
the audit surface, so "what has this thing recorded" has an answer that is a screen rather
than a log grep. Building the Dad route first would arguably be the safer order.

### The grammar, decided (owner, 2026-09-22)

*"Eventually when all the text recognition stuff is going, I want the commands to be 'send
xyz' or 'send to dad xyz'. If we didn't say 'to dad', default the voice message to the other
robot. Voice commands not starting with 'send' should go to the LLM."*

That is the whole routing rule, and it is a better one than the three-recipient sketch above
proposed, for three reasons.

**One reserved word, not a vocabulary.** Everything hinges on whether the transcript begins
with `send`. There is no list of names to keep in step with the panels that exist, no
disambiguation between "Elora" and "a Laura", and nothing to relearn when a third panel
arrives. The recogniser question shrinks to: did the first word come back as `send`?

**The default is the useful one.** A panel in Elora's room has exactly one obvious other
robot, and it is Lydian's. Making that the no-argument case means the common message — one
twin to the other — costs the shorter sentence, and the rarer one (to a parent, who is not a
device) costs the longer. The sketch above had this backwards by treating all three
recipients as equals.

**Everything else is a conversation.** No classifier decides whether an utterance is a
command; the absence of one word does. An utterance that is not a `send` goes to the LLM
unchanged, which is what the panel already does with a press-and-hold today — so this adds
a prefix check in front of an existing path rather than a second path.

Three things it still needs, none of them hard but none of them free:

- **Whose "other robot" is whose.** The default recipient is a property of the sending panel,
  so it is a column on `endpoint_settings`, not a constant. The bench unit is provisioned with
  an empty name, which is the same gap §10.4bs flagged for telemetry.
- **What "dad" resolves to.** A parent is a principal with a PWA, not a device; the recipient
  table has to hold both kinds without the panel knowing the difference.
- **Where the prefix is stripped.** On the box, not the panel: the panel uploads audio and has
  no transcript. `POST /endpoint/converse` already has the text and is the only place that can
  both read the first word and route the rest — which also means an accidental "send" costs a
  misrouted note rather than a lost one, so the fallback when a recipient does not resolve is
  to answer it as a conversation, not to drop it.

The retention and audit points below apply unchanged: this makes the routing simpler, not the
recording less consequential.

### The dependency, stated plainly

None of this is buildable until **press-and-hold is confirmed working on hardware** —
capture, upload, reply, playback, end to end, with the timings out of `turn:` and
`endpoint.converse`. Voice notes reuse every one of those pieces. Building the routing on top
of an unverified transport would repeat the mistake §10.4bk was written to avoid: committing
to a design on a number nobody has yet.

## Open questions

- ~~**Does `base.en` get under 2 s?**~~ **Answered, and the question was wrong.** The
  window was the cost, not the model: per-request `audio_ctx` gets `large-v3-turbo`
  to 1.3 s on a short clip. See "What the box actually measured".
- **Which model answers?** Still open, but no longer a guess about speed: the 120B answers
  a child's turn in ~900 ms when its one slot is free and in 12–43 s when the assistant
  has it. The reason to move `pet.turn` to a small co-resident model is the SLOT, not the
  tokens/s. What is unmeasured is whether `qwen3.5-4b` is a good enough pet — that needs
  the real prompt run against it, and the owner's ear, not another log.
- **What does it say?** A pet talking to a four-year-old needs a persona and bounds, and that
  is a content decision, not a plumbing one. `agent_for_owner_reply(...)` is not the right
  profile for this.
- **Does the whisper call need accounting?** The ledger cannot see it today. A voice feature
  that fires whisper on every utterance makes that blind spot much easier to hit.
