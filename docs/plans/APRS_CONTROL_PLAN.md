# APRS — a heard log, position as a location transport, and authenticated station control

> **Status:** In progress · **Last verified:** 2026-09-04 · **Waves:** P0✅ P1✅(**on air** — the box decoded live traffic on 144.390 and has been logging since: 184 frames in the first 90 minutes) P1a✅ P3✅ P4🟡(built, independent review folded in; on-box run pending) P0b✅(**both halves** — naming, roles and `-d <serial>`; then one session per device, so APRS and the tuner run at once) P2◻️(deferred — geo is not in the first build). Both GUI gates are **closed** — shape A throughout (`../mocks/aprs/a-launcher-shape.html`, `b-trigger-editor.html`, `c-single-dongle.html`), and `c-single-dongle`'s switch placement was **superseded 2026-09-04** by the launcher's own round (`../mocks/sdr-launcher/`): the switch is now a job on a radio. One state, never two switches, unchanged.

A second RTL-SDR dongle, permanently parked on a packet frequency, decoding APRS.
What it produces is three things that get progressively more dangerous, so they ship
in that order: a **log** of what was heard, **position fixes** feeding the location
core the box already has, and **authenticated one-time commands** that can act.

Companion to `SDR_RADIO_PLAN.md`, which owns the interactive tuner and the first
dongle. This plan owns the second dongle and everything packet.

Once P1 was on the air, what it recorded turned out not to look like the spec — 5 values
in `source` over 15 actual stations, because 71% of the channel was one IGate relaying
APRS-IS. Making the log readable in the face of that is its own plan:
`APRS_FILTERING_PLAN.md`.

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
3. **RLS** — `app.aprs_packets` and `app.command_attempts` are owner-only, each with an
   isolation test. (Written as three tables; built as two. A command's credential and
   counter live on its own `app.tasks` row rather than in a separate table, because the
   GUI gate chose a fourth trigger kind over a parallel command system — so revoking a
   command is deleting one row, and the existing `tasks_owner` policy already covers it.)
   Position fixes inherit the **location** domain firewall by going through the existing
   location core rather than around it.
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

### P0 · One radio, two jobs — the lease grows a purpose

**There will not always be a second dongle**, so the first build must work with one. APRS
logging is something the owner *enables*, and it *reserves the tuner until released* —
which is the lease that already exists. `SessionInfo` grows a **`purpose`** (`listen` |
`aprs`); a logging session holds the radio, 409s the other job, shows its elapsed time
and Release, and lights the omnibox icon, all through machinery that is already shipped.

Two consequences to build deliberately rather than discover:

- **Contention has to read honestly.** Tapping to listen while APRS holds the radio is a
  409, and it must say *"the radio is logging APRS — release it to listen"*, never a
  generic busy error. Same in reverse.
- **Armed is not the same as listening.** A command task fires only while logging is on,
  and with one dongle it often will not be. An armed-but-deaf task must announce itself —
  the same rule that deleted the tuner's signal meter. Auto-enabling logging when a task
  is armed is **rejected**: silently seizing the tuner is how a radio starts to feel
  possessed. The warning offers the switch; it never throws it.

**Landed (sidecar + tool + client):** `SessionInfo.purpose`, validated in `Session`
itself rather than only in `Tuner.start`; a 409 that names the holder and degrades to
"in use" rather than raising inside the tuner lock; a retune REFUSED on a logging
session (it would otherwise move the packet channel while the lease went on claiming to
log, then refuse the next caller with a reason that had become false); `/healthz`
advertising `purposes` so a caller can tell an un-updated sidecar from a working one
instead of trusting a 200; `sdr_listen` passing the sidecar's reason through instead of
overwriting it with a hardcoded "already listening"; and audio that follows the JOB, so
a logging session no longer plays 1200-baud squawk through the owner's speakers.

Still open here: no route above the sidecar can *start* an APRS session — that is P1a's
`sdr_aprs_logging`, and until it lands the capability is reachable only from inside the
`radio` network.

**GUI gate closed 2026-09-02: a switch in the APRS tab.** "Enable APRS logging" lived
in the APRS tab; the Tuner tab read *in use by APRS logging* and offered the handoff
back. Binding spec `../mocks/aprs/c-single-dongle.html`.

