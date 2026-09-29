# jpanel — the panels as a product: voice post, and a screen that sleeps

> **Status:** In progress · **Last verified:** 2026-09-29 · **Waves:** W1◻️ W2✅ W3✅ W4✅
> — W2 and W4 shipped together in #1498; W3 shipped across #1504/#1508/#1511/#1513 and is
> **confirmed working on a panel** (voice post both ways, the pop-up, the queue).
>
> **W1's code has landed** — firmware 0.2.92, `screen.{c,h}` plus the render loop — and stays
> open rather than ticked because the wave is not done until it has run on hardware: the
> panel is away until tonight, the movement threshold it ships with is reasoned rather than
> measured, and §5 says how to correct it from the box without a terminal.
>
> **The Panels tab has had its design pass.** It shipped ahead of one and read it: the owner,
> *"the panels sub view looks considerably less polished."* Three directions were mocked
> (`docs/mocks/jpanel-panels/`) and **A — converge on the Ops fleet card** was chosen, since
> both surfaces answer "is this thing as it should be" and the owner reads them minutes apart.
>
> Most of what made it read as unfinished was not taste. **`--text-dim` did not exist**: it was
> referenced seventeen times, across this sheet and the Ops fleet card, and declared nowhere, so
> every line written to recede rendered at full `--text`. A `var()` at an undefined name is not
> an error — it resolves to nothing, and `color:` then inherits while `color-mix(…)` drops the
> declaration whole — which is why nothing caught it. `cssTokens.test.ts` catches it now, and
> catching it turned up five more of the same shape elsewhere in `styles.css`.
>
> **`.jp-panel`, `.jp-panel-head` and `.jp-panel-name` were each declared twice**, and the
> Messages tab uses the first set — so the Panels block had been quietly restyling the message
> threads. The Panels tab is `.jp-unit*` now, and no top-level selector in the sheet may repeat.
> Beyond that: a 16px gutter, 44px action buttons with Revoke rose from its resting state, the
> type scale (every size in the half was a bare rem, all below every token in the scale), and
> the health said in WORDS rather than by fading the card to 75% opacity. Four state bugs went
> with it — a revoke that armed and never disarmed, a permanent "Saved.", a relative timestamp
> computed once at mount, and a knobs card that rendered `null` while loading.
>
> **"Dim" is now a number the owner can turn** (0.3.04): the bird slept on schedule but the
> screen still looked lit — `screen_level()` dimmed to a hardcoded QUARTER, and a quarter of 255
> is 63, which does not read as dim in a bedroom because a quarter of a register is nowhere near
> a quarter of perceived brightness. It is a percentage from the box now (25 reproduces the old
> behaviour), with brightness and dim sliders beside the sound knobs.
>
> **The microphone can turn itself up** (0.3.03): the ES8311's own AGC had never been on — the
> driver never writes REG18, so it sat at the chip's reset default and telemetry reported
> `00 already-off` on every panel. A fixed gain cannot serve two units whose last readings were
> a clipping 32767 and a near-silent 814. It is a SETTING rather than a rebuild, and the shared
> panel knobs (volume, microphone gain, AGC) now have a PWA control at all — until this they
> were reachable only from the debug console, which needs a token the owner has to be handed.
>
> **The sleep timer no longer counts voice, and the pet sleeps rather than merely dimming**
> (0.3.02): ambient speech held the screen lit — a parent in the hall, a sibling, a television —
> which is the opposite of what a bedroom timer is for. Only a touch or real movement resets it
> now; the pet ANSWERING still holds the screen, because a question answered to a dimming screen
> reads as broken. On entering dim the pet shuts its eyes and drifts a zzz.
>
> **§5's open roster item is closed** (migration 0211): a device now records whether it is a
> phone, one of the twins' pets, or a display the owner operates, and panels are managed on the
> jpanel screen's **Panels** tab — rename, pet name, body and revoke — rather than on the
> Location screen's phone list. `vocab.c`'s own open note is closed with it (migration 0212,
> firmware 0.3.00): the wake word is a per-panel setting, so the pet can be renamed without a
> cable, and the chosen body survives a reboot.

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
The PWA surface is tabbed: **Messages**, **Panels** and **Flash**, the last being today's
`EndpointsScreen` moved rather than rebuilt (it is already "its own surface, not a card inside
Ops"). One launcher for "the panels in my house" beats several that each do half.

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
render loop.

> **And the brightness half of that does not happen, measured 2026-09-27.** The runtime `0x51`
> write is unframed and the controller discards it (`../reference/PANEL_COMMANDS.md`), so dim and
> dark both reach the panel as *stop blitting* alone: a static last frame at full brightness, not
> a dark screen. That is the owner's own observation — *"even when it times out and the robot
> sleeps, the display never changes brightness"* — and it is why this section read as working for
> weeks. Framing the write fixed it and hung the render task, so the framing is backed out and
> this stays a half-feature deliberately. The animation stopping is real; the darkness is not. On an AMOLED a black frame at zero brightness is genuinely dark and genuinely
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
and a sender name — **and, since 0.3.33, one name and sender-kind per queued message**, because a
child can now swipe between them and a face that is per-message cannot come from a field that is
per-queue. Thirty seconds was also the WHOLE latency budget in practice, because both of the fast
paths that were supposed to make it a backstop failed on a freshly-woken panel; `GET
/endpoint/settings` carries the count every three seconds now. See `ROOM_ENDPOINT_PLAN.md`
"The queue stops being a number (0.3.33)". A waiting message draws a **pop-up over the face** — big, tappable anywhere
inside, naming who it is from — and wakes the screen if it is asleep. It does not auto-play; an
unplayed message survives a reboot because the state lives on the box.

**WHAT THE CHILDREN SAY TO THE PET IS A RECORD NOW, NOT A LOG LINE (0.3.33).** The owner: *"I
think we need some way of logging [what] the kids say to the large language model ... another tab
in the jpanel side that is llm conversations that are stored that I can clear and read through
sorted by panel."*

The words already existed — every turn writes an `endpoint.converse` line carrying `heard` and
`reply` — truncated to 120 characters, interleaved with every other event on the box, rotated away
on a schedule nobody chose for this, and reachable only by reading an access log. The most
interesting thing this box produces was a debug field.

Migration **0219 `pet_turn`** and `GET`/`DELETE /api/jpanel/chats`, grouped by panel because the
question is about a child and the panel is how this box names one. It shipped as a fourth tab
beside Messages/Panels/Flash and that tab is gone again — see "One child, one conversation"
below, which folded it into the thread rather than leaving half of a child's afternoon in each
of two places.

**THE PANEL MAY WRITE THESE AND MAY NEVER READ THEM**, which is the whole of the policy. For
messages there is a delivery reason to let a panel read a row addressed to it; here there is none —
the pet's memory of a conversation is four minutes of process RAM in the API, deliberately not this
table. So a panel cannot read back its sibling's transcripts, and cannot read back its own, and the
insert policy pins `device_id` to its own principal because an impersonated row here is not a
forged message but a forged account of what a child said. Six tests assert it against real
Postgres, self and sibling separately — a later `device_id = principal_id` policy, the
plausible-sounding change, passes one and breaks the other.

**No audio.** Storing text is a diary; storing every clip is a wire in a child's bedroom. **Kept
until the owner clears it**, with no expiry and no sweep, because a transcript that vanishes on a
timer is not a record a parent can rely on — and the Clear button asks before it destroys anything.

**ONE CHILD, ONE CONVERSATION (PWA).** The owner: *"I want there to be a separate selection
underneath the top ... that'll be the panel's names. So if I select lydian or Elora up there it
should show those two as conversations ... kind of like a normal conversation does with jerv or
the other ones in the pwa where there's an omnibox at the bottom and a left and right conversation
bubble."*

Two things were wrong with the shape this replaced, and they were the same thing twice. Messages
stacked every panel down one page, each with its own list and its own composer — which gets worse
with each panel added and reads as a report rather than a conversation. And the pet transcript sat
on a tab of its own, holding the OTHER half of the same child's afternoon, with no way to read
either in the order things happened. A child does not experience those as two things: she asks the
pet why fish sleep and then records something for her father about it.

So the tab bar is **Messages / Panels / Flash** again, a **panel-name picker** sits under it, and
the thread is everything that happened on that panel, **merged by time** (`jpanelThread.ts`): a
message lands between the question she asked the pet and the answer it gave her, because that is
where it happened. `pet_turn` is written once the reply has been MADE, so its timestamp is the
answer's and her question is placed `total_ms` earlier — without which the interleaving is a
plausible-looking lie.

**Left is the panel, right is the owner**, and the pet's replies are on the left as well: the pet
is not him, it is the other voice in her room, and a machine's answer sitting where his own words
go would read as something he said. Her words to the pet carry a `to the pet` label for the same
reason — unlabelled, they are her words arriving in his conversation and a parent scrolling
quickly would read them as addressed to him.

**Two clears, not one**, although they now clear two halves of one visible thread: the messages
are post between two people and the box refuses to destroy one a child has not heard, while the
pet transcript is a record of what she said to a machine. One button would mean one press
destroying both.

The unplayed count rides the **picker chip**, because "who is waiting on me?" has to be answerable
without opening either child. A panel that has only ever talked to its pet still gets a chip: the
picker is the union of the two routes, and one built from message threads alone would leave such a
child off this screen entirely.

**A REPLY THAT DOES NOT GO NOW SAYS SO (0.3.38).** The owner: *"sometimes when we're in the menu
for playback and they hit the green reply button and record a message, it doesn't actually get sent
and doesn't show up in my inbox on the pwa."*

**The box could not see it at all.** Every `POST /jpanel/send` that reached it in the reported
window returned 200 and produced a `jpanel.sent` event — the failures are exactly the ones that
never arrived. `do_send_once` fails six ways and logged all of them to a serial console that does
not exist in a bedroom (CLAUDE.md #10). Each branch now names itself and the send path reports as
`REACH_SEND`, so the reason survives the outage and lands on `GET /api/debug/endpoint/reach` with
the others. **`JPANEL_NOBODY` is not a fault** — the box answered; there is simply no second panel
to address — and counting it would bury the failures that LOSE a message under a condition a
one-panel house produces on every attempt. Reasoning in `ROOM_ENDPOINT_PLAN.md`, "The two failures
that left no record at all (0.3.38)".

**PRESS THE SENDER'S FACE TO CHANGE MESSAGE (0.3.37).** The owner: *"instead of swiping if we
just press the icon on the top left it should cycle through the numbers of messages that we have"*,
and on where: *"it should be the top left icon after that playback menu is up"*.

**A swipe is the wrong gesture for this audience** — which is why `swipes` and `swipe_dx` ride
telemetry at all: a four-year-old jabs. The face is the one thing on this screen that already
answers "which message is this?", so pressing it to change the answer explains itself, and a press
is the only gesture on this panel that has never needed teaching. Same effect as the swipe and
through the same code (`select_step`): stop what is sounding, drop a deferred play aimed at the
message she left, sound the acknowledgement.

**ONE PRESS PER MENU, AND THE REST IS AN OPEN QUESTION.** Stopping takes the playback menu down,
and the face is only offered while that menu is up — so cycling twice needs the face live in the
again/reply state, where **its rectangle is the waiting badge's** and the badge already means
"press to hear this". Two opposite meanings on one pixel, and the badge is already the selector's
display (it shows the *selected* message's sender). That collision is the owner's to settle; it is
stated here rather than resolved by a guess.

**AND PRESSING REPLAY TWICE NO LONGER PAUSES WHAT NEVER STARTED (0.3.36).** The owner: *"it
played through once and has stopped and has the play button again, but when we click the play
button sometimes it just pauses ... usually just on the first time."*

**What the finger is on changes under it.** When a message ends the pair comes up and the
arbitration uses the IDLE table, where that left disc is `UI_TARGET_PAIR` — replay. Pressing it
arms `PEND_REPLAY`, which makes `run_controls_up()` true, which swaps the table to
`UI_TAP_ORDER_PLAYING` — where the **same disc in the same place** is now the TRANSPORT. A replay
waits out its own cue before any sound, so for those few hundred milliseconds nothing has happened;
a child presses again and the second press pauses a message that never started. *"Usually just the
first time"* is the press that flips the table.

The press is **claimed and ignored**, not refused: a transport that stopped being live would let
the press fall through to the pet, which is a poke nobody asked for. Bounded by the **pending**
rather than a clock — `EXIT_GRACE_MS` was tried first and 700 ms of dead pause button is a real
cost to a child who wants the sound to stop the moment it starts, and it broke
`test_a_held_stream_resumes_rather_than_being_thrown_away` for exactly that reason. While a play is
pending there is nothing sounding to pause, so refusing costs nothing and needs no number.

**A LONG PRESS ON A MENU IS JUST A PRESS (0.3.35).** The owner: *"in the menu we need to disable
the long press and have long press treated as a normal press of menu items. I think this is the
cause of the girl icon showing up on the top left."*

He is right, and the reason is `TALK_MARGIN_PX`. "On the pet" — the region where a hold opens the
send-to grid — is everything more than 72 px from an edge, **a 224×224 square in the middle of a
368×368 face**, which is exactly where the notice, the again/reply pair and the grid's own icons
are drawn. So a press on a menu item was **both**: the target fired on the down edge, and the same
unmoved finger opened the grid over the top of it 700 ms later. A child holding the reply button
armed a reply **and** opened a "who do you want to send to?" menu, and both of those put a person's
face on the glass.

`stream_active` and `sendto_until` were already excluded, which is why this only ever showed on the
two menus that can be up while nothing is playing. `ui_menu_up` is now the whole set, so a fifth
overlay cannot reintroduce it. **The gesture still works on the bare pet**, which is where a child
reaching for "I want to send something" starts — asserted explicitly, because suppressing it
everywhere would be a worse bug than the one being fixed.

**AND SO DOES A REPLAY — THE SAME BUG, THE SECOND CALL SITE.** The owner, a release later:
*"the replay seems to sometimes not work where I hit it and it just kind of goes to a pause button
for a second and then stops and other times it plays."* `do_fetch` was fixed and `do_replay` was
not, because the fix was written where the failure had been **seen** rather than everywhere the
mechanism applies. One `stream_begin_waiting` helper now owns the retry, and the test walks every
`audio_stream_begin` call site — the first version asserted the wait existed *somewhere* in the
file, which it did, in the one function that had it.

**AND A REPLAY NOW REACHES THE STATE THAT ENDS A PLAYBACK.** `case JPANEL_PLAYING` is the only
place that notices a message has finished: it clears the state and re-arms `s_repeat_until`, which
is what puts the "again" and "reply" pair back on the glass. `do_fetch` set it; `do_replay` did
not. A replay was audible and then simply over — the pair kept counting down from the end of the
**first** play, so a replay longer than what was left of that window took the buttons away
mid-sentence and a child who wanted to hear it once more had nothing to press. The two states
replay deliberately does not join (`s_owed`, `s_run`) are argued in its own comment; this one was
not on that list, it was missed.

**A MESSAGE WAITS FOR THE SPEAKER RATHER THAN BEING DROPPED (0.3.33).** The owner: *"[it looks]
like it's going to play and only stays about one second before it disappears again ... Seems that
sometime if I long press on the notification it seems to work a little bit better. Like maybe the
initial click isn't passing to the correct place unless I'm holding the button longer."*

A press on the notice plays `CUE_PLAY` first, so the finger gets an answer before the message
arrives, and `audio_stream_begin` refuses while anything is on the speaker. The renderer defers the
fetch until the cue is done — but the fetch then crosses a task boundary, and a cue starting in
that window takes the speaker back. Whether one does depends on what the finger did next, which is
exactly why holding behaved differently from tapping. **A child's message must not depend on how
long they press.** The old answer was `goto done`: no stream, one log line to a console that does
not exist in a bedroom, and a menu that vanished a second after it appeared. It waits out the cue
now, bounded, and reports the longest wait — because "it works" and "it works BECAUSE we wait" are
different facts.

**AN OPEN MICROPHONE IS DEAF TO COMMANDS, AND A CUE NO LONGER EATS THE RECORDING (0.3.33).**
The owner: *"there are still occasional times when we are talking and recording a message that
commands get recognized and sound effects come through."* Two faults behind one symptom.

The mute covered `TALK_RECORDING` — a message to a sibling — and not `TALK_LISTENING`, the pet
conversation, so a child telling the robot about her day and using one of the nineteen action
words got the action fired mid-sentence while the same words went to the box. One utterance, two
readers, neither told about the other. "Occasional" is the shape of a vocabulary collision: it
needs the sentence to contain one of the words, which is why it survived deliberate testing.

And the owner's own hypothesis — *"the sound effects prohibit the microphone from properly
recording during that time since they shared the same SPI or whatever?"* — was right about the
effect. Not a shared bus: the codec routes its DAC into its ADC by design, so the panel genuinely
hears its own cues, and `s_deaf` is what stops it answering its own beep. The defect was that the
deaf path `continue`d past the capture copy, so those samples were **deleted and the ends spliced**
rather than silenced. `s_deaf` re-arms on every written chunk, so a 300 ms cue cost about 540 ms
out of the middle of a recording with the join inaudible — a child saying "I went to the park
today" came back shorter than she spoke, and the transcript read as though she had said the
shorter thing. It writes silence now: what was said during the cue is lost either way, but a gap
transcribes as a pause instead of inventing a sentence.

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
genuinely unheard and the pop-up comes back for them. The pop-up itself now names the count rather
than saying "some": the number is what tells a child whether one press costs them ten seconds or a
minute.

**AND THE COUNT BECAME A POSITION IN 0.3.33.** `SENT YOU 4` was right while the oldest was the only
thing a press could reach; once a finger can point at one of them, a bare total says nothing about
WHICH — and a child looking at the sister's face under "SENT YOU 2" has no way to tell that the 2 is
not about her. The line reads `2/4` now, the numeral between the two playback discs says the same,
and the pop-up's action line says `- SWIPE -`. One press still plays the chosen message and walks on
through the rest; the swipe is the addition, not a replacement.

Deliberately NOT announcing each sender between messages — the voices are recognisable, and a
child sitting through four messages wants them, not an index. The FACE answers that instead, which
is the half a swipe needs: it is the one thing on the glass that changes as the finger moves.

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

Three tabs:

- **Messages** — **one child at a time**, picked by name under the tabs, as one conversation
  read downwards: her messages and his, and what she said to the pet, merged by time. An unplayed
  count on each picker chip, because that is the question being asked at work. Each message shows
  sender, time, duration, and **the transcript as the primary content**, with a play button beside
  it: the text is what gets read, the audio is the fallback for when the transcript does not make
  sense — which, given how the transcriber handles four-year-olds, it often will not. One composer
  pinned at the bottom: type, send; TTS speaks it, the audio is stored, and the typed text is kept
  as the transcript so both ends agree about what was said. Reasoning in "One child, one
  conversation" above.
- **Panels** — the units themselves, one row per unit (the fleet route already collapses a
  panel's flashes, so this reads that rather than deriving it a second time): name, role,
  firmware, last seen, and what the owner can do to one — **rename**, **its pet** (the
  creature's name and which body it wears) and **revoke**.

  **The panel's name and the pet's name are different things**, and the buttons say so. The
  panel's is which unit this is: the heading on the thread, what a sibling's pop-up reads out.
  The pet's IS THE WAKE WORD — `vocab.c` builds its listen phrase as `hey <name>` and the label
  above the creature's head is the last word of it — so renaming the pet changes what a
  four-year-old says to the thing on her wall. That file asked for this the day it was written:
  *"a name only a rebuild can change is a name they cannot change, and the two panels will want
  different ones."* A rebuild is a cable.

  **The body is a DEFAULT, not a lock.** Four taps and a hold still swaps it on the glass; this
  is what the panel comes back as, which until `endpoint_panel` existed was always the ostrich
  because nothing wrote the choice down — so every reboot and every OTA quietly undid a child
  who had chosen the robot. The panel may read that row and not write it, which is what keeps
  the gesture from promoting itself into a setting nobody made on purpose.
  Added late, and the reason is worth keeping: both used to live somewhere else or nowhere.
  Revoke was on the LOCATION screen's phone list, because panels are the same
  `Subject(kind='device')` substrate as an OwnTracks phone — under a swipe rail, among one row
  per flash, beside a status line a panel never produces — and the owner could not find it at
  all: *"I don't see a way to revoke from PWA."* Rename had a route and no UI whatsoever, so a
  unit enrolled without a name answered to "the other one" until somebody re-flashed it over
  USB. The Ops fleet card stays READ-ONLY: it is where a fault is noticed, this is where a panel
  is managed, and two places to revoke would be two places to get it wrong.
- **Flash** — today's `EndpointsScreen`, moved rather than rebuilt. It also carries the one flag
  that decides what a unit IS: a pet, or a display the owner operates.

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

  It does not make the convention a mechanism — see the bullet below, which the third unit
  closed.
- ~~**There is no way to enumerate panels that is a mechanism rather than a convention.**~~
  **CLOSED by migration 0211 (`subjects.device_role`).** The note said it wanted a real marker
  *"the first time a third device key exists in this house"*, and that is exactly when it broke.
  A third unit was flashed, took the unnamed default, and `"room endpoint panel"` matched
  `label LIKE 'panel%' OR label = :unnamed` — so a box on the owner's desk joined two children's
  addressing, `send(to="panel")` saw three candidates where it needs one, and the twins' voice
  messages stopped. The predicted failure was "a dead letter, not a leak"; the actual one was a
  feature that stopped working in two bedrooms, which is worse and was not on the list.

  A device now records what it IS at flash time: `NULL` = a phone, `'jpet'` = one of the twins'
  panels, `'display'` = an endpoint the owner operates (OTA, settings, telemetry, `/converse`)
  that no pet can reach. The roster reads `device_role = 'jpet'`; the fleet view reads
  `device_role IS NOT NULL`; the Location screen's phone list reads `device_role IS NULL` and so
  no longer lists panels at all. A device cannot write the column — `subjects_access` is
  `WITH CHECK (app.is_owner())` — which is what stops a display talking its way into a bedroom.

  Two things fell out of it that the note had not anticipated:

  1. **Revoking a panel was unreachable.** It existed only on the Location screen's Phones tab,
     under a swipe rail, among one row per flash — and the owner, who has no terminal
     (CLAUDE.md #10), could not find it: *"I don't see a way to revoke from PWA."* There is now
     a revoke on the fleet view, which retires every live key for that name rather than the one
     the row was drawn from.
  2. **A re-flash never retired what it replaced**, though `/flash` had claimed in a comment
     since it was written that the old key "should stop working at that moment". Thirteen
     flashes of one panel meant thirteen live keys under one name. It does now.
- **Notifying Dad.** A message to the PWA currently waits to be looked at. Whether it should
  push is a question about a parent's phone, not about the panels.
- **The transcriber mangles small children.** `"tell us a joke"` arrived as `"There is a joke.
  There is a joke."` Every transcript here inherits that, which is exactly why the audio is
  always kept and always playable. Fixing STT for child speech is its own piece of work.
