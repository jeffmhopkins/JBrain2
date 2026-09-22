# jtalk — the twins' voice post, panel to panel and panel to Dad

> **Status:** Proposed · **Last verified:** 2026-09-22 · **Waves:** W1◻ W2◻ W3◻

The owner: *"cross panel or panel to pwa messaging… a little pop-up box would show up if a
message is available to play… this way if I'm at work they send me a message, I can read it as
a text and if it doesn't make sense I can try and listen to it, and then I can send them a
message back as a text and it'll be rendered to them."*

## 1. What it is, and the one word that decides the design

**Asynchronous voice post, not a call.** A message is recorded, stored, and waits until the
recipient chooses to play it. Nothing rings, nothing interrupts, nothing is missed by not being
there. That is why it is `jtalk` and not a walkie-talkie: a four-year-old cannot be expected to
be present at the moment their sister speaks, and an intercom that only works when both ends
are listening would be a toy that mostly fails.

It also decides the failure behaviour, which is the part that matters in a bedroom: **a message
that cannot be delivered is still a message.** The box holds it; the panel shows the pop-up
whenever it next asks.

### The asymmetry, stated once

|  | sends | receives | reads |
|---|---|---|---|
| **panel** (each twin) | audio only | audio, played aloud | — |
| **PWA** (Dad) | **text**, spoken to them by TTS | audio | **transcript**, with the audio available |

The panels never send text and never read. The PWA never has to listen if it does not want to.
That is the owner's requirement verbatim and it is also the right split: a four-year-old cannot
type, and a parent at work cannot play audio out loud.

## 2. Why this is not built on `/converse`

`/converse` is a request/response turn: audio up, reply down, nothing stored, nothing addressed.
jtalk is the opposite on all three — it is addressed to a principal, it persists until played,
and the reply may arrive hours later from a different device. Sharing the route would mean a
turn that sometimes answers and sometimes files, which is the kind of overload that makes a
failure mode impossible to name.

