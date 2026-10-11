# Jeff's jpanel — the ESP32-C6 panel as a set of small apps

> **Status:** In progress · **Last verified:** 2026-10-11 · **Waves:** C0🟡 C1◻️ C2🟡 C2d◻️ C3◻️ C3b◻️ C4◻️
> — C0's code has landed (the bring-up image in `../../firmware-c6/` and the box's
> two-board flasher/manifest) and stays open until it has run on the board. C2's framework
> half has landed beside it, not in the image: `components/` (the contract, `ui_kit`, the
> shell, fake services, every app as descriptor + model + view, `svc_debug`), `host/` and
> `sim/` — see "What the framework lays down" under §1.

The owner's own panel: a Waveshare ESP32-C6-Touch-AMOLED-2.16 (480×480 AMOLED, CO5300 over
QSPI, CST9220 touch, AXP2101, PCF85063, QMI8658, ES8311 + ES7210, three top buttons). It is
not a pet. It is an owner's tool: talk to Jerv, post voice to Quorra and Tamsin, a clock with
alarms, settings — and later Minecraft.

The approved on-glass design is `../mocks/jpanel-c6/` (README there lists every decision).
The firmware's own README (`../../firmware-c6/README.md`) covers the board, pins and the
bring-up image.

## 1. The rule this plan exists for: every app is its own module

The owner: *"all these apps [should be] self-contained little things that we can develop on
their own … a central UI library that it pulls stuff from, but the clock versus jerv versus
all this should have its own little object."*

That is the architecture, not a style preference. Each app — **Jerv, Thread (Quorra, Tamsin),
Clock, Settings, Minecraft** — lives in its own ESP-IDF component, depends only on the UI kit
and on service *interfaces*, and can be built, run and tested without any other app present.

```
firmware-c6/
  main/                 boot, Wi-Fi, OTA, rollback gate (the bring-up image, kept)
  components/
    ui_kit/             the shared look: theme tokens, scroll page, card, switch, slider,
                        bubble, tile, toast, talk overlay, header. No app knowledge.
    shell/              home pages + tile grid, app lifecycle, button routing, overlays
    svc_*/              services: one owner per chip (display, touch, audio, power, rtc,
                        buttons) and per box route (box client, talk upload, messages, alarms)
    app_jerv/
    app_thread/         ONE app, instantiated twice (Quorra, Tamsin) with a config
    app_clock/
    app_settings/
    app_minecraft/      a stub until the Minecraft wave
  sim/                  the same components on the host (LVGL + SDL / headless PNG)
```

### The app contract

Every app exports exactly one descriptor; the shell knows apps only through it.

```c
typedef struct {
    const char *id;            /* "jerv", "clock" … stable: telemetry and settings keys */
    const char *title;
    const ui_icon_t *icon;     /* tile glyph, from ui_kit */
    ui_accent_t accent;        /* DESIGN.md accent token */
    uint8_t page, slot;        /* where its tile sits on the home pages */
    const void *config;        /* per-instance: Thread gets {principal, name} */

    void (*open)(app_t *self, lv_obj_t *root);   /* build its screen under root */
    void (*close)(app_t *self);                  /* free everything it built */
    bool (*on_event)(app_t *self, const app_event_t *ev); /* true = consumed */
    void (*tile_state)(app_t *self, ui_tile_state_t *out); /* badge, dimmed, live label */
} app_t;
```

`app_event_t` carries what an app may react to: `KEY_DOWN`/`KEY_UP` (hold-to-talk),
`BOOT` (home/back — the shell handles it unless the app consumes it), `NET_UP`/`NET_DOWN`,
`MSG_ARRIVED`, `ALARM_DUE`, `NOTICE_ARRIVED`, `TICK_MINUTE`, `WAKE`. Apps never poll hardware.

As built (`../../firmware-c6/components/app_api/include/app_api.h`) the contract is the one
above plus three members, each for a reason the sketch did not meet:

- `describe(self, dbg_json_t *)`, optional — the open app's own state in a debug snapshot (§4).
- `host` — `go_home()` and `toast()`, set by `shell_register`. An app cannot include the shell,
  and declaring shell functions in `app_api` would make every app link-depend on it; an injected
  table keeps the dependency pointing down and lets the sim run one app alone.
