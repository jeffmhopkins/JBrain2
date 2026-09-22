# jpanel — the panels as a product: voice post, and a screen that sleeps

> **Status:** Proposed · **Last verified:** 2026-09-22 · **Waves:** W1◻ W2◻ W3◻ W4◻

The owner, across two asks:

> *"Cross panel or panel to pwa messaging… a little pop-up box would show up if a message is
> available to play… this way if I'm at work they send me a message, I can read it as a text and
> if it doesn't make sense I can try and listen to it, and then I can send them a message back
> as a text and it'll be rendered to them."*
>
> *"Add a 5-minute dim the screen, 15 minute turn off the screen timer, that wakes up on any
> kind of accelerometer movement or screen touch."*
>
> *"Change it to jpanel, and integrate it with the flashing stuff on another tab."*

**`jpanel` is the panels as one thing** — the messages they carry, and the panels themselves.
The PWA surface is tabbed: **Messages** and **Flash**, the latter being today's
`EndpointsScreen` moved rather than rebuilt (it is already "its own surface, not a card inside
Ops"). One launcher for "the panels in my house" beats two that each do half.

---

## Part A — the screen that sleeps (W1)

Independent of everything else here, shippable on its own, and the owner feels it the same day.
It goes first for exactly that reason.

### The rule

| after | what happens |
|---|---|
| 5 min idle | dim to a low brightness |
| 15 min idle | dark: brightness 0, and the animation stops |
| touch, movement, a voice command, or an arriving message | full brightness, animation resumes |

**Idle** means no touch, no meaningful accelerometer movement, and no turn in flight. A panel
that is speaking, listening or thinking is never idle.

### Why it does NOT call `esp_lcd_panel_disp_on_off(false)`

This is the whole design decision, and it is driven by this panel's history rather than by
taste. The obvious implementation of "turn off the screen" is the display-off command, with
display-on to wake. **This board has a documented habit of not coming back from display state
transitions** — §10.4am through §10.4cs are a long record of a CO5300 that goes dark and stays
dark, and §10.4cs establishes that the controller's hardware reset has never once been pulled.

A sleep feature whose wake path is the exact operation that has been failing would convert a
power saving into *a child's toy that is dead every morning*. That trade is not worth 15
minutes of AMOLED backlight.

So instead: **brightness to 0 and stop blitting.** Nothing touches the controller's on/off
state, nothing re-runs an init sequence, and waking is a brightness write plus resuming the
render loop. On an AMOLED a black frame at zero brightness is genuinely dark and genuinely
cheap, and stopping the blits is where the real saving is anyway — a frame is ~330 KB over
QSPI at ~25 fps, which costs far more than the panel idling.

It also sidesteps §10.4o's rule that the panel must never go *still* — that rule is about a
screen expected to be visible. A deliberately dark panel that is about to be woken by a full
fresh frame is a different case, and the first blit on wake is a complete frame rather than a
delta.

True display-off remains available if measurement later shows the residual draw matters. It
should not be reached for until the black screen has a confirmed root cause.

### Waking

- **Touch** — already polled every frame; free.
- **Accelerometer** — `imu_read` gives raw counts at ±4 g (1 g ≈ 8192). Movement is a change in
  the smoothed vector beyond a threshold, with hysteresis, because the part is noisy at rest
  (§10.4af spent three releases on exactly that noise). Picking the threshold off a bedside
  table rather than a desk is a bring-up task, not a constant to guess here.
- **Voice** — the microphone and the recogniser stay live while dark. A panel that cannot hear
  "hey fish" in the dark is a panel that is off, and the owner asked for a screen timer, not a
  power switch.
- **An arriving message** (Part B) wakes it, because a pop-up nobody can see is not a pop-up.

### What sleep must not break

The render loop is the panel's clock — the level meter, the recogniser feed and the capture all
hang off it (§10.4). Sleep stops *blitting*, not the loop. The task keeps turning at a slower
cadence; it simply stops sending pixels.

---

## Part B — voice post

### The one word that decides the design

**Asynchronous post, not a call.** A message is recorded, stored, and waits until the recipient
chooses to play it. Nothing rings, nothing interrupts, nothing is missed by not being there. A
four-year-old cannot be expected to be present at the moment their sister speaks, so an
intercom that only works when both ends are listening would be a toy that mostly fails.

It also decides the failure behaviour, which is the part that matters in a bedroom: **a message
that cannot be delivered is still a message.** The box holds it; the panel raises the pop-up
whenever it next asks.

### The asymmetry, stated once

|  | sends | receives | reads |
|---|---|---|---|
| **panel** (each twin) | audio only | audio, played aloud | — |
| **PWA** (Dad) | **text**, spoken to them by TTS | audio | **transcript**, with the audio available |

The panels never send text and never read. The PWA never has to listen if it does not want to.
That is the owner's requirement verbatim and it is also the right split: a four-year-old cannot
type, and a parent at work cannot play audio out loud.

### Why this is not built on `/converse`

`/converse` is a request/response turn: audio up, reply down, nothing stored, nothing addressed.
This is the opposite on all three — addressed to a principal, persists until played, and the
reply may arrive hours later from a different device. Sharing the route would mean a turn that
sometimes answers and sometimes files, which is the kind of overload that makes a failure mode
impossible to name.

What it DOES reuse: the capture buffer and upload path in `talk.c`, the `_trim_to_speech`
silence trim, the playback path through `s_play`, Whisper for the transcript, and the storage
abstraction for the audio (`CLAUDE.md` #2).

### Data

One table, `jpanel_message`:

| column | why |
|---|---|
| `id` | |
| `sender_principal`, `recipient_principal` | who it is from and for — a panel or the owner |
| `blob_sha256` | the audio, through the storage abstraction, never a raw path |
| `transcript` | STT for a panel's message; the typed text for Dad's |
| `composed` | `voice` or `text` — how it was made, which is not how it is played |
| `duration_ms` | a length the PWA can show without fetching audio |
| `created_at`, `played_at` | `played_at IS NULL` is the whole inbox query |

**RLS is the feature, not an afterthought.** These are recordings of small children in their
bedrooms. A panel principal may read only messages addressed to it and rows it sent, and may
**never** read its sibling's inbox. Ships with an RLS isolation test in the same PR
(`CLAUDE.md` #3) that asserts the **sibling** case explicitly, not just owner/not-owner.

**Retention:** unplayed messages are kept indefinitely — a message nobody heard is the one thing
that must not evaporate. Played messages are kept 30 days, then swept. Proposed; §5.

### The panel

Two phrases, obeying `vocab.h`'s three rules (lowercase and spaces, two words minimum, no
phrase a prefix of another):

- `send a message` → the other panel
- `send dad a message` → the owner's PWA

Neither is a prefix of the other (`send a…` vs `send d…`).

**"The other panel" needs a rule, because it is only obvious with exactly two.** With one other
enrolled panel that is the recipient; with none or several the phrase is refused out loud —
*"I don't know who to send that to"* — rather than guessing. A toy that silently posts to the
wrong sibling is worse than one that says it cannot.

**Recording** is a new state beside `TALK_LISTENING`, not a flag on it: the two differ in where
the audio goes, what is drawn, and what ends them.

- **Ends on silence**, the same hush as a listening turn — already tuned for a four-year-old.
- **A touch cancels and discards it** (owner's choice, and the rule a listening turn now has).
  Silence sends; a finger abandons. No third gesture to learn.
- **A blue dot, not the red one.** This is the owner's actual requirement: red means *the robot
  is listening to you*, and talking to your sister must not look like that. The recipient's
  name sits beside it so the child can see who they are talking to before they speak.
- **Capped at 20 s.**

**Being told there is one.** The manifest poll is ~15 minutes, far too slow for *"my sister just
sent me something"*, so a small `GET /jpanel/waiting` runs on a ~30 s cadence returning a count
and a sender name. A waiting message draws a **pop-up over the face** — big, tappable anywhere
inside, naming who it is from — and wakes the screen if it is asleep. It does not auto-play; an
unplayed message survives a reboot because the state lives on the box.

**After it plays**, a repeat icon in the **top-left** for 5 seconds; a tap replays, then it
clears. Deliberately short: it is for *"what did she say?"*, not a permanent control.

### The PWA

`Pet face` comes out of the launcher and **`jpanel`** goes in (`Launcher.tsx`, target `petface`
→ `jpanel`). The separate `Pet` → `petcontrol` tile stays; it is a different thing.

Two tabs:

- **Messages** — one list grouped by panel, newest first. An unplayed badge per panel, because
  that is the question being asked at work. Each message shows sender, time, duration, and **the
  transcript as the primary content**, with a play button beside it: the text is what gets read,
  the audio is the fallback for when the transcript does not make sense — which, given how the
  transcriber handles four-year-olds, it often will not. A compose box per panel: type, send;
  TTS speaks it, the audio is stored, and the typed text is kept as the transcript so both ends
  agree about what was said.
- **Flash** — today's `EndpointsScreen`, moved rather than rebuilt.

**Dad's voice is male.** The pet answers in `kokoro-af_heart`; a message from Dad arriving in
the pet's own voice would teach a four-year-old that the robot and their father are the same
thing. A separate setting, defaulting to a male Kokoro voice — the cheapest possible signal that
this is a person and not the toy.

---

## 3b. The contract

Written down before either side is built, because W3 (the panel) and W4 (the PWA) are both
clients of W2 and a route that moves under them costs two rewrites. This section is the single
source of truth; if an implementation disagrees with it, the implementation is wrong or this
section gets edited first.

### Panel-facing — `/api/endpoint/jpanel/*`, `Authorization: Bearer <device_key>`

| route | takes | gives |
|---|---|---|
| `POST /send?to=panel\|dad` | the raw 16 kHz mono s16 body, exactly as `/converse` takes it | `200 {id, to_name}`; `409` when `to=panel` and there is not exactly one other panel |
| `GET /waiting` | — | `200 {count, from_name}` — tiny on purpose, polled ~30 s per panel forever |
| `GET /next` | — | `200` raw 16-bit PCM at 16 kHz with `X-Jpanel-Id` and `X-Jpanel-From`; `204` when the inbox is empty |
| `POST /played` | `{id}` | `204` |

**The body is raw PCM, not multipart**, and the first cut of this contract said multipart
before `/converse` was re-read: *"In, raw 16 kHz mono s16 — no container, no codec, because the
panel has neither."* A panel that cannot build a multipart body cannot send a message, and
asking the firmware to grow a MIME encoder to satisfy a table in a plan would have been the
wrong way round.

`GET /next` returns the OLDEST unplayed message and does **not** mark it played — `POST /played`
does, after the panel has actually finished playing it. Separating them is what makes a message
survive a reboot mid-playback instead of being lost by having been handed over.

### Owner-facing — `/api/jpanel/*`, owner cookie

| route | takes | gives |
|---|---|---|
| `GET /messages` | `limit` (default 100) | `200 {panels: [PanelThread]}` |
| `POST /messages` | `{to_device, text}` | `201 Message` — TTS renders it, the typed text is kept as the transcript |
| `GET /messages/{id}/audio` | — | `200 audio/wav` |

```ts
type Message = {
  id: string;
  from_name: string;          // "Ellie", "Dad"
  to_name: string;
  direction: "in" | "out";    // relative to the OWNER: in = from a panel
  transcript: string;         // STT for a panel's; the typed text for Dad's
  composed: "voice" | "text";
  duration_ms: number;
  created_at: string;         // RFC3339
  played_at: string | null;   // null = still waiting
};

type PanelThread = {
  device_id: string;
  name: string;
  unplayed: number;           // messages from THIS panel the owner has not played
  messages: Message[];        // newest first
};
```

**`transcript` is the primary content in the PWA, not a caption.** The text is what gets read at
work; the audio is the fallback for when the transcript does not make sense — which, given how
the transcriber handles four-year-olds, it often will not.

## 4. Waves

- **W1 — the screen sleeps.** Dim, dark, wake on touch/movement/voice. Firmware only,
  independent of everything below, and the owner feels it immediately.
- **W2 — the spine.** Migration + RLS + isolation tests. Panel routes (`send`, `waiting`,
  `next`, `played`) and owner routes (list, send-text-as-audio, stream). Whisper in, TTS out.
  No UI; verifiable entirely by tests.
- **W3 — the panel.** Vocabulary, `TALK_RECORDING`, the blue dot, the pop-up, playback, the
  repeat icon.
- **W4 — the PWA.** The jpanel screen with both tabs, the launcher swap, transcripts, compose.

W2 lands before W3 and W4 because both are its clients and a route that changes under them
costs two rewrites.

## 5. Open, and deliberately not guessed

- **The movement threshold** for waking. It has to be picked against a panel on a bedside table,
  not reasoned about here; the part is noisy enough at rest that §10.4af spent three releases
  on it.
- **Retention.** 30 days after playing is a proposal. Unplayed-forever is not.
- **More than two panels.** The refusal rule above is safe but unhelpful; addressing by name
  needs the twins' names in the offline vocabulary, which the owner has deferred.
- **There is no way to enumerate panels that is a mechanism rather than a convention**, and W2
  ran into it immediately. A panel is an ordinary `device_key` principal — the same substrate as
  an OwnTracks phone — and the only thing marking one is the label `/flash` writes:
  `"panel {name}"`, or `"room endpoint panel"` when the owner named no unit. So "the other
  panel" has to be resolved by `label LIKE 'panel%'`, which a hand-labelled device key could
  join and which a re-flash without a name degrades.
  It is survivable — the worst case is a message offered to a device that RLS then refuses to
  deliver to, so the failure is a dead letter rather than a leak — and W2 proceeds on it. But it
  wants a real marker (a principal sub-kind, or a panel roster table) the first time a third
  device key exists in this house, and that is a schema change rather than a patch.
- **Notifying Dad.** A message to the PWA currently waits to be looked at. Whether it should
  push is a question about a parent's phone, not about the panels.
- **The transcriber mangles small children.** `"tell us a joke"` arrived as `"There is a joke.
  There is a joke."` Every transcript here inherits that, which is exactly why the audio is
  always kept and always playable. Fixing STT for child speech is its own piece of work.
