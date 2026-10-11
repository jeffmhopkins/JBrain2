# Jeff's jpanel firmware — ESP32-C6-Touch-AMOLED-2.16

> **Status:** Living · **Last verified:** 2026-10-11

Plan: `../docs/plans/JPANEL_C6_PLAN.md` — the waves, and the app-module architecture every
later image follows (§1). Approved screens: `../docs/mocks/jpanel-c6/`.

The firmware for the owner's own panel: a Waveshare **ESP32-C6-Touch-AMOLED-2.16** (sold as
Kctozo). A separate project from `../firmware/`, which is the twins' ESP32-S3-Touch-AMOLED-1.8
panels and is not touched by anything here. The two share a protocol with the box, not source:
the networking, OTA and config code was copied from `../firmware/main/` and trimmed, on purpose,
so a change for one board can never ship to the other by accident.

## The board

| | |
| --- | --- |
| SoC | ESP32-C6: one RISC-V core at 160 MHz, 512 KB HP SRAM, **no PSRAM** |
| Flash | 16 MB NOR |
| Radio | Wi-Fi 6 (**2.4 GHz only**), BLE 5, 802.15.4 |
| USB-C | the chip's own USB-Serial/JTAG (303a:1001) — flashing and the console |
| Display | 2.16" AMOLED, CO5300 over QSPI |
| Touch | CST9220 |
| Power | AXP2101 PMIC, battery connector |
| Audio | ES8311 codec (speaker), ES7210 ADC (microphones) |
| Other | PCF85063 RTC, QMI8658 IMU, TF card slot, three top buttons |

The 303a:1001 identity is the same one the S3 panels enumerate with, so nothing on USB tells
the two boards apart; the box's flasher is told which chip it is writing (`--chip esp32c6`) and
keeps the flash size, mode and frequency this build put in the bootloader header.

## What this first image is for

**Making every firmware after it arrive over Wi-Fi.** The discipline is the one
`../firmware/README.md` ("What this first image is for") set for the twins' panels, and it held:
display, touch, audio and PSRAM all arrived there by OTA onto a unit that had already proved it
could take one.

This image does exactly six things:

1. reads its provisioning from NVS;
2. joins Wi-Fi;
3. fetches the manifest from the box — which is also the **rollback gate**: a freshly OTA'd
   image marks itself good only after reaching the box, and gives its slot back after five
   failed tries over ~100 s;
4. posts telemetry (the heartbeat) at boot, every 15 minutes, and straight after a failed install;
5. asks for a newer image every minute and installs it;
6. reports the version it is running — from `version.txt`, compiled into the app descriptor.

Deliberately absent: **display, touch, audio, IMU, RTC, SD card, buttons and the PMIC.** Each is
the class of fault that ends in a boot loop. They arrive over the air, one at a time, onto a
panel that has already taken an update. **Check `bootloader.bin` is byte-identical before
shipping each one** — that is what makes it a pure app OTA whose rollback lands on the same
bootloader.

A minute between checks, rather than the S3's fifteen, because this panel sits on the owner's
desk and its next dozen images are bring-up steps: an update should land while he is still
looking at it. That is one TLS handshake a minute against the box, which is nothing.

### Why the PMIC is left alone

The AXP2101's power-on defaults are what power the ESP32-C6 in the first place — the chip is
running this code because of them. A wrong register write is the one bring-up mistake that can
cut the chip's own rail or the battery path, and it would survive an OTA rollback (the PMIC
keeps its registers across an ESP reset). Nothing this image does needs a rail the defaults do
not already provide, so it does not open the I2C bus at all. The PMIC arrives in the image that
needs the display's and the amplifier's rails, which is also the first image that can show what
it did.

### Why the console is USB-only

