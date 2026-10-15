# jpanel Cast — notices on the house's Chromecast TVs

> **Status:** In progress · **Last verified:** 2026-10-15 · **Waves:** K0✅ K1◻️ K2◻️ K3◻️

The owner's ask (2026-10-14): *"I don't want an app. I want the ability to just cast. We'd check
to make sure the TV wasn't in use … I'd be okay to also cast to a TV that is off if it turns it
on. But it would always have to be idle."* The binding mock is `../mocks/jpanel-cast/` (approved
2026-10-14): a **Cast** launcher tile beside jpanel, the TVs and their state, one TV's settings,
Send through the panel composer, a per-TV result sheet, and Sent.

The design that mock commits to has not moved: no app on the TVs. The box renders each notice
to a 1920×1080 frame and casts it to Google's Default Media Receiver, and the idle gate is
checked at the instant of casting. What changed is that the spike answered the questions the mock
left open, so the waves below are written against measured TVs rather than guesses.

## K0 ✅ — the spike, and what the TVs said

Built in #1752 (`deploy/cast/`, `/api/debug/cast*`), corrected in #1757, and run against the
house's TVs on 2026-10-15 with the owner changing their states by remote.

| TV | Model | Reaches the box | Wakes from off by cast |
|---|---|---|---|
| Big TV | Philips 4K (Google TV) | after a power-setting change | **yes**, card up in ~4 s |
| Parents | Philips 4K (Google TV) | after the same change | **yes**, card up in ~12 s |
| Twins Room | Philips 4K A1 | yes | not tried |
| Primary Bedroom | Vizio D24h | yes | not tried (never seen off) |

What that settles, and what it corrects:

1. **An app opened with the remote does report.** Netflix started from the Parents home screen
   read `app_id: "Netflix"`, so the gate can tell a TV is in use. The mock's fallback (a set
   that hides local apps only takes notices while off) is not needed for these TVs.
2. **`is_stand_by` is not "off" on the Philips sets.** They report it while lit, Netflix
   included. The spike's gate checked standby first and would have cast over the show; #1757
   puts an open app or active player ahead of standby. A really-off TV reads `is_stand_by: true`
   with `is_active_input: false` (all three Philips, every time), and that pair is the best
   "off" signal we have. The PWA must not label a TV "off" from `is_stand_by` alone.
3. **Off means off the network unless the TV is told otherwise.** Big TV and Parents dropped
   off the LAN when turned off (the connection failed, then mDNS stopped resolving them), so no
   cast could reach them. The owner changed each one's power setting so Cast stays available
   in standby. After that, both stayed connected while off and woke on a cast. **This is a
   per-TV setting the owner has to make once**, and the TV list has to make its absence
   visible: "not reachable while off" rather than a silent gap.
4. **A woken TV stays on.** Cast has no power-off, and neither Philips went back to standby by
   itself. Parents was still on the home screen ten minutes after its card, and Big TV went off
   only because the owner turned it off. So the mock's per-TV "back to sleep after" can't be
   built over Cast. Until another control path exists, a TV woken for a notice stays on until
   its own sleep timer or the remote turns it off, and the PWA says so.
5. **Reconnects are slow.** A TV that comes back after being unreachable can take a few minutes
   to show as connected, because pychromecast backs off between retries. A notice sent in that
   window gets refused as "unknown". K1 handles that as a hold, not a failure.
6. **Our own card reads as busy.** While a card is up, the receiver reports the Default Media
   Receiver with `PAUSED`. That's right for the gate (two notices must not stack), but the
   PWA should say "showing a notice" rather than "busy".

## K1 ◻️ — the box casts a notice

**Shipped (K1a):**

- `cast_notice` + `cast_notice_tv` (migration 0250, owner-only RLS with its isolation test): one
  row per send, a child row per TV with its outcome (`shown`, `woke`, `skipped` with the gate's
  why, `failed`) and the sidecar's `cast_id`. Kept 7 days like the panel's, swept lazily on
  every send and read, each frame freed through `blob_refs` once nothing else holds it.
- `jbrain.cast.frame` draws the notice on the box as one 1920×1080 PNG in the mock's three
  layouts: words alone, a picture split beside the words, or a picture whole with its title over
  it. The sender and time sit on top ("Dad · via jbrain · 18:30"). The sidecar serves the PNG
  under a one-time name for as long as it is up, the same way it serves the test card.
- Fan-out: the sidecar's `POST /tvs/<id>/cast` re-checks each TV's gate at the instant of
  casting. `idle` casts, `standby` wakes and casts, anything else comes back skipped with its
  reason. After `seconds` (5–300) the box quits the receiver, but only if its own cast is still
  showing; `POST /tvs/<id>/stop` does the same at once (Take it down).
- Owner routes `/api/cast/{tvs,send,sent,preview,<id>,<id>/frame,<id>/stop}`, and debug routes
  `POST /api/debug/cast/send` and `GET /api/debug/cast/sent` so it runs without a terminal.

**Still to do in K1:** an `unknown` TV holds the notice and retries until it expires instead of
being skipped; quiet hours (a house default of 21:00–07:00, per-TV override, "held for quiet
hours"); per-TV settings (notices on/off, allow waking) and their routes. Jerv's tool comes
later, if wanted.

## K2 ◻️ — the PWA

**Shipped (K2a):** the **Cast** launcher tile with a badge counting the TVs that would take a
notice now; the TV list with each verdict in plain words ("Showing a notice" for our own card);
Send, every TV that would take it picked to start with and an in-use one struck through; the
per-TV result sheet with Take it down; and Sent, per send. After the first real cast the owner
asked for Send to compose like the panel composer, the picture always under the words (2026-10-15):
the photo fills a 16:9 canvas (drag, pinch, zoom) and the words are dragged into place and sized
over it in the panel's four styles. The phone draws the 1920×1080 frame and sends it whole
(`frame_b64`); the box keeps it as drawn (letterboxed only if it is the wrong shape) and the
sidecar casts it as a JPEG, since a PNG of a photo can run past the sidecar's body cap. The box's
own renderer (title, text, picture) stays for the debug route and later senders such as Jerv.
Then the owner asked to drag and scale both the text and the image directly: a touch picks
either up, a drag moves it, a pinch-and-twist or its corner handle resizes and rotates it, and
the photo can shrink below the screen and sit anywhere on it. On the phone that still read as a
form (owner: "This is awful"), so Send became a touch editor: Add text and Add image put layers on
the canvas, each placed with drag, pinch and twist, with no tabs or sliders.

**Still to do in K2:** one TV's settings (on/off for notices, quiet hours, allow waking), once
K1's per-TV settings land.

## K3 ◻️ — sound

A notice with sound is cast as a short clip (the frame plus its audio) rather than a still
image. That needs one muxing step on the box and a check that the Default Media Receiver plays it
on each TV.

## Open

- **A lit TV on an HDMI input.** If it reads like off (`is_stand_by: true`,
  `is_active_input: false`), the gate can't tell them apart and would switch the TV away from
  the input. Moot in this house for now: the owner has no HDMI devices plugged into any TV
  (2026-10-15). If one is added, test it, and if it reads like off, the fix is a per-TV "never
  wake" setting rather than a guess.
- **Back to sleep.** Cast can't do it (finding 4). Whether another path can turn a Google TV off,
  such as the TV's own sleep timer or a Google Home routine, is unexplored. Until something does,
  "back to sleep after" comes out of K2, and "allow waking" says the TV will stay on.
- **Twins Room and Primary Bedroom off.** Neither has been woken by a cast yet. The Vizio has not
  been seen off at all.
