# Jeff's jpanel (ESP32-C6-Touch-AMOLED-2.16) — on-glass UI

Kind: Living (mock set) · Status: **approved; Settings, Clock and KEY routing built in firmware 0.0.8, BOOT held in 0.0.9, Clear chat, real panel names and the Minecraft menu in 0.0.11; the Clock's two-second hold, Jerv's PWA icon and the live status line in 0.0.12; alarms (the list, ringing, and the PWA editor) in 0.0.13; the steps list, the steady spinner and KEY-outside-starts-new in 0.0.14; notices with a picture, a sound and hide-by-tap-or-time mocked 2026-10-12 (box side built, panel side next)** · Last verified: 2026-10-12

The owner's own panel's interface, agreed before any of it is drawn by firmware. The
board is the 480×480 Waveshare C6 (`firmware-c6/`); these files are drawn 1:1 in panel
pixels, in Montserrat because that is the face LVGL ships, so what was approved here is
what the glass can show.

Interactive copy: https://claude.ai/artifact/EtKP5EZF1VjmTNnvxTfY93 (the `.dc.html` files
here are its source and need that canvas's runtime to render; `Main.dc.html` is the live
panel, the others import it at a fixed `start` state).

## What was decided

- **Home, page 1:** Jerv top-left, Minecraft top-right (since 0.0.11 a menu of Dave and Compass), Quorra
  bottom-left, Tamsin bottom-right. Unplayed messages ride the tile as an amber count.
- **Home, page 2 (swipe left):** Clock, then Settings. Kept off page 1 because it is rarely used and the
  four main tiles already fill the glass.
- **Buttons** (measured order, seen from the front, 0.0.3 probe): BOOT (left) home / cancel (held: the Clock),
  PWR (middle) screen on/off, KEY (right) hold-to-talk. PWR is not the talk key because the
  AXP2101 shuts down on a long press of it.
- **Hold-to-talk is the only input for speech**, to Jerv and to the girls alike: hold KEY,
  speak, release to send; a tap on the glass cancels. 1:00 cap. Audio streams to the box as
  it is recorded — the C6 has no PSRAM to hold a clip. Rose ring = Jerv listening, steel
  ring = a message to a person, matching the twins' panels' red/blue rule.
- **No typing anywhere.** Everything Jeff sends is voice; everything shown is a transcript.
- **Every page longer than the glass scrolls** (Settings, every conversation) with a thin
  thumb on the right edge only when there is more. One shared scroll page in firmware, not
  per-screen code. A conversation pins to the newest message only when one arrives.