`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, with no UART console. The C6's UART0 defaults to
GPIO16/17, which this board wires to the IMU's INT1/INT2 outputs, so a UART console would drive
TX into an interrupt line. USB-Serial/JTAG is also the only port the connector reaches.

**Do not rely on it.** On a charger there is no host, and on the S3 opening the port reset the
chip before the fault could be seen. Telemetry is the channel that works where the panel lives.

## How it talks to the box

The same protocol as the S3 panels, so the box needs no new route for it:

- **Provisioning** is the `jbrain` NVS namespace the box's flasher writes: `ssid`, `pass`,
  `api`, `token`, optional `ca`, `name` and `role`. An OTA rewrites only an app slot, so this
  survives every update.
- **Auth** is the unit's `device_key`, sent as `Authorization: Bearer …`.
- **TLS** trusts exactly one thing: the box's Caddy root when `ca` is present (LAN name), the
  public CA bundle when it is absent (public hostname). Never both.
- **`GET {api}/endpoint/firmware?board=c6-amoled-216`** is the manifest and the health signal.
  The image URL it returns carries the same board id and is fetched exactly as given. The id is
  one constant, `BOARD_ID` in `main/ota.c`; the box's per-board table must use the same string.
  Without it the box answers for the S3 panels.
- **`GET {api}/endpoint/settings`** is not polled by this image. When a later one polls it, it
  must send the same `?board=` — the response's `fw_version` is per board.
- **`POST {api}/endpoint/telemetry`** is the heartbeat, in the S3's `TelemetryIn` shape:
  `version`, `uptime_ms`, `reset_reason`, the internal-heap readings (`free_heap`, `int_free`,
  `int_min`, `int_big`, `int_largest`), `wifi_reason` / `wifi_drops`, `ota_err` / `ota_tries`,
  `relinks`, `box_quiet_s`, and `note` — `board=jpanel-c6 image=bring-up chip=esp32c6 rev=… dump=…`,
  because nothing else in the body says which board sent it.

Each request is its own HTTPS session, closed before the next. The S3 panels moved to one shared
WebSocket (`../firmware/main/link.c`) because talk turns and polls overlapped; this image makes
one request at a time on one task, so there is nothing to share.

**It refuses another board's image before writing anything.** A box that predates `?board=`
ignores it and offers the S3 panels' `jbrain-endpoint`. `ota.c` reads the incoming image's app
descriptor — the first few KB — and refuses anything whose project name is not
`jbrain-jpanel-c6`, remembering the version so it is not fetched again every minute. Telemetry
says `ota_err: "wrong-board"`. ESP-IDF's own chip-id check would also refuse it, but only after
it had started erasing the inactive slot.

**No Wi-Fi is a retry, not an exit**, and a box that has been silent for ten minutes with the
link up gets a disconnect and re-join — the half-open link that kept an S3 panel silent for
eight hours with `wifi_drops: 0`.

## No PSRAM, no ESP-SR: what that forces later

The S3 panels lean on 8 MB of PSRAM and on ESP-SR. This board has neither, and that decides
the shape of every later wave:

- **The screen is drawn in strips.** A full RGB565 frame at this panel's resolution does not fit
  next to Wi-Fi and TLS in ~300 KB of free internal RAM. LVGL renders into partial buffers (a
  band of the screen, double-buffered for DMA) and the CO5300 receives a window at a time.
- **Audio is streamed, both ways.** At 16 kHz, 16-bit mono a second is 32 KB, so ten seconds of
  speech is 320 KB — more than the chip has free. A recording goes to the box while it is being
  made; a reply plays through a small ring as it arrives. Nothing holds a whole clip.
- **No wake word.** ESP-SR does not support the C6, so there is no WakeNet or MultiNet and no
  `srmodels.bin`. Talking is **tap or hold-to-talk on the three top buttons**, and recognition
  happens on the box.
- **One TLS session at a time, still.** Each handshake takes ~40 KB of internal RAM. That was
  the S3's hardest-won lesson with PSRAM; without PSRAM it holds harder.

## Pin map

From the vendor's listing. Recorded for later waves — **this image drives none of them.**

| Function | Pins |
| --- | --- |
| BOOT button | GPIO9 |
| KEY button | GPIO10 |
| PWR button | GPIO18 |
| I2C (shared) | SDA GPIO8, SCL GPIO7 — AXP2101, PCF85063, QMI8658, CST9220, ES8311, ES7210 |
| IMU interrupts | INT1 GPIO16, INT2 GPIO17 |
| Touch | TP_RST GPIO11, TP_INT GPIO5 |
| Display (CO5300, QSPI) | CS GPIO15, SCL GPIO0, D0 GPIO1, D1 GPIO2, D2 GPIO3, D3 GPIO4; LCD_RST appears to be an AXP2101 ALDO rail (**unverified**) |
| I2S | MCLK GPIO19, SCLK GPIO20, LRCK GPIO22, ASDOUT GPIO21, DSDIN GPIO23; PA_CTRL appears to be an AXP2101 ALDO rail (**unverified**) |
| TF card (SPI) | SCK GPIO0, MOSI GPIO1, MISO GPIO2, CS GPIO6 |

Two constraints the table implies:

- **The TF card shares the display's bus.** SCK, MOSI and MISO are the LCD's SCL, D0 and D1, so
  the card and the panel are two devices on one SPI host, and the display task that owns the bus
  (the S3's one-task-per-chip rule) owns the card too.
- **Several of these are strapping pins on the C6** (GPIO4, 5, 8, 9 and 15, per the datasheet):
  D3, TP_INT, SDA, BOOT and LCD CS. Nothing may hold one at the wrong level through a reset.

## Partition table

`partitions.csv` carries the full reasoning; in short:

| Partition | Offset | Size | Why |
| --- | --- | --- | --- |
| nvs | 0x9000 | 24K | the S3's offset, which the box's flasher hardcodes |
| otadata | 0xF000 | 8K | the S3's offset; written on every USB flash |
| phy_init | 0x11000 | 4K | |
| factory | 0x20000 | 4M | where a USB flash writes; never OTA'd |
| ota_0 / ota_1 | 0x420000 / 0x820000 | 4M each | three equal app slots |
| coredump | 0xC20000 | 128K | a panic's dump, kept across the reset |
| storage | 0xC40000 | 3.75M | app-written cache (littlefs); ends at 16M |

**Effectively permanent: changing it needs a USB flash.** The app slots get the space because they
are the only thing OTA can grow — anything that must change later has to ride the app image, and
an asset put in a data partition is frozen until the next cable. 4M is roughly twice a generous
estimate of the finished app (LVGL, Wi-Fi + TLS, an audio stream client, fonts) now that there
is no 3 MB speech library. The **coredump** partition is the S3's standing regret, reserved now
while it is free; this image already writes panics into it and reports `dump=1`. There is **no
`model` partition**, because there are no models.

## Layout: the app framework

The plan's §1 architecture is laid down beside the bring-up image but **not yet in it**:

```
main/               the bring-up image (Wi-Fi, OTA, rollback, telemetry) — what dist/ is built from
components/
  app_api/          app_t, app_event_t, the svc_*.h interfaces, the redacting debug writer
  ui_kit/           theme tokens from the mock, the shared widgets, the talk ring, toasts
  shell/            home pages, the one open app, button routing, overlays
                    (shell_core.c: every decision, no LVGL, host-tested; shell.c: carries it out)
  svc_*/            one per chip or box route; in-memory implementations until each driver lands
  svc_debug/        the debug surface: describer registry, log ring, commands, stripe screenshots
  app_jerv/ app_thread/ app_clock/ app_settings/ app_minecraft/
                    each a descriptor + a pure *_model.c + a view; app_thread is one component
                    instantiated per person by config