**SUPERSEDED 2026-09-04 — the switch moved to the radio.** That round was decided on a
**one-dongle box**, where "which radio" was not a question and a radio-wide job selector
was explicitly rejected as answering nothing. P0b made it a question, and the owner was
asked again rather than the change being inferred: the launcher's round chose **shape A**
(`../mocks/sdr-launcher/`), where the radio is the object and its job — Listen / APRS /
Spectrum / Idle — is chosen inside it. So "Enable APRS logging" is now a job on a radio,
and the Tuner tab's handoff panel is gone with it: which jobs a radio may take, and why
not, is said on that radio's own control before the tap.

**What did NOT change is the rule this section exists for: one state, never two
switches.** The APRS tab keeps its log, its health line, its roster and its command
tasks, and where the switch was it now carries a POINTER to the radio. A second control
over the same state is how one of them ends up lying, and that is still the failure
everything here is written against.

### P0b · Second tuner, addressable — **shipped 2026-09-04**

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

#### What shipped, in two rounds

The second dongle arrived (`77192819` on bus 3-4, beside `09022796` on 1-1) and the
plan's own framing turned out to bury the urgent half. **Addressing** was not a
nice-to-have that simultaneity would bring along — it was a live defect on its own:
`rtl_fm` and `rtl_power` were invoked with **no `-d`**, so both opened whichever device
librtlsdr enumerated first. With one radio on a desk whip and one on a long wire, APRS
could change antenna on a re-plug with no symptom but worse reception. So addressing
shipped first, alone.

| P0b clause | state |
|---|---|
| enumerate by USB serial | ✅ `serials_in()`, one place that knows the payload shape |
| address sessions by device | ✅ `-d <serial>` on both pipelines; the lease reports which radio it holds |
| PWA-visible | ✅ Settings → Radios: name, description, role |
| 409 semantics **per device** | ✅ `Tuner._sessions` keyed by serial; the refusal names the radio |
| two dongles **independently tunable** | ✅ APRS on one and the tuner on the other, at once |

**Round two: one session per device.** `Tuner` became a map keyed by serial. Three things
fell out of that and are the part worth remembering:

*An unnamed session conflicts with everything.* A session started with no serial opens
whatever librtlsdr enumerates first, so nothing can prove it is not on the radio the next
caller wants. Refusing is the only honest answer — the alternative fails as two processes
garbling one dongle rather than as an error. The rule lives in `listen.blocking_key`,
shared with the one-shot capture path, because a capture and a session are the two things
that open a device and a rule they could disagree about eventually puts both on one.

*The capture lock was the same bug one layer down, and leaked both ways.* It was global,
so recording off one dongle refused a capture on the other; and `Tuner.start` never
consulted it, so a listening session could open a dongle mid-recording. Captures now
reserve in the same registry under the same lock.

*`listening` stopped being "the session", and three callers were still reading it.* The
sidecar's `/healthz` keeps `listening` for the omnibox, which draws one icon and prefers
the tuner — and gained `sessions` beside it. The PWA's APRS routes, jerv's
`sdr_aprs_logging` and the packet drain all asked `listening.purpose == "aprs"`, so
opening the tuner sheet while APRS logged would have flipped the switch off in front of
the owner and, worst of the three, **detached the drain** — frames still decoding, none
stored, no error anywhere. One reader (`jbrain/sdr/health.session_for`) now answers for
all three, falling back to `listening` for the seconds during an update when the sidecar
is the older build.

*An unnamed `/listen/stop` is now a choice rather than a tautology.* It takes the
listening session and nothing else, and reports what IS holding a radio so the caller
can name it. A first cut fell back to "the only session when there is exactly one",
reasoning that a one-dongle box has nothing to choose between — but the condition it
tested was `len(sessions) == 1`, equally true of a two-dongle box running only APRS. It
would have stopped the log there. The cost is a real change on a one-dongle box: "release
the radio" while APRS holds it now answers `stopped: false` and points at the APRS switch
rather than stopping the log.

