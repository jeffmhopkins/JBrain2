# APRS — a heard log, position as a location transport, and authenticated station control

> **Status:** Planned · **Last verified:** 2026-09-02 · **Waves:** P0◻️ P1◻️ P2◻️ P3◻️ P4◻️ (nothing built; the P3 GUI gate is **closed** — shape A, a tab of the Radio launcher, `../mocks/aprs/a-launcher-shape.html`)

A second RTL-SDR dongle, permanently parked on a packet frequency, decoding APRS.
What it produces is three things that get progressively more dangerous, so they ship
in that order: a **log** of what was heard, **position fixes** feeding the location
core the box already has, and **authenticated one-time commands** that can act.

Companion to `SDR_RADIO_PLAN.md`, which owns the interactive tuner and the first
dongle. This plan owns the second dongle and everything packet.

## Why this shape

The design converged through three rejected alternatives, and the reasons are worth
keeping because each one is a trap that looks reasonable from the outside.

**Rejected: whisper listening for a spoken code.** Digits are whisper's worst case —
it normalises unpredictably ("four one two" → `412`, `4 12`, `for one to`), and
narrowband voice with fading is where it degrades hardest. Measured on this box,
transcription also costs ~9.8 s per call (`SDR_RADIO_PLAN.md`, caption timing), so a
spoken trigger would fire ten to twenty seconds after the words. A closed-set match
against a small pad with a rejection margin would have made it *workable*; APRS makes
it unnecessary.

**Rejected: DTMF.** Right answer for FM repeater control, wrong one here. On SSB a
tuning error shifts audio frequencies 1:1, so a 100 Hz mistune moves every tone
outside DTMF's ±1.5–3.5 % tolerance. It also carries no identity and no integrity.

**Chosen: APRS.** AX.25 frames are CRC-checked — a packet decodes or it does not, so
there is no false-accept rate to characterise, no confidence floor, no margin
threshold. Latency drops to sub-second, nothing touches the GPU, and the whole path is
**testable from a WAV file in CI**, which the voice designs never could be.

**Frequency is a deliberate choice, not a default.** 144.390 is digipeated regionally
and gated to APRS-IS, where every packet is archived publicly and permanently on
aprs.fi. That is acceptable — arguably desirable — for position beacons, and wrong for
command traffic. The two roles may end up on different frequencies; §7 holds that open.

## Regulatory footing

**The box never transmits.** It is a receiver, and receiving is not regulated. The only
regulated act is the owner's mobile transmitting, which is ordinary licensed operation:
the command travels in **plaintext** (`GATE 7K2M9` — anyone may read it) with an
authentication tag appended, and the operator IDs per §97.119. Nothing about a
message's meaning is obscured, which is the actual test in §97.113(a)(4); only the
credential is a MAC. This is why the design authenticates rather than encrypts, and
why an earlier one-time-pad sketch was dropped even though it was cryptographically
fine.

## Non-negotiables (root `CLAUDE.md`)

1. **LLM adapter** — the decode path touches no model at all. Where an authenticated
   command starts an agent chat (P4), it goes through the ordinary agent entrypoint.
2. **Storage abstraction** — no file I/O in the decode path; frames are rows.
3. **RLS** — `app.aprs_packets`, `app.aprs_commands` and `app.aprs_counters` are
   owner-only, each with an isolation test. Position fixes inherit the **location**
   domain firewall by going through the existing location core rather than around it.
4. **Tests with the code** — and here they are unusually good: AX.25 decoding is
   deterministic, so CI decodes fixture WAVs and asserts exact frames. No radio, no
   human, no flake.
5. **Docs travel** — this plan's waves flip here as they land.
6. **No terminal** — the second dongle must be discoverable, arm-able and diagnosable
   entirely from the PWA. A `.env` flag or an ssh step is a design failure, not a
   deployment note.

## The two trust tiers

The single most important boundary in this plan. It is not a guideline.