- **Settings:** Wi-Fi (state, network, signal, Connect/Disconnect — no SSID or password
  entry; the network is the one the box wrote at flash time), Battery (AXP2101 fuel gauge
  percent, voltage, charging/USB, or "No battery fitted"), Firmware (version, **Update
  now** forces a manifest check and OTA; disabled while Wi-Fi is off), Jerv (**Read replies
  aloud** switch — off means text only, with a Listen button on each reply), Brightness
  (10-100% slider; 10% floor, the same reason the twins' settings API clamps it), Touch screen
  (**Calibrate**: the twins' 16-target, taps-until-they-agree routine; BOOT cancels and
  keeps the old map).
- **Ground is pure black** (an unlit AMOLED pixel is off); colours are `DESIGN.md`'s accents:
  Jerv the PWA's blue (`#7FA7C9`, since 2026-10-12; violet before), Quorra amber, Tamsin teal,
  Minecraft green, steel for actions.
- **Clock:** opens on a **giant 24-hour time** and nothing else. A **two-second hold** on it
  opens the alarm list (next alarm, on/off switches only) — a tap did until 2026-10-12, see
  below; back returns to the face. Everything is 24-hour,
  on the panel and in the PWA editor. Alarms are **created and edited in the PWA** (jpanel →
  Panels → Jeff's, `PwaAlarms.dc.html`): time, repeat days (none = once, then it switches
  itself off), optional label. The panel keeps its copy and rings from its own clock
  (PCF85063), so an alarm still sounds with Wi-Fi down. Ringing is full-screen: Snooze 9 min
  / Stop; KEY snoozes, BOOT stops. Alarms are offered on the owner's panel only.
- **Night clock:** between bedtime hours (default 21:30–06:30, set in the PWA beside the
  alarms) the face drops to dim digits on black — deep red by default, amber or grey by
  choice; on/off and colour are switched on the panel under Clock → alarms. Red first: least
  disruptive to dark-adapted eyes, and on an AMOLED it lights almost nothing else.
- **Firmware shape:** each of these screens is its own app module over a shared UI kit —
  `../../plans/JPANEL_C6_PLAN.md` §1.
- **Push messages from the box:** a full-screen pop-up over any app (an alarm outranks it):
  sender, title, short body, and up to four answer buttons whose tap goes back to the sender
  (a plain notice has none since 2026-10-12: a tap anywhere hides it). Queues as "1 of N"; BOOT dismisses without answering. Settings → Notifications:
  **Accept messages from the box** and **Refuse during night hours** — the panel decides, and
  the box turns a send away at once (`409 not_accepting` / `409 quiet_hours` with `until`, or
  `503 offline`), so nothing is held to pop up later — plus a chime switch. Box side:
  `../../plans/JPANEL_C6_PLAN.md` §2.
- **The clock face is never covered** (owner, 2026-10-11): while the giant clock is up, a push
  message or a girl's message does not pop up — it waits as a small badge (bell + count) in
  the top-right corner, drawn in the night-clock colour at night, and opens only when tapped.
  An alarm ringing still takes the whole screen (that is the clock doing its job). Board 17.
- **Day and date under the clock** (owner, 2026-10-11): a small "Sun 11 Oct" near the bottom of
  the face, in the face's colour at lower opacity (so it dims with the night clock). On by
  default; a switch on the Clock → alarms page turns it off.
- **Wi-Fi is the last card in Settings** (owner, 2026-10-11), below everything else, so a scroll
  or a stray tap near the top cannot reach it; and **Disconnect takes two taps** within three
  seconds (the first turns the button rose: "Tap again to disconnect"). Order is now Brightness, Battery,
  Notifications, Jerv, Firmware, Touch screen, Wi-Fi — brightness first because it is the
  one reached for most (owner, 2026-10-11).
- **Auto-rotate** (owner, 2026-10-11): the square glass turns its content in 90° steps to keep
  "down" down, from the QMI8658's gravity vector. A switch under Brightness ("Auto-rotate",
  on by default; off = locked upright). The live panel's **tilt** tweak turns the device to
  show it. Firmware rules: plan §2b.
- **KEY is "talk to Jerv" from anywhere** (owner, 2026-10-12): held outside a conversation —
  home, clock face, Settings, Minecraft — it records to Jerv, and on release the panel opens the
  Jerv thread so the transcript and the answer are seen. Inside Quorra's or Tamsin's thread it
  is still a message to her. A ringing alarm still takes KEY as snooze.