- `state` — the instance's runtime state. `config` is const, and a static per component would
  merge the two Thread instances into one.

Events carry values (`principal[16]`, `alarm_id`, `notice_id`), never pointers: they cross from
service tasks to the UI task through a queue.

### What the boundaries buy, and the rules that keep them

- **Only the open app's screen exists.** `open` builds it, `close` frees it. With 512 KB of
  RAM and no PSRAM, nine apps' widget trees resident at once is the first thing that would
  not fit; this makes an app's cost "while it is on screen" and nothing else.
- **Apps never touch a chip.** They call a service. That is the S3 firmware's hardest-won
  lesson (`../../firmware/README.md`, "One task owns the codec, one owns the panel"): two
  tasks on one `esp_lcd` or `esp_codec_dev` handle cost a panic that took weeks to find. One
  owner per chip, and an app is never that owner.
- **Background duties live in services, not apps.** An alarm must ring with the Clock app
  closed and Wi-Fi down, so the *alarm service* owns the schedule (NVS copy, RTC wake) and
  raises `ALARM_DUE`; the shell then opens Clock in its ringing view. Likewise the message
  service owns the waiting count, which is how a Thread tile shows a badge while closed.
- **Apps never include each other.** Anything two apps share moves down into `ui_kit` or a
  service. CI enforces it (`firmware-c6/tools/check_app_deps.py`): each `app_*` component's
  `REQUIRES` may name only `app_api` (which holds the `svc_*` interfaces), `ui_kit` and LVGL,
  and its sources may include only those headers and its own.
- **Logic is separate from widgets.** Each app splits into a pure model (`clock_model.c`:
  next-alarm arithmetic, snooze, 24-hour formatting) and its view. Models are host-tested the
  way `firmware/host` tests the S3's `ui.c`, `screen.c` and `gesture.c`.

### What the framework lays down (C2/C3 groundwork, landed 2026-10-11)

The structure above exists and runs, with no chip driver behind it:

- **`app_api`** — the contract, the service interfaces (`svc_audio`, `svc_box`, `svc_messages`,
  `svc_alarms`, `svc_notice`, `svc_power`, `svc_net`, `svc_settings`, `svc_display`, `svc_rtc`,
  `svc_debug`), the event bus services post through, and the redacting JSON writer.
- **`ui_kit`** — the mock's tokens and its widgets: scroll page with the right-edge thumb and
  pin-to-newest, card, switch row, slider row, buttons, tile, header, chat bubble, toast, the
  full-screen overlay, and the talk ring with its own timer.
- **`shell`** — `shell_core.c` decides (overlay priority, button routing, event routing, tile
  layout, swipe, registry clashes) and is host-tested; `shell.c` carries it out. Only the open
  app's tree exists; the shell deletes its root after `close`.
- **`svc_*`** — in-memory implementations, the sim's and (until each driver lands) the device's.
- **The apps** — Jerv, Thread (one component, two instances by config), Clock, Settings,
  Minecraft, each descriptor + pure model + view. The models hold the cheap real logic: 24-hour
  text, next alarm across repeat days, the night window past midnight, snooze; the brightness
  floor; §2's accept table (`svc_notice/notice_policy.c`) and the chime rule.
- **`host/`** (166 checks, ~1 s), **`sim/`** (every mock board's state to PNG through the debug
  screenshot path, and an open/close check that every app — closed mid-hold included — leaves
  the widget and timer count it found), **`tools/check_app_deps.py`**. All three run in CI.

It is excluded from the device build so `dist/` stays byte-identical; C2 switches it on
(firmware README "Layout"). Built against the S3's lessons and the twins' messaging review
(`JPANEL_MESSAGING_FIXES_PLAN.md`): one decision drives both the drawn overlay and its hit area
(B0, B3); a hold's release follows its press and a notice arriving mid-hold waits rather than
burying the talk ring (B1, E2.9); the event queue counts what it drops (§7.3); cross-task events
carry values, never pointers (A7); nothing assumes the CO5300 applied a brightness.