| | **Authenticated** | **Unauthenticated** |
|---|---|---|
| Established by | HMAC + monotonic counter | nothing — any heard packet |
| Source | verified | a source callsign, which is plain bytes and trivially forged |
| May | fire allowlisted actions; start an agent chat from a **canned** prompt | be logged, displayed, and produce position fixes |
| May **never** | — | supply text that reaches a model as instructions |

A received packet becoming an LLM prompt is prompt injection with an antenna. Heard
text is stored as untrusted content and is never concatenated into an agent prompt,
including anywhere it is later summarised. The callsign filter is a *filter* — it
narrows noise, it does not authenticate.

## Waves

### P0 · Second tuner, addressable

The sidecar assumes one radio ("one tuner, one session"). It becomes **one session per
device**: enumerate by USB serial (the supervisor's probe already reports it — a
Nooelec NESDR SMArt v5 read `09022796`), address sessions by device, and keep the
existing 409 semantics per device rather than globally. The tuner sheet keeps working
against whichever device it holds.

Bought rather than built: a priority scheduler that lets a 24/7 watch yield to
interactive listening and resume afterwards is real complexity, and a second dongle is
$30. USB passthrough is already `/dev/bus/usb`, so the container sees both.

**Exit:** two dongles enumerated and independently tunable, PWA-visible, with the
existing tuner unaffected.

### P1 · Decode and log

Direwolf (the reference soft-TNC — bit sync, NRZI, HDLC, CRC) joins `rtl-sdr` and
`ffmpeg` in `Dockerfile.sdr`, fed FM-demodulated PCM from `rtl_fm`. The sidecar exposes
`/listen/packets`, mirroring the existing `/listen/segments` framing. The api stores
rows: `heard_at`, `frequency_hz`, `source`, `destination`, `path`, `type`, `payload`,
`raw`.

No UI, no actions, no position handling. **This wave is the de-risking wave** — it runs
for a week against real traffic before anything is allowed to act on a packet.

**Exit:** frames from a fixture WAV decode byte-identically in CI; real traffic
accumulates on the box; `aprs_recent()` is readable by jerv (a `read` tool).

### P2 · Position as a third location transport

`backend/src/jbrain/locations/ingest.py` is already a shared core: OwnTracks-over-HTTP
and MQTT both feed it, and it does parsing, the future-clock guard, idempotent storage
and **inline geofence detection** under a subject-pinned device context — deliberately
shared so the transports cannot drift. APRS position becomes the **third transport into
that same core**, never a parallel evaluator.

Geo triggers then require no new trigger machinery whatsoever: the core already emits
`location.geofence_transition` into the Phase 5 dispatcher, and `TriggerFilter.
forward_keys` already documents that event as forwarding opaque ids and **deliberately
not raw coordinates**.

Two things this wave must decide rather than discover:

- **Why it exists.** The owner's phone already feeds this core, so geo triggers work
  today with no radio. APRS position earns its place only **where there is no cell
  coverage** — which is exactly where digipeaters shine. It is therefore a *fallback
  tier*: marked as a distinct source, with lower accuracy expectations.
- **Dedup.** Phone and radio reporting together produce near-duplicate fixes, and
  geofence transitions must not flap between sources. A source field plus an explicit
  reconciliation rule, chosen deliberately and tested with both sources interleaved.

**Exit:** a beacon from the truck produces a fix and, crossing a geofence, the same
event the phone produces — with no second geofence code path in existence.

### P3 · The launcher — an APRS tab, and triggers that are ordinary automations

**GUI gate closed 2026-09-02: shape A**, a tab of the Radio launcher (Tuner / APRS /
Recordings). Binding spec `../mocks/aprs/a-launcher-shape.html`.

Observability is the point, not decoration: a watch that silently died is worse than no
watch. `last_heard_at` and a decode rate are load-bearing, for the same reason the
tuner's signal meter was deleted — a control that lies is worse than an absent one. So
the health reading is **last decode and rate**, never a signal bar.

**Triggers are not a new list.** An APRS trigger is an `EventTrigger` — the shape
`workflow/contracts.py` already models beside `ScheduleTrigger`, and which the Ops
Workflow screen already renders (`whenLine`: "When \<ev\> → run \<pipeline\>"). The tab
therefore shows the **same automation cards**, with the same enable switch, the same
recent-run summary off the `runs` log, and the same run-now. Wiring is one field: the
reader groups an automation by its pipeline's primary action's `category`
(`ActionSpec.category`), explicitly "never a hardcoded id list", so declaring a `radio`
category is the whole of it.

