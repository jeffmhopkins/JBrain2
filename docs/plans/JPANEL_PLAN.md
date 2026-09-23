# jpanel — the panels as a product: voice post, and a screen that sleeps

> **Status:** In progress · **Last verified:** 2026-09-23 · **Waves:** W1◻️ W2✅ W3✅ W4✅
> — W2 and W4 shipped together in #1498; W3 shipped across #1504/#1508/#1511/#1513 and is
> **confirmed working on a panel** (voice post both ways, the pop-up, the queue).
>
> **W1's code has landed** — firmware 0.2.92, `screen.{c,h}` plus the render loop — and stays
> open rather than ticked because the wave is not done until it has run on hardware: the
> panel is away until tonight, the movement threshold it ships with is reasoned rather than
> measured, and §5 says how to correct it from the box without a terminal.

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

### What shipped (0.2.92)

The policy is `firmware/main/screen.{c,h}` — thresholds, the dim level and the movement test,
pure arithmetic and host-tested, extracted for the same reason `orient.c` was: a rule about
time and thresholds that is only ever *reasoned* about is how the first orientation shipped
backwards. `display.c` keeps the half that needs the panel: which stage it is in, what wakes
it, and the frame that does not get drawn.

Three things are worth knowing beyond the rule above.

**A message arriving wakes the screen; a message *waiting* does not hold it awake.** Counting
the queue as activity would mean one unacknowledged good-night left the panel lit until
morning, which is the exact thing this feature exists to stop. Nothing about sleeping drops the
queue, so the pop-up is still there when the child touches the panel awake.

**Waking to a message left overnight shows the big box again.** The pop-up stands down to a
badge after fifteen seconds and deliberately does *not* come back when a second message arrives
— the child has been interrupted once and has chosen not to come. A night is not that: whoever
is looking at the panel now was not in the room when it shrank, and a corner badge is not how a
four-year-old finds out their sister sent them something. Only out of dark, because at dim the
screen was visible the whole time; and without a sound, because a message that chirped whenever
somebody walked past the table would be the panel nagging.

**A finger on a dark screen buys the screen and nothing else.** The child cannot see what they
are aiming at, so the waking touch is spent on waking — it does not poke the pet, arm a gesture
or acknowledge a message. The next tap, aimed at a face that is now visible, lands normally.
Dim does not consume the touch: the pet is still on screen there, and a tap that hits what you
can see should do what it looks like it does.

