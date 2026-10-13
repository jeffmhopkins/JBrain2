# Jeff's jpanel — the ESP32-C6 panel as a set of small apps

> **Status:** In progress · **Last verified:** 2026-10-13 · **Waves:** C0✅ C1✅ C2🟡 C2d🟡 C2r🟡 C3🟡 C3b🟡 C4🟡
> — C0 and C1 are proven on the board (2026-10-11): USB-flashed 0.0.1 from the PWA's board
> picker, it joined Wi-Fi 6, reached the box over TLS, and took 0.0.2 over the air and kept it
> across a reset. The deliberate bad-image test is dropped at the owner's call. C2's framework
> half landed first (`components/`, `host/`, `sim/` — "What the framework lays down" under §1);
> **0.0.4 switches it into the image** with the first real drivers — the AXP2101, the buttons and
> the CO5300 under LVGL, the shell over fake services — now running on the glass (firmware README
> "What 0.0.4 adds"). **0.0.6 adds touch** (the CST9220 and the shell's real-pointer rules), built
> and in `dist/`, not yet felt on the glass (firmware README "What 0.0.6 adds"). **0.0.7** (touch
> mapping) is proven on the glass. **0.0.8** (built, awaiting the board) starts C3: Settings and
> the Clock as the mock agreed, filled from the real services — saved settings in NVS, the
> PCF85063 and the box's `Date` header for the time, the radio and the update loop behind the
> Wi-Fi and Firmware cards — and KEY routed to Jerv from anywhere (firmware README "What 0.0.8
> adds").
> **C2d's box side is built (2026-10-12)** — the table, its RLS, the command/result routes,
> the `JPS1` → PNG decoder and `debug-connect.sh panel-cmd` / `panel-shot` (§4 "The box
> side"). **0.0.9 (built, awaiting the board)** is the panel's half: the 3 s pickup over one
> kept-alive HTTPS session, every command on the UI task, the screenshot streamed from the
> renderer through two 4 KB chunks, the `esp_log` hook and the boot's secret check — plus BOOT
> held for the Clock and the mock's microphone (firmware README "What 0.0.9 adds").
> **0.0.10 (built, awaiting the board)** brings sound: `svc_audio` owns I2S0, the ES8311 and the
> ES7210 (esp_codec_dev over `svc_power`'s bus, the amp on ALDO2), and KEY really talks to Jerv —
> the recording streamed up `POST /endpoint/converse?persona=jerv` while KEY is held, the reply's
> transcript and words shown in the Jerv thread and its speech streamed to the speaker unless
> read-aloud is off; KEY or BOOT over a reply stops it (firmware README "What 0.0.10 adds"). The
> first two talks worked on the glass. **0.0.11 (built, awaiting the board)** carries four owner
> decisions of 2026-10-12: Jerv is the owner's REAL Jerv (and Dave his real Minecraft_Dave), in the
> PWA's voice, behind a server-side per-panel allow; a long press in any conversation offers Clear
> chat; the twins' threads carry their panels' real names from the box; and Minecraft is a menu of
> Dave and Compass (firmware README "What 0.0.11 adds", and the decisions under §1).
> **0.0.12 (built, awaiting the board)** answers the owner's first agent talks (2026-10-12): the
> panel's chats now land under the owner principal his PWA uses (they were filed under a revoked
> one); the turn streams to the panel (JT2) — the transcript at once, a live status line per
> tool, the whole answer on the glass and read aloud in full, sentence by sentence — with no
> 100 s cut-off; KEY while Jerv works says so; the reply's first word is no longer clipped; the
> Clock opens its alarms on a two-second hold; Jerv wears the PWA's own icon in its blue
> (firmware README "What 0.0.12 adds", and the decisions under §1).
> **0.0.13 (built, awaiting the board) is the alarms wave**: the box's per-panel alarms table and
> routes (migration 0236, `api/panel_alarms.py` — the owner edits, a `display` panel reads its own
> and may switch only `on`), the PWA's Alarms section on the desk panel's card, and the panel's
> `svc_alarms`: the list fetched with an etag on every update-loop pass and cached in NVS, fired in
> UTC through the zone's daylight saving, the Ringing screen over everything with a synthesised,
> ramping sound, KEY snooze / BOOT stop / a 10-minute cap, and the switch on the glass queued back
> to the box (firmware README "What 0.0.13 adds", and "Alarms" under §1).
> **C3b's box side is built (2026-10-12)** — notices that can carry a picture the sender drew
> and a sound of up to eight seconds, hidden by a tap or by a timeout (three owner decisions
> that day): `panel_notice` and its RLS, the send/read routes and the debug route, the refusal
> at the door from what the panel reports on its pickup, the `JN1` stream with test vectors,
> Jerv's `panel_notify` and the PWA's *Send test notice* (§2). The panel half's plumbing is
> in 0.0.19 (below); its look waits for the owner's approval of the re-mocked overlay.
> **0.0.15 (built, awaiting the board) is the conversation switcher** the owner approved on
> 2026-10-12: in Jerv's or Dave's thread the long press offers New conversation (one tap) and
> Recent conversations — the agent's last ten chats from the panel and the PWA, to switch the
> panel into one — and the thread's header carries the conversation's title. On the box,
> `agent_sessions.origin` (migration 0239), the `/endpoint/convo/{agent}/…` routes behind the talk's
> own gate, and panel chats titled the way PWA chats are (§1 "Switching conversations", firmware
> README "What 0.0.15 adds").
> **0.0.16 (built, awaiting the board) is voice post with the twins** (owner, 2026-10-12: *"Am I
> able to receive voice messages now from the kids' other jpanel?"*): the person threads list the
> box's real messages, a tap plays one (streamed, marked played on the box and so in the PWA), KEY
> in a twin's thread sends her a voice message, and a new one from her badges her tile and the
> clock face and chimes once. On the box, `api/panel_messages.py` behind the talk's gate, and the
> debug pickup's `X-Jpanel-Post` signal (§1 "Voice messages with the twins", firmware README "What
> 0.0.16 adds").
> **0.0.17 (built, awaiting the board) is the owner's first use of 0.0.16** (2026-10-13): her thread
> no longer jumps during a play (the bubble changes in place, the list rebuilds only when its
> messages change and keeps the reader's scroll), the play's bar fills left to right from the
> speaker's own sample count, a message is heard once its audio starts (§1 "Voice messages with
> the twins", the unread rule), and a play's ring is taken after its connection's handshake, not
> before, for the 44 KB `int_min` the board read; `int_min_at` now says which phase set the low
> (firmware README "What 0.0.17 adds").
> **0.0.18 (built, awaiting the board) is three things the owner approved on 2026-10-13** (*"Go
> ahead with 0.0.18."*): a play's ring is a 6 KB one of its own, not a talk's 16 KB, for the
> 43.5 KB `int_min` 0.0.17 still read at `play+ring` (the receive window and mbedTLS's record buffer
> already hold the box's audio behind it); **auto-rotate** (wave C2r, §2b, as built there): the
> QMI8658 read at 8 Hz while the screen is lit, the content turned in the flush and touch with it;
> and **touch calibration** (Settings → Touch screen → Calibrate): four corner crosses, an affine
> fit in the glass's own frame kept in NVS, applied before the rotation (firmware README "What
> 0.0.18 adds").
> **0.0.19 (built, awaiting the board) is C3b's panel half, the plumbing only** (§2 "The panel
> side"): the pickup reports `na nq nc nf nt tz`, so the box answers notices instead of
> `no_notice_surface`; on `X-Notice` the panel collects each JN1 stream record by record into its
> own file on the `storage` partition (littlefs, first mounted here), checked as `decode()` checks
> it and fetched again from `…/{id}/body` when torn; the existing overlay shows the real text and
> answers; `shown` / `answered` / `dismissed` / `expired` reach the box across outages and reboots;
> `hide_s` hides it; a sound plays once from the file. The overlay's new look (picture, speaker
> row, countdown line) is the next wave, after the owner approves its re-mock (firmware README
> "What 0.0.19 adds").

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
  closed and Wi-Fi down, so the *alarm service* owns the schedule (an NVS copy of the box's
  list, fired in UTC from the system clock the PCF85063 keeps) and raises `ALARM_DUE`; the shell
  then opens Clock in its ringing view. Likewise the message
  service owns the waiting count, which is how a Thread tile shows a badge while closed.
- **Apps never include each other.** Anything two apps share moves down into `ui_kit` or a
  service. CI enforces it (`firmware-c6/tools/check_app_deps.py`): each `app_*` component's
  `REQUIRES` may name only `app_api` (which holds the `svc_*` interfaces), `ui_kit` and LVGL,
  and its sources may include only those headers and its own.
- **Logic is separate from widgets.** Each app splits into a pure model (`clock_model.c`:
  24-hour formatting, the night face; a service's own rules likewise, e.g. the alarms' schedule in
  `svc_alarms/alarm_logic.c`) and its view. Models are host-tested the
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

It was excluded from the device build until 0.0.4 switched it on (firmware README "Layout"). Built against the S3's lessons and the twins' messaging review
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

### KEY routing (owner, 2026-10-12)

KEY held outside a conversation goes to **Jerv**, wherever the panel is (home, clock face,
Settings, any app that does not consume KEY): the shell, not an app, owns that default, so it
works with every app closed. Release sends, and the shell opens the Jerv thread to show the
answer. A Thread app consumes KEY for its own recipient; a ringing alarm consumes it as snooze;
the talk overlay keeps the rule that a release goes where its press went. Built in 0.0.8
(`shell_core.c`: `sh_route_button` offers KEY to the open app with Jerv as the fallback, and
`sh_declined` hands the hold to the shell); 0.0.10 records and sends it. While Jerv is thinking
about or speaking a reply, **KEY or BOOT stops the reply** in every app and does nothing else —
no recording starts, and the release sends nothing (`SH_ACT_STOP_REPLY`, host-tested).

**Inside or outside the conversation decides whether it is a new one (owner, 2026-10-12, built in
0.0.14):** *"If I start off from not within the Jerv conversation it starts a new one."* KEY held
**inside** Jerv's thread continues that conversation. KEY held **outside** it — home, the clock
face, Settings, Minecraft's menu — starts a **new** Jerv conversation (inside Quorra's or Tamsin's
thread KEY is still a message to her): once the
hold is sent, the panel's view of Jerv's thread is cleared to just the new exchange and the talk
carries `new_thread=true`, so the box starts a fresh "Desk panel" session (the old ones stay in the
PWA). A hold cancelled or too short changes nothing. Dave follows the same rule from his side: KEY
in Dave's thread continues Dave; KEY anywhere else — Minecraft's menu included — is outside every
conversation and so goes to Jerv, new, never to Dave. Clear chat still starts the next talk fresh
in either. One pure rule, `talk_key_route` (`svc_audio/talk_policy.c`), answers both the
conversation view and the shell's own talk, and is host-tested.

### BOOT held opens the Clock (owner, 2026-10-12)

A **long press of BOOT (800 ms) opens the Clock from anywhere**, fired once the moment the
threshold is crossed while still held. A short press keeps home / back but now fires on the
**release** (only then is it known to be short), so a long press is never also a back. On the
Clock it is a no-op; over the talk ring or a ringing alarm it is plain BOOT (cancel, stop); over a
notice it opens the Clock, whose face keeps the notice as its badge; on a dark screen it only
wakes. The timing is `btn_hold_step` (`svc_buttons/btn_logic.c`), the routing `sh_route_button`
(`SH_BTN_BOOT_LONG` → `SH_ACT_OPEN_CLOCK`), both host-tested; built in 0.0.9.

### Jerv on the panel is the real Jerv (owner, 2026-10-12)

The owner, after the first talks on 0.0.10: *"Chat is working. Note: it should be using the same
tts voice as my pwa. Also, it should be the full jerv with full tool access. I'm explicitly
permitting this."* That closes §5's question and supersedes 0.0.10's tool-less persona.

- **The same agent.** A talk from this panel runs through the PWA's own `/chat` handler as the
  owner (`backend/src/jbrain/api/panel_agent.py`, `run_turn`): the same prompt, tools, memory,
  approvals and transcript. It lands in one rolling **"Desk panel"** thread in the owner's Jerv
  history (`app.endpoint_panel_thread`), so he reads and continues it in the PWA.
- **Nothing is approved from the panel.** A Proposal waits for his tap in the PWA and an
  `ask_owner` question for his answer there; since 0.0.12 the panel says in plain words what is
  waiting — *"Jerv wants to change your calendar. Confirm it in the app."*, *"Jerv asks: Which
  beach? Answer it in the app."* — the one case it still points at the app.
- **Only this panel.** The box answers with the agent only when the caller was flashed as a
  `display` AND the owner switched **agent access** on for it (`endpoint_panel.agent_access`,
  migration 0233; PWA → Panels → "Talks to the full Jerv and Dave", or `PUT
  /api/debug/panel/{id}/agent`). Both are server-side; a panel reads its own row and writes none.
  The twins' `jpet` panels never reach it, whatever they send. Migration 0233 switches it on for
  the one live display panel named Jeff.
- **The PWA's voice.** The reply is spoken by the box's Kokoro in the PWA read-aloud's voice and
  effects (`brain_answer_voice`, speed, pitch, chorus, robot) with the owner's pronunciation
  lexicon, resampled to the panel's 16 kHz s16 mono. When the PWA is set to the phone's own
  voice ("native") there is no box voice to match and the panel keeps the chosen Kokoro voice.
- **Long turns.** 0.0.11 waited 100 s for the agent and then said the answer would be in the app,
  with the answer condensed to 240 bytes. 0.0.12 streams the turn instead and keeps the whole
  answer on the panel (the next decision); JT1's condensed frame remains for a panel that does not
  ask for JT2.
- **Which owner (fixed 2026-10-12).** 0.0.11 ran the turn as `WHERE kind='owner' LIMIT 1`, and on
  the live box — two owner rows after a key rotation — that was the old, revoked one: the "Desk
  panel" chats were filed under a principal the PWA no longer logs in as, and the owner's standing
  instructions (keyed by principal) were read from the wrong row. The turn now runs as the owner
  principal the PWA's logins use (`jbrain.auth.owner`: un-revoked, then the one with the most
  recently seen live PWA session, then the newest), and the same resolver backs the scheduler's
  helper every other headless "act as the owner" caller shares. Migration 0234 re-homed the
  misfiled threads (and their Proposals); each panel turn re-homes its thread again if a later
  rotation strands it. jmolt deliberately keeps its own oldest-row anchor.

### Dave on the panel, under a Minecraft menu (owner, 2026-10-12)

*"When you click Minecraft it should go to a next layer of a menu, options will be Dave or
Compass. Make Dave connect to Minecraft. Dave chat kind of the same as Jerv is getting built out.
Make Compass a work in progress."* — and then: *"I don't want Dave and Jerv to share the same
screen. Code-wise that's fine, but they should be separate instances with separate history."*

- The Minecraft tile opens a second level with **Dave** and **Compass**. BOOT climbs one level.
- **Dave is the real Minecraft_Dave** on the box (`persona=dave`), behind the same per-panel
  allow as Jerv, in a "Desk panel" chat in the owner's Minecraft_Dave history. His words are
  governed by his own system prompt (the lore is canon in the Bleepbloop_City repo); nothing on
  the panel writes lines for him. There is no voice configured for Dave anywhere on the box, so he
  speaks in the PWA read-aloud's voice, as Jerv does.
- **Shared code, separate instances.** One conversation view (`firmware-c6/components/convo`),
  instantiated by `app_jerv` and by `app_minecraft`: separate screens, separate on-panel
  histories (each its own eight-message buffer), separate scroll positions, separate box threads;
  clearing one never touches the other (host- and sim-tested). KEY inside Dave's thread talks to
  Dave; KEY anywhere outside a conversation is still Jerv's.
- **Compass** is a "Work in progress" page.

### Clear chat by a long press (owner, 2026-10-12)

> 0.0.15 keeps this for the people's threads only; in Jerv's and Dave's the long press is now New
> conversation and Recent conversations ("Switching conversations" below).

*"Find a way to clear the chat. Maybe a long press in a chat window pulls up a menu?"* A finger
held still for 600 ms (within the tap's 20 px slop, so a scroll never does it) anywhere in a
conversation — Jerv's, Dave's, either person's thread — opens a small card: Clear chat, Cancel.
Clear takes two taps within 3 s (the Wi-Fi Disconnect pattern). BOOT, a tap outside, or KEY
closes it, and KEY then records nothing. Clearing empties that thread on the panel; for Jerv or
Dave it also starts a fresh conversation on the box on the next talk (`new_thread=true` on that
`/converse`, kept until a talk is answered). Nothing is deleted on the box; the old chat stays in
the PWA. The people's threads only clear the panel's view. One shell decision for every
conversation (`shell_core.c`: `sh_long_press`, `sh_menu_*`), host-tested; a debug session drives it
with `inject` `hold` at x,y.

### Switching conversations (owner, 2026-10-12)

*"On long press of the Jerv [conversation] where it makes you able to clear a conversation, it
should also have another button so that you can click it and view recent conversations to
re-change what conversation we're actually in … change the clear conversation to new session."*
The mock (`../mocks/jpanel-c6/`, the Convo boards and DaveRecent) was **approved by the owner on
2026-10-12**; it is the spec. He approved it without answering its four open questions, so their
defaults stand:

1. The conversation's title always shows under "Jerv" / "Dave" in the thread's header. A new
   conversation reads "Desk panel" until the box has a title for it.
2. After a switch the thread shows the chip "Earlier messages are in the app", not "Load earlier".
3. Dave's Recent lists only his panel and PWA conversations; the in-game Minecraft chats are not
   in it for now.
4. Conversations started by KEY from outside a thread appear in Recent like any other.

**On the panel (0.0.15).** In an agent's thread the long press opens "This conversation" /
"<Agent> · <title>" with **New conversation** (the agent's colour, one tap, no confirm: "New
conversation — the last one is in Recent"; on a conversation that is already new and empty it does
nothing and says "Already a new conversation"), **Recent conversations ›** and **Cancel**. That
replaces 0.0.11's Clear chat for Jerv and Dave; the people's threads keep Clear chat as it was.
Recent is its own screen over the thread: a "+ New conversation" row, then up to ten 84 px rows —
where it began (a microphone for the panel, a phone for the app), the title cut with "…", the time
in the panel's 24-hour clock ("12 min ago", "Yesterday 19:40", "Sat 10:05", "5 Oct"), and the last
line, the owner's prefixed "You: "; the current one with a 6 px accent bar, a tint and a
"Current" chip; skeleton rows while loading; an empty state. Tapping a row says "Opening…" and
switches: the thread then holds that conversation's last exchanges from the box under its title,
and the next KEY inside it continues THAT conversation. KEY in Recent closes it and talks in the
current conversation; BOOT or the back arrow returns to the thread. KEY routing outside a thread is
0.0.14's, unchanged.

**On the box.** Three device-key routes behind `panel_agent.agent_allowed` — the same gate as
`/endpoint/converse`, so a twin's panel never reaches them — acting for the live owner principal
(`auth/owner.py`) on owner-scoped sessions (`api/panel_convo.py`): `GET
/endpoint/convo/{agent}/recent`, `POST …/select {session_id}` (a chat that is not the owner's root
chat with that agent, from the panel or the PWA, is 404, and so is an archived or empty one). A
chat opened with domain or subject scopes (health, finance, location) is never listed or
selectable: its turns would read behind the firewall and the panel speaks answers aloud at the
desk, so the panel only ever runs in unscoped chats and `GET …/messages?limit=N` (the last N
exchanges through `panel_agent.condensed`, the newest answer up to 1,000 bytes and the rest 255;
`limit=0` is the title alone). New conversation has no route: it is the talk's existing
`new_thread=true`, so nothing is created on the box until something is said. Where a chat began
is `agent_sessions.origin` (migration 0239: "panel" from `panel_thread`, "pwa" from `POST
/api/sessions`, NULL for every other maker; the backfill marks only what the rows can tell).
Panel chats are titled like PWA chats: the "Desk panel" placeholder on a panel-started chat no
longer counts as a name (`session.is_unnamed`), so Jerv's in-turn `name_session` titles it —
no new LLM call. Dave has no `name_session`, so his panel chats stay "Desk panel".

### The threads carry the panels' real names (owner, 2026-10-12)

*"Quorra and Tamsin are also placeholder names, it should pull the panel name from the panel
config."* Each person's thread is bound to a sibling panel's **device id** and shows the name the
owner gave that panel on the box — the PWA Panels screen's name — with the tile's letter and colour
from it. The box serves `GET /endpoint/siblings` (device key; only a `display` panel is
answered; ids and names only, the caller excluded, at most four); the panel fetches it once a
boot and every fifteen minutes, keeps it in RAM and in an NVS cache so names show after a reboot
before the box answers, and says "Panel 1" / "Panel 2" until it has ever had an answer. No name
is compiled into the image; Quorra and Tamsin remain only in the mocks, docs, host tests and sim
fixtures, and the sim feeds its names through the box's JSON and the panel's own path. The
history behind a thread is RAM-only on the panel (the box keeps the agents' full history; paging
it back onto the panel is a later change).

### Voice messages with the twins (owner, 2026-10-12; firmware-c6 0.0.16)

*"Am I able to receive voice messages now from the kids' other jpanel?"* Until 0.0.16, no: a
twin's panel (`jpet`) sent "to Dad" through `jpanel.send`, which filed an owner-addressed row, and
only the PWA read those rows; the desk panel's two person threads were stubs. The desk panel is now
a **second owner client** for the same rows, beside the PWA.

**The box** (`api/panel_messages.py`): four device-key routes, each a thin door onto `jpanel.py`'s
own functions so the two owner clients cannot disagree —

| Route | Reuses |
| --- | --- |
| `GET /endpoint/messages/{device}?limit=N` — the owner's conversation with that twin, last N (≤ 8, the panel asks 4), oldest first: `id`, `mine`, `text` (≤ 255 B), `ms`, `played`, `voice`, and the `tag` | `jpanel.thread_with` (the PWA thread's rows and `Message`), `screen_text` |
| `GET …/{id}/pcm` — 16 kHz s16 with `X-Jpanel-Sha256` | `jpanel.pcm_reply` (a twin's replay body) |
| `POST …/{id}/played` — only a message she sent him | `jpanel.mark_owner_played` (the PWA's stamp) |
| `POST …/send` — chunked 16 kHz s16, `X-Jpanel-Sha256: trailer` + the 32-byte digest after the audio | `jpanel.file_owner_voice` (the PWA's `send_audio` path: the same row, blob, transcript and nudge) |

**The gate is the talk's** (`panel_convo.owner_for_panel` → `panel_agent.agent_allowed`: a
`display` panel the owner switched on), not a flag of its own. That switch already lets the panel
act as the owner with his full agent, which is strictly more than reading and answering his post;
a second flag would be a second state to keep right, a PWA toggle and a migration for a combination
nobody has asked for. The twins' `jpet` panels are refused (403) whatever their row says, and reach
their own post only through `jpanel.py`'s panel routes. Everything runs on owner-scoped sessions;
the thread is the rows she sent him and he sent her, never twin-to-twin post; `jpanel_message`
carries no health, finance or location domain. No new table or policy, so no migration.

**The signal** rides the debug pickup the panel already makes every ~3 s: for a gated panel,
`GET /endpoint/debug/next` carries `X-Jpanel-Post`, eight hex of a hash over the owner's post (count,
newest filed, newest played, unplayed). The panel fetches both threads when it moves, so a twin's
message is on the glass within a pickup or two; an idle panel adds one aggregate query a pickup and
no request.

**The panel** (`svc_post`; `svc_audio`'s `TK_KIND_POST` / `TK_KIND_LISTEN`): hers on the left with
▶ and her length, his on the right with Sent / Played; a tap streams it through the reply's ring
and speaker start and marks it played (here, then on the box) — since 0.0.17 once its audio starts
(the unread rule, below); in 0.0.16 only when it played to its end with its digest
checked — the stamp to the box waits in an NVS-kept list of up to eight, retried at 5 s,
15 s, 60 s then every five minutes until the box answers 204 (a 404 drops it), so neither a reboot
nor an outage brings the badge back; KEY in her thread records up to 30 s to her, uploaded as it is recorded with the
digest after it. New: her tile's badge, the clock face's corner badge (never a pop-up), the thread
live if open, one chime with the chime switch on and outside the night window, never over a hold,
an answer, a play or an alarm. **No auto-play** — the owner's default: a message is heard when he
taps. Four messages a person, as before; streams only.

**The unread rule (owner, 2026-10-13; firmware-c6 0.0.17).** *"A message should no longer be
considered unread if I even hit play once on it."* 0.0.16 counted a message heard only once it had
played to its end with its digest matching, so a play he stopped part way, or one the radio cut
off, kept its dot and its badge. Now a message is heard once its audio starts — the box's answer
to the play, a fraction of a second after he taps ▶: the dot, her tile's badge and the clock
face's go, and the "played" stamp is queued for the box through the same NVS-kept retry list (it
goes out on the panel's one connection right behind the play's stream). A play stopped, cut off or
failing its digest after that still counts; the digest still judges the audio itself (a mismatch
counts as a failed play), not whether he heard it. A play the box never answers with audio
(unreachable, not a 200, no memory for the ring) and a press the panel refuses leave it unread
and send no stamp; a message already heard is never stamped again. In
the same image, from the same session: the thread stays where he scrolled while a message plays,
and the play's bar fills left to right.

### Progress on the panel, and the whole answer (owner, 2026-10-12)

*"When I ask it to do something like find the high tide near Port St John Florida, it sat there
for a while. Just saying transcribing and then said 'Still working on that. The answer will be in
the app.' … I want better updates for tool use and such to come through the panel and not to have
it direct me to my app."* — and then: *"We need to make it so that longer replies have a way to
read longer than 30 seconds."*

- **The turn streams (JT2).** The panel asks `POST /endpoint/converse?…&jt=2` and the box answers
  with typed, length-prefixed records (`backend/src/jbrain/api/panel_stream.py`): the transcript
  the moment Whisper has it (it replaces "Transcribing…"), a status line each time the agent starts
  a tool — the PWA's own step label in the present, with the call's target ("Searching the web:
  high tide Port St John FL…") — keep-alives at least every 8 s, then the answer and its speech.
  A panel that asks for nothing gets JT1 as before; the twins' pets are never offered either.
- **No 100 s cut-off.** The box follows the turn for up to 5 minutes with progress flowing; only
  then does the panel say it is still going (it finishes into the "Desk panel" chat regardless).
  The panel's own limit is "no byte for 45 s", not a total.
- **The whole answer.** The box sends up to 1,000 bytes of answer text and the panel keeps an
  agent's newest answer whole (older ones keep a bubble's 255 bytes). The speech is the whole
  answer, synthesised sentence by sentence while the panel plays the last, paced by the panel's
  speaker through TCP, capped at 5 minutes of audio; KEY or BOOT stops it at once and the box
  stops synthesising. Read-aloud off: text only.
- **KEY and BOOT while Jerv works.** KEY starts nothing new and says "Still working on the last
  one"; BOOT leaves the thread, the turn goes on, and its answer lands in the thread (and is
  spoken) while the panel holds the connection — it does, wherever the glass is.
- **The first word.** The amp and DAC are on and settled (200 ms of silence) and 150 ms of speech
  is queued before the first sample plays, every time; the snapshot reports both.

**The steps, not one line (owner, 2026-10-12, built in 0.0.14).** *"If Jerv is using tools we
should be able to see that too. Not just stuck with the thinking. I don't need to know the output
of the tools, but you could say using xtool or whatever the tool shorthand is."* And: *"The
thinking animation on Jerv is a little bit weird when it gets past 1/3 … it's kind of like
resetting."*

- **JT2 gains two records**, sent only to a panel that asks with `steps=1`: `STEP_START` (0x07:
  index u8, then a label of at most 60 bytes — the tool's own name and the call's target,
  "web_search · tide Port St John", "web_fetch · tide-forecast.com"; the same index again relabels
  it with a progress note) and `STEP_DONE` (0x08: index, ok). STATUS now carries only the phase
  between tools, "Thinking…" or "Writing the answer…". A panel before 0.0.14 skips the new types
  by their length, as JT2 always allowed.
- **The panel lists them under the question:** a finished step gets a ✓, a failed one a dim ✗,
  and the spinner sits on whatever is happening now — the running step, or "Thinking… 0:42" with
  a clock from the moment the question went. The last four are shown and the rest fold into
  "+N more". When the answer lands the list collapses to one dim line over it, "2 steps · 58 s",
  like the PWA's "Worked · 2 steps". Fixed buffers only: ~270 bytes of steps in the audio service
  and a stack copy on the UI task while drawing; nothing per step on the heap.
- **The spinner never restarts.** A status change used to rebuild the whole conversation and with
  it LVGL's spinner, whose grow-and-shrink sweep began again — and that sweep, shrinking a third of
  the way round, read as a reset even when it was not rebuilt. Now only a change to the messages
  rebuilds the list (`convo_redraw`, host-tested); a status, a step or the clock updates the
  working view where it stands and moves the one spinner between its rows. The spinner itself is a
  fixed quarter arc turning once a second whose angle comes from the tick, so even a spinner made
  later carries on exactly where the last one was. The sim checks both.

### The Clock opens its alarms on a two-second hold (owner, 2026-10-12)

*"When we're on clock and we press the screen and it pulls up the clock menu, I think that needs to
be a long press, maybe 2 seconds or more."* A plain tap on the face does nothing; a finger held
still for 2 s opens the alarms list. After 0.3 s a thin ring fills around the finger over the rest
of the hold — in the night colour when the night clock is on, steel by day — and lifting early
cancels it quietly. A tap still wakes a dark screen (the shell's pointer gate). The rule is the
conversation menu's own (`ui_kit/ui_hold.c`, one copy with the threshold as an argument),
host-tested: tap, 1.9 s, 2 s, a drag.

### Alarms (0.0.13)

Set in the PWA, switched on the panel (owner decision, mocks README). The design, all of it
host-tested or sim-proven since the owner is away from the panel:

- **Box.** `app.endpoint_panel_alarm` (migration 0236): per display panel, at most eight; minute of
  day in the panel's zone, the firmware's days mask (bit 0 Monday, 0 = once), on, a 24-character
  ASCII label without `"` or `\` (JSON escapes those, and the panel's fetch buffer is sized for
  eight full labels with nothing escaped — the table, the route and the PWA all refuse them), a
  sound (`chime`, `beeps`, `rise`). The cap of eight is the table's trigger, which takes a
  per-panel advisory lock before it counts, so two adds at once cannot both take the eighth
  place. RLS: the owner everything; a panel reads its own rows and may UPDATE only `enabled` on
  them (a trigger refuses any other column from a device key); a sibling sees nothing. Owner CRUD
  under `/endpoint/panels/{device_id}/alarms`; the panel's `GET /endpoint/alarms` (etag, 304) and
  `PUT /endpoint/alarms/{id}` `{on}`, both 403 to anything but a `display`, so the twins' S3
  panels cannot use them. The owner's list carries the version the panel last ASKED with — which
  it sends only once that list is stored — so the PWA says "On the panel" only when it is, one
  poll after the panel took it.
- **Panel.** `svc_alarms` fetches on every update-loop pass (and right after boot), caches the list
  and the unsent switches in NVS, and fires from the UTC clock with the zone's own rule: a time in
  the spring-forward gap rings when the gap ends, a time in the fall-back overlap rings once, a due
  time up to 2 minutes late still rings (a reboot across it), later is `missed`. A snooze ends if
  its alarm is deleted, switched off or moved. The schedule and the zone are the UI task's alone;
  telemetry and the debug snapshot read a copy it publishes once a second and after every change.
- **A switch flipped on the panel while it is offline wins when it reconnects, even over a later
  PWA edit of that alarm's on/off.** The panel holds the switch until the box takes it, and the
  box applies switches as they arrive, not by when they were flipped. Chosen over carrying a
  panel-side timestamp: the panel's clock is the one that may be wrong after a long time off the
  network, and the owner can always flip it again from the PWA. Time, days, label and sound are
  the PWA's alone and are never overwritten by the panel.
- **Precedence: a ringing alarm beats everything** — dark screen, menu, talk ring, notice, a reply
  being spoken, a test tone: KEY snoozes, BOOT (short or held) stops, PWR cannot darken it; the
  shell ends the talk, the menu and the voice when it starts, and the audio task gives it the
  speaker. Stops itself after 10 minutes.
- **Debug.** `inject event=alarm_test` rings a test alarm through the whole path; the snapshot's
  `alarms` section and telemetry's `alarm=` token say what is set, what is next, what last rang
  and how it ended, and how the fetch went.

### Jerv wears the PWA's icon, in its blue (owner, 2026-10-12)

The home tile, Jerv's header and every Jerv glyph draw the PWA's own mark — the neuron in
`frontend/public/favicon.svg` — rasterised from that SVG's geometry by
`firmware-c6/tools/gen_jerv_mark.py` into A8 masks (one per glyph size, checked in, CI-checked
against the SVG) and recoloured `#7FA7C9`, which replaces the purple as Jerv's accent everywhere.
Contrast: 5.6:1 on the tile's tinted disc, 7.1:1 on the tile; Jerv does not appear on the night
clock's corner badge.

## 2. The push path: the box can tap the owner on the shoulder

The owner: *"an API … that just shows a pop-up of a message … so the machine can send this
panel messages that light it up and let me know to go check things … maybe even with a
response of choices, so you could ask a question with a yes or no."*

Anything on the box — Jerv, a workflow, an Ops alert — can put a notice on this panel and,
optionally, ask a question whose answer goes back to the sender. Three more owner decisions of
**2026-10-12**:

- **A picture.** *"I want to be able to send a whole picture as a notification. This way some
  push notifications can draw their own banner or picture or whatever and show that."*
- **A sound.** *"allow the push notification to have a short 8 second or less audio clip to
  play if it wants to."*
- **Hidden by a tap or by time.** *"these notifications could be pressed to hide or time out to
  hide."*

Mocked in `../mocks/jpanel-c6/` (boards 15-20: NotifyInfo, NotifyQuestion, ClockBadge,
NotifyPicture, NotifyBanner, NotifyAudio; the live panel's *Send a test message* tweak sends
every kind).

### What a notice is

| part | |
|---|---|
| sender | shown as who it is from: "Jerv", "Ops", "You", "Debug", or a workflow's own name (`workflow:nightly_backup` → "Nightly backup") |
| title | one line, ≤ 60 bytes — also what the clock face's badge and the log name it by |
| body | ≤ 300 bytes, may break lines |
| choices | 0-4 answers of ≤ 16 bytes; none is a plain notice. The tapped one goes back to the sender |
| picture | optional, PNG/JPEG/WebP from the sender, fitted and encoded on the box (below) |
| sound | optional, ≤ 8 s: a clip from the sender, or words the box speaks in the PWA's read-aloud voice |
| chime | chime as it arrives (never inside quiet hours: a refusal is never bypassed) |
| hide after | 3-600 s once shown, or none: up until tapped |
| answer window | how long an answer is accepted once the panel has it (30 s - 24 h, default 15 min) |

**Picture modes.** `full`: the whole 480×480 glass, the picture contained and letterboxed on
black, only a small sender chip and the hide hint over it. `banner`: 480 wide across the top,
as tall as its shape makes it up to 240 px (a taller one is contained in 480×240), with the
title, body and answers below. **The default (`auto`) is `full` when there is no body and no
choices, `banner` otherwise** — a picture with nothing beside it is the message, and one with
words is a header over them. A sender that draws its own banner at 480 px wide gets its
pixels exactly (no rescale).

**Hiding.** A plain notice or a picture hides on a tap anywhere (`dismissed`). A question hides
only by an answer, or by BOOT without one (`dismissed`) — a stray tap must not throw away a
question. With `hide_after_s`, a thin line along the bottom edge drains to the left over that
time and the notice hides itself when it runs out (`expired`); the hint names the seconds left.
A sound plays once as the notice appears (a speaker row with its progress; the picture mode
plays it under the picture), never over an alarm, and stops when the notice hides.

### The box side (wave C3b, **built 2026-10-12**)

One table, `app.panel_notice` (migration 0235; `test_panel_notice_rls.py` pins owner / self /
sibling, CLAUDE.md #3):

| column | |
|---|---|
| `id`, `device_id` | which panel (the panel's principal) |
| `sender` | `jerv` \| `ops` \| `owner` \| `debug` \| `workflow:<name>` |
| `title`, `body`, `choices` | as above; a CHECK function holds choices to ≤ 4 strings of 1-16 bytes |
| `chime`, `hide_after_s`, `ttl_s` | as above (`ttl_s` is the answer window) |
| `image_mode`, `image_w`, `image_h`, `image_sha256`, `image_bytes` | the picture as the panel takes it — the encoded stripes, in the blob store (CLAUDE.md #2) |
| `audio_sha256`, `audio_ms` | the sound as 16 kHz mono s16 PCM, in the blob store; ≤ 8000 ms |
| `created_by` | the token's label, `owner (…)`, `jerv (session …)` — what sent it, on the record |
| `state` | `queued` → `delivered` → `shown` → `answered` / `dismissed` / `expired` |
| `answer` | the tapped choice's label; set only with `answered` |
| `created_at`, `expires_at`, `delivered_at`, `shown_at`, `finished_at` | `expires_at` is the pickup deadline (60 s) while queued, the answer deadline once delivered |

- **RLS.** The owner (and every box-side sender, which runs as the owner) does everything. A
  panel reads only its own live notices, may only move them forward, and has no INSERT or
  DELETE policy. A trigger holds what RLS cannot: for a panel, everything a notice says is
  immutable, an answer must be one of its own choices byte for byte, and the clock columns are
  the box's — delivery, not the panel, starts the answer window.
- **The blobs** are registered in `jbrain.blob_refs`; finished rows are swept 24 h on, each
  blob only once nothing else on the box holds the same bytes.
- **One service**, `jpanel/notice_service.py` (`app.state.panel_notices`), behind every sender:
  the owner's route, the debug console, Jerv's tool, and any Ops alert or workflow step that
  calls `NoticeService.send(sender="ops" | "workflow:<name>", …)` in-process.

**The routes.**

| | |
|---|---|
| `POST /api/panel/notify` | owner. The notice as JSON (`title`, `body`, `choices`, `chime`, `hide_after_s`, `ttl_s`, `image_b64` + `image_mode`, `audio_b64` or `say`, `sender`, `device_id`) → `202 {id, device_id, panel, state: "queued", expires_in_s: 60, image, audio_ms}`. `device_id` omitted: the one panel that takes notices (409 `which_panel` with the list when there are several). |
| `GET /api/panel/notify/{id}?wait=` | owner. State and answer; `wait` (≤ 25 s) holds until it is answered, dismissed or expired. |
| `POST /api/debug/panel/{device_id}/notice`, `GET …/notice/{id}?wait=` | the same for a debug token (sender `debug` unless named), so a Claude session can put a test notice on the glass and read the tap. `debug-connect.sh panel-notify`. |
| `GET /api/endpoint/notice/next` | panel key. The oldest queued notice as a JN1 stream (`application/x-jpanel-notice`), marked delivered; 204 when none. |
| `GET /api/endpoint/notice/{id}/body` | panel key. The same stream again for a notice it already collected (a download the radio tore). |
| `POST /api/endpoint/notice/{id}/state` | panel key. `{"state": "shown"}`, `{"state": "answered", "choice": 1}` (the button's index), `{"state": "dismissed"}`, `{"state": "expired"}` → 204; 404 for anything not its own live notice, 409 a move out of order, 422 a choice that is not one of the notice's. A repeated `shown` is fine (a retry). |

**The panel decides, and the box refuses at send time.** The owner: *"I could disable it from
the device … so if I don't want it bothering me I can turn it off, or not be on during
bedtime. It would reject it and return the API response that the device is not online or not
accepting push."* So a refused notice is never stored and never queued for later — it does
not exist, and the sender is told why at once, while it can still choose another route:

| answer | when | decided from |
|---|---|---|
| `202 {id, state: "queued", …}` | accepted; poll `GET …/{id}?wait=` for `shown` / `answered` | |
| `503 {reason: "offline", last_seen_s}` | the panel has not picked up in 30 s (`null`: not since the box started) | the last debug pickup (`debug_relay.last_seen`), checked first |
| `409 {reason: "no_notice_surface", board}` | online, but its pickup reports no notice settings — firmware before C3b's panel half | the pickup's query |
| `409 {reason: "not_accepting"}` | the owner switched notices off on the panel | `notif_accept` |
| `409 {reason: "quiet_hours", until: "06:30", until_at, tz}` | inside the night window and *Refuse during night hours* is on | `notif_quiet`, `night_from`/`night_to`, in the panel's own zone (`tz`) |
| `429 {reason: "busy", open: 4}` | four notices already open on that panel | the table |
| `413` / `415` / `422` | a picture, sound or field the panel cannot take (`image_too_big`, `image_too_complex`, `image_format`, `image_unreadable`, `audio_too_big`, `audio_too_long` + `ms`, `audio_format`, `audio_unreadable`, `too_long` + `field`, `too_many_choices`, `bad_field`, `bad_sender`) | the request |
| `503 {reason: "tts_unavailable"}` | `say` was asked for and the box cannot speak | |

**Where the switches come from.** The panel REPORTS them on the debug pickup it already makes
every 3 s, as query parameters: `GET /api/endpoint/debug/next?board=c6-amoled-216&na=1&nq=1&nc=1&nf=1290&nt=390&tz=5`
— `na` `notif_accept`, `nq` `notif_quiet`, `nc` `notif_chime` (0/1), `nf`/`nt` `night_from`/
`night_to` (minutes after local midnight, 0-1439), `tz` the panel's zone index (the
`time_logic.c` table, mirrored in `notice.PANEL_ZONES` and unit-checked against it). A pickup
without all six (an older image) clears what the box knew, so the box never decides from
switches a panel no longer has. The box keeps the last report in memory beside `last_seen`
(a restart costs one pickup) and decides from that, never from a guess. The panel is the source
of truth — a setting changed on the glass reaches the box within one pickup — and the PWA can
show it (`GET /api/debug/panel`'s `notices`) but not override it, which is the point: "don't
bother me" set at the panel has to hold. An empty window (`nf == nt`) is never quiet. A notice
accepted a moment before quiet hours begin is still shown (silently): the refusal is at the
door, not a recall.

**The pickup says when one waits.** Both answers of `GET /api/endpoint/debug/next` (the 204 and
the 200) carry `X-Notice: <n>` when n notices are queued for that panel; the panel then calls
`GET /api/endpoint/notice/next` until it answers 204. An idle panel costs nothing more than the
pickup it already makes; a notice reaches the glass within ~3 s.

### The picture pipeline

PNG, JPEG or WebP (anything else 415), ≤ 8 MiB and ≤ 40 MP (413 before it is decoded), EXIF
rotation honoured, transparency composited on black (an unlit AMOLED pixel). Fitted as above
(LANCZOS when it must scale), converted to rgb565 (`r>>3, g>>2, b>>3`) and cut into stripes:
**the runs of the `JPS1` screenshot** (`dbg_shot.c`: `(len-1, rgb565 LE)`, run ≤ 128, greedy
across rows, never across a stripe — a unit test holds the bytes identical to the screenshot
encoder's for the same rows), but box → panel, with as many whole rows per stripe as fit one
4 KB record and never more than 40 (the panel's 480×40 draw buffer, so one stripe decodes into
one buffer). **The encoded picture is capped at 320 KiB**: over it the colours are posterized
to 4 and then 3 bits a channel (a photograph's grain is what costs runs), and past that it is
refused `413 image_too_complex` with the size it reached. A drawn banner is a few KB; a
480×480 checkerboard is 43 KB; a photograph lands near the cap after posterizing.

### The sound pipeline

A PCM WAV (16-bit, mono or stereo, any rate) is read on the box with no dependency; anything
else (MP3, Ogg/Opus, M4A, FLAC, WebM) goes through ffmpeg (`jbrain.media`'s protocol
whitelist, the first 30 s at most, a temp file because MP4 keeps its index at the end), ≤ 4 MiB
uploaded. Either way it becomes **16 kHz mono s16le**, the panel's playback format, and over
**8000 ms** it is refused `422 audio_too_long` with its length. Or `say: "…"` (≤ 200
characters): the box synthesises it with the on-box TTS in the PWA's read-aloud voice
(`panel_agent.owner_voice` / `tts_params`, the owner's pronunciations included), trims Kokoro's
padding, and holds it to the same eight seconds.

### JN1: the notice on the wire, box → panel

`JN1`, then records until END. Every record is JT2's framing (`api/panel_stream.py`):

    type: u8 | length: u16 little-endian | payload: `length` bytes (≤ 4096)

    0x01 META    fixed 30 bytes, little-endian, then the sender's label (UTF-8, ≤ 32 bytes):
                   id        16  the notice's uuid, raw bytes (its text form goes in the state POST)
                   flags      u8 bit 0 chime, bit 1 question (≥ 1 CHOICE follows),
                                 bit 2 picture (STRIPEs follow), bit 3 sound (PCMs follow)
                   hide_s    u16 hide this long after it is shown; 0 = until tapped
                   ttl_s     u32 the answer is accepted for this long from now
                   img_mode   u8 0 none, 1 full (480×480), 2 banner (480 × img_h)
                   img_w     u16 0 without a picture
                   img_h     u16
                   audio_ms  u16 0 without a sound
    0x02 TITLE   UTF-8, ≤ 60 bytes, one line
    0x03 BODY    UTF-8, ≤ 300 bytes, may hold '\n'; absent when empty
    0x04 CHOICE  UTF-8, ≤ 16 bytes; one record per answer button, in order (index 0-3)
    0x05 STRIPE  u16 y0 | u16 rows | runs (u8 len-1, u16 rgb565 LE) covering rows × img_w
                 pixels exactly; top to bottom, contiguous from row 0, rows ≤ 40, run ≤ 128
    0x06 PCM     16 kHz mono s16le, ≤ 4096 bytes, an even count
    0x07 END     u8 0, then u32: the byte count of everything before this record

Order is fixed — META, TITLE, BODY?, CHOICE*, STRIPE*, PCM*, END. A reader skips a type it does
not know (its length says how far). A stream without END, or whose END count disagrees, was
torn and is discarded whole: a notice is shown complete or not at all (and fetched again from
`…/{id}/body`). The flags must agree with the records. `jpanel/notice.py` has the encoder and
the reference reader (`decode`), which refuses everything above.

**Test vectors** for the firmware wave: `backend/tests/fixtures/jpanel_notice/` — `plain.jn1`
(text only), `question.jn1` (two choices, chime, hide 30 s), `banner.jn1` (a 480×48 drawn
banner, three choices) and `full_sound.jn1` (a 480×480 checkerboard and 250 ms of tone), with
`vectors.json` giving each one's fields and the SHA-256 of its decoded rgb565 pixels (LE) and
PCM. `test_panel_notice.py` rebuilds every file from its description and fails when a byte
changes (`JPANEL_NOTICE_REGEN=1` rewrites them after a deliberate format change).

### Jerv, Ops and workflows

- **`panel_notify`** (Jerv; `web`-classed — opt-in, jerv-only, direct: the notice reaches only
  the owner's own panel on his own box, so there is no egress to approve): `title`, `body`,
  `choices`, `await_answer` (default on with choices) and `wait_s` (5-240, default 90), `say`,
  `image_id` / `attachment_id` (an image in the chat — one he attached, or one Jerv rendered,
  fetched or grabbed), `chime`, `hide_after_s`. It returns the tap ("He answered on the Jeff
  panel: Yes."), how it ended, or the refusal in words with "do not retry; tell him in your
  reply" — never an exception, so the turn goes on.
- **Ops and workflows** call `app.state.panel_notices.send(...)` with `sender="ops"` or
  `"workflow:<name>"` and get a `Sent` back or a `NoticeRefused` saying why; `wait(id, s)`
  awaits the answer. No Ops alert or workflow action sends one yet — the first is a follow-up,
  and a workflow action needs its `app.actions` row (WORKFLOW_ENGINE_PLAN §2).
- **The PWA** has a *Send test notice* control on the display panel's card (jpanel → Panels):
  a question goes to the panel and the tap — or the refusal, in words — comes back on the card.

### The panel side (C3b; plumbing built in 0.0.19)

One more service (`svc_notice`) and a shell overlay, not an app: it has to appear over whatever
app is open, the way an alarm does. Its work, from the box side above. **0.0.19 builds the first,
second (the access functions; the drawing waits) and last bullets and the state POSTs of the
third**, through the overlay as it stood (firmware README "What 0.0.19 adds"); the overlay's look —
the picture, the speaker row, the countdown line, a tap hiding a plain notice — is the next wave,
on the re-mocked design once the owner approves it.

- Report `na nq nc nf nt tz` on every debug pickup; on `X-Notice`, call `notice/next` until
  204, stream each JN1 record straight into the `storage` partition (littlefs: one file per
  notice — the picture and the sound never sit in RAM whole), check END, then POST `shown`
  when it is on the glass.
- Draw the picture from the file a stripe at a time through the draw buffer (one STRIPE is at
  most one 480×40 buffer); play the PCM from the file through `svc_audio`'s ring.
- Wake the screen, chime unless it is the night window (the box will have refused it at night
  anyway when *Refuse during night hours* is on), queue ("1 of 2"), BOOT dismisses without
  answering, a tap hides a plain notice or a picture, a choice POSTs `answered` with its index,
  the countdown line and `expired` for `hide_s`.
- An alarm outranks it; it outranks every app **except the clock face**, where it waits as a
  corner badge (bell + count) and opens only on a tap, so the time is never covered (owner,
  2026-10-11). A girl's message arriving on the clock face does the same.
- Host-test the reader against the fixtures; delete a notice's file when it finishes.

**Open for the owner:** the default `hide_after_s` (today: none — until tapped; 30 s for a plain
notice is the alternative); whether a question may carry a sound (allowed today); whether a
picture should be allowed through at night when quiet hours are off but the night clock is on
(today the box only refuses on *Refuse during night hours*, and a picture lights the whole
glass); whether a tap outside a question's buttons should hide it (today it does not).

## 2b. Auto-rotate

The owner: *"auto rotate … you should have hysteresis for which way is down via the
accelerometer … and a setting where I can turn it on and off."* The glass is 480×480, so all
four orientations fit with no relayout of the frame — only the content turns.

As built in 0.0.18 (firmware README "What 0.0.18 adds"; the owner's numbers of 2026-10-13 over
this section's first sketch of 20° and ~500 ms):

- **Down comes from gravity in the glass's plane** (QMI8658 X/Y, ±2 g, read at 8 Hz on the UI
  task's own passes while the screen is lit and the switch is on, powered down otherwise). The
  orientation changes only when that vector is **25° past the 45° boundary** into the new quadrant
  (i.e. within 20° of the new edge's axis) **and stays there 700 ms**, every reading of it.
  Between those, the current orientation holds: a panel leaned near 45° never flickers.
- **Flat means "keep what you have"**: when the in-plane magnitude is under 0.5 g (within 30° of
  lying flat, face up or down), nothing changes. **Moving** (a total more than 0.2 g off 1 g) starts
  the dwell over. A panel picked up off the desk does not spin to a guess.
- **Pure, host-tested** (`components/svc_orient/orient_logic.c`): the quadrant, margin, dwell,
  flat and moving rules, fed raw counts. The snapshot's `orient` section reports the raw reading,
  what it was seen as, the rotation and every change, so a wrong axis is visible from the box; the
  chip's axes against the glass are the `imu_axes` setting (HARDWARE.md §6), changeable with the
  debug `set`.
- **Rendering**: the CO5300 cannot swap axes (HARDWARE.md §2), and a second ~38 KB strip buffer for
  LVGL's own rotation is RAM this board does not have, so LVGL keeps drawing the upright screen (the
  glass is square: nothing is laid out again) and **the flush turns each strip**: 180° reverses it
  in place and sends it to the mirrored window (no memory); 90°/270° gather it into a 2.5 KB piece
  buffer, allocated only while the panel is on its side, a few glass rows at a time. Every window
  stays even-aligned. **Touch is mapped through the same rotation**, in the one place that maps it
  (`tp_logic.c`, after the calibration), so a tap lands where it looks.
- **The buttons stay where they are** — they are physical. Only the content turns.
- **Setting**: `Auto-rotate`, in the Brightness card, on by default; off is the panel's own
  upright at once. No sensor at boot: upright, and the switch drawn dead with the reason.

## 3. Waves

| Wave | What | Done when |
|---|---|---|
| **C0** ✅ | Bring-up image (Wi-Fi, box, OTA, rollback, telemetry) + the box flashing/serving two boards (`?board=c6-amoled-216`). | Proven on the board 2026-10-11. |
| **C1** ✅ | First USB flash from the PWA (Flash tab **board picker** from `GET /api/endpoint/boards` — built 2026-10-11, nothing preselected because both boards enumerate as the same USB device); prove one OTA (0.0.1 → 0.0.2) and one rollback on the real board. Answer the bring-up questions in the firmware README from telemetry. | The panel updated itself over Wi-Fi, twice, once refusing a bad image. |
| **C2** 🟡 | Display (CO5300, strip rendering into internal RAM), touch, buttons, PMIC, RTC — each a `svc_*`; `ui_kit`; `shell` with the home pages; `sim/`. Brightness is tested on the glass first: the S3's runtime brightness write is discarded by the same controller. **Landed 2026-10-11:** the framework (`ui_kit`, `shell`, `app_api`, fake `svc_*`, `svc_debug`, `sim/`, `host/`, the dependency check). **0.0.4 (built, awaiting the board):** the framework in the image; `svc_power` (AXP2101, PEK polled), `svc_buttons` (BOOT/KEY/PWR, PWR by GPIO18 or the PMIC), `svc_display` (CO5300 via `esp_lcd_sh8601`, 480×40 strips, runtime brightness tested between frames); the boot after a fault runs headless. **0.0.4 on the glass (measured):** home renders, runtime brightness and PWR dark/wake work, KEY and BOOT route as designed. **0.0.6 (built, awaiting the board):** `svc_touch` — the CST9220 polled every 20 ms over `svc_power`'s bus through a locked transfer API (still one owner), raw→display mapping, failure reported as `touch=failed:<step>`; the shell's real-pointer rules host- and sim-tested (tap within 20 px, sideways swipe ≥ 60 px, a stroke on a dark screen wakes and reaches nothing, a notice appearing under a finger ends its stroke). **Left:** confirming 0.0.5 installs and 0.0.6's taps, swipes and corners on the glass, then the RTC. | Home grid on the glass, tiles open and close an empty app, BOOT goes home, swipe pages. |
| **C2d** 🟡 | The debug relay (§4): `panel_debug_request` + RLS test, the command/result routes, settings-poll pickup and result upload, the `JPS1` → PNG decoder, the device's stripe handoff to the network task, Ops → Panels buttons. | A session holding the owner's token gets a snapshot and a screenshot of the live panel, and `reboot` is refused without `confirm`. **Box side built 2026-10-12** (table + RLS test, routes, decoder, CLI). **0.0.9 (built, awaiting the board):** the pickup every 3 s from the update loop (manifest first, backoff to 60 s on a silent box, none while the box is unreachable), one kept-alive session for every box request, commands on the UI task through a one-slot job (3 s, else `ui busy`; inline on a boot with no UI), the screenshot streamed chunked from the renderer (2 × 4 KB, ≤ 2 s of stillness, `net slow` / `capture torn` as JSON), the `esp_log` ring hook with clip-aware secret scrubbing, `dbg=` / `tls=` / `sec=` telemetry. **Left:** proving it on the glass, Ops → Panels buttons. |
| **C3** 🟡 | The apps (descriptors, models and partial views already in `components/app_*`), each independently: Settings (Wi-Fi, battery, Jerv read-aloud, brightness, update now, calibrate), Clock (+ alarm service + PWA alarm editor + box API), Jerv (streamed hold-to-talk), Thread ×2 (voice post, playback). **0.0.8 (built, awaiting the board):** Settings in the mock's final order with real battery, Wi-Fi (two-tap Disconnect, Connect) and Firmware (Update now through the update loop), every setting saved in NVS; the Clock face with generated 124 px digits, the day and date, the night clock and the corner badge; the time from the PCF85063 and the box's `Date` header, in a zone set on the panel; KEY to Jerv from anywhere. **0.0.10 (built, awaiting the board):** Jerv's streamed hold-to-talk end to end — the ES8311/ES7210 under `svc_audio` (one task, priority 1), the upload chunked while KEY is held, the reply frame (transcript, Jerv's words, then speech) over the S3s' `/endpoint/converse` with `persona=jerv`, the speech streamed through a 16 KB ring, text only when read-aloud is off, KEY/BOOT stops a reply, a speaker-volume slider, `audio=` telemetry, the debug `play_tone`. Jerv here is a tool-less spoken persona, not the owner's agent (§5). **0.0.11 (built, awaiting the board):** Jerv is the owner's real agent, Dave is Minecraft_Dave in a conversation of his own a level under the Minecraft tile (Compass a work-in-progress page), one shared conversation view (`components/convo`) with an instance per agent, Clear chat by a long press, the people's threads named and bound by device id from `GET /endpoint/siblings`, the reply waited for 130 s, the audio stack 4.25 KB and microphone gain 24 dB from the first talks' numbers. **0.0.12 (built, awaiting the board):** the agent turn streamed (JT2: transcript at once, a live status line per tool, keep-alives, no 100 s cut-off, 5-minute cap), the whole answer kept (1,000 bytes) and read in full sentence by sentence (5-minute cap), KEY while Jerv works says so and BOOT leaves the turn running, the speaker's start gated so the first word is never clipped, the Clock's alarms on a 2 s hold, the PWA's icon and blue for Jerv. **0.0.14 (built, awaiting the board):** each tool Jerv or Dave runs listed under the question (✓, a dim ✗, the spinner on the running one, "+N more" past four) and folded into "2 steps · 58 s" over the answer, "Thinking… 0:42" between tools, a spinner that never restarts, and KEY outside a conversation starting a new Jerv conversation (§1 "KEY routing"); on the box, the voice prefix gone from the owner's words and a panel turn shown live in an open PWA chat. **0.0.15 (built, awaiting the board):** the conversation switcher (§1 "Switching conversations") — New conversation and Recent conversations on the agents' long press, ten rows from `GET /endpoint/convo/{agent}/recent`, a switch through `…/select` and `…/messages`, the conversation's title in the thread's header, `agent_sessions.origin` (0239) and panel chats titled in-turn. **0.0.13 (built, awaiting the board):** alarms end to end — the box's table, RLS and routes, the PWA editor on the desk panel's card, `svc_alarms` (etag fetch, NVS cache, UTC + daylight-saving schedule, snooze, 10-minute cap, the switch queued back to the box), the Ringing screen over everything with three synthesised ramping sounds, `inject event=alarm_test`, `alarm=` telemetry. **0.0.16 (built, awaiting the board):** voice post with the twins (§1 "Voice messages with the twins") — the person threads from `GET /endpoint/messages/{device}`, a tap streams one and marks it played on the box, KEY in a twin's thread sends her a voice message (digest as a trailer), the pickup's `X-Jpanel-Post` signal, her tile's and the clock's badges and a chime. **0.0.17 (built, awaiting the board):** the owner's first use of 0.0.16 — her thread changed in place during a play (no rebuild, no scroll jump), the bar filling left to right from the speaker's sample count at 10 Hz, heard once its audio starts (the unread rule), a play's ring after its handshake (`int_min` 44 KB → ~56 KB expected) and `int_min_at` naming the phase of the low. **0.0.18 (built, awaiting the board):** touch calibration (mock `Calib.dc.html`, with four corner crosses rather than its sixteen, at the owner's word): three taps that agree per cross, BOOT cancelling, an affine least-squares fit in the glass's own frame over the 0.0.7 mapping, refused when degenerate, wild or scattered, kept in NVS (`jpanel`/`tp_cal`) and applied before the rotation, Reset calibration; and a play's own 6 KB ring. **Left:** Listen on a stored reply, the night clock's hours from the PWA. | Each app's mock states reproduced on the glass and in `sim/`. |
| **C2r** 🟡 | Auto-rotate (§2b): QMI8658 read in a service, `orient_logic` with host tests, software rotation, touch mapped through it, the Settings switch. **0.0.18 (built, awaiting the board):** `svc_orient` (no task of its own: the UI task's passes at 8 Hz), the 25° / 700 ms / 0.5 g rules host-tested, the flush turning each strip (180° in place, 90°/270° through a 2.5 KB piece buffer), touch through the same turn, `imu_axes` for the chip's unknown axes, `home-turned` and `clock-upside-down` in `sim/`. **Left:** the chip's axes against the glass, which only the board can settle (HARDWARE.md §6). | The panel turned on its side reads upright; leaned at 45° it does not flicker; flat it holds. |
| **C3b** 🟡 | The push path (§2): `panel_notice` + RLS test, the notify routes, the Jerv tool, `svc_notice` and the overlay; pictures, sounds and hide-by-tap-or-time (owner, 2026-10-12). **Box side built 2026-10-12:** migration 0235 + `test_panel_notice_rls.py`, `POST/GET /api/panel/notify`, the debug route + `debug-connect.sh panel-notify`, the panel's `notice/next`, `…/body` and `…/state`, the refusal table from the pickup's `na nq nc nf nt tz`, the picture (fit, rgb565 runs, 320 KiB cap) and sound (16 kHz s16, ≤ 8 s, WAV/ffmpeg/`say`) pipelines, `JN1` with fixtures, Jerv's `panel_notify`, the PWA's *Send test notice*. **Panel half: plumbing built (0.0.19, awaiting the board)** — the pickup's report, `X-Notice` → `notice/next` until 204, each JN1 stream checked as `decode()` checks it and written record by record to its own file on `storage` (littlefs; torn or refused → discarded and fetched again from `…/{id}/body`, three tries), at most four open, files deleted when finished and swept at boot (whole ones re-queued), the existing overlay fed the real text and choices, `shown` / `answered` / `dismissed` / `expired` retried from an NVS list, `hide_s`, the sound played once from the file through `svc_audio`'s ring (never over an alarm), `svc_notice_stripe` for the picture, the reader host-tested against every fixture. **Next:** the overlay restyled to the re-mocked design (the picture drawn, the speaker row, the countdown line, a tap hiding a plain notice) once the owner approves it. | Jerv asks a yes/no on the panel and acts on the tap. |
| **C4** 🟡 | Minecraft. **0.0.11:** the tile is a menu of Dave (the real Minecraft_Dave, a conversation of his own) and Compass (work in progress). **0.0.12:** Dave's talks stream like Jerv's (status line, the whole answer, read in full). | Compass scoped and built. |

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
  capture aborts with `net slow` rather than stall a frame. (As built in 0.0.9: the UI is inside
  one render for the whole capture, so the bound is the total — 2 s — with 250 ms per wait,
  because one TCP ACK held by Wi-Fi power save can take ~100 ms; power save is off while the
  stream goes up.) On the wire the mock's screens are
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

**The box side** (wave C2d; **built 2026-10-12**, the panel half lands in 0.0.9). As built,
which differs from the sketch above in three places: the pickup is a route of its own rather
than a field on the settings poll, because the C6 has no settings poll yet (it polls the
manifest every 60 s, too slow for a debug session) — 0.0.9 calls it on a short interval, and it
can fold into the settings poll when the C6 grows one; the request is columns rather than one
`cmd` jsonb; and the screenshot is stored decoded (a PNG digest in the blob store, CLAUDE.md #2),
not as the raw stream.

| | |
|---|---|
| `panel_debug_request` (migration 0231) | `id`, `device_id` (the panel's principal), `command`, `args` (flat jsonb, ≤ 512 B), `confirm`, `created_by` (the token's label and id), `created_at`, `expires_at`, `picked_up_at`, `state` `queued` → `sent` → `done` / `expired`, `result` (jsonb), `png_sha256`, `result_at`. A request not picked up in 60 s expires; once picked up the panel has 120 s to answer; finished rows and their PNGs are swept 15 minutes on (lazily, by the owner routes, through `blob_refs`). |
| RLS + trigger | The owner (and the debug console, which runs as the owner) does everything. A panel (`device_key`) reads only its own `queued`/`sent` rows inside their deadline and may move them only `queued` → `sent` → `done`; it has no INSERT or DELETE policy. A trigger holds what RLS cannot: for a panel the request columns are immutable and the timestamps are the box's (pickup sets the answer deadline). `test_panel_debug_request_rls.py` pins owner / self / sibling. |
| `GET /api/debug/panel` | every live panel: `board` it last picked up as, `last_seen_s`, `online`, the commands its last `help` published. |
| `POST /api/debug/panel/{device_id}/command` | `{command, args, confirm}` → `202 {request_id}`; `409 needs_confirm`, `503 offline` (+ `last_seen_s`), `422 unknown_command`/`bad_arg`, `409 unavailable` (its `help` says unbound), `409 no_debug_surface`, `429 busy`. The whitelist is checked at the box from a copy a unit test pins to `dbg_cmd.c`. |
| `GET /api/debug/panel/{device_id}/result/{request_id}?wait=` | long-poll ≤ 25 s → `{state, result, screenshot}`; `GET …/screenshot/{request_id}.png`. |
| `GET /api/endpoint/debug/next?board=` | panel key → `200` the flat request `dbg_cmd_run` takes (`{"id", "cmd", …args, "confirm"?}`), marked `sent`; `204` when nothing waits. Calling it is the panel's proof of life for the 30 s offline rule. |
| `POST /api/endpoint/debug/{request_id}` | panel key → `204`. `application/json` (≤ 64 KiB): `dbg_cmd_run`'s output. `application/octet-stream` (≤ 1 MiB, chunked fine), `screenshot` only: the `JPS1` stream; one that does not decode is recorded as `ok:false, err:"bad_stream"` and answered `422`. `404` for anything not this panel's request in `sent`. |

Every command is logged with the token's label (`debug.panel_command`), so what a session did to
the panel is on the record. The PWA's **Ops → Panels** gets Snapshot and Screenshot buttons on
the same routes, so the owner can look without a session too.

## 5. Open questions (owner or hardware)

- Thread messages from this panel: do they reach the girls as **Dad** (the owner's
  principal), or as a third panel? The twins' "send a message → the other panel" rule
  assumes exactly two panels and must become addressed either way.
- ~~Jerv on the panel: a session of its own, or the owner's current Jerv chat in the PWA?~~
  **Closed 2026-10-12 by the owner** — the full agent, in a thread of its own ("Jerv on the panel
  is the real Jerv", §1).
- Fonts: settled in 0.0.8 — the 92/124 px clock digits and the missing punctuation are generated
  with `lv_font_conv` and vendored (`firmware-c6/tools/gen_fonts.sh`).
- The panel's time zone: the box keeps the owner's (`owner_timezone`) but does not tell the panel;
  0.0.8 sets it on the glass (default New York). Should the manifest carry it?
- The hardware list in `../../firmware-c6/README.md` (PMIC rails for LCD reset and the speaker
  amplifier, which button is which, strapping pins).
