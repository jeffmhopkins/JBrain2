# Jeff's jpanel (ESP32-C6-Touch-AMOLED-2.16) — on-glass UI

Kind: Living (mock set) · Status: **approved, not yet built** · Last verified: 2026-10-11

The owner's own panel's interface, agreed before any of it is drawn by firmware. The
board is the 480×480 Waveshare C6 (`firmware-c6/`); these files are drawn 1:1 in panel
pixels, in Montserrat because that is the face LVGL ships, so what was approved here is
what the glass can show.

Interactive copy: https://claude.ai/artifact/EtKP5EZF1VjmTNnvxTfY93 (the `.dc.html` files
here are its source and need that canvas's runtime to render; `Main.dc.html` is the live
panel, the others import it at a fixed `start` state).

## What was decided

- **Home, page 1:** Jerv top-left, Minecraft top-right (dimmed, "Soon" — not built), Quorra
  bottom-left, Tamsin bottom-right. Unplayed messages ride the tile as an amber count.
- **Home, page 2 (swipe left):** Clock, then Settings. Kept off page 1 because it is rarely used and the
  four main tiles already fill the glass.
- **Buttons** (measured order, seen from the front, 0.0.3 probe): BOOT (left) home / cancel,
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
  Jerv violet, Quorra amber, Tamsin teal, Minecraft green, steel for actions.
- **Clock:** opens on a **giant 24-hour time** and nothing else. Tapping it opens the alarm
  list (next alarm, on/off switches only); back returns to the face. Everything is 24-hour,
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
  sender, title, short body, and either OK or up to four answer buttons whose tap goes back
  to the sender. Queues as "1 of N"; BOOT dismisses without answering. Settings → Notifications:
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