**The stage is in the heartbeat and in the render beat, because a sleeping panel reports what a
broken one reports.** Sleep stops blitting on purpose, so `blit_ok` stops climbing — which is
the signature of the stalled render task that cost 0.2.44 a photograph from the owner to
diagnose. `screen: "awake" | "dim" | "dark"` in telemetry, and the same word in the
`render: N frames ok` log line, is what tells the two apart for someone with no terminal
(CLAUDE.md #10).

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
| **PWA** (Dad) | **audio OR text** — text is spoken by TTS | audio | **transcript**, with the audio available |

The panels never send text and never read. The PWA never has to *listen* if it does not want
to. A four-year-old cannot type, and a parent at work cannot play audio out loud — so the
reading half of this is unchanged and load-bearing.

> **AMENDED 2026-09-23.** This table said the PWA sends **text and nothing else**, and that
> the panels are "the only half of this that speaks" — a `JpanelScreen` test asserted no
> recorder existed anywhere on the surface. The owner: *"PWA should also be able to actually
> send audio, a voice message, that have the option to send text that gets rendered."*
>
> **The reason is the one `DAD_VOICE` already exists for.** A separate male voice was chosen
> because a message from Dad arriving in the pet's own voice would teach a four-year-old that
> the robot and their father are the same thing. A synthesised voice reading a father's words
> is a weaker answer to the same problem than his actual voice, and for a child who cannot
> read, the recording is the ONLY version that carries who it is from. Typing stays, because a
> parent in an open-plan office cannot always speak — it is now one of two options rather than
> the only one.
>
> **It needed no firmware change and no OTA**, which is the contract having been drawn at the
> right seam: `GET /next` hands the panel raw PCM and the panel plays it. Nothing in the
> firmware knows or cares whether that audio came from a microphone, from Kokoro, or from a
> phone in an office.
>
> **The browser converts, not the box.** A `MediaRecorder` blob is webm/opus in Chrome and
> Firefox and mp4/aac in Safari; decoding that server-side would mean a codec dependency in
> the api container for a job the recording browser can already do. `frontend/src/voiceMessage.ts`
> resamples to the same 16 kHz mono s16 a panel uploads, so ONE audio format crosses this
> boundary and the owner's voice takes the identical path through the box as a child's — same
> trim, same transcription, same blob store. It avoids `OfflineAudioContext` deliberately:
> Safari has historically refused sample rates below 44.1 kHz there, and Safari on a phone is
> exactly where the owner is.
>
> **The transcript of a recording may be wrong**, unlike the typed path where the text IS what
> was said and is kept verbatim. Acceptable for the same reason it is on the panel's side: the
> audio is the message and the text is a convenience.

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

**A PANEL THAT CANNOT ACKNOWLEDGE IS BOUNDED BY THE BOX** (migration 0209, `deliveries`).
`GET /next` counts how many times it has handed a message over and stops offering one collected
`JPANEL_MAX_DELIVERIES` (5) times without ever being acknowledged.

This exists because it happened: a firmware bug meant `POST /played` never fired, so the row
stayed unplayed, every poll fetched it again, and a panel repeated the same message in a child's
bedroom every thirty seconds until new firmware could be built (`ROOM_ENDPOINT_PLAN.md`
§10.4cw). Nothing on the box could stop it — the debug SQL surface is read-only and
`DELETE /messages` deliberately preserves exactly that row.

The firmware bug is fixed; **the class of bug never will be**. A crash mid-playback, a dropped
POST, a future regression — every path to "the panel did not acknowledge" ends with the same
audio repeating. That is a property of the BOX, and the box should own it, because the box is
the half that can be fixed without an OTA.

**It does NOT mark the message played**, and that is the whole design. `played_at` means a child
heard it; writing it here would be the box telling the owner a lie about his children, and his
thread would show a message delivered that nobody heard a word of. The row stays unplayed
because it IS unplayed. `deliveries` goes out on the wire instead and the PWA says
**"Couldn't be delivered"** — because unplayed-and-waiting and unplayed-and-abandoned are
identical in `played_at`, and only one of them means something is wrong.

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

**MULTIPLE MESSAGES ARE ONE PRESS, and a finger gets out.** The owner: *"when multiple messages
stack up it doesn't have a good way to show them."* One pop-up per message meant five messages
were five pop-ups and five taps — tedious, and indistinguishable from the panel repeating itself
even when it was working.

A tap now plays **everything waiting, oldest first**. "Press once, hear everything" is how a
four-year-old thinks, and the escape is the gesture this panel already has in two other places:
**a finger cancels**. Touch during a run and it stops — the third use of the same rule rather
than something new to teach. A bar along the bottom says how many are left and that a touch
stops it, because a run whose end you cannot see needs a way out you can see.

Each message is **acknowledged as it plays**, one at a time, so stopping halfway leaves the rest
genuinely unheard and the pop-up comes back for them. The pop-up itself now names the count
(`SENT YOU 4` / `TAP FOR ALL`) rather than saying "some": the number is what tells a child
whether one press costs them ten seconds or a minute.

Deliberately NOT announcing each sender between messages — the voices are recognisable, and a
child sitting through four messages wants them, not an index.

**The pop-up stands down after 15 s** to a small badge in the top-left, carrying the sender's
name and the same tap target. Big is right while it must interrupt; big for an hour holds a
child's toy hostage over a message nobody has come to. The clock runs from when the WAIT began,
not from the last count change, or a second message would restore the big box on a child who
has already declined the first.

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

### Panel-facing — `/api/jpanel/*`, `Authorization: Bearer <device_key>`

> **CORRECTED 2026-09-23, and the correction cost a bug.** This section said
> `/api/endpoint/jpanel/*` — the device surface, beside `/endpoint/converse` — on the argument
> that panel routes belong with the other panel routes. **W2 did not build it that way**: it
> mounted one `/jpanel` router carrying both the panel's four and the owner's four, so the
> panel's live paths are `/api/jpanel/*`. Nothing noticed, because the section above says this
> contract is the source of truth and W3's firmware was written from it — so the panel asked
> for `/api/endpoint/jpanel/waiting`, got a 404, and **a 404 there is indistinguishable from
> "nobody sent me anything"**. The feature would have shipped looking merely quiet.
>
> Corrected to what is deployed rather than the reverse: the backend is live and the PWA
> already calls it, and moving production routes to match a document buys nothing a rename
> would not cost twice. Auth is per-route (`PanelDep` vs `OwnerDep`), so sharing a prefix is
> not a hole — but it does mean the device surface is no longer one prefix, which is the thing
> to weigh if a future wall ever gates by path.
>
> Pinned now from the end that can run: `test_the_panel_facing_routes_are_where_the_firmware_looks`
> in `backend/tests/unit/test_jpanel_api.py` reads the URL out of `firmware/main/jpanel.c` and
> fails if either side moves. Nothing on the host can check a URL the firmware builds, which is
> why this went unseen through a clean build, a green host suite and a byte-compared image.

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
| `POST /messages/audio?to_device=` | the raw 16 kHz mono s16 body, exactly as the panel's `/send` takes it | `201 Message` — Dad's own voice; transcribed on the way in, `composed: "voice"`; `404` for an unknown panel |
| `POST /messages/{id}/played` | — | `204` — the owner has dealt with it; idempotent, keeps the first timestamp |
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

**`unplayed` counts only what a panel sent to the OWNER, and is counted independently of
`limit`.** Two bugs live in the obvious implementation, and the PWA found the first by being
built against this: counting the fetched rows deflates the badge as soon as `limit` truncates,
and counting everything a panel sent includes twin-to-twin post, which is not the owner's to
clear and would leave a badge he cannot make go away. `limit` bounds rows across all panels.

**Every enrolled panel is listed, including one that has never sent anything.** Otherwise the
owner could not message a twin who has not yet spoken into her panel — exactly the child he
would most want to reach.

**Clearing the badge is an explicit call, not a side effect of fetching the audio.** The
tempting fix is to stamp `played_at` in `GET .../audio`, and it is wrong for the same reason the
next paragraph gives: the expected interaction is READING. A father who reads the transcript and
never presses play would leave the count sitting there forever.

**IT IS A CONVERSATION WINDOW, and the fix for a long one is a scroll cap, not forgetting.**
This briefly showed only the newest outbound message, on a misreading of *"we shouldn't just
keep on piling up message after message."* The owner corrected it: *"This is a conversation
window. It needs to limit the max height of each panel conversation and scroll is larger. And
add a 'clear history' button per panel."* Throwing away what was said is the wrong answer to a
list that is too tall.

So each thread caps its own height and scrolls inside — which keeps every panel's compose row
on screen at once, the layout a parent with two children actually needs, rather than one column
metres long with the box you came to use below the fold. Each row still carries its status
(`Heard 4m ago` / `Not heard yet`) on what the owner sent, because that is the only live
question about something he already knows he said.

**`DELETE /messages?device=` clears one panel's conversation, EXCEPT a message a child has not
heard yet.** §5 says a message nobody heard must not evaporate, and a row addressed to a panel
with `played_at IS NULL` is sitting on a bedroom wall waiting for a four-year-old to come back
to it — the owner tidying his own view is not a decision about her post. A panel's unread
message to the OWNER is a different thing and goes: that is his own badge and clearing is
exactly the call he is making. The route returns how many it kept so the PWA can **say so**; a
clear that silently leaves rows behind is worse than one that refuses, because the whole point
of the button is that the list afterwards matches what he expects.

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

- **The movement threshold has never met a bedside table.** `SCREEN_MOVE_COUNTS` is 900 raw
  counts summed over three axes, sample to sample — about 0.11 g, chosen to sit far above the
  tens of counts a resting panel jitters by and far below a hand lifting it. That is reasoning,
  not measurement. It is correctable without a terminal: every wake it causes logs
  `screen: movement N counts (threshold 900)`, and a panel waking itself on an empty table will
  say so with the number that justifies raising it. Two nights of logs decide it — and the part
  is noisy enough at rest that §10.4af spent three releases on exactly that noise, which is why
  this is measured rather than argued.

- ~~**Nothing checked that a message arrived whole**~~ — **VERIFIED BOTH WAYS (0.2.97).**

  The upload is a chunked write over a radio in a bedroom. A stalled write the panel already
  caught; a connection that ended cleanly two thirds of the way through a sentence it did not —
  and from the box that is indistinguishable from a child who stopped talking. The fragment was
  stored, transcribed, and she was told her message went.

  The panel now hashes the recording before it sends (`X-Jpanel-Sha256`, SHA-256 in the S3's
  hardware, `mbedtls` already linked for the CA bundle) and the box compares it against what
  arrived. A mismatch is a **422**, which is the one status the panel retries — its capture
  buffer still holds the good copy, which is precisely why the recording is not streamed
  straight off the microphone. Two failures in a row is reported rather than retried forever.

  **And the same proof on the way down**, which streaming made necessary: the panel discards a
  message as it plays it, so a short download is a message that stops mid-sentence — and
  acknowledging that would RETIRE it, because `/next` never offers a played message again. The
  box sends the digest with the audio, the panel hashes as it streams, and what it cannot
  verify it simply does not acknowledge: the row stays unplayed, the pop-up comes back, and the
  delivery cap turns a repeated failure into the box giving up loudly rather than a message
  quietly lost.

  **An unverified upload is still accepted**, with a log line, because a panel mid-fleet-upgrade
  sends no header and refusing it would take voice post away from a unit to fix a fault it does
  not have. The header name is pinned at both ends by a test, since a misspelt one fails
  silently — it simply looks like every panel being old.

  **Oversize is now a 413 rather than a truncation.** Keeping the first N bytes was the same
  fault the hash exists to catch, committed on purpose.

- ~~**The message length cap**~~ — **GONE (0.2.96); the recording cap is thirty (0.2.95).**

  Playback no longer has a ceiling at all. The panel streams a message through a four-second
  ring in `audio.c` — the network writes into it as bytes arrive, the codec drains it — so a
  message is bounded by what the box will store rather than by this board's PSRAM. The ring's
  arithmetic is `ring.c`, its own file and host-tested for the reason `orient.c` and `screen.c`
  are: a wrap off by one plays a fragment of an earlier second in the middle of a child's
  message, and neither that nor a full-versus-empty mistake shows up as a crash.

  **It gave memory back rather than costing it.** The 960 KB inbound buffer and the 960 KB play
  buffer are both gone; what replaces them is a 128 KB ring, and the play buffer shrinks to the
  reply cap it always described. **And it made the tap faster**, which is why the prefetch could
  go: the first sound needs only the 1.5 s preroll (~48 KB) rather than a whole message, so a
  panel that used to fetch ahead and hold the bytes now holds nothing and answers sooner. The
  owner's *"couple of seconds between me acknowledging the message and it starting to play"* is
  removed at its source rather than worked around.

  Three things had to move with it, and each is the kind of thing that fails quietly:
  **`audio_playing()` now covers a stream**, including while the ring is momentarily dry — the
  render loop, the pop-up, the queue and `POST /played` all ask that one question, and a panel
  that answered "finished" during a Wi-Fi stall would mark a message played mid-sentence and
  drop the rest. **The producer checks `audio_stream_live()` every pass**, because a full ring
  and a stopped stream both refuse bytes: a writer that could not tell them apart would spin
  forever on the task that also polls, sends and acknowledges — a child tapping to stop a long
  message is exactly how that would have been found. And **"again" re-asks the box**
  (`GET /message/{id}/pcm`), since the bytes are gone as they play; that route does not spend a
  delivery attempt, or listening twice would become a way to lose a message.

- ~~**The recording cap is ten seconds**~~ — **THIRTY (0.2.95), at the owner's ask.**
  Ten was reasoned rather than measured: "ten seconds of a four-year-old is a long message",
  with a note to revisit it if the twins hit the ceiling and a `full` branch that logs when
  they do. Nobody waited for that evidence — the ask came first, and the only real cost was
  memory. Four numbers had to move together or a thirty-second message would be cut at
  whichever stayed lowest: `CAPTURE_MAX_MS` and `PLAY_BUF_MS` in `audio.c`, `JPANEL_MAX_BYTES`
  in `jpanel.c`, and `MAX_MESSAGE_MS` on the box (which the PWA recorder reads, pinned by a
  test at each end). **That figure was briefly wrong here**: it said 2.8 MB, which was the total
  while the play buffer was also thirty seconds. Streaming (0.2.96) removed the inbound buffer
  and cut playback back to the ten-second reply it actually holds, so the three come to about
  **1.4 MB** of the board's 8 MB — 960 KB of capture, 320 KB of playback, 128 KB of ring —
  beside a 322 KB framebuffer. `free_psram` in telemetry is the number to watch rather than any
  arithmetic written here, which is the lesson of having got it wrong.

  **A maxed-out TYPED message is still cut, and that gap is now the open one.** `SendText`
  allows 600 characters, which through Kokoro runs nearer fifty seconds — and `send_text`
  does not cap what it stores, so the panel truncates on fetch and `audio_play` says so in its
  log. Thirty narrows the gap; closing it means bringing the text cap and the audio cap into
  line, which is a decision about how long a message to a four-year-old should be rather than
  a memory question.
- ~~**A panel cannot learn the other panel's name**~~ — **CLOSED (0.2.93).**

  The gap was real and the placeholder was honest: a panel is flashed with its OWN name, the
  box mints the other one's at the OTHER unit's flash, and no route answered "what is my twin
  called", so the blue recording indicator said `TO DAD` or, for the sibling, `MESSAGE`.
  Inventing a word for a child's twin would have been worse.

  `GET /waiting` now carries `sibling`, so it says `TO ELORA`. It rides the poll the panel was
  already making rather than adding a route, and the box answers only where there is EXACTLY
  ONE other panel — the same rule `send(to="panel")` follows, because with two siblings "the
  other one" is a question rather than a name and a guess puts the wrong child on the glass.
  `MESSAGE` remains for that case, for a single-panel box, and for the moments before the
  first poll. The caption ticker still shows the phrase they just said in the same frame, so
  the recipient was on the glass either way; this makes it the name.

- ~~**Retention.**~~ **BUILT.** `jbrain/jpanel/sweep.py`, a lifespan loop beside the
  guided-intake reaper, every six hours. Played messages go 30 days after they were **played**
  — not after they were sent, so a year-old message the owner listened to this morning is a
  message from this morning as far as retention is concerned. Unplayed rows are not swept at
  any age, which is the half that mattered: a message nobody has heard is a four-year-old's
  words waiting on a wall, and `played_at IS NULL` is exactly that set. Thirty days is still
  the proposal rather than a measured number, so it is one named constant.

  **The audio is not the row's to delete.** Rows are deleted and committed first, and only
  then is each digest offered for collection through `blob_referenced` — a message whose file
  is also an unheard message's, or the owner's chat attachment of the same clip, keeps its
  file. Which is how this turned up a live fault: **`app.jpanel_message.blob_sha256` was never
  registered in `BLOB_REFERENCES`**, though migration 0208 added it and `blob_refs.py` says in
  its own header that a blob column joins that list in the same PR. Nothing had gone wrong
  yet, because nothing had deleted a digest these rows share — but a delete anywhere else on
  the box would have unlinked a child's voice message, with a 200 on the delete and a 500 when
  she pressed play. Registered now, and the sweep's test fails when the entry is removed.

  That module also claimed an omission of this kind "cannot be tested from the other side,
  because the omission is an absence". It can, from *this* side: ask the schema which columns
  look like digests and require each to be registered or explicitly named as something else.
  `test_sdr_recordings_rls.py` does, so the next table added and forgotten fails CI rather
  than the owner's disk.
- **More than two panels.** The refusal rule above is safe but unhelpful; addressing by name
  needs the twins' names in the offline vocabulary, which the owner has deferred.
- **Panel-to-panel post could never have worked, for two reasons found on the live box**
  (2026-09-23, both fixed, both with tests that fail when reverted):

  1. **RLS.** `principals_select` opens for the owner, for `auth_ctx()` in
     ('login','bootstrap'), and for a principal reading ITS OWN ROW. `send` resolved "the
     other panel" inside a session scoped to the *asking panel*, so the roster it read held
     exactly one row — itself — `others` was always empty and **every sibling message answered
     409, on any box, from the first commit**. Two more symptoms shared the cause: the pop-up
     never learned who a message was from, and `GET /next`'s `X-Jpanel-From` always said "the
     other one".

     Fixed by reading the roster under the narrow `login` context in a session of its own, NOT
     by widening the policy. That ordering is the security posture: a panel must not be able
     to enumerate principals — it says "the other panel" and the box decides. Widening
     `principals_select` would hand a device on a bedroom wall the whole principal table,
     `key_hash` included, to answer a question it should never have been asking.

  2. **Every `/flash` mints a key and nothing retires the old one.** The live box carried
     **thirteen** unrevoked principals labelled `panel Elora` — one physical panel, re-flashed
     — plus two unnamed, so the roster held fifteen candidates where `send` needs exactly one.
     The roster now keeps one row per NAME, newest `created_at` winning, which is right rather
     than merely tidy: `/flash` rewrites the unit's NVS, so the newest key for a name is the
     one that panel is using and every older one is dead by construction. No liveness signal
     is needed, which is why this did not wait on one.

     `send` also excludes by NAME rather than by id, because a panel still running a
     superseded key is not in the roster under its own id — filtering on `pid != principal.id`
     would leave its own name in the list and post the child's message back to the unit they
     spoke into.

  **The residual risk is a dead letter, and it is the one §5 has always described.** RLS
  delivers on `recipient_device = app.principal_id`, so a message is readable only by the
  exact key it was addressed to. Newest-key-per-name is right whenever the last `/flash`
  reached the panel; a flash that minted a key and then failed leaves a newest key no unit
  holds, and messages to that name go nowhere. Not a leak — the panel simply never sees a
  pop-up — and not fixable without knowing which key is LIVE, which nothing records:
  `principals.last_used_at` is never written and RLS forbids any panel- or login-context
  write to that table (`principals_update` needs owner or bootstrap). That is the panel
  roster this section keeps asking for, and it is the next thing to build if a message ever
  goes missing.

  **The cost is naming.** Two physical panels flashed with the SAME name collapse to one row
  and one twin becomes unreachable. Not a regression — a child saying "send a message" could
  not have picked between two panels called Elora either — but it is now the one thing that
  breaks addressing, so the collapse is logged.

- **A panel can be NAMED from the PWA now, which is not the same as the roster being a
  mechanism.** `POST /api/jpanel/panels/{id}/name` writes the `panel <name>` label the
  convention below turns on, so a unit enrolled without a name no longer needs a cable to stop
  being "the other one" — which was a re-flash over USB, i.e. a terminal, for a fault the owner
  can see from his phone (CLAUDE.md #10). Two things about it are worth keeping in mind:

  1. **It moves every unrevoked key under the old label, not the one addressed.** The label IS
     the identity while `_panel_names` collapses with `DISTINCT ON (label)`; renaming one key
     would leave the superseded ones under the old name and grow a second, unreachable panel in
     the roster. Asserted against real Postgres in `test_jpanel_rename_pg.py`, which fails when
     the `UPDATE` is narrowed to the addressed id.
  2. **The name is constrained by the PANEL'S FONT, two packages away.** `font.c` has 5x7 cells
     for A-Z, the digits, space, hyphen and full stop and nothing else, and a character it does
     not have draws as *nothing* — so "O'Brien" would reach a four-year-old as a pop-up from
     someone missing a letter. The route refuses those at the door and the cap (14) is the
     arithmetic of `draw_popup`'s bubble at its shrunk scale. Both ends are read out of the
     firmware by unit tests rather than transcribed.

  It does not make the convention a mechanism, and the bullet below still stands.
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