**Arming windows reuse the task schedule spec.** A task is `on_demand | once | repeat`
with freq/days/time in an IANA timezone (`tasks/schedule.py`), and `AutomationsScreen`
already reuses that editor once — its `SchedDraft` is documented as "the task-style
day/time/repeat surface, reused". An armed RF trigger wants exactly that vocabulary,
answering a different question: not *when does it run* but **when is it listening**.

| Kind | Meaning here |
|---|---|
| `on_demand` | armed whenever enabled — today's behaviour |
| `once` | **disarms itself after firing** — the one-time-command semantic, for free |
| `repeat` | armed only inside a window, e.g. weekdays 06:00–09:00 |

`repeat` is a **security control, not a convenience**: outside its window the gate
command does not exist, which shrinks the attack surface to the hours the owner would
actually use it. `once` means an armed command cannot be re-fired even if the counter
logic were ever wrong — defence in depth against P4's own credential.

#### The trap: arming must gate at verify time, NOT as a precondition

`ActionSpec.precondition` looks like the seam for this — it is described as the engine
seam for "only run when X holds", and an `armed_now` check would drop in neatly. It is
the wrong seam, and using it would build a badly broken system.

A precondition **defers**: unmet means a fixed retry that "can wait indefinitely",
because it was built for "the local model is not resident yet", where waiting is
exactly right. Arming is the opposite. A command received at 03:00 outside its window
must be **refused, logged and pushed** — not queued until 06:00 and then executed. A
deferred gate command is a gate that opens hours later, for someone who is no longer
there, in response to a transmission the owner may not have sent.

So the arming window is evaluated at **dispatch/verify time** and produces a rejection,
which lands in the feed and the push like any other rejected attempt. The precondition
seam is left alone.

### P4 · Authenticated commands

Last, deliberately, on a decoder already proven by P1–P3.

- **Credential:** HMAC over a monotonic counter, truncated to ~5 base32 characters
  (`GATE 7K2M9`). Short is correct here: the threat model is *online guessing over a
  radio channel* — every attempt must be transmitted, slowly, loudly, and
  direction-findably, against a lockout. ~25 bits with a 5-attempt lockout is
  overwhelming, and it is short enough to key into a mobile head by hand. A phone-app
  sender computes it and length stops mattering, but the design must not *require* the
  phone.
- **Counter resync is mandatory.** A packet that fails to decode advances the sender's
  counter and not the box's; without a look-ahead window of the next K counters the
  system wedges permanently after one miss.
- **Atomic consume.** Code and command in a single frame, counter burned on first
  match, so a listener can only replay a command that is already spent — they cannot
  substitute a different one without the key.
- **Action allowlist**, capped by permission class. An over-the-air trigger never
  reaches `sensitive`.
- **Push on every attempt, accepted _and_ rejected.** The box cannot answer over the
  air, so the phone is the only feedback channel — and "heard you, code wrong" must be
  distinguishable from "never heard you". A push the owner did not cause is also the
  intrusion alarm. With no cell coverage the operator is blind; that is the honest limit
  of a receive-only box and is documented, not designed around.

**Exit:** a command from the truck fires an allowlisted action; a replay does nothing; a
forged callsign does nothing; every attempt is visible.

## Open decisions (§7)

- **One frequency or two.** Commands want a private simplex channel; position wants the
  digipeated network. Two dongles could split them, at the cost of losing the interactive
  tuner's second radio.
- **Sender.** Radio-head only (short codes mandatory) or a phone app with a TNC (codes
  can be long and automatic).
- **First command.** Recommended: something harmless — a push, or a logged note — so P4
  ships with the auth path exercised and nothing consequential wired up.
- **Key storage and rotation** on both ends.
- **Retention** for `app.aprs_packets`: a busy channel is a lot of rows, and heard
  traffic is other people's data.