host/               the pure modules' suite
sim/                every component against LVGL on the host, headless to PNG
tools/check_app_deps.py   an app may require app_api, ui_kit and LVGL and nothing else
```

**Why it is out of the image.** ESP-IDF builds every directory under `components/`, and these
need LVGL and drivers the bring-up image does not carry; `CMakeLists.txt` excludes them, so
`dist/` stays byte-identical to what `main/` alone builds. **C2 switches it on**: delete the
exclude, add `lvgl/lvgl: "==9.2.2"` (the sim's pin) to a `main/idf_component.yml`, move
`sim/apps.c` into `main/`, have `main` require the shell, the apps and the services, and put
`dbg_log_chain(esp_log_set_vprintf(dbg_log_vprintf))` first in `app_main`. The whole framework
has been compiled and linked for the esp32c6 that way (LVGL as a managed component, Kconfig
fonts 14-30 and 48): a 691 KB image, 136 KB of static DIRAM before Wi-Fi.

**The rules the code holds itself to**, from the S3 panels and their review:

- **One task owns the panel.** Services never call LVGL or an app; they post to the shell's
  queue (`app_events_post` → `shell_pump` on the UI task), and the queue counts what it drops.
  Events carry values, never pointers into another task's memory.
- **One decision per question.** `shell_core.c` answers which overlay is up and where a button
  goes; `shell.c` asks it and keeps no copy. The overlay that is drawn is the one that takes
  presses, decided in one place. `KEY_UP` goes wherever its `KEY_DOWN` went, so a pop-up
  arriving mid-hold cannot strand an open microphone; a notice waits for the hold to end.
- **Only the open app's widgets exist.** The shell deletes the app's root after `close`, and
  `sim --lifecycle` proves every app — closed mid-hold included — leaves exactly the widget and
  timer count it found.
- **Nothing assumes the glass obeyed.** `svc_display` records a brightness and reports
  `applied` only once the display task confirmed it: the S3's CO5300 discarded runtime
  brightness writes. Dark is brightness 0, never display-off.

### Running it

```sh
make -C firmware-c6/host test                       # the pure modules, ~1 s
python3 firmware-c6/tools/check_app_deps.py         # the dependency rule
firmware-c6/sim/render.sh /tmp/jpanel-sim           # every mock state to PNG + the checks
firmware-c6/sim/build/jpanel-sim --state clock-night --png /tmp/night.png
firmware-c6/sim/build/jpanel-sim --state session --snapshot /tmp/snap.json
```

The sim needs only cmake and a C compiler; it fetches LVGL v9.2.2 at configure time (point
`FETCHCONTENT_SOURCE_DIR_LVGL` at a checkout to skip that). `--list` names the states, which are
the mock boards' `start` names. Each is reached the way the box would reach a live panel: fake
services set the world and **debug commands** (`open`, `inject`) drive the UI, and the picture
is the debug `screenshot` command's stripe stream decoded to PNG — so every render also tests
the debug path. `--only <app>` registers a single app, for working on one with no other present.
Fonts are LVGL's Montserrat; the 92/124 px clock digits are 48 px scaled, and `·`, `–`, `…`
are ASCII, until C3 generates the glyphs.

### Debugging a panel with no cable

`svc_debug` is how a session holding the owner's debug token reads and drives this panel through
the box (plan §4 has the box routes). A **snapshot** is every describer — the shell (open app,
page, overlay, key owner, queue depth and drops, the last events, widget and timer counts, and
the open app's own `describe`), each service, and `system` (heap, low-water mark, largest block,
reset reason, crash breadcrumb, coredump present, watched tasks' stack high-water marks). The
**log ring** keeps the last ~3 KB of `ESP_LOGx` lines. **Commands** are a whitelist: `help`,
`snapshot`, `logs`, `log_level`, `open`, `home`, `set`, `inject` (KEY/BOOT/PWR, touch, swipe),
`ota_check`, `report_now`, `screenshot`, and `reboot` — the only destructive one, refused
without `"confirm": true`. **Secrets cannot leave:** the writer every byte goes through refuses
the provisioning keys (`pass`, `token`, `ca`, any `*_key`/`*_token`…) and scrubs the registered
secret values from every string, log lines included; `host/tests.c` and `sim/render.sh` both
check a snapshot for the fake provisioning's password, device key and CA.

## Building and shipping

```sh
IDF_TARGETS=esp32c6 scripts/firmware-setup.sh     # once — ESP-IDF v5.5.5 + the RISC-V toolchain
echo 0.0.2 > firmware-c6/version.txt               # bump FIRST: the version is compiled in
. ~/esp-idf/export.sh && (cd firmware-c6 && idf.py fullclean && idf.py build)
scripts/firmware-dist.sh firmware-c6               # copies the built set into dist/ — it ships
```

`dist/` is committed and is the whole distribution, exactly as for the S3 panels: the box
flashes and serves these bytes off its own checkout. `CONFIG_APP_REPRODUCIBLE_BUILD=y` makes the
same source build the same bytes on any machine, and the `firmware-c6` job in
`.github/workflows/firmware.yml` fails the PR when `dist/` is not what the source builds.
**Every source change rebuilds the image, a comment included** — the app descriptor carries the
ELF's hash and the ELF carries line numbers.

Bring-up image 0.0.1: app 1.08 MB in a 4 MB slot (73% free); 149 KB of the 442 KB of DIRAM used
statically, before Wi-Fi and TLS allocate at runtime.

## Recovery

The C6's ROM bootloader is mask ROM, so the S3's runbook (`../docs/runbooks/ENDPOINT_RECOVERY.md`)
holds: hold **BOOT (GPIO9)** while resetting and the chip enters download mode over the same
USB-C, whatever is in flash. The worst realistic outcome is "needs the cable again".
