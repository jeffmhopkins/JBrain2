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
4. **Waking does not reliably go back to sleep.** Cast has no power-off. After its card,
   Parents stayed on the home screen. Big TV read "off" again about a minute later, and whether
   the owner or the TV did that is not yet known. The mock's per-TV "back to sleep after" can't
   be promised until it is.
5. **Reconnects are slow.** A TV that comes back after being unreachable can take a few minutes
   to show as connected, because pychromecast backs off between retries. A notice sent in that
   window gets refused as "unknown". K1 handles that as a hold, not a failure.
6. **Our own card reads as busy.** While a card is up, the receiver reports the Default Media
   Receiver with `PAUSED`. That's right for the gate (two notices must not stack), but the
   PWA should say "showing a notice" rather than "busy".

## K1 ◻️ — the box casts a notice

- `cast_notice` (+ RLS isolation test): one row per send, a child row per TV with its outcome
  (`shown`, `woke`, `held`, `refused: <why>`, `failed`) and timestamps. The owner's rows keep 7
  days, like the panel's.
- Render the composer's payload (picture, words, style) to a 1920×1080 PNG on the box, reusing
  the panel notice's text layout at TV scale. The sidecar serves it under a one-time name, the
  same way it serves the test card.
- Fan-out with the gate applied per TV at the moment of casting. `idle` casts, `standby` wakes
  and casts, anything else is refused with its reason. An `unknown` TV, or quiet hours, holds
  the notice and retries until it expires.
- Quiet hours: a house default (21:00–07:00 in the mock) with a per-TV override. Held notices
  show as "held for quiet hours".
- Owner routes for Send, the TVs, per-TV settings and Sent. Debug routes for the same, so it can
  be run without a terminal. Jerv's tool comes later, if wanted.

## K2 ◻️ — the PWA

Per the mock: the **Cast** launcher tile with a badge counting TVs that would take a notice now;
the TV list with each verdict in plain words; one TV's settings (on/off for notices, quiet
hours, allow waking); Send through the panel composer from #1756 (reply choices removed, since a
TV can't answer); the per-TV result sheet; and Sent, per send.

## K3 ◻️ — sound

A notice with sound is cast as a short clip (the frame plus its audio) rather than a still
image. That needs one muxing step on the box and a check that the Default Media Receiver plays it
on each TV.

## Open

- **A lit TV on an HDMI input.** If it reads like off (`is_stand_by: true`,
  `is_active_input: false`), the gate can't tell them apart and would switch the TV away from
  the console or cable box. Test this on a TV with something plugged in before K1 ships. If it
  does read like off, the fix is a per-TV "never wake" setting rather than a guess.
- **Back to sleep.** Find out whether Big TV turned itself off after its card. If the Google TV
  sets return to standby on their own after a cast wake, "back to sleep after" can stay in the
  mock. If not, it comes out or waits on another control path.
- **Twins Room and Primary Bedroom off.** Neither has been woken by a cast yet. The Vizio has not
  been seen off at all.
