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

## What the first images were for

**Making every firmware after them arrive over Wi-Fi.** The discipline is the one
`../firmware/README.md` ("What this first image is for") set for the twins' panels, and it held:
display, touch, audio and PSRAM all arrived there by OTA onto a unit that had already proved it
could take one.

The bring-up image (0.0.1-0.0.3) did exactly six things, and every image since still does them
first:

1. reads its provisioning from NVS;
2. joins Wi-Fi;
3. fetches the manifest from the box — which is also the **rollback gate**: a freshly OTA'd
   image marks itself good only after reaching the box, and gives its slot back after five
   failed tries over ~100 s;
4. posts telemetry (the heartbeat) at boot, every 15 minutes, and straight after a failed install;
5. asks for a newer image every minute and installs it;
6. reports the version it is running — from `version.txt`, compiled into the app descriptor.

Deliberately absent from it: **display, touch, audio, IMU, RTC, SD card, buttons and the PMIC.**
Each is the class of fault that ends in a boot loop. They arrive over the air, one at a time,
onto a panel that has already taken an update (0.0.4 brings the first three of them). **Check `bootloader.bin` is byte-identical before
shipping each one** — that is what makes it a pure app OTA whose rollback lands on the same
bootloader.

A minute between checks, rather than the S3's fifteen, because this panel sits on the owner's
desk and its next dozen images are bring-up steps: an update should land while he is still
looking at it. That is one TLS handshake a minute against the box, which is nothing.

### The 0.0.3 probe: the board answers what the documents could not

On the board since 0.0.1/0.0.2 (proven 2026-10-11): USB flash from the PWA, Wi-Fi 6, TLS to the
box, and an OTA kept across a reset. 0.0.3 adds `main/probe.c`, **read-only**, started only
after the rollback gate: it scans the I2C bus, reads the AXP2101's status and every rail's
enable/voltage register, and watches GPIO9/10/18 as plain inputs. Its findings ride the
telemetry `note` (`i2c=… axp=reg:val,… lvl=g9,g10,g18 edges=gN=level@deciseconds …`), and a
button change wakes the loop so the box hears a press within seconds — which is how the
left/middle/right order and GPIO18's polarity get settled with nothing but a finger on the
panel. It writes no register and drives no pin, so it cannot leave the panel dark; it
releases the bus afterwards because the C2 power service must be that bus's only owner.
`HARDWARE.md` holds the researched facts these readings confirm or correct. 0.0.4 replaces it
with the real services; its answers are in `HARDWARE.md` "Measured on the board".

## What 0.0.4 adds: the screen

The first image that drives the glass: the AXP2101, the buttons, the CO5300 under LVGL 9.2.2,
and the framework's shell with every app over the **fake** services (no touch, audio or box data
yet — the home grid, BOOT, KEY and PWR are what can be exercised).

| Piece | What it is |
| --- | --- |
| `svc_power` (`esp/svc_power_axp2101.c`) | The I2C bus's ONE owner. Checks chip id 0x4A; enables the battery/VBUS/VSYS ADC and battery detection; sets pre-charge 50 mA, termination 50 mA and constant current **300 mA** (the BSP's 500, lowered until the cell is chosen); latches the PWR key's edges in IRQ register 0x49 and polls them every 50 ms. Every write is read back. |
| `svc_buttons` (`esp/svc_buttons.c`) | GPIO9 (BOOT, left) and GPIO10 (KEY, right), sampled every 10 ms and debounced over three samples; GPIO18 with its **internal pull-up**, pressed meaning "not the level it idled at", since its polarity is still being measured. |
| `svc_display` (`esp/svc_display_co5300.c`) | ALDO3 off/on as the panel reset, SPI2 in quad mode at 40 MHz (CS15, SCLK0, D0-D3 GPIO1-4), `espressif/esp_lcd_sh8601` 2.0.1 with the vendor's 480×480 init table, RGB565 byte-swapped, one 480×40 internal DMA strip (38.4 KB), every window rounded to even. |
| `main/ui_task.c` | The UI task: the one task that touches LVGL, the shell, an app or the panel io handle. |

**PWR** (middle) is screen dark/wake, **BOOT** home/back, **KEY** hold-to-talk to the open app,
as the shell defines them. A PWR press reaches the shell from whichever route fires first —
GPIO18 or the PMIC's latched press — and the second report of the same press inside 400 ms is
dropped; telemetry names every route that has ever fired (`pwr=`).

### The safety rules, and how each is held

- **Nothing new runs before the rollback gate.** `start_panel()` is called after
  `ota_confirm_health`, exactly where the 0.0.3 probe started: whatever the display does, this
  image has already proved it can reach the box and take the next one.