*A general radio someone is already on is not a free one.* `roles.choose` had no notion
of "in use", so with two undedicated dongles APRS and the tuner both got `generals[0]`
and the second caller met the new per-radio 409 naming the radio it had asked for — the
original symptom, through a per-radio sidecar. `resolve.busy_serials` reads what the
sidecar is running and `choose` sorts those last. Serial order still decides between the
FREE radios, and a DEDICATED radio that is busy is still its service's: offering a
substitute there is the silent antenna change this all exists to stop. **So the two
dongles now share themselves out with no settings visit** — dedicating one is how you
pin an ANTENNA to a service, not how you get simultaneity.

*A retune could resurrect a released session and orphan its dongle.* `/listen/tune`
resolves the Session under the tuner's lock and calls `tune` outside it, so a stop
landing in between made `_start_pipeline` spawn a fresh `rtl_fm` for a session no longer
in `_sessions`: invisible to `blocking_key`, never reaped, holding the dongle until the
container restarts — after which the next caller for that serial is let through and two
processes fight over one radio. `Session.tune` now refuses once released
(`SessionGone`). Structurally pre-existing; the window grew with the extra holders.

*A capture reservation lapses.* A session is reaped by asking its process whether it is
alive; a reservation has no process to ask — it is a claim a `capture` promises to
release in a `finally`, and a promise is not a reap. A signal between taking the claim
and entering that `try` stranded the key for the life of the process, refusing that radio
for ever while `/healthz` reported `busy` with nothing running. `RESERVATION_TTL_S` is
longer than any capture the sidecar will run, so an expiry is always a leak.

*The PWA was reading the old shape at the surface the owner touches.* Found by the
independent review, and the same bug the three backend readers had: with APRS on one
dongle and the tuner on the other, `listening` is the tuner's, so the APRS tab showed the
one-dongle contention panel, replaced **Stop APRS logging** with a button whose only
effect was a no-op 200, and printed the TUNER's elapsed time as how long APRS had held a
radio. The Tuner tab had it mirrored. `/api/sdr/status` now carries `sessions` and
`sdrSession.sessionFor` asks per job — `listening` is what to DRAW, `sessionFor` is what
to ASK. No shape changed, so no GUI gate: the panels are the ones the mock specifies,
selected by a condition that is now true.

Still one radio for the omnibox icon: showing two live sessions at once IS a shape change
and needs the gate.

**The rule the round settled** (binding spec `../mocks/sdr-dongles/a-named-roles.html`,
enforced in `jbrain/sdr/roles.py`): a **dedicated** radio that is unplugged makes its
service WAIT, naming the radio, rather than falling back. Falling back is precisely the
failure being fixed. A **never-dedicated** service still shares a general radio — that
is a different case from dedicated-and-absent, and collapsing the two is how a service
changes antenna without saying so. Dedication also binds the **tuner**, or a reservation
would only mean "APRS prefers this one" and would evaporate whenever APRS went idle.

Two smaller decisions worth keeping: choice between general radios is by **serial
order**, because arbitrary-but-repeatable is a choice and arbitrary-per-boot is the bug;
and an **unreachable USB scan names no radio at all** rather than guessing or refusing,
which is exactly what a one-dongle box always did.

### P1a · Turning logging on and off — a tool, and an action

Logging is a lease (P0), so starting and stopping it is the same kind of act
`sdr_listen` already performs. Exposing that lets a **scheduled task** run the window —
"weekdays 06:00, turn APRS logging on; 09:00, turn it off" — using the task scheduler
that already exists, instead of a bespoke arming scheduler for the receiver.

**A separate tool, not a flag on `sdr_listen`.** Extending `sdr_listen` was considered
and is the smaller diff, but its description is entirely about *hearing* — "start
listening", "hear the audio", "you cannot hear it" — and a mode that produces no audio
contradicts the text the model selects on. So:

```
sdr_aprs_logging(enabled: bool, frequency_mhz?: number)   # one concept, both directions
sdr_listen(frequency_mhz, mode)                           # unchanged
sdr_stop()                                                # unchanged: release what is held
```

`enabled` as a boolean rather than start/stop tools keeps a scheduled task to one call
either way, and makes it **idempotent** — "turn it on" when it is already on must be a
no-op that succeeds, or a retry becomes a failure. It returns the **resulting state**,
never "ok", so a model cannot report success it did not achieve. Turning it off stops
the *APRS* session specifically; it must never fall through to releasing a listening
session the owner started, which matters the moment there are two dongles.

`web` permission and jerv's closed allowlist, exactly as the existing pair.

