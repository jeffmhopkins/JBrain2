# Jeff's jpanel — the ESP32-C6 panel as a set of small apps

> **Status:** In progress · **Last verified:** 2026-10-11 · **Waves:** C0🟡 C1◻️ C2◻️ C3◻️ C3b◻️ C4◻️
> — C0's code has landed (the bring-up image in `../../firmware-c6/` and the box's
> two-board flasher/manifest) and stays open until it has run on the board.

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
`MSG_ARRIVED`, `ALARM_DUE`, `TICK_MINUTE`, `WAKE`. Apps never poll hardware.

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
  service. CI enforces it: each `app_*` component's `REQUIRES` may name only `ui_kit`,
  `svc_*` interfaces and `lvgl`.
- **Logic is separate from widgets.** Each app splits into a pure model (`clock_model.c`:
  next-alarm arithmetic, snooze, 24-hour formatting) and its view. Models are host-tested the
  way `firmware/host` tests the S3's `ui.c`, `screen.c` and `gesture.c`.

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
| `urgent` | chimes even during night hours |
| `expires_at` | a question nobody answered in time is `expired`, not hanging forever |
| `state`, `answer`, `answered_at` | `queued` → `shown` → `answered` / `dismissed` / `expired` |

- `POST /api/panel/notify` (owner, debug scope, and in-process callers) → `{id}`; `GET
  /api/panel/notify/{id}` → state and answer, with `?wait=` to long-poll for the answer.
- A Jerv tool, `ask_panel(question, choices)` / `tell_panel(message)`, so a conversation or a
  scheduled run can ask "run the cleanup now?" and act on the tap.
- The panel learns of one on the settings poll it already makes every 3 s (the same path that
  carries the twins' message count), so no new connection and no new latency budget.
- A panel with notifications switched off answers the send with `refused`, so a sender knows
  to try another route (a PWA push) rather than wait on a question nobody will see.

**The panel side** is one more service (`svc_notice`) and a shell overlay, not an app: it has
to appear over whatever app is open, the way an alarm does. It wakes the screen, chimes unless
it is night hours (urgent always chimes), queues ("1 of 2"), and BOOT dismisses without
answering. An alarm outranks it; it outranks every app.

## 3. Waves

| Wave | What | Done when |
|---|---|---|
| **C0** 🟡 | Bring-up image (Wi-Fi, box, OTA, rollback, telemetry) + the box flashing/serving two boards (`?board=c6-amoled-216`). | Code landed 2026-10-11; open until C1 proves it on hardware. |
| **C1** | First USB flash from the PWA (Flash tab **board picker** from `GET /api/endpoint/boards` — built 2026-10-11, nothing preselected because both boards enumerate as the same USB device); prove one OTA (0.0.1 → 0.0.2) and one rollback on the real board. Answer the bring-up questions in the firmware README from telemetry. | The panel updated itself over Wi-Fi, twice, once refusing a bad image. |
| **C2** | Display (CO5300, strip rendering into internal RAM), touch, buttons, PMIC, RTC — each a `svc_*`; `ui_kit`; `shell` with the home pages; `sim/`. Brightness is tested on the glass first: the S3's runtime brightness write is discarded by the same controller. | Home grid on the glass, tiles open and close an empty app, BOOT goes home, swipe pages. |
| **C3** | The apps, each independently: Settings (Wi-Fi, battery, Jerv read-aloud, brightness, update now, calibrate), Clock (+ alarm service + PWA alarm editor + box API), Jerv (streamed hold-to-talk), Thread ×2 (voice post, playback). | Each app's mock states reproduced on the glass and in `sim/`. |
| **C3b** | The push path (§2): `panel_notice` + RLS test, the notify routes, the Jerv tools, `svc_notice` and the overlay. | Jerv asks a yes/no on the panel and acts on the tap. |
| **C4** | Minecraft. | Parked until scoped. |

## 4. Open questions (owner or hardware)

- Thread messages from this panel: do they reach the girls as **Dad** (the owner's
  principal), or as a third panel? The twins' "send a message → the other panel" rule
  assumes exactly two panels and must become addressed either way.
- Jerv on the panel: a session of its own, or the owner's current Jerv chat in the PWA?
- The hardware list in `../../firmware-c6/README.md` (PMIC rails for LCD reset and the speaker
  amplifier, which button is which, strapping pins).