- **The boot after a fault runs headless.** `disp_guard.c` (host-tested) refuses the display
  when the last reset was a panic, an interrupt/task/RTC watchdog, a CPU lockup, a brownout or a
  power glitch. That boot reports `display=skipped-after-panic` with the stage the last one died
  in (`crumb=`), does not write the PMIC at all, and keeps polling for updates — so a display
  driver that faults every boot alternates with a boot that can take the image that fixes it.
  The next clean boot (an OTA's reset included) tries the display again.
- **An LVGL assert is a panic, not a spin.** LVGL's default handler is `while(1);`;
  `lv_port/lv_assert_trap.h` makes it a trap, which the rule above then catches.
- **Nothing new can starve the update loop.** The UI, power and button tasks all run at the main
  task's priority 1, so FreeRTOS round-robins them: even a UI task stuck in a loop would leave the
  update loop its share. The UI loop always sleeps at least a tick.
- **One task owns the panel, and brightness waits for the frame.** Flushes are synchronous —
  with one buffer LVGL could not draw ahead anyway — so no pixel transfer is in flight when the UI
  task returns from `lv_timer_handler`, and that is the only moment it writes `0x51`. On the S3
  the same correctly framed write sent mid-frame hung the renderer. A flush that does not complete
  in 1 s stops the UI (`display=failed:flush`) rather than queue onto a stuck bus.
- **Dark is brightness 0, never display-off** (svc_display.h), and the PMIC switches no rail but
  ALDO3, and that one only as the reset pulse, back on before anything else runs.

### Reading 0.0.4 from the box

The first report goes out while the panel is still starting; the UI asks for a second once it
has run for ~5 s (and a PWR press asks for one too). Everything is in the telemetry `note`:

| Token | Good | Bad, and what it means |
| --- | --- | --- |
| `display=` | `ok` | `failed:<step>` (`sem`, `spi`, `io`, `panel`, `reset`, `init`, `on`, `buf`, `lvgl`, `flush`): the step that failed, panel left alone. `skipped-after-panic`: the last boot faulted — read `crumb=` and `reset_reason`. |
| `lcdrst=` | `aldo3` | `sw-only`: the PMIC refused the reset pulse; the controller got a software reset only. |
| `frames=` / `flush_ms=` | climbing / ~20-30 | stuck at 0 with `display=ok`: LVGL never flushed. `flush_err` > 0: a strip never completed. |
| `bri=req/sent/state`, `bri_n=` | `50/128/sent` ~2 s after boot | `…/err`: the bus refused the write. `sent` while the glass did not step down from full brightness: the write is discarded — the S3's failure. **Only the eye can tell**; PWR must black the screen and bring it back. |
| `ui=`, `stage=`, `ui_age=` | `up`, 10-13, 0 | `ui_age` growing: the UI task is stuck at `stage` (1 LVGL, 2 shell, 10 events, 11 render, 12 brightness, 13 idle, 101-109 the display's init steps, 14 stopped). |
| `ui_heap=free/min` | free ≥ 100000 | under ~100 KB: the next TLS handshake or OTA is at risk. Compare `int_min` after an OTA. |
| `ui_stack=` / `main_stack=` | > 1500 / > 1500 bytes | small: a stack near overflow. |
| `pmu=` | `ok` | `failed:<reg>`: that register did not read back; `absent`: no AXP2101 on the bus; `readonly`: a fault boot. |
| `bat=` `vbus=` `vsys=` `chg=` | 0, ~5000, ~5000, 0 on USB with no cell | `vbus=0` on USB: the ADC is not enabled. `axp=16:…,30:…,62:…,64:…` are the VBUS limit, ADC enable, charge current and charge voltage as read. |
| `pwr=` | `pek`, `gpio18` or both after a PWR press | `none` after pressing PWR: neither route works yet. `g18idle=` is GPIO18's level with the pull-up. |
| `edges=` | `boot=dn@…`, `key=dn@…`, `g18=dn@…`, `pek@…` (deciseconds) | missing presses: debounce or wiring. |

The critical test is the **next** update: 0.0.5 must arrive and install with the display running
(`ota_err` empty, version moving on).

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
  `relinks`, `box_quiet_s`, and `note` — `board=jpanel-c6 image=display chip=esp32c6 rev=… dump=…`,
  because nothing else in the body says which board sent it, then each service's own tokens
  ("Reading 0.0.4 from the box" above). The note is bounded on its own, so a long one is cut
  rather than costing the whole report.

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
  next to Wi-Fi and TLS in ~300 KB of free internal RAM. LVGL renders into one 480×40 partial
  buffer in internal DMA memory and the CO5300 receives a window at a time.
- **Audio is streamed, both ways.** At 16 kHz, 16-bit mono a second is 32 KB, so ten seconds of
  speech is 320 KB — more than the chip has free. A recording goes to the box while it is being
  made; a reply plays through a small ring as it arrives. Nothing holds a whole clip.
- **No wake word.** ESP-SR does not support the C6, so there is no WakeNet or MultiNet and no
  `srmodels.bin`. Talking is **tap or hold-to-talk on the three top buttons**, and recognition
  happens on the box.
- **One TLS session at a time, still.** Each handshake takes ~40 KB of internal RAM. That was
  the S3's hardest-won lesson with PSRAM; without PSRAM it holds harder.

## Pin map

From the vendor's listing, corrected by `HARDWARE.md`. 0.0.4 drives the buttons, the I2C bus (the
AXP2101 only) and the display; the rest are for later waves.

| Function | Pins |
| --- | --- |
| BOOT button | GPIO9 |
| KEY button | GPIO10 |
| PWR button | GPIO18 |
| I2C (shared) | SDA GPIO8, SCL GPIO7 — AXP2101, PCF85063, QMI8658, CST9220, ES8311, ES7210 |
| IMU interrupts | INT1 GPIO16, INT2 GPIO17 |
| Touch | TP_RST GPIO11, TP_INT GPIO5 |
| Display (CO5300, QSPI) | CS GPIO15, SCL GPIO0, D0 GPIO1, D1 GPIO2, D2 GPIO3, D3 GPIO4; LCD reset is the AXP2101's ALDO3 |
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

The plan's §1 architecture, in the image since 0.0.4:

```
main/               Wi-Fi, OTA, rollback, telemetry; the UI task (ui_task.c); the app set (apps.c)
lv_port/            LVGL's assert handler: a trap, not a spin
components/
  app_api/          app_t, app_event_t, the svc_*.h interfaces, the redacting debug writer
  ui_kit/           theme tokens from the mock, the shared widgets, the talk ring, toasts
  shell/            home pages, the one open app, button routing, overlays
                    (shell_core.c: every decision, no LVGL, host-tested; shell.c: carries it out)
  svc_*/            one per chip or box route; in-memory implementations until each driver lands.
                    svc_power, svc_display and svc_buttons are real: the device builds their
                    esp/ driver and pure *_logic.c, the sim and host the top-level files
  svc_debug/        the debug surface: describer registry, log ring, commands, stripe screenshots
  app_jerv/ app_thread/ app_clock/ app_settings/ app_minecraft/
                    each a descriptor + a pure *_model.c + a view; app_thread is one component
                    instantiated per person by config
host/               the pure modules' suite
sim/                every component against LVGL on the host, headless to PNG
tools/check_app_deps.py   an app may require app_api, ui_kit and LVGL and nothing else
```

**How it is in the image.** LVGL `==9.2.2` (the sim's pin) is a managed component declared in
`main/idf_component.yml`, the sh8601 driver `==2.0.1` in `components/svc_display/`, and
`dependencies.lock` pins both by content hash so `dist/` stays reproducible. `main/apps.c` is the
one app set, compiled by the sim too. LVGL is configured in `sdkconfig.defaults` to match
`sim/lv_conf.h` (RGB565, Montserrat 14-30 and 48, no theme, a static 64 KB heap). The services
the drivers have not reached yet are still the fakes, on the device as in the sim. **Not yet
wired:** the debug ring's `esp_log` hook and the debug commands — they have no transport until
C2d, and a hook in every task's log path is risk with no reader.

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
  `applied` only once the UI task sent it between frames and the bus took it: the S3's CO5300
  discarded runtime brightness writes, and the glass cannot be read back over this bus at
  40 MHz, so the owner's eye is the last check. Dark is brightness 0, never display-off.

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
echo 0.0.5 > firmware-c6/version.txt               # bump FIRST: the version is compiled in
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
statically, before Wi-Fi and TLS allocate at runtime. 0.0.4, with LVGL and the framework: app
1.74 MB (58% of the slot free); 239 KB of DIRAM static, 64 KB of it LVGL's heap. At runtime the
UI adds a 38.4 KB strip buffer and a 12 KB task stack.

## Recovery

The C6's ROM bootloader is mask ROM, so the S3's runbook (`../docs/runbooks/ENDPOINT_RECOVERY.md`)
holds: hold **BOOT (GPIO9)** while resetting and the chip enters download mode over the same
USB-C, whatever is in flash. The worst realistic outcome is "needs the cable again".