**The trap: a clock-driven hardware toggle should not depend on a model.** An agent task
can decline, mis-call, fail its turn, or report a success it never performed — and the
cost lands on the one contended tuner. It leaves logging on all day so nothing can
listen, or never turns it on so the gate command is deaf at 06:00, which is precisely
when it was wanted. So the scheduled path gets a **registered action** as well
(`ActionSpec`, deterministic, E3), which the Automations scheduler fires directly:

| Path | Use | Why |
|---|---|---|
| `sdr_aprs_logging` tool | conversational — "turn APRS logging on" | jerv needs to do it when asked |
| a logging **window** on the session | the timed case | a clock must not route through a model to flip a switch |

**Correction, found while building (2026-09-02).** The timed path was specified as a
registered `sdr_aprs_set` action fired by the Automations scheduler. That does not work:
a seeded automation carries its schedule in a **migration**, and the Automations screen
can enable, retime and run automations but cannot *create* one — so the owner could
never set their own hours without a code change. The remaining alternative, a scheduled
agent task, is the unreliable path this very section warns against.

So the timed case becomes an owner-set **logging window** on the APRS surface itself,
enforced by the api rather than by a model — which is where the switch already lives
(round 3, shape A) and needs no scheduler at all. Built in P3.

The owner-visible truth stays the backstop either way: the APRS tab reads last decode
and rate, so a toggle that silently did not happen is visible rather than assumed.

**This is a resource control, not a security control.** Scheduling logging frees the
tuner; it does not narrow *which* commands are live. While logging runs, every armed
task is armed. Per-task arming windows (P4) remain the thing that says "this command
exists only on weekday mornings", and one does not replace the other — dropping arming
in favour of a logging schedule would widen the command surface, not narrow it.

**Sequencing:** this lands *with* P1, never before it. A toggle for a capability that
does not yet exist toggles nothing.

**Landed.** `sdr_aprs_logging` (jerv, `web`, closed allowlist) and `POST /api/sdr/aprs`
for the PWA. Idempotent both ways; reports the state it actually reached rather than
"ok"; stops the APRS session **by id**, so it can never release a listening session the
owner started; and refuses outright against a sidecar too old to understand `purpose`,
which would otherwise return 200 with a plain listening session and let the tool report
success while logging nothing.

**Corrected after review.** Those four guarantees were true of the TOOL and not of the
ROUTE, which had no tests at all — gutting both route bodies passed the whole backend
suite. The route ignored `purposes`, both callers discarded `stopped` (so a stop that
stopped nothing reported success, which for a timed "turn logging off" leaves the tuner
held all day), and an unreachable sidecar read as "not logging". All four now hold on
both paths, and the route tests fail against that gutted-body mutant.

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

**Landed.** The sidecar runs rtl_fm → direwolf for a logging session and serves decoded
frames on `/listen/packets`; `jbrain/sdr/aprslog.py` drains them into `app.aprs_packets`
from a background loop — a loop rather than a route because a log that only records
while someone is looking at it is not a log. `aprs_recent` is in jerv's allowlist, and
its own prose tells the model never to act on a packet.

The fixture is a **capture**, not a construction (`scripts/regen-aprs-fixture.sh`
rebuilds it byte-identically). Two constraints it surfaced, which reading the docs would
not have: direwolf forwards frames only to KISS clients ALREADY attached, so a late or
reconnecting reader gets a hole rather than history; and EOF on its audio stdin ends its
session, so the pipe is held open for the life of the lease.

An independent review mutation-tested this wave and found **13 of 26 mutants surviving**
— including the one that defines it, an `aprs` session running the audio pipeline. What
that turned up, and what now defends it:

- Direwolf's stdout was never drained, so at 64 KB it would have **blocked and stopped
  decoding for ever** while the session reported healthy — the same hazard
  `_drain_tuner_log` was written for, reintroduced 150 lines later. (`-q d` was also
  claimed to silence it: measured, 67 lines against 64 for `-q hd`. It does not.)
- `_publish_packet` dropped the NEWEST frame, losing **every other packet** while a
  reader lagged. This is a log; late still counts.
- A released session left every `/listen/packets` reader blocked for ever, emitting
  keep-alives the api reads as healthy.