- **KEY outside the conversation starts a new one** (owner, 2026-10-12: *"If I start off from not
  within the Jerv conversation it starts a new one"*): KEY held inside Jerv's thread continues it;
  held anywhere else — home, the clock, Settings, Minecraft's menu — it starts a new Jerv
  conversation: on release the thread opens holding just the new exchange, and the box starts a
  fresh "Desk panel" chat (the old one stays in the PWA). The same rule from Dave's side: KEY in
  his thread continues Dave, and KEY anywhere else is Jerv's, new — never Dave's. A cancelled or
  too-short hold changes nothing. Firmware 0.0.14; the sim's `jerv-after-key` and `jerv-reply`
  states now show a thread that starts over.
- **BOOT held opens the Clock** (owner, 2026-10-12): a long press of BOOT (800 ms) opens the
  Clock from anywhere — home, any app, over a notice — the moment it is crossed, while still
  held. A short press keeps home / back but now acts on release, so a long press is never also
  a back. Already on the Clock it does nothing; over the talk ring or a ringing alarm it is
  plain BOOT (cancel, stop). Built in firmware 0.0.9.
- **Clear chat by a long press** (owner, 2026-10-12: *"find a way to clear the chat. Maybe a long
  press in a chat window pulls up a menu?"*): a finger held still (600 ms, within the tap's 20 px
  slop, so a scroll never does it) anywhere in a conversation — Jerv's, Dave's, Quorra's or
  Tamsin's — opens a small card over a dimmed screen: **Clear chat** (rose) and **Cancel**. Clear
  takes two taps within 3 s, the Wi-Fi Disconnect pattern ("Tap again to clear"). BOOT, a tap on
  the dim, or KEY closes it, and KEY then records nothing. Clearing empties that thread on the
  panel; for Jerv or Dave the next talk also starts a fresh conversation on the box (the old one
  stays in the PWA). One shell decision for every conversation, not a copy per app. Built in
  firmware 0.0.11.
- **The threads carry the panels' real names** (owner, 2026-10-12: *"it should pull the panel
  name from the panel config"*): Quorra and Tamsin are placeholders here and in the sim only.
  On the glass each thread is bound to a sibling panel's device id and shows the name the owner
  gave it on the box, its tile letter from that name and its colour from it too; before the box
  has ever answered it is "Panel 1" / "Panel 2". Built in firmware 0.0.11.
- **Minecraft is a menu: Dave and Compass** (owner, 2026-10-12): the Minecraft tile opens a
  second level with two tiles. **Dave** is a conversation exactly like Jerv's — the same view,
  hold KEY inside it to talk to Dave, spoken replies, the long-press menu — but its own instance
  with its own history: Jerv's and Dave's never share a screen or a message. **Compass** is a
  "Work in progress" page. BOOT climbs one level. KEY outside Dave's thread is still Jerv's.
  Built in firmware 0.0.11; there is no mock board for these, the sim's `minecraft`, `dave`,
  `dave-reply`, `compass`, `chat-menu` and `chat-menu-armed` states are their pictures.
- **The Clock opens its alarms on a two-second hold** (owner, 2026-10-12: *"when we're on clock
  and we press the screen and it pulls up the clock menu, I think that needs to be a long press,
  maybe 2 seconds or more"*): a plain tap on the face does nothing; a finger held still for 2 s
  opens the alarm list. After 0.3 s a thin ring fills around the finger over the rest of the hold,
  in the night colour when the night clock is on (steel by day); lifting early or moving cancels
  quietly. A tap on a dark screen still only wakes it. The mock's face still opens on a click (its
  runtime has no hold); the sim's `clock-hold`, `clock-hold-night`, `clock-tap` and
  `clock-alarms` states are the pictures. Firmware 0.0.12.
- **Jerv wears the PWA's own icon, in the PWA's blue** (owner, 2026-10-12): the home tile, Jerv's
  header and every Jerv glyph draw the neuron mark of `frontend/public/favicon.svg` (centre disc
  r 8, branching strokes 3.4 wide with round caps and joins, terminal dots) in `#7FA7C9`, which
  replaces the purple four-point star and the violet everywhere Jerv's colour is used (his tint,
  "Speaking…", Listen). `Main.dc.html` carries the same SVG; the firmware rasterises it from the
  SVG's geometry (`firmware-c6/tools/gen_jerv_mark.py`). Contrast 5.6:1 on the tile's tinted disc,
  7.1:1 on the tile; Jerv never appears on the night clock's corner badge.