What it DOES reuse, deliberately: the capture buffer and upload path in `talk.c`, the
`_trim_to_speech` silence trim, the playback path through `s_play`, Whisper for the transcript,
and the storage abstraction for the audio itself (`CLAUDE.md` #2).

## 3. Data

One table, `jtalk_message`:

| column | why |
|---|---|
| `id` | |
| `sender_principal`, `recipient_principal` | who it is from and for. A panel or the owner. |
| `blob_sha256` | the audio, through the storage abstraction — never a raw path |
| `transcript` | STT for a panel's message; the typed text for Dad's |
| `composed` | `voice` or `text` — how it was made, which is not the same as how it is played |
| `duration_ms` | so the PWA can show a length without fetching the audio |
| `created_at`, `played_at` | `played_at IS NULL` is the whole inbox query |

**RLS is the feature, not an afterthought.** These are recordings of small children in their
bedrooms. A panel principal may read only messages addressed to it and rows it sent; it may
never read its sibling's inbox. The owner sees everything. This ships with an RLS isolation
test in the same PR (`CLAUDE.md` #3), and the test asserts the sibling case explicitly rather
than only the owner/not-owner case.

**Retention:** unplayed messages are kept indefinitely — a message nobody heard is the one thing
that must not evaporate. Played messages are kept 30 days so Dad can go back through them, then
swept. Proposed, not settled; §7.

## 4. The panel

### Asking for it

Two phrases, obeying `vocab.h`'s three rules (lowercase and spaces, two words minimum, no
phrase a prefix of another):

- `send a message` → the other panel
- `send dad a message` → the owner's PWA

Neither is a prefix of the other (`send a…` vs `send d…`).

**"The other panel" needs a rule, because it is only obvious with exactly two.** With one other
enrolled panel, that is the recipient. With none or several, the phrase is refused out loud —
*"I don't know who to send that to"* — rather than guessing. A toy that silently posts to the
wrong sibling is worse than one that says it cannot.

### Recording

A new state beside `TALK_LISTENING`, not a flag on it — the two differ in where the audio goes,
what is drawn, and what ends them, and a boolean inside one state machine would be three
implicit modes.

- **Ends on silence**, the same hush as a listening turn. Consistent, and already tuned for a
  four-year-old who pauses.
- **A touch cancels and discards it** (owner's choice, and the same rule a listening turn now
  has). Silence sends; a finger abandons. There is no third gesture to learn.
- **A different colour**, which is the owner's actual requirement: the red pulsing dot means
  *the robot is listening to you*. Talking to your sister must not look like that. jtalk
  records under a **blue** dot with the recipient's name beside it, so the child can see who
  they are talking to before they speak.
- **Capped at 20 s.** Longer than a conversational turn, short enough that a pocket-dial does
  not fill the box.

### Being told there is one

The panel already talks to the box on a schedule, and the manifest poll is ~15 minutes — far
too slow for *"my sister just sent me something"*. jtalk adds a small `GET /jtalk/waiting`
on a ~30 s cadence returning a count and the sender's name, which is a few dozen bytes.

A waiting message draws a **pop-up box over the face**: big, tappable anywhere inside, naming
who it is from. Tap plays it. It does not auto-play — the child chooses, and an unplayed
message survives a reboot because the state lives on the box, not the panel.

### After it plays

For **5 seconds**, a repeat icon in the **top-left**; a tap replays. Then it clears. The window
is deliberately short: it is for *"what did she say?"*, not a permanent control, and the corner
is otherwise the label's.

## 5. The PWA

**`Pet face` comes out of the launcher and `jtalk` goes in** (`Launcher.tsx`, target `petface`
→ `jtalk`). The separate `Pet` → `petcontrol` tile stays; it is a different thing.

The screen is one list, **grouped by panel**, newest first:

- an **unplayed** badge per panel, because that is the question being asked at work
- each message: sender, time, duration, **the transcript as the primary content**, and a play
  button beside it. The text is what gets read; the audio is the fallback for when the
  transcript does not make sense — which, given how the transcriber handles four-year-olds, it
  often will not.
- a compose box per panel: type, send. The text is spoken by TTS, stored as audio, and the
  typed text is kept as the transcript so both ends agree about what was said.

**Dad's voice is male.** The pet answers in `kokoro-af_heart`; a message from Dad arriving in
the pet's own voice would teach a four-year-old that the robot and their father are the same
thing. A separate setting, defaulting to a male Kokoro voice, and a different voice is the
cheapest possible signal that this is a person and not the toy.

## 6. Waves

- **W1 — the spine.** Migration + RLS + isolation tests. Panel routes (`send`, `waiting`,
  `next`, `played`) and owner routes (list, send-text-as-audio, stream). Whisper on the way in,
  TTS on the way out. No UI. Verifiable entirely by tests.
- **W2 — the panel.** Vocabulary, `TALK_RECORDING`, the blue dot, the pop-up, playback, the
  repeat icon. One firmware version, one OTA.
- **W3 — the PWA.** The jtalk screen, the launcher swap, transcripts, play, compose.

W1 first and alone, because both W2 and W3 are clients of it and a route that changes under
them costs two rewrites.

## 7. Open, and deliberately not guessed

- **Retention.** 30 days after playing is a proposal. Unplayed-forever is not.
- **More than two panels.** The refusal rule above is safe but unhelpful; addressing by name
  needs the twins' names in the offline vocabulary, which the owner has deferred.
- **Notifying Dad.** A message to the PWA currently waits to be looked at. Whether it should
  push is a question about a parent's phone, not about the panels.
- **The transcriber mangles small children.** `"tell us a joke"` arrived as `"There is a joke.
  There is a joke."` Every jtalk transcript inherits that, which is exactly why the audio is
  always kept and always playable. Fixing STT for child speech is its own piece of work.