- `alive` did not know direwolf existed, so a dead decoder kept a lease claiming to log.
- A failed KISS connect was a silent permanent no-op that still reported healthy.
- Addresses were shifted but never validated, so a CRC-valid crafted frame could carry
  NUL into a Postgres `text` column — which cannot hold U+0000, so the insert would
  raise on receipt.
- `FEND a FEND b FEND` is legal KISS and dropped every second frame; an unclosed frame
  grew the deframer's buffer without bound.
- A logging session accepted `usb`/`wbfm`, which can never carry 1200-baud AFSK.

Still outstanding for the wave's real exit: the on-box run against live traffic.

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

**Landed.** A Radio launcher tile and screen. Packets render badged as **heard** — a
stranger's transmission with a forgeable callsign — which is where the trust-tier rule
stops being a line in a plan and meets the owner's eye.

**The tabs are now Radios / APRS / Recordings (2026-09-04).** They were Tuner / APRS /
Recordings, and the launcher's own GUI round replaced that with shape A: the radio is
the object, so the tuner's transport and the APRS switch are both jobs chosen inside a
radio rather than tabs of their own. The transport is still the composer's own
`SdrTunerControls`, mounted where the radio is listening — reflected, never duplicated
(`../mocks/sdr-tuner/`). Recordings is a later wave and says so.

An independent review caught four ways this screen could be confidently wrong about the
radio — a failed first load spinning on "Reading the log…" for ever with the error
swallowed, the one-dongle contention state unbuilt so the switch could only fail, a
decode rate that never reached zero (a receiver silent for 41 minutes read "26 pkt/hr"
beside "nothing for 41 min"), and quiet and stale sharing an amber dot. All are fixed;
the rate now shares the staleness threshold, which makes that disagreement
unrepresentable rather than merely fixed. Who holds the tuner is read once, from the
lease poll, so the two tabs cannot disagree.

The mock's **command tasks** section is built as a read-only summary — what is armed,
whether it is armed *and deaf*, and what has been tried against it. Editing lives in
Tasks. The deaf warning is the pairing this screen exists for: arming a command and
enabling the receiver are two switches on purpose, so "armed" while nothing is
receiving is the same lie a signal meter on a dead channel tells.

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

#### Scope cut 2026-09-02: the gate, and one action type

The first build is **the authenticated command path only** — no geofence and no position
triggers — and the only thing a verified command may do is **run an agent task**. P2
(position as a location transport) is therefore deferred behind P4 rather than preceding
it; nothing in it changes, it simply is not the first thing built.

That cut is larger than it sounds, because it makes an APRS task **a task**: same name,
prompt, agent, scopes, push and enabled as `TasksScreen.Draft`, with only the trigger
differing. The action registry, the pipeline picker and the permission-class cap all
leave the design with it — and the cap leaves with them, so **the task's scopes become
the cap**. A radio-triggered task scoped to health, finance or location reaches a
firewalled domain on a command sent over the air, and the editor is where that gets
said out loud.

**GUI gate closed 2026-09-02: `on_command` becomes a fourth `ScheduleKind`**, beside
`on_demand` / `once` / `repeat`. One list, one editor, one runs history, one table —
`Draft`, the row, the runs log and the push are untouched, and what is new is a trigger
kind plus the verify path behind it. The Radio → APRS tab shows a read-only summary that
links into Tasks. A parallel collection was rejected as a second place tasks live.

#### Add and edit — a second round, because automations cannot be created

Automations are seeded system config: the Ops screen enables, retimes and runs them, but
never *creates* one. An APRS trigger is owner-created, which is exactly the capability
that paradigm lacks — so the split is **list like Automations, edit like Tasks**, and the
editor takes its own mock round (`../mocks/aprs/b-trigger-editor.html`, awaiting
decision). Note for whoever builds it: Tasks' editor is a **full-screen layer**
(`useBackLayer`), not a `Sheet`, while DESIGN.md's paradigm table sends contextual quick
forms to the bottom sheet — the round decides which.

The editor is also where two invariants stop being prose: it names the **trust tier** of
the trigger being built (authenticated command vs unauthenticated geofence or message),
and it shows each action's **permission class**, with `sensitive` visibly unavailable
rather than silently filtered.

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