### Developing one app on its own

`sim/` builds the UI kit, the shell and any subset of apps for the host against **fake
services** (a fake box, a fake clock, a fake microphone), with LVGL drawing to an SDL window
at 480×480 or headless to PNG. So the Clock can be built and iterated on a laptop with no
board, no box and no other app compiled in — and CI renders each app's states to PNG beside
the mock it was approved from, so a drift from `../mocks/jpanel-c6/` is visible in review.

## 2. The push path: the box can tap the owner on the shoulder

The owner: *"an API … that just shows a pop-up of a message … so the machine can send this
panel messages that light it up and let me know to go check things … maybe even with a
response of choices, so you could ask a question with a yes or no."*

Anything on the box — Jerv, a workflow, an Ops alert — can put a message on this panel and,
optionally, ask a question whose answer goes back to the sender. Mocked in
`../mocks/jpanel-c6/` (boards 15-16, and the live panel's *Send a test message* tweak).

**The box side.** One table, `panel_notice` (RLS: the owner and the box's own services write;
a panel reads only rows addressed to it and may write only `answer` / `answered_at` on them —
an isolation test pins both, CLAUDE.md #3):

| column | |
|---|---|
| `id`, `device_id` | which panel |
| `source` | shown as the sender: "Jerv", "Ops", a workflow's name |
| `title`, `body` | short; the panel shows ~3 lines of body |
| `choices` | 0-4 labels; empty means a plain notice with OK |
| `urgent` | chimes even when the panel's chime is off; does NOT bypass a refusal |
| `expires_at` | a question nobody answered in time is `expired`, not hanging forever |
| `state`, `answer`, `answered_at` | `queued` → `shown` → `answered` / `dismissed` / `expired` |

- `POST /api/panel/notify` (owner, debug scope, and in-process callers) → `{id}`; `GET
  /api/panel/notify/{id}` → state and answer, with `?wait=` to long-poll for the answer.
- A Jerv tool, `ask_panel(question, choices)` / `tell_panel(message)`, so a conversation or a
  scheduled run can ask "run the cleanup now?" and act on the tap.
- The panel learns of one on the settings poll it already makes every 3 s (the same path that
  carries the twins' message count), so no new connection and no new latency budget.

**The panel decides, and the box refuses at send time.** The owner: *"I could disable it from
the device … so if I don't want it bothering me I can turn it off, or not be on during
bedtime. It would reject it and return the API response that the device is not online or not
accepting push."* So a refused message is never stored and never queued for later — it does
not exist, and the sender is told why at once, while it can still choose another route:

| answer | when |
|---|---|
| `202 {id, state: "queued"}` | accepted; poll `GET …/{id}` (or `?wait=`) for `shown` / `answered` |
| `409 {reason: "not_accepting"}` | the owner switched messages off on the panel |
| `409 {reason: "quiet_hours", until: "06:30"}` | inside the panel's quiet hours (the night-clock hours) |
| `503 {reason: "offline", last_seen_s}` | the panel has not polled for 30 s — it could not show it anyway |

The box can answer instantly because the panel REPORTS its accept switch, its quiet-hours
switch and its window on the 3 s settings poll; the box keeps the last report beside the
panel's `last_seen` and decides from that, never from a guess. The panel is the source of
truth — a setting changed on the glass reaches the box within one poll — and the PWA can show
it but not override it, which is the point: "don't bother me" set at the panel has to hold.
The window is evaluated in the panel's own timezone, reported with it. A message accepted a
moment before quiet hours begin is still shown (silently): the refusal is at the door, not a
recall.

**The panel side** is one more service (`svc_notice`) and a shell overlay, not an app: it has
to appear over whatever app is open, the way an alarm does. It wakes the screen, chimes unless
it is night hours (urgent always chimes), queues ("1 of 2"), and BOOT dismisses without
answering. An alarm outranks it; it outranks every app.

## 3. Waves

| Wave | What | Done when |
|---|---|---|
| **C0** 🟡 | Bring-up image (Wi-Fi, box, OTA, rollback, telemetry) + the box flashing/serving two boards (`?board=c6-amoled-216`). | Code landed 2026-10-11; open until C1 proves it on hardware. |
| **C1** | First USB flash from the PWA (Flash tab **board picker** from `GET /api/endpoint/boards` — built 2026-10-11, nothing preselected because both boards enumerate as the same USB device); prove one OTA (0.0.1 → 0.0.2) and one rollback on the real board. Answer the bring-up questions in the firmware README from telemetry. | The panel updated itself over Wi-Fi, twice, once refusing a bad image. |
| **C2** 🟡 | Display (CO5300, strip rendering into internal RAM), touch, buttons, PMIC, RTC — each a `svc_*`; `ui_kit`; `shell` with the home pages; `sim/`. Brightness is tested on the glass first: the S3's runtime brightness write is discarded by the same controller. **Landed 2026-10-11:** the framework (`ui_kit`, `shell`, `app_api`, fake `svc_*`, `svc_debug`, `sim/`, `host/`, the dependency check), outside the image. **Left:** the drivers behind the services, and switching the framework into the image. | Home grid on the glass, tiles open and close an empty app, BOOT goes home, swipe pages. |
| **C2d** | The debug relay (§4): `panel_debug_request` + RLS test, the command/result routes, settings-poll pickup and result upload, the `JPS1` → PNG decoder, the device's stripe handoff to the network task, Ops → Panels buttons. | A session holding the owner's token gets a snapshot and a screenshot of the live panel, and `reboot` is refused without `confirm`. |
| **C3** | The apps (descriptors, models and partial views already in `components/app_*`), each independently: Settings (Wi-Fi, battery, Jerv read-aloud, brightness, update now, calibrate), Clock (+ alarm service + PWA alarm editor + box API), Jerv (streamed hold-to-talk), Thread ×2 (voice post, playback). | Each app's mock states reproduced on the glass and in `sim/`. |
| **C3b** | The push path (§2): `panel_notice` + RLS test, the notify routes, the Jerv tools, `svc_notice` and the overlay. | Jerv asks a yes/no on the panel and acts on the tap. |
| **C4** | Minecraft. | Parked until scoped. |

## 4. Debugging a panel through the box

The owner: *"a very good debugging API so that if I give you a token you can pull current state
or whatever data you need from the device."* The owner has no terminal (CLAUDE.md #10), and a
panel cannot be reached directly — the box relays.

**What the S3 panels have, and where it stops.** Telemetry every fifteen minutes in a fixed
shape; `POST /api/debug/endpoint/report-now` raises a counter the ~3 s settings poll carries so
every panel posts at once; `GET /endpoint/reach` and `/endpoint/heard` read what was posted;
RTC breadcrumbs say where a panic happened. All of it is *push, fixed fields*: a question the
telemetry shape did not anticipate needs a new firmware. Nothing reads logs, nothing shows the
screen, nothing can press a button.

**The C6 asks instead.** A debug token holder sends a command; the panel runs it and answers.

- **State is described, not enumerated.** The shell, every service and the open app register a
  `describe` with `svc_debug`; a `snapshot` walks them all. A new service is in the snapshot
  the day it registers — no box change. It includes the shell's open app, page, overlay, key
  owner, queue depth and drops, last events and widget/timer counts; net (state, RSSI, IP,
  reconnects); heap (free, low-water, largest block); watched tasks' stack high-water marks;
  uptime, reset reason, the crash breadcrumb, coredump present; power; RTC time; settings.
- **Secrets cannot be described.** Every byte goes through one writer (`dbg_json`) that refuses
  the provisioning keys — `pass`, `token`, `ca`, `*_key`, `*_token` … — and scrubs every
  registered secret *value* from every string, so a token logged by mistake is caught too. A host
  test and the CI sim both assert a snapshot never holds the fake provisioning's secrets.
- **A RAM log ring** keeps the last ~3 KB of `ESP_LOGx` lines (a `vprintf` hook that still
  prints), dumped by `logs`.
- **Commands are a whitelist** with two flags each, published by `help`: `destructive` (only
  `reboot`; refused without `"confirm": true`) and `idempotent` (safe to retry after a lost
  result). `snapshot`, `logs`, `log_level`, `open`, `home`, `set`, `inject` (KEY/BOOT/PWR, a
  tap, a swipe — through a virtual pointer, so it reaches widgets exactly as a finger does),
  `ota_check`, `report_now`, `screenshot`, `reboot`. The request is a flat JSON object; the
  parser cannot recurse because nothing needs it to. All of it is host-tested.
- **A screenshot without a framebuffer.** There is no PSRAM and no room for a 460 KB frame. A
  capture invalidates the screen and rides the renderer: each 480×40 strip LVGL draws into its
  existing draw buffer is run-length encoded as it is flushed (`JPS1`: per strip `y0, rows, len`,
  then `(len-1, rgb565)` runs). Extra RAM is the encoder (~0.6 KB) plus a 2×4 KB handoff to the
  network task — the UI task never blocks on the network; if the handoff is full for 50 ms the
  capture aborts with `net slow` rather than stall a frame. On the wire the mock's screens are
  17-46 KB (measured in the sim); the worst case, noise, is 3 bytes a pixel. The box decodes to
  PNG. The sim's every render goes through this exact path.

**Transport: the settings poll, not a new connection.** The panel already polls every ~3 s (§2),
one TLS session at a time — the S3's hardest-won rule, harder without PSRAM. The poll response
carries at most one pending request; the panel runs it on the UI task (commands touch widgets)
and POSTs the result. Three seconds is fine for a debug session, and the box's existing nudge
can cut it. **Not a WebSocket yet** because the C6 has none: when it adopts the S3's one-socket
tunnel (`../../firmware/main/link.c`, `panel_ws.py`), the request arrives as a frame and the
result rides the same socket — `svc_debug` is JSON in, JSON (or a byte stream) out, and does
not change.

**The box side** (wave C2d; nothing built yet):

| | |
|---|---|
| `panel_debug_request` | `id`, `device_id`, `token_id` (who asked), `cmd` (jsonb), `destructive`, `state` `queued` → `sent` → `done` / `expired`, `result` (jsonb), `blob` (a screenshot stream, ≤ 1 MB), `created_at`, `sent_at`, `done_at`. A request not picked up in 60 s expires; results are deleted after 15 minutes. |
| RLS | The owner and debug-scoped capability tokens insert and read. A panel (`device_key`) reads only its own `queued` rows and may write `result`/`blob`/`done_at` only on its own `sent` rows. The isolation test pins: panel A cannot read B's request, cannot write B's result, cannot insert a request at all (CLAUDE.md #3). |
| `POST /api/debug/panel/{device_id}/command` | debug token → `202 {request_id}`; `409 {reason: "needs_confirm"}` for a destructive command without `confirm` (checked at the box too, from the same whitelist); `503 {reason: "offline", last_seen_s}` when the panel has not polled for 30 s (§2's rule) — refused at the door, never queued for later. |
| `GET /api/debug/panel/{device_id}/result/{request_id}?wait=` | long-poll ≤ 25 s → `{state, result}`; a screenshot's result links `GET …/screenshot/{request_id}.png`, decoded on the box. |
| panel-facing | the settings poll gains `debug` (one queued request; marks it `sent`); `POST /api/endpoint/debug/{request_id}` (device key) with the result JSON, or the `JPS1` stream as the body for `screenshot`. |

Every command is logged with the token's label (`debug.panel_command`), so what a session did to
the panel is on the record. The PWA's **Ops → Panels** gets Snapshot and Screenshot buttons on
the same routes, so the owner can look without a session too.

## 5. Open questions (owner or hardware)

- Thread messages from this panel: do they reach the girls as **Dad** (the owner's
  principal), or as a third panel? The twins' "send a message → the other panel" rule
  assumes exactly two panels and must become addressed either way.
- Jerv on the panel: a session of its own, or the owner's current Jerv chat in the PWA?
- Fonts: LVGL ships Montserrat at one weight up to 48 px, without `·`, `–` or `…`. C3 generates
  the 92/124 px clock digits and those glyphs (`lv_font_conv`), or the mock adapts.
- The hardware list in `../../firmware-c6/README.md` (PMIC rails for LCD reset and the speaker
  amplifier, which button is which, strapping pins).