- **A live status line while Jerv or Dave works** (owner, 2026-10-12: *"I want better updates for
  tool use and such to come through the panel and not to have it direct me to my app"*): under
  the waiting question, a small spinner and dim words — "Searching the web: …" — replaced by the
  answer, which is kept whole on the glass (up to 1,000 bytes) and read aloud in full. KEY while
  he works says "Still working on the last one". The sim's `jerv-working`, `dave-working` and
  `jerv-long` states are the pictures. Firmware 0.0.12.
- **The steps Jerv takes, not one line** (owner, 2026-10-12: *"if Jerv is using tools we should be
  able to see that too … you could say using xtool or whatever the tool shorthand is"*): under the
  question, one dim line per tool, in the tool's own shorthand and its target — "web_search · tide
  Port St John", "web_fetch · tide-forecast.com" — a ✓ when it finished, a dim ✗ when it failed,
  the spinner on the one running. Between tools the line is "Thinking… 0:42" with a clock from the
  moment the question went. The last four show; older ones fold into "+N more" above them. When the
  answer lands they collapse to one dim line over it, "2 steps · 58 s" (the PWA's "Worked · 2
  steps"); an answer that ran no tool has none. **The spinner is a steady quarter arc going all the
  way round once a second** (owner: *"I expected it to go all the way around but it's kind of like
  resetting"*) — not LVGL's growing and shrinking sweep — and a status change no longer remakes it.
  The sim's `jerv-thinking`, `jerv-steps` and `jerv-steps-done` states are the pictures; there is
  no mock board. Firmware 0.0.14.
- **Alarms, built** (firmware 0.0.13 and the PWA): `ClockAlarms`, `Ringing` and `PwaAlarms` as
  drawn, with these differences, each on purpose. **A sound per alarm** — the PWA's form gains a
  Chime / Beeps / Rise picker below the label (the mock has none; the brief asked for a small fixed
  set), and the list's second line names it. **Delete takes two taps** within 3 s ("Tap again to
  delete"), Revoke's pattern; the mock deletes on one. **"On the panel" is earned**: the mock flips
  to it after 1.5 s, the PWA only once the panel asks the box with that version, which it does
  only after storing it ("Sending to panel…" until then, re-checked every 10 s). **No `"` or `\`
  in a label** (the panel's fetch buffer is sized for labels with nothing to escape). **The time
  field is the phone's own** — `<input
  type="time">` shows 12-hour on a phone set to 12-hour; the list, the panel and the box are 24-hour.
  **The night-clock hours are not in the PWA yet** (still the panel's default 21:30–06:30); that
  half of the mock's card is a later wave. On the panel the ringing screen's second button stays
  **Stop**, as drawn. The sim's `ringing`, `ringing-dark`, `ringing-over-notice`, `alarm-snoozed`,
  `alarm-test` and `clock-alarms` states are the panel's pictures, from the box's own JSON.
- **A notice can be a whole picture** (owner, 2026-10-12: *"I want to be able to send a whole
  picture as a notification. This way some push notifications can draw their own banner or
  picture or whatever and show that."*): the sender sends a PNG, JPEG or WebP and the box fits
  and encodes it for the panel. With nothing else to say it fills the glass (NotifyPicture,
  board 18) under a small sender chip and the hide hint; with words or answers it is a banner
  across the top, up to half the glass, over the usual card (NotifyBanner, board 19). The
  pictures in the boards are drawn by the "sender" to show the idea; the panel draws whatever
  pixels arrive.
- **A notice can carry a sound of eight seconds or less** (owner, 2026-10-12: *"allow the push
  notification to have a short 8 second or less audio clip to play if it wants to."*): a clip
  from the sender, or words the box speaks in the PWA's read-aloud voice. It plays once as the
  notice appears; the card shows a speaker row with the clip's progress and time (NotifyAudio,
  board 20). Never over an alarm; it stops when the notice hides.
- **Hidden by a tap or by time** (owner, 2026-10-12: *"these notifications could be pressed to
  hide or time out to hide."*): a plain notice or a picture hides on a tap anywhere; a question
  only by an answer or BOOT, so a stray tap never throws one away. A notice the sender gave a
  timeout shows a thin line along the bottom edge draining to the left, and the hint counts the
  seconds ("Tap to hide · hides in 18 s"); when it runs out the notice hides itself and the
  sender reads `expired`. Without a timeout it waits for the tap. The live panel's *Send a test
  message* tweak sends a picture, a banner question and one with a sound. Box side and wire
  format: `../../plans/JPANEL_C6_PLAN.md` §2.