**Landed.** `sdr/command.py` is the credential (HMAC over a counter, look-ahead resync,
lockout checked before any comparison) and `sdr/gate.py` the verify path, riding on the
P1 drain: a frame is stored, then offered to the owner's commands. `0181` adds the
fourth kind and its columns; `0182` adds `app.command_attempts`. The editor is the
fourth trigger kind (mock B, shape A); the key is generated on the box and shown once.

Three decisions worth keeping, because each replaced something that looked right:

- **The arming window is checked at VERIFY time, not as an `ActionSpec.precondition`.**
  A precondition *defers* with a retry, and a deferred gate command is a gate that opens
  hours later for someone who is no longer there.
- **"Push" here is the box's own notification stream**, not a third-party push service:
  `NotifyBus` → SSE → the owner's phone. No FCM notifier is configured on this box, so
  the plan's "push on every attempt" is delivered by that path, and by the attempt log
  the Radio tab reads back.
- **Every attempt is RECORDED, not only pushed.** A push is ephemeral, arrives only if a
  device is registered, and is precisely what an attacker hopes goes unread. The push is
  the alarm; the table is the evidence, and the radio tab reads it back. Pushes stop
  once a command is locked out — otherwise an attacker turns the owner's phone into the
  denial of service the lockout exists to prevent — and the rows keep accumulating.
- **Firewalled scopes are warned, not blocked.** Blocking read like defence in depth and
  is not: location is exactly what a command from the truck wants, and the box never
  transmits, so a fired task cannot answer over the air. Only a verified command fires
  anything, and that is the cap that holds.

**Corrected after review.** The first build did not work at all, and the reason it
looked like it did is worth keeping:

- **A verified command could not fire.** `app.task_runs.trigger` had allowed only
  `schedule` and `manual` since the table was made, and the gate fires with `command`.
  Everything upstream worked — code checked, counter burned, attempt recorded
  `accepted`, the owner's phone told that it had run — and then the run insert violated
  the CHECK and was swallowed to a log line. The worst shape a failure can take: told it
  worked, credential spent, nothing done. The integration test missed it because it
  faked the runner *in the Postgres suite*, so the one thing that had to be real was the
  one thing stubbed. It now writes the run row for real (`0183`).
- **A digipeated command locked the owner out.** 144.390 is digipeated, so one
  transmission arrives several times; every copy after the first fails the forward-only
  check, and counting those as guesses spent the whole lockout budget on the owner
  SUCCEEDING. `verify` now names a spent code as spent, and only a code that was never
  ours counts toward the lockout.
- **One NUL byte erased the evidence.** Postgres rejects it in a `text` column, both
  INSERTs swallow their errors so the log survives a bad row, and the comparison strips
  control characters before matching — so a NUL-suffixed code behaved like a clean one
  while leaving no packet row and no attempt row. Five of those locked a command with
  nothing recorded anywhere. Scrubbed on the way in, and again on the evidence path.
- **The atomic consume had no test.** Dropping its `AND command_counter = :seen` passed
  every test in the repository. Sequential duplicates take the spent-code path, so the
  branch is only reachable under real concurrency; the test now offers one frame four
  times at once.
- **An empty key was a public key** (`hmac.new(b"", …)` does not raise, so the codes
  become computable by anyone who has read this repository). Unreachable through the
  API, one column value away — now a CHECK, and a refusal in `verify` besides.
- **An unreadable timezone failed CLOSED**, shifting every window by up to twelve hours
  and refusing the owner's own code. A window is a narrowing; one the box cannot read is
  unapplied, never "denied".
- **The mock's one-shot arming mode was missing** (`Always | Once | Window`). Built, and
  the disarm happens in the same statement as the consume — doing it afterwards leaves a
  window in which a duplicate finds the command still armed.
- **Heard text reached a model unframed.** `aprs_recent` now wraps the log in the same
  `untrusted_external_data` boundary the research feed uses, with the sentinel
  neutralised so a transmission cannot close the envelope it sits in, and jerv's prompt
  carries the pinned clause naming that tag inert. The COMMAND path was already clean —
  nothing from a packet reaches the executor — but the read-back path was not.

**Not done:** an on-box run. The credential, the consume, the window and the lockout are
covered against real Postgres; what no test can stand in for is a real transmission from
the truck decoding into a fired task.

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
