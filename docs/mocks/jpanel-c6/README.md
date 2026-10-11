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

- **Home, page 1:** Jerv top-left, Minecraft top-right (dimmed, "Soon" — not built), Elora
  bottom-left, Lydian bottom-right. Unplayed messages ride the tile as an amber count.
- **Home, page 2 (swipe left):** Settings. Kept off page 1 because it is rarely used and the
  four main tiles already fill the glass.
- **Buttons:** KEY (left) is hold-to-talk, PWR (middle) screen on/off, BOOT (right) home /
  cancel. PWR is not the talk key because the AXP2101 shuts down on a long press of it.
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
  Jerv violet, Elora amber, Lydian teal, Minecraft green, steel for actions.
