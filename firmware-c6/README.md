# Jeff's jpanel firmware — ESP32-C6-Touch-AMOLED-2.16

> **Status:** Living · **Last verified:** 2026-10-13

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
| `ui=`, `stage=`, `ui_age=` | `up`, 10-13, 0 | `ui_age` growing: the UI task is stuck at `stage` (1 LVGL, 2 shell, 10 events, 11 render, 12 brightness, 13 idle, 101-109 the display's init steps, 14 stopped, 15 a debug command — since 0.0.9). |
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

## What 0.0.6 adds: touch

The CST9220, under 0.0.4's rules: it starts after the rollback gate, only on a boot that may start
the display, on a priority-1 task, and a failure to start leaves the panel running on BOOT, KEY
and PWR with telemetry saying which step failed. 0.0.5 changed nothing but the version, to prove
an OTA installs with the display running.

| Piece | What it is |
| --- | --- |
| `svc_touch` (`esp/svc_touch_cst9220.c`) | Pulses TP_RST (GPIO11, low 20 ms, then 100 ms to boot), checks the chip answers at 0x5A, then **polls** register 0xD000 every 20 ms: a 2-byte write and a 10-byte read with a repeated start, at 400 kHz. `tp_logic.c` (host-tested) decodes the report — byte 6 must be `0xAB`, point 0 pressed is status `0x06` — and maps it to the glass: **x = 479 − rawB, y = rawA** (measured on the board in 0.0.6 — the vendor's mapping put every tap in the opposite corner; corrected in 0.0.7), clamped to the edge. Press, move and release samples reach the UI task through an 8-entry ring in which moves coalesce, so no press or release is lost however late LVGL reads. |
| The bus | Still `svc_power`'s alone. `svc_power_i2c_attach` / `svc_power_i2c_xfer` run one transaction under its lock with a timeout capped at 50 ms (touch asks for 20); the touch service never holds the bus handle, so it cannot forget the lock. |
| The shell's pointer | One LVGL pointer, fed by the debug command's synthetic stroke while one runs and by the touch ring otherwise; a press and its release that land inside one read period are both delivered (`continue_reading`). |

**Polled, not interrupt-driven.** TP_INT is GPIO5, a C6 strapping pin, and whether the chip holds
it low through a touch or pulses it once per report is not known. A poll needs no ISR and cannot
miss a pulse; Waveshare's own Arduino reader for this board polls the same register the same way.
GPIO5 is configured as a plain input — never driven, never pulled — and its level is counted
against the reports (`tp_int=`), so a later image can choose the interrupt from a measurement.
The vendor drivers' "2 ms" gap between the write and the read is a `vTaskDelay` at their 1 kHz
tick; this image uses the Arduino reader's repeated start instead.

**A sick touch chip cannot take the PMIC with it.** A transfer holds `svc_power`'s lock for at
most its 20 ms timeout and is not retried on the spot — the next poll is the retry. Five failed
reads in a row pulse TP_RST (which also frees SDA if the chip is the one holding it); three pulses
with no good read between them report `touch=failed:lost` and slow the poll to once every 5 s;
any good read puts it back to `ok`. Touch errors are counted as `tp_err`, never in the PMIC's
`i2c_err`.

**What a finger does** — decided in `shell_core.c` (host-tested) and carried out in `shell.c`:

- A **tap** is a stroke that stayed within 20 px (~1.6 mm) of where it went down; on home it
  opens the tile it started on. LVGL starts a scroll after the same 20 px, so "a tap" and "not yet
  a scroll" are one number.
- A **swipe** is at least 60 px and mostly sideways: it turns the home page and opens nothing.
  Anything between a tap and a swipe does nothing on home, so a short swipe never opens a tile
  and a mostly-vertical drag turns no page. On a scroll page a vertical drag scrolls (LVGL).
- **A finger on a dark screen buys the screen and nothing else** (the twins' rule): the stroke
  that starts dark wakes it, and none of it — press, moves or release — reaches a widget, however
  long the finger stays. A stroke that started lit is never cut, so its release goes where its
  press went. This replaces 0.0.4's full-screen "waker", whose deletion under the finger let the
  rest of the stroke click whatever was beneath it.
- **A notice that appears under a finger ends that stroke**, so its release cannot open the tile
  it started on behind the notice. The notice itself is full-screen and swallows presses.
- **BOOT** is still back, then home; **KEY** and **PWR** are unchanged.

`sim/render.sh` runs `jpanel-sim --touch`, a scripted finger through this exact path (each check
fails if its rule is removed). Calibration is a later Settings feature (the twins' lesson,
`../firmware/README.md` "Touch calibration"); 0.0.6 ships the raw mapping and reports raw and
mapped coordinates so its edges can be judged from real taps.

### Reading 0.0.6 from the box

The touch tokens follow the 0.0.4 ones in the `note` (the whole note stays under its 720-byte
cap with room to spare):

| Token | Good | Bad, and what it means |
| --- | --- | --- |
| `touch=` | `ok` | `failed:<step>` at start — `rst`/`int` (GPIO config), `bus` (no bus to borrow: the PMIC service never opened it), `i2c` (no answer at 0x5A after the reset), `task`: the panel runs without touch. `failed:lost`: it answered, then stopped (read `tp_err`, `tp_rst`). `off`: a fault boot, which starts neither display nor touch. |
| `taps=` | one more per finger-down (the wake stroke and swipes count too) | stays 0 while tapping: the chip reports no pressed point — read `tp_noack`. |
| `tp_last=` | `rawA,rawB>x,y` for the last four touch-downs, oldest first | a corner tap that does not land near its corner. Tap top-left, top-right, bottom-right, bottom-left: expect `x,y` near `0,0`, `479,0`, `479,479`, `0,479` (raw a,b near `0,479`, `0,0`, `479,0`, `479,479`). A swapped or mirrored pattern is the mapping; corners that stop short (e.g. never below 20) are the edge accuracy calibration fixes. From 0.0.18 `x,y` is after the calibration and the rotation (`cal=`, `tp_rot=`). |
| `tp_err=` / `tp_rst=` | `0` / `0` | climbing: failed reads, and how often the chip was reset. The PMIC's `i2c_err=` must stay 0 whatever these do. |
| `tp_noack=down/all` | `down` 0 or tiny | `all` counts reads without `0xAB` (normal while idle if the chip only acks fresh reports); `down` counts those inside a stroke — held over, up to 3 in a row. Large `down` with broken drags: strokes are being cut. |
| `tp_int=down/up` | (a measurement) | polls with GPIO5 low while pressed / while not. `down` near the press polls and `up` near 0: INT is a level, and a later image can sleep on it. |
| `tp_lat=last/max` | ≲ 35 ms | ms from the poll that saw a press to LVGL reading it; add up to 20 ms for the poll itself. Over ~100: the UI task is starved. |
| `tp_rd=` | a few hundred µs | the longest transfer, waiting for the lock included. ≥ 20000: timeouts. |
| `tp_drop=` | `0` | > 0: eight edges piled up unread, the UI task stalled. |

## What 0.0.8 adds: the agreed screens, with the panel's real state

0.0.7 put a finger on the glass; 0.0.8 makes the screens it reaches the ones the mock agreed
(`../docs/mocks/jpanel-c6/Main.dc.html`), filled from the hardware and the box wherever they
already answer, and saying plainly where they do not yet.

### Settings, card by card

In the mock's final order: Brightness, Battery, Notifications, Jerv, Firmware, Touch screen,
Wi-Fi last.

| Card | Real now | Still not real |
| --- | --- | --- |
| Brightness | The slider (10-100 %, 10 % floor, 5 % steps) reaches the glass on every tick through `svc_display` (the UI task records, sends between frames) and is **saved** and **restored at boot**. Auto-rotate is a saved switch. | Auto-rotate turned nothing until 0.0.18 ("What 0.0.18 adds"). |
| Battery | The AXP2101's readings: percent, voltage and charging when a cell is fitted, otherwise "No battery fitted · USB x.xx V". | — |
| Notifications | Three saved switches; the quiet-hours note names the night window. | Nothing polls the box for notices yet, so they decide nothing (C3b). |
| Jerv | "Read replies aloud", saved. | No audio, so nothing is read aloud. |
| Firmware | The running version (the app descriptor's). **Update now** really checks: it raises a request and wakes the update loop, which fetches the manifest at once and shows Checking → Already on the newest version, or Downloading x · N % → Restarting into x, or Update failed (why) — each from what the loop is actually doing. The boot after an install that marked itself good says "Updated". | — |
| Touch screen | — | Calibrate said it was coming in a later update, until 0.0.18 ("What 0.0.18 adds"). |
| Wi-Fi | The network the radio is on and its signal (`esp_wifi`), the state (Connected / Connecting / Off) from the driver's events. **Disconnect takes two taps within 3 s** (the first turns it rose: "Tap again to disconnect") and drops the radio at once; nothing re-joins until **Connect** or a restart. Connect is carried to the update loop, which re-joins. | — |

**Why the UI never touches the box or the radio's join itself.** The box connection belongs to
the update loop on the main task: one TLS session at a time, the S3's hardest-won rule. Update
now and Connect only set a flag and notify that task (`svc_box_update_now`,
`svc_net_connect`), and the loop reports back through `svc_box_set_phase` / the Wi-Fi events,
so the card shows what happened, not what was asked. Update now also skips the 15-minute retry
backoff a failed install earns — the owner asked. Disconnect is the one direct call:
`esp_wifi_disconnect` is safe from any task, and the flag it raises first is what stops
`net.c`'s handler re-joining.

### Settings are saved

`svc_settings` keeps every setting in NVS namespace **`jpanel`**, one `i32` per key named as
`svc_settings_name` (`brightness`, `tts`, `notif_accept`, `notif_quiet`, `notif_chime`,
`night_on`, `night_color`, `night_from`, `night_to`, `show_date`, `auto_rotate`, `tz`; later
`volume`, `mic_gain` and, from 0.0.18, `imu_axes`). The touch calibration (0.0.18) is a blob of its
own beside them, `tp_cal`. The box's provisioning (`jbrain`) is never written.

- **One writer.** Loaded on the main task before the UI starts; written only from the UI task,
  where every setter runs (the glass and the debug commands).
- **No flash wear from a slider.** A change only marks the key; the keys are written once the
  panel has been left alone for 2 s, and only those whose value differs from what flash holds
  (`settings_store.c`, host-tested). A slider dragged for a minute is one write.
- **Never a reason not to boot.** A missing namespace is "fresh" (every image before this one),
  an unreadable one is the defaults, a failed write is counted and reported. A value saved by a
  later image with a wider range loads clamped and is written back corrected.

### The time: the box's clock and the PCF85063

**Where the time comes from.** The box serves no NTP (nothing in `deploy/` runs a time server),
and a public pool is a peer this panel is not guaranteed to reach. What it always reaches is the
box, every minute, for the manifest — and every reply carries an HTTP `Date` header. So
`ota.c` catches the manifest reply's `Date` (RFC 9110 IMF-fixdate only; anything else is
refused) and `svc_rtc` steps the system clock when it is 2 s or more off, which is the header's
own resolution plus a request's latency. No new connection, no new port, no new failure mode.

**The PCF85063 (0x51)** holds UTC, reached through `svc_power`'s bus API (one owner) and only
from the main task. At boot, before Wi-Fi, it sets the system clock — so the face is right
before the box answers. It is rewritten when it held no time (its oscillator-stopped flag), when
the box had to correct the panel, or once a day. Only the two control bits that would stop the
clock or put it in 12-hour mode are ever written. Its supply is the AXP2101's RTCLDO, backed by
the battery: **with no cell fitted, a power cut loses the time** and the face shows `--:--` until
the box answers (a reset or an OTA reboot does not).

**Time zone.** The chip and the box speak UTC; local time is newlib's `localtime` with the POSIX
rule of the zone chosen on the panel, so daylight saving is computed on the panel. The zone is
a saved setting from a short table (`time_logic.c`: Honolulu … New York … London, Paris, Athens,
Sydney), **default New York**. **Setting it needs no terminal:** Clock → tap the face → the
**Time** card → **Time zone: …** steps to the next zone (a toast names it). The box keeps the
owner's IANA zone (`owner_timezone`) but does not send it to the panel yet; a manifest `tz` field
is the natural carrier (an open question for the box side).

### The clock

- **The face** is the mock's: a giant 24-hour time in Montserrat Bold at 124 px — a generated
  font, not 48 px scaled — with the **day and date** ("Mon 12 Oct", en-GB short) 40 px from the
  bottom at the face's colour and lower opacity. Its switch is on the Clock → alarms page, on by
  default, saved.
- **The night clock**: inside the night hours (21:30–06:30 by default) the digits drop to the
  chosen colour (red by default, amber, grey) at the mock's opacity, and the date dims with them.
  On/off and colour are saved switches on the alarms page. The hours are not settable yet (the
  PWA editor is a later wave), and the page says so.
- **The corner badge** (plan §2): while the face is up, a notice or an unheard message from a
  person waits as a bell and a count in the top-right corner, in the night colour at night (full
  strength when a notice is urgent). A tap opens the notice over the face, or the person's
  thread. The app declares it holds notices (`app_t.holds_notices`); the shell's overlay
  decision reads that, so what is drawn and what takes presses stay one decision.
- **The alarms** were in-memory samples in 0.0.8-0.0.12; since 0.0.13 they are the box's, set in
  the PWA, and they ring ("What 0.0.13 adds").

### KEY talks to Jerv from anywhere (plan "KEY routing")

KEY held outside a conversation — home, the clock face, Settings, Minecraft — is the **shell's**:
it opens the talk ring ("Jerv is listening", the timer, "Tap the screen to cancel · 1:00 max")
over whatever is open, with every app closed if need be. Inside Quorra's or Tamsin's thread KEY
is that thread's; on a ringing alarm it snoozes; in the Jerv thread it is Jerv's own. The routing
is `shell_core.c` (`sh_route_button`, `sh_declined`), host-tested; the release goes where the
press went whatever opened or closed in between, and a notice waits for the hold to end. BOOT,
PWR, going home or an alarm cancel the hold.

**There is no microphone yet** (0.0.10 brings it: "What 0.0.10 adds"), so nothing is recorded.
On release the panel opens the Jerv thread and adds a voice bubble of the length held that says
**"Not sent: this panel cannot record yet. Voice arrives in a later update."** — never a made-up
transcript. A hold under 0.6 s
is a knock ("Hold KEY the whole time you talk"); a tap cancels ("Cancelled — nothing sent"). The
sample conversations the sim draws are not loaded on the device: its threads start empty and say
so.

### Fonts

LVGL's built-in Montserrat stops at 48 px and has none of `·` `–` `—` `…` `→` `−`. 0.0.8 vendors
generated bitmap fonts in `components/ui_kit/fonts/`, cut with `lv_font_conv` 1.5.3 from the TTFs
in LVGL v9.2.2's own tree:

```sh
npm install lv_font_conv@1.5.3            # or let npx fetch it
firmware-c6/tools/gen_fonts.sh firmware-c6/sim/build/_deps/lvgl-src
```

The script holds the exact commands: Montserrat Bold at 124 and 92 px with only `0-9 : -`
(`--bpp 4 --no-compress --format lvgl`), and Montserrat Medium at every built-in size (14-30) with
only the six marks, each hung off the built-in font of its size as an LVGL fallback
(`ui_kit.c`, `ui_font`). Flash cost: 32.6 KB (124 px), 18.1 KB (92 px), ~0.3 KB per fallback —
54 KB in all.

### The safety rules, again

- **Nothing new before the rollback gate** but bookkeeping: `svc_net_start`/`svc_box_start`
  store two pointers and the Wi-Fi handler records a state. NVS settings, the RTC and everything
  on screen start in `start_panel`, after `ota_confirm_health`.
- **No new task**: the settings writer and the clock poll ride the UI task; the RTC and the box's
  time ride the update loop. Every task is still priority 1.
- **Persistence degrades, never blocks**: see "Settings are saved".
- **NVS writes from one owner**: `svc_settings`, on the UI task.
- **The PMIC is still read-only on a fault boot.** The clock chip is read on every boot (a read
  cannot leave anything changed) and written only from the box's time.

### Reading 0.0.8 from the box

The note grew three parts; its cap went from 720 to 960 bytes (the body from 1280 to 1536), and
a long note is still cut rather than costing the report.

| Token | Good | Bad, and what it means |
| --- | --- | --- |
| `set=` | `fresh:0` on the first boot of 0.0.8, `ok:N` after a setting was changed | `defaults:<err>`: NVS would not open, every setting is its default. `partial:<err>`: some keys did not read. |
| `bri=V/nvs` | `nvs` once the slider was moved and left | `dflt` after it was: the write did not land (read `set_err`). |
| `set_w=` / `set_err=n/<err>` | climbing by one per change / `0` | errors: a write or commit failed; the setting comes back at its old value after a restart. |
| `time=src:age` | `box:N` with N under ~60 (seconds since the box last set the time) | `rtc:-1`: the box has not been reached this boot, the chip's time is shown. `none:-1`: no time at all, the face shows `--:--`. |
| `step=n/s` | `1/0` after the first boot's sync, then rare | many steps, or large `s`: the panel's clock drifts or the box's does. |
| `rtc=` | `ok` | `unset`: the chip held no time (first boot, or a power cut with no cell) — it is written at the next sync. `absent:<err>`: no answer at 0x51. |
| `rtc_w=` / `rtc_err=` | `1` then about one a day / `0` | `rtc_err` climbing: writes to the chip fail. |
| `tz=` | the zone index (5 = New York) | not the owner's: set it on the Clock page's Time card. |
| `key=` | `jerv:N,app:N,snz:N,sent:N,short:N,cxl:N` — where KEY went | `jerv` stuck at 0 after holding KEY on home: the routing is broken. |

### What to check on the glass

1. Settings opens on Brightness; the slider dims the glass as it moves; set it, wait 3 s, power
   the panel off and on: it comes back at that brightness (`bri=…/nvs`).
2. Battery reads "No battery fitted · USB 5.xx V" on USB with no cell.
3. Firmware shows 0.0.8; **Update now** says "Already on the newest version" within a few
   seconds.
4. Wi-Fi (scroll to the bottom) shows the home network and a signal in dBm; one tap on
   Disconnect turns it rose, a second within 3 s goes Off; Connect comes back to Connected.
5. The clock face shows the right local time and "Mon 12 Oct"-style date; turn the date off on
   the alarms page; check the Time card says "Set from the box".
6. Hold KEY on the home screen: "Jerv is listening" with a running timer; let go after a few
   seconds: the Jerv thread opens with the "Not sent" bubble. A quick tap of KEY says "Hold KEY
   the whole time you talk".

## What 0.0.9 adds: the panel's half of the debug relay

The box side has been live since 2026-10-12 (plan §4, `../backend/src/jbrain/api/panel_debug.py`);
0.0.9 is the panel collecting requests and answering them, so a session holding the owner's debug
token can take a snapshot, read the logs, see the screen, press a button or reboot the panel with
nobody at it. Also: a long press of BOOT opens the Clock, and the conversations draw the mock's
microphone instead of a music note.

### The pickup, and what it costs

`GET /endpoint/debug/next?board=c6-amoled-216` every **3 s**, from the update loop on the main
task — the task that already owns every box request — in the time between manifest checks
(`dbg_sched.c`, host-tested):

- **The manifest goes first.** A due check (every 60 s, or at once on a wake) always runs
  before a pickup, so a debug session can delay an update by one exchange at most, and every
  exchange is bounded (8 s per request, one retry; 3 s for the UI's turn; 5 s for a capture).
- **A dead box is not hammered.** A pickup the box does not answer backs off — 6, 12, 24, 48,
  then every 60 s — and there are no pickups at all while the last manifest check failed: the
  manifest is the reachability probe, and it alone runs. Any answer resets to 3 s.
- **A session is served quickly.** After a command the next pickup is 0.5 s out, since a
  session usually asks again at once.
- **Each pickup is the panel's proof of life** to the box (a command is refused `offline` after
  30 s without one), and counts as the box being reached for the 10-minute re-join rule.

**One kept-alive HTTPS session** (`main/box_http.c`) now carries the manifest, telemetry and the
pickups. A fresh TLS session per pickup — 0.0.8's pattern — would be an ECDHE handshake and a
certificate check every 3 s on a 160 MHz core at the UI's priority, estimated at 0.2-0.4 s of CPU
each: ~10 % of the core, taken in round-robin from the UI. Kept alive, a pickup is two small TLS
records (a ~300-byte request, an empty 204). It is still ONE session: closed before the OTA
download opens its own, closed on a Wi-Fi re-join, and a request that fails on a session the box
has since closed is retried once on a fresh one — so at worst this is 0.0.8's transport. The
cost moves from CPU to RAM: the session's TLS buffers (~25 KB, mostly mbedTLS's 16 KB receive
record) stay allocated between requests where 0.0.8 freed them, so steady-state `int_free` is
that much lower; `int_min`, the handshake's peak, is the same peak 0.0.8 reached every minute.
`tls=` in the note counts handshakes and requests that rode the kept session, and times the
connects, so the estimate is measured on the first day.

Static RAM grows by 11.4 KB (measured from the two ELFs): the 6 KB answer buffer, the 3 KB copy
the log dump writes from (linked for the first time now that something calls it), the 768-byte
request, the capture's 0.6 KB state and the larger telemetry note. With 0.0.8's `int_min` at
~114 KB, expect ~100-103 KB — still above the ~100 KB line "Reading 0.0.4" draws, and the first
thing to read in 0.0.9's telemetry.

### Running a command

Every command runs through `svc_debug`'s dispatcher (`dbg_cmd_run`: the whitelist, `confirm`
for `reboot`, the flat parser) **on the UI task**, the one owner of LVGL: the main task fills a
one-slot job and the UI loop runs it on its next pass (≤ 50 ms), between frames. The main task
waits up to **3 s** and otherwise answers `ui busy`; a job the UI had already started is left to
finish on its own, and nothing is collected from the box until it has. On a boot **without** a
UI — after a fault, or a UI that stopped — nothing else touches LVGL, so the main task runs the
command itself: a snapshot and the logs work exactly when they matter most.

| Command | On the panel |
| --- | --- |
| `help` | The whitelist with each command's `destructive` / `idempotent` / `available`. |
| `snapshot` | Every describer: shell, services, `system` (heap, stacks, reset reason, crash breadcrumb, coredump). |
| `logs` | The RAM log ring (last ~3 KB of `ESP_LOGx` lines, oldest first) and how many lines were evicted. |
| `log_level` | `esp_log_level_set(tag, level)`, `tag` `*` for all, level 0-5; answers `max`, the compiled ceiling (3, INFO: higher levels are compiled out). |
| `open` / `home` | The shell opens that app / goes home, as a tile tap or BOOT would. |
| `set` | A setting by `svc_settings_name` and `value`, through the UI's own setter, so it is saved like a slider move. |
| `inject` | `event` `key_down`, `key_up`, `boot`, `boot_long`, `pwr`, or `touch`/`swipe` at `x`,`y` (swipe `value` = dx) through the virtual pointer — exactly where a finger would land. |
| `ota_check` | Update now's own path: the Firmware card shows Checking, and the loop checks once this answer is sent. `offline or busy` when the button would be dead. |
| `report_now` | Telemetry on the loop's next pass (seconds). |
| `screenshot` | The screen, streamed as `JPS1` (below). |
| `reboot` | Only with `"confirm": true`. Answers first, then restarts 0.3 s later. |

The answer is `dbg_cmd_run`'s JSON, POSTed as is (≤ 6 KB: anything larger is answered
`result too large` rather than truncated).

### The screenshot, without a framebuffer

The UI task re-renders the whole screen once (`svc_display_capture`) and each 480×40 strip is
run-length encoded **as it is flushed** — before the byte swap the CO5300 needs, so the stream
carries LVGL's little-endian RGB565 — into one of **two 4 KB chunks**; the main task streams
each full chunk to the box as a chunked `application/octet-stream` POST while the UI fills the
other. Nothing larger than a chunk is ever held: a strip is the display's own DMA buffer, and
the extra RAM is the 8 KB pair — allocated once the upload's handshake is over, so it never adds
to the handshake's peak, and freed after — and the 0.6 KB encoder.

The screen stands still for the capture — the UI is inside one render from the first strip to
the last — so it is bounded: **at most 2 s**, and no single wait for a free chunk over 250 ms.
Either limit aborts the capture as `net slow`; a strip that is not full-width or out of order
is `capture torn`. A failed capture is never finished as a stream (the box would decode a torn
picture): the upload is cut off without its last chunk, so the box records nothing, and the
reason follows as the JSON answer. Wi-Fi power save is off while the stream goes up — one TCP ACK
held a beacon interval at the access point would otherwise be most of the wait. The mock's
screens are 13-70 KB on the wire (the sim prints each).

### Logs, and the secrets that may not leave

`esp_log_set_vprintf` now feeds the log ring as well as the console (the 0.0.4 image left the
hook out). It allocates nothing, never blocks, and takes the ring's lock as an ISR-safe critical
section — interrupts are off for one copy of at most the 3 KB ring. ESP_LOGx itself is not for
ISRs; `ESP_DRAM_LOGx` and `ESP_EARLY_LOGx` print through the ROM and never reach it.

Every byte that leaves goes through `dbg_json`, which refuses the provisioning keys and scrubs
every registered secret value. The ring scrubs too, as each line is stored, because that is
where a line is clipped to 120 characters: a device key cut off at the limit is still most of
one, so a clipped line whose tail starts a secret has that tail replaced (`dbg_secret_scrub`,
host-tested). At boot `check_secrets` confirms the device key, Wi-Fi password and CA from NVS
are registered by value before the hook is installed or a command can run, and registers any
that are not (`sec=`).

### BOOT held opens the Clock (owner, 2026-10-12)

**Held 800 ms, BOOT opens the Clock from anywhere** — home, any app, over a notice (the face
keeps it as its badge) — the moment the threshold passes, without letting go. A shorter press
is home/back as before, but now fires on the **release**: only then is it known not to be a long
one, so a long press is never also a back. Held for a minute it fires once. On the Clock it does
nothing; on the talk ring or a ringing alarm it is plain BOOT (cancel, stop); on a dark screen
it only wakes. The press timing is `btn_hold_step` (`btn_logic.c`) and the routing
`sh_route_button` (`shell_core.c`), both host-tested; the debug `inject` takes `boot_long`.

### The microphone

The "Hold KEY to talk" footer and the voice bubbles drew `LV_SYMBOL_AUDIO`, a music note: the
symbol font has no microphone. `ui_mic` draws the mock's (a capsule, its cradle and stem) as
strokes, at 20 px in the footer and 18 px in a bubble.

### The safety rules, again

- **Nothing new before the rollback gate** but the transport the gate tests: the manifest now
  rides `box_http.c`, whose first request is a fresh session exactly like 0.0.8's. The log hook,
  the secret check, the commands and the pickup all start in `start_panel`, after
  `ota_confirm_health`.
- **No new task.** The pickup rides the update loop; commands and the capture ride the UI loop.
  Every task is still priority 1.
- **A debug failure cannot block OTA or telemetry:** the manifest check outranks a pickup, every
  exchange is bounded, and a dead box costs one 8 s attempt a minute at most.
- **Every buffer is bounded:** request 768 B, answer 6 KB, log line 120 B, ring 3 KB, capture
  chunks 2 × 4 KB, failure answers 192 B.

### Reading 0.0.9 from the box

The note's cap grew from 960 to 1280 bytes (the body from 1536 to 1856) for three new tokens.

| Token | Good | Bad, and what it means |
| --- | --- | --- |
| `dbg=ok:N,err:N,cmds:N,last:<cmd>/<status>,shot:<bytes>/<ms>` | `ok` climbing ~20 a minute; `last:…/204` | `err` climbing: pickups failing (read `box_quiet_s`). `last:…/404`: the answer came after the box's 120 s. `/422` on a screenshot: the stream did not decode (recorded as `bad_stream`). `/-1`: the answer never landed. |
| `tls=hs:N,reuse:N,hs_ms:N,max_ms:N` | `hs` a handful a day, `reuse` climbing with every request | `hs` climbing with `reuse`: the box closes the kept session each time — every request pays a handshake. `hs_ms`: the last connect (TCP + TLS + headers). |
| `sec=` | `ok` | `fixed:token,pass,…`: provisioning did not register those as debug secrets and this boot did (a regression to fix, not a leak). |
| `stage=15` | — | the UI was running a debug command (`ui_age` growing there: a command is stuck). |
| `edges=` | `boot=long@…` after a long press | — |

### Checking it from the box

From a session with the owner's token (`../docs/runbooks/DEBUG_ACCESS.md`):

```sh
scripts/debug-connect.sh panel-list                                   # online: true, board c6-amoled-216
scripts/debug-connect.sh panel-cmd c6 help                            # the whitelist, all available
scripts/debug-connect.sh panel-cmd c6 snapshot                        # system, shell, settings… no secrets
scripts/debug-connect.sh panel-cmd c6 logs
scripts/debug-connect.sh panel-shot c6 out.png                        # the screen as it is
scripts/debug-connect.sh panel-cmd c6 inject --arg event=boot_long    # the Clock opens
scripts/debug-connect.sh panel-cmd c6 reboot                          # 409 needs_confirm
```

## What 0.0.10 adds: sound, and talking to Jerv

The ES8311 (speaker) and ES7210 (microphones) come up, and KEY really talks to Jerv: hold it
and speak, let go, and the Jerv thread shows what the box heard and Jerv's answer — spoken
through the speaker too unless **Read replies aloud** is off. The plan's wave C3 "Jerv
(streamed hold-to-talk)".

### On the glass

- **Hold KEY** (anywhere outside Quorra's or Tamsin's thread, as 0.0.8 routed it): the rose
  ring, "Jerv is listening", the timer, and the ring now pulses with the microphone's level.
  The recording goes up to the box **while KEY is held**.
- **Let go**: the Jerv thread opens with the hold's voice bubble saying "Transcribing…". When
  the box answers, the bubble becomes what it heard and Jerv's reply appears beneath it, with
  "Speaking…" while its voice plays. Nothing the panel shows is made up: a hold the box heard
  nothing in says "Nothing heard. Hold KEY and speak."; one it could not answer says "Not
  answered: <why>"; one cut off mid-hold says "Not sent: <why>".
- **Too short** (under 0.6 s) says "Hold KEY the whole time you talk" and sends nothing; **a
  tap on the glass or BOOT** while holding cancels ("Cancelled — nothing sent"). Either way the
  upload is cut off before its end, so the box never answers it.
- **KEY or BOOT while Jerv is thinking or speaking stops it** (owner, 2026-10-12), in any app,
  and does nothing else: no new recording starts and the release sends nothing. Stopped while
  thinking, the bubble says "Stopped before Jerv answered"; stopped while speaking, the words
  stay and only the voice ends. The next press talks again.
- **No Wi-Fi** or **no microphone**: KEY says so in a toast instead of recording into nothing.
- **Settings → Jerv**: "Read replies aloud" as before, and under it a **Speaker volume**
  slider (10-100 %, saved, heard from the next reply). When a codec did not start the card
  says so in amber instead — "Sound did not start, so holding KEY cannot record", or "The
  speaker did not start: replies are text only" — and the panel is otherwise unchanged.
- The thread keeps the last eight messages in RAM (four exchanges), each up to 255 bytes of
  text, cut on a character with an ellipsis; a restart forgets them. There is no **Listen**
  button on a reply yet: speaking a stored reply again needs the box to synthesise text it
  already sent, which is a later change.

### The protocol: the S3s' route, with Jerv answering

The twins' panels talk on `POST /endpoint/converse`: raw 16 kHz mono s16 up, the reply's
speech back as raw PCM. This panel reuses it — same route, same device key, same audio
format, same box-side trim, Whisper and Kokoro — with **one backward-compatible parameter**
and no change to the S3s (`backend/src/jbrain/api/endpoint.py`, `_jerv_turn`):

`POST /endpoint/converse?persona=jerv&voice=true|false`

Why it had to change at all, each for a reason the S3 protocol cannot meet:

- **Who answers.** The route's prompt is the pet's, written for a four-year-old, and its turns
  are written to `app.pet_turn` — the parents' diary of what the twins told their pets. With
  `persona=jerv` the box answers with a short spoken Jerv prompt and writes no diary row. It is
  **not** the tool-using agent: that runs as the owner over his notes, and a device key that
  could reach it would make a stolen panel worth far more than the route's promise of "one
  conversation". This Jerv has no tools and no data and says so when asked for something that
  needs them (the plan's §5 open question — the panel joining the owner's real Jerv — is still
  the owner's to decide). It runs on the `pet.turn` model slot, the panels' cheap spoken one,
  not `agent.turn`'s, where a one-off call would evict the agent's primed prefix.
- **The words.** The S3s show no text; this panel shows what was heard and what Jerv said. The
  reply is a frame, `application/x-jpanel-turn`: `JT1`, a flags byte (bit 0: speech follows),
  the transcript and the reply as little-endian u16 lengths and UTF-8, then the speech as raw
  PCM to the end of the body (`jerv_frame`; `tk_frame_feed` reads it as it arrives).
- **Text only.** `voice=false` (Read replies aloud off) skips synthesis on the box and sends no
  speech, so nothing is downloaded that will not be played. A box whose speech fails sends the
  words with the flag clear rather than a 503: the answer matters more than the voice.
- **The upload streams**, so the box takes it chunked (it reads the whole body before
  transcribing, as for the S3s) and up to 61 s for this persona (the mock's 1:00 cap; the pet's
  35 s ceiling is unchanged). A hold cancelled on the glass ends as an upload cut off mid-body;
  the route now answers that with a quiet 204 instead of a `ClientDisconnect` error, for either
  persona.

Tested in `backend/tests/unit/test_endpoint_api.py` (`TestJervOnTheDeskPanel`): the frame, the
persona and slot, text-only never synthesising, words without a voice, silence, a dead model,
the two personas' memories kept apart, a minute-long hold heard whole, a cut-off upload.

### The pieces

| Piece | What it is |
| --- | --- |
| `svc_audio` (`esp/svc_audio_es.c`) | The **one owner** of I2S0 and both codecs. Brings up one full-duplex I2S0 (MCLK 19, BCLK 20, WS 22, DOUT 23, DIN 21; STD Philips 16-bit at 16 kHz, MCLK = 256 × fs) and `espressif/esp_codec_dev` 1.6.2's ES8311 (DAC only) and ES7210 (MIC1 + MIC2) drivers — the release the S3s ship, pinned in `dependencies.lock` — over **a control interface of its own that borrows `svc_power`'s bus** (`svc_power_i2c_attach`/`_xfer`, 100 kHz): no second I2C driver, no handle outside the lock. The amplifier is `svc_power_amp` (ALDO2): off once the codecs are up, on only while a reply plays. A codec that does not answer leaves the other working (`mic-only` / `speaker-only`) or neither (`failed:<step>`). |
| `svc_audio_talk.c` | The talk's one state, shared by the UI task (KEY, BOOT, bubbles), the audio task and the main task, every call under one critical section; the same file runs in the sim and the host suite over `svc_audio_fake.c`'s port, so the bubbles the sim draws are the panel's. |
| `talk_core.c` | Pure, host-tested: the state machine (record → send → play, knock, cancel, stop, failure mid-hold), the one-producer one-consumer byte ring, the reply frame reader, the two-microphone downmix and the level meter, the bubbles, the `audio=` token. |
| `main/talk_link.c` | The talk on the box connection: a chunked POST filled from the ring as the microphone fills it, then the reply read in slices — headers in 500 ms slices, the body in 250 ms ones — so a drop on the glass is let go within one, the words handed to the glass the moment they are in, the speech into the ring at the speaker's pace. |
| `main/box_http.c` | A stream may now ride the kept session (`reuse`) when it carried a request in the last 20 s — a talk cannot wait out a handshake while the microphone fills its ring, and the 3 s pickups keep the session warm; if opening on it fails before a byte of body is sent it is retried fresh. The reply-in-slices calls are new. |
| `app_jerv`, `shell` | The thread's bubbles, "Speaking…", and a redraw that keeps the reader where they were; `sh_route_button` gives KEY and BOOT over a reply to `SH_ACT_STOP_REPLY` (host-tested); a refused hold says why. |
| `svc_settings` | Two keys: `volume` (10-100, default 80, Settings slider) and `mic_gain` (0-37 dB, default 30 — 24 since 0.0.11 — debug `set` only). |

### The network: who goes first

The main task still owns all HTTPS, one request at a time, over the one kept-alive session.
**A talk outranks everything else on it**: KEY's hold wakes the update loop, which takes the
exchange before any manifest check, telemetry or debug pickup, and holds the connection for
its length. It is bounded — a minute of recording (+5 s backstop), 60 s for the box to answer,
then the reply's speech at the speaker's pace (the box caps it at 30 s) — and **after every
talk the loop makes its regular pass**, so a manifest check or heartbeat that fell due runs
next: an update or a report is at most one talk late, never starved. A debug pickup waits
like everything else; a request queued during a long talk may pass the box's 60 s pickup
window and expire, and is simply asked again. Wi-Fi power save is off for
the talk's length (the screenshot's lesson: an ACK held a beacon at the access point stalls a
32 KB/s upload into the ring's half second). A press while a request is already in flight
waits for it (usually ~100 ms on the kept session); the ring holds half a second, and anything
past it is dropped and counted (`over`), never blocking the microphone. **The UI never waits
on any of it**: it reads the talk's state and writes bubbles from what the main task left.

### Tasks

| Task | Priority | Stack | Why |
| --- | --- | --- | --- |
| `audio` (new) | **1**, the update loop's own | 3.5 KB (4.25 KB since 0.0.11) | It blocks on the I2S DMA for each 20 ms chunk, so it takes its round-robin share and no more; at priority 1 nothing new can starve the loop that takes the next image. Four 20 ms DMA buffers each way give it 80 ms of slack against the UI, the main task's TLS and touch at the same priority. Its high-water mark is in the snapshot (`audio.stack_free`), to be cut from a measurement. |

The UI, main, power, buttons and touch tasks are unchanged; every task is still priority 1.

### The memory budget

0.0.9 on the board: `int_free` ≈ 109 KB steady (the kept TLS session holds ~25 KB of it),
`int_min` ≈ 102 KB, `int_largest` 90 KB. What 0.0.10 adds, internal RAM throughout (there is
no PSRAM):

| | Bytes | When |
| --- | --- | --- |
| Static DIRAM (measured from the two ELFs: 263,704 against 254,260) | +9,444 | always: two 20 ms sample buffers (1.9 KB), the upload slice (1 KB) and the frame reader (0.5 KB), the conversations' larger bubbles (+2.3 KB: 8 per thread × 256 B text, was 12 × 96), the telemetry note's room for `audio=`, `esp_codec_dev` and the I2S driver |
| `audio` task stack + TCB | ~3,950 | from boot |
| I2S DMA: RX 4 × 320 frames × 4 B, TX 4 × 320 × 2 B, + descriptors | ~7,800 | from boot |
| Codec, data-interface and I2C device objects | ~1,500 (estimate) | from boot |
| **Idle total** | **~22.7 KB** | |
| The talk ring | 16,384 | per exchange: allocated at KEY down, freed once nothing touches it |
| TX DMA reconfigured to stereo while recording (full duplex shares one frame shape) | +2,560 | while recording |
| **During a talk** | **~41.6 KB** | |

So steady `int_free` should read **~86 KB** (109 − 22.7), and `int_min` **~79 KB** on a boot
with no talk (0.0.9's handshake peak, 102, less the idle cost). **After a talk expect `int_min`
≈ 60-67 KB**: ~67 KB when the talk rides the kept session (the normal case), ~60 KB in the
worst one, a talk that needs a fresh handshake (the kept session's ~25 KB freed, ~32 KB of
handshake peak, with the talk's buffers held). The OTA path is unaffected by a talk (the two
never overlap) and pays only the idle 22.7 KB: it closes the kept session first, as before.

**The trade, if the board's numbers are tighter than this.** Three levers, in the order to pull
them: (1) create the I2S channels and codec objects per exchange instead of at boot — the
idle cost falls to the stack (~4 KB), giving ~9 KB back to every handshake and OTA, for a few
milliseconds of codec set-up per talk; (2) a ring of 8 KB instead of 16 (0.25 s of slack
instead of 0.5) — 8 KB back during a talk; (3) 10 ms DMA buffers instead of 20 — ~4 KB, at the
cost of a starved audio task losing samples sooner. None is in 0.0.10 because none is needed
on paper; `int_min` after the first talks says whether one is.

### The safety rules, again

- **Nothing new before the rollback gate.** `svc_audio_start` runs last in `start_panel`,
  after `ota_confirm_health`, and only on a boot that may start the display: the boot after a
  fault starts no codec (`audio=off`).
- **No new task above priority 1** (above).
- **Every buffer is bounded**: the ring 16 KB (per exchange), two 20 ms chunks, a 1 KB upload
  slice, bubbles of 255 bytes, a frame reader that keeps at most a bubble of each string, eight
  messages per thread, the reply's speech at most 30 s (the box's cap) and read at the
  speaker's pace — nothing holds a whole clip either way.
- **A codec that fails leaves the panel working**: no sound, KEY says why, Settings says so in
  amber, the snapshot names the step, telemetry carries `audio=failed:<step>`.
- **One owner per chip**: the audio task alone calls `esp_codec_dev`; the codecs' registers
  ride `svc_power`'s bus lock (each transfer ≤ 20 ms); the amp rail is a `svc_power` call.
- **The PMIC is still read-only on a fault boot**: `svc_power_amp` refuses there, and audio
  does not start anyway.

### Reading 0.0.10 from the box

The note's cap grew from 1280 to 1408 bytes (the body from 1856 to 2048) for one new token:

| Token | Good | Bad, and what it means |
| --- | --- | --- |
| `audio=<state>,talks:N,sent:N,heard:N,none:N,fail:N,stop:N,up:<KB>,down:<KB>,ms:<last>/<max>,under:N,over:N` | `ok`; `heard` close to `sent`; `ms` a few thousand; `under`/`over` 0 or rare | `failed:<step>` — `bus` (no I2C attach), `i2c` (neither codec answered), `i2s`, `i2s-tx`, `i2s-rx`, `data`, `codecs`, `task`. `mic-only:es8311` / `speaker-only:es7210`: one codec is missing. `fail` climbing: read the snapshot's `audio.last_failure`. `none` high: the box hears silence — microphone gain or a dead mic (`mic_peak_l/r`). `over` > 0: the upload fell behind the microphone (radio). `under` > 0: the reply's speech arrived slower than it played. `ms` is from the last upload byte to the reply's words — Whisper, the model and, with read-aloud on, Kokoro. |
| `key=…,fail:N,stop:N` | — | `fail`: holds the box dropped mid-recording; `stop`: replies stopped with KEY or BOOT. |

The snapshot's `audio` section has the same counters and more: `phase`
(`idle`/`recording`/`thinking`/`speaking`), `mic_on`/`spk_on`/`net_busy`, `ring`/`queued`,
`es8311_id`/`es7210_id` (the chips' id registers as read), `amp`, `volume`, `mic_gain_db`
(what the codecs accepted, -1 before the first talk), `i2c_errors`, `codec_errors`, `tones`,
`stack_free`, `mic_peak_l`/`mic_peak_r` and `last_failure`.

### What to check on the glass

1. Settings → Jerv shows "Replies are spoken and shown as text." and a Speaker volume slider
   — not an amber line. (Amber: read `audio=` in telemetry.)
2. Hold KEY on the home screen and ask something short ("what's two plus two"): the ring
   pulses as you speak. Let go: the Jerv thread opens with "Transcribing…", then your words
   and Jerv's answer, spoken.
3. Ask again and press KEY (or BOOT) while Jerv is speaking: it stops at once, the words stay,
   and nothing new records.
4. Turn Read replies aloud off and ask again: the answer arrives as text only, no sound.
5. Tap KEY quickly: "Hold KEY the whole time you talk". Hold, then tap the glass: "Cancelled —
   nothing sent".
6. Move the volume slider and ask again: the reply is louder or quieter.

### Checking it from the box

```sh
scripts/debug-connect.sh panel-cmd c6 snapshot                         # "audio": state "ok", ids, counters
scripts/debug-connect.sh panel-cmd c6 play_tone                        # a second of 880 Hz: listen
scripts/debug-connect.sh panel-cmd c6 play_tone --arg value=440        # another pitch
scripts/debug-connect.sh panel-cmd c6 set --arg name=volume --arg value=60
scripts/debug-connect.sh panel-cmd c6 set --arg name=mic_gain --arg value=33
scripts/debug-connect.sh panel-cmd c6 logs                             # "sound ok (ES8311 id …)", "talk: up … down …"
scripts/debug-connect.sh panel-shot c6 out.png                         # the Jerv thread after a talk
```

After a talk the snapshot should show `talks` and `sent` up by one, `replies` (or
`nothing_heard`) up by one, `up_bytes` ≈ 32 KB per second held, `reply_ms` a few thousand, and
`mic_peak_l`/`mic_peak_r` both in the thousands (one near zero is a dead microphone; either at
32767 is clipping — lower `mic_gain`).

### What only the board can settle

Each is reported so it can be settled remotely:

- **That the codecs answer and come up**: `audio=` / snapshot `state`, `es8311_id` /
  `es7210_id`, `i2c_errors`. The register values are esp_codec_dev's own for these chips and
  the BSP's pins; they have not been run on this board.
- **The amp's polarity** (ALDO2 on = enabled, from the schematic and the BSP): `play_tone` with
  someone listening, and the snapshot's `amp` during it. Silent with `amp: true`, or hiss when
  idle, means it is the other way round.
- **Microphone gain** (30 dB): `mic_peak_l`/`mic_peak_r` after a talk, and whether the box
  heard the words (`heard` against `none`); `set mic_gain` moves it without an image.
- **Which ES7210 channels carry the two microphones**: both peaks moving with the voice says
  MIC1/MIC2 are on SDOUT1's two slots as the driver assumes; one flat at zero says otherwise.
- **Volume** (80): the owner's ear; the slider and `set volume` move it.
- **The audio task's stack** (3.5 KB): `stack_free` after a talk and a tone.
- **The memory** (above): `int_free` / `int_min` before and after the first talks.

## What 0.0.11 adds: the real Jerv and Dave, Clear chat, the panels' names

Four owner decisions of 2026-10-12, one image (plan §1, "Jerv on the panel is the real Jerv" and
the decisions after it; `../docs/mocks/jpanel-c6/README.md`).

### On the glass

- **Jerv is the owner's real Jerv.** The same talk as 0.0.10 — hold KEY, speak, let go — but the
  box now runs it through the PWA's own agent, as the owner, with every tool and memory, into a
  "Desk panel" chat in his Jerv history. A turn that needs his OK says so ("It's waiting for your
  OK in the app"); a long one says "Still working on that. The answer will be in the app." The
  voice is the PWA read-aloud's.
- **Minecraft is a menu: Dave and Compass.** The tile opens a second level. **Dave** is the box's
  Minecraft_Dave in a conversation exactly like Jerv's: hold KEY inside it to talk to Dave,
  spoken replies with the read-aloud switch, the long-press menu. **Compass** says "Work in
  progress". BOOT (or the back arrow) climbs a level: Dave or Compass to the menu, the menu home.
  KEY outside Dave's thread — the menu, Compass, anywhere else — is still the shell's talk to Jerv.
- **Clear chat.** A finger held still for 600 ms anywhere in a conversation (Jerv, Dave, either
  person's thread) opens a small card: **Clear chat** and **Cancel**. Clear takes two taps within
  3 s ("Tap again to clear"); BOOT, a tap on the dim around it, or KEY closes it — and KEY then
  records nothing. Cleared, the thread is empty on this panel, and Jerv's or Dave's next talk
  starts a new chat on the box (the old one stays in the PWA). Clearing one never touches the
  other: Jerv and Dave are two instances with two histories. A scroll never opens it: the finger
  must stay within the tap's 20 px, which is also where LVGL starts scrolling.
- **The people's threads carry the panels' real names**, from the box (`GET /endpoint/siblings`):
  the name the owner gave each panel in the PWA, its tile letter, and a colour from the name. A
  thread is bound to that panel's device id, not to a slot name, so a renamed panel keeps its
  thread and a replaced one starts empty. Until the box has answered once — and with nothing
  cached from an earlier boot — they read **Panel 1** and **Panel 2**. No name is compiled in, and
  the image ships no sample messages.

### The protocol

`POST /endpoint/converse?persona=jerv|dave&voice=true|false[&new_thread=true]` — the same route,
body and reply frame as 0.0.10 (`tk_converse_path`, host-tested). `persona=dave` is new, and
`new_thread=true` rides the first talk to an agent after Clear chat; the panel keeps asking until
a talk is answered, so a hold the box heard nothing in does not lose it. The box answers with the
real agent only for this panel (a `display` the owner switched on, server-side; the twins'
panels never), and otherwise with 0.0.10's spoken persona for Jerv and a 403 for Dave.

The reply's words fit the 255 bytes a bubble keeps: the box condenses the agent's answer to whole
sentences within 240 bytes (" More in the app." when cut) and speaks exactly that, so no firmware
buffer grew. **The panel waits 130 s for the answer** (`ANSWER_LIMIT_MS`, was 60): the box gives
the agent 100 s, then answers "still working" and lets the turn finish into the chat.

`GET /endpoint/siblings` (device key; only a `display` panel is answered) is fetched once a boot,
right after the first manifest, then every fifteen minutes on the manifest pass, and after a
failure on the next pass (a minute). `{"siblings":[{"device_id","name"}]}`, at most four. The
panel keeps up to four (`SIB_MAX`), two of which have tiles, each name cut to 24 bytes on a
character, every character the fonts lack drawn as `?` (`sib_parse`, `sib_clean_name`).

### The pieces

| Piece | What it is |
| --- | --- |
| `components/convo` | **One** conversation view for every agent — the bubbles, hold-KEY-to-talk, "Speaking…", Clear — parameterised by thread, title, icon and colour. `app_jerv` and `app_minecraft` each own an instance; neither includes the other. `tools/check_app_deps.py` lets an app require it beside `app_api` and `ui_kit`. `convo_model.c` (was `jerv_model.c`) is host-tested. |
| `svc_audio` | A talk names its agent (`tk_set_agent`) and whether it asks for a new chat; bubbles land in that agent's thread. `svc_audio_fresh` is Clear chat's half, per agent. |
| `svc_messages` | Four threads, each its own buffer: Jerv and Dave keep eight messages, the two people's threads four (they hold only "not sent" placeholders until messaging lands). Same static RAM as 0.0.10's three threads of eight. `svc_messages_clear`. |
| `svc_siblings` | The siblings list: the box's JSON parsed and cleaned (`sib_logic.c`, host-tested), slots bound by device id (`sib_merge`), a hand-over from the main task to the UI task with no lock (a sequence count), an NVS blob cache (`esp/sib_port_nvs.c`, namespace `jpanel`, key `siblings`) so names show before the box answers after a reboot. |
| `main/sib_link.c` | The fetch, on the main task like every box request. |
| `shell` | The long press (`sh_long_press`), the menu's state and two-tap confirm (`sh_menu_*`) and the button routing over it (`SH_ACT_CLOSE_MENU`) in `shell_core.c`, host-tested; the card is `ui_menu`. A notice waits while the menu is up; an alarm closes it. |
| `app_minecraft` | The menu, Dave (a `convo` instance on thread `dave`), Compass. |
| `ui_kit` | `ui_menu`, the compass glyph, four people's colours (`UI_ACCENT_PERSON_A`–`D`) in place of two named ones. |

### Debugging it

- The snapshot's `shell` gains `menu` (`closed` / `open` / `armed`), `menus` and `clears`; an open
  conversation's `app` section says `thread` and `new_chat_next`; Minecraft's says `view`
  (`menu` / `dave` / `compass`); `messages` lists four threads with `keeps`; and a new
  **`siblings`** section lists the slots — `device_id`, `name`, `accent`, `thread` — with
  `source` (`none`, `cache`, `box`), `fetched`, `unreadable`, `failed` and `last_status`.
- **`inject` takes `hold`**: `{"cmd":"inject","event":"hold","x":240,"y":240,"value":800}` is a
  finger held still at x,y for `value` ms (default 800) — the long press that opens a
  conversation's menu. Then `inject touch` on the card's buttons drives Clear chat, exactly as a
  finger would. No `clear` command: the menu is reachable this way.
- Telemetry's note gains `sib=<source>,box:N,bad:N,fails:N,last:<status>`.

```sh
scripts/debug-connect.sh panel-cmd c6 open --arg app=jerv
scripts/debug-connect.sh panel-cmd c6 inject --arg event=hold --arg x=240 --arg y=240 --arg value=800
scripts/debug-connect.sh panel-shot c6 menu.png                       # the card
scripts/debug-connect.sh panel-cmd c6 snapshot                        # shell.menu, siblings
```

### From the first talks on 0.0.10

- **The audio task's stack** is 4.25 KB (was 3.5): 0.0.10 reported `stack_free` 1,524 B after two
  talks — 2,060 B used — and 2 KB of headroom wants ~4.1 KB. Cost: 768 B of internal RAM from
  boot.
- **Microphone gain defaults to 24 dB** (was 30): the two talks peaked at 24,816 and 28,889 of
  32,767, 1–2 dB from clipping, and the box heard both. 6 dB down halves the peaks. A panel that
  was given `set mic_gain` keeps its own value (NVS holds only what was set).
- **`int_min` fell to 56.6 KB** after those talks (the estimate was 60–67). This image moves it by
  about **−2.5 KB**: the stack (+768 B) and +1,716 B of static DIRAM (263,704 → 265,420 B,
  measured from the two ELFs: the siblings slots and their hand-over, the fetch's 768 B body, the
  menu; the messages are the same bytes, moved from `.data` to `.bss` with the sample text gone).
  So expect `int_min` ≈ 54 KB after a talk. No reply buffer grew, so no lever from 0.0.10's list
  is pulled; the first one to pull if `int_min` reads under ~45 KB is (2), an 8 KB talk ring.

### What to check on the glass

1. Hold KEY and ask Jerv something only the real one can answer ("what's on my calendar
   today?"). The answer is spoken in the PWA's voice, and the PWA's Jerv list has a "Desk panel"
   chat with the exchange.
2. Ask something long ("research …"): after up to ~100 s the panel says the answer will be in the
   app, and it arrives there.
3. Long-press in Jerv's thread: the card. Clear chat, tap again: the thread empties. The next
   question starts a new "Desk panel" chat in the PWA; the old one is still there.
4. Minecraft → Dave: hold KEY and ask Dave something. Back, Compass: "Work in progress".
   Back, back: home. Jerv's thread does not show Dave's exchange.
5. Home: the bottom tiles show the twins' panels by the names in the PWA's Panels screen.
6. After a talk, the snapshot's `audio.stack_free` (want ≥ 2 KB), `mic_peak_l`/`r` (want well under
   32,767), and telemetry's `int_min`.

## What 0.0.12 adds: the turn on the panel, the whole answer, the Clock's hold, Jerv's icon

The owner's first agent talks on 0.0.11 (2026-10-12): *"when I ask it to do something like find
the high tide near Port St John Florida, it sat there for a while. Just saying transcribing and
then said 'Still working on that. The answer will be in the app.' … I want better updates for tool
use and such to come through the panel and not to have it direct me to my app."* Then: longer
replies read past 30 s, the first quarter second of a reply sometimes clipped, the Clock's alarms
on a long press, and Jerv in the PWA's own icon. Plan §1 has each decision, dated.

### On the glass

- **"Transcribing…" becomes the question as soon as the box has it**, not when the answer comes.
- **A live status line under it** while the agent works: a small spinner and dim words —
  "Thinking…", "Searching the web: high tide Port St John FL…", "Reading a web page: tides…",
  "Writing the answer…" — the PWA's own step labels, in the present, one line per tool the agent
  starts. The answer replaces it. (The mock's status row is italic; the image has no italic face,
  so it is the dim colour and a smaller size.)
- **The whole answer**: the box sends up to 1,000 bytes and the thread keeps an agent's newest
  answer whole; older answers keep a bubble's 255 bytes. A longer answer is cut on a word with
  "…" on the glass and still spoken in full.
- **Read in full**: the speech is the whole answer, sentence by sentence, up to 5 minutes. **KEY
  or BOOT stops it at once.** Read-aloud off: words only, nothing synthesised.
- **No "the answer will be in the app"** for a slow turn: the box follows it for up to 5 minutes
  with progress flowing; only past that does the panel say it is still going ("…the answer will
  be in our Desk panel chat"). A staged change or a question says WHAT waits — "Jerv wants to
  change your calendar. Confirm it in the app." — the one case that still points at the app.
- **KEY while Jerv is still working** starts no new talk: the toast "Still working on the last
  one". **BOOT while he works leaves the thread** as BOOT always does; the turn goes on, and its
  answer lands in the thread (and is spoken) wherever the glass is — the main task holds the
  connection whatever the UI shows. Clear chat while he works lets the connection go (the box
  finishes the turn into its chat).
- **The first word is never clipped**: the speaker starts the same way every time (below).
- **The Clock opens its alarms on a two-second hold**, not a tap. After 0.3 s a thin ring fills
  around the finger over the rest of the hold — the night colour on the night clock, steel by day.
  Lifting early or moving the finger cancels quietly. A tap on a dark screen still wakes it.
- **Jerv wears the PWA's icon** — the neuron mark of `frontend/public/favicon.svg` — on the home
  tile and in his header, and the PWA's blue `#7FA7C9` replaces the purple everywhere Jerv's colour
  is used ("Speaking…", Listen, his tint).

### The protocol: JT2

`POST /endpoint/converse?persona=jerv|dave&voice=true|false&jt=2[&new_thread=true]`
(`tk_converse_path`). `jt=2` asks for the turn as a stream (backend `api/panel_stream.py`); a
panel that does not send it (0.0.11) gets JT1 unchanged, and the twins' pet path never streams.
The box sends its headers as soon as the upload ends, then `JT2` and records until the body ends:

| Type | Name | Payload |
| --- | --- | --- |
| `0x01` | HEARD | UTF-8: what the box heard, as soon as Whisper has it |
| `0x02` | STATUS | UTF-8, ≤ 60 bytes: what the agent is doing now |
| `0x03` | ALIVE | empty: nothing new — at least every 8 s while the agent or the synthesis is quiet |
| `0x04` | ANSWER | flags (bit 0 speech follows, bit 1 the text was cut) + UTF-8, ≤ 1,000 bytes |
| `0x05` | PCM | 16 kHz mono s16le, ≤ 4,096 bytes a record |
| `0x06` | END | code (0 answered, 1 nothing heard, 2 failed) + UTF-8 why |

Every record is `type u8 | length u16 LE | payload`. **The reader** (`tk_stream_feed`,
`talk_core.c`, host-tested) is incremental and bounded: it keeps one text record at a time (1 KB,
cut on a character with "…" past it), hands speech on in the slices it arrived in — never holding
a whole response — skips a type it does not know by its length, and refuses a record longer than
4,096 bytes or a body that does not start `JT2`, cleanly (the talk says why).

**Timeouts.** The headers within 30 s of the upload's end (the box sends them at once). Then
**"no byte for 45 s"** is the only idle limit (`IDLE_MS`; the box's keep-alives come every 8 s),
not a total: the box caps the agent at 5 minutes and the speech at 5 minutes of audio, and the
panel's backstop is 12 minutes. 0.0.11's `ANSWER_LIMIT_MS` (130 s) is gone.

**The speech is paced by the speaker.** The box synthesises a sentence (then groups of up to 400
characters) while the panel plays the one before, and sends it as PCM records; the main task
writes them into the 16 KB ring at the speaker's pace, so TCP's backpressure is the flow control
and neither side holds more than a sentence or two. A sentence the box is still synthesising when
the ring runs dry is **a pause, never a stutter**: the speaker re-arms its start gate (below) and
waits for 150 ms of the next one; an underrun is counted once per pause (`under:` in `audio=`).
KEY or BOOT ends the read: the ring stops taking speech, the connection is let go, and the box
(Starlette's disconnect) cancels the synthesis ahead of it; the agent's turn is a separate task the
stream only follows, so it is never touched.

### The first word: why it was clipped, and the fix

`speak()` opened the ES8311, turned the NS4150B's rail (ALDO2) on and, once 4 KB was queued,
wrote the first sample — the amplifier's start-up and the DAC's unmute ramp ate the start. It
was "sometimes" because 0.0.11's frame carried the speech right behind the words: when the speech
was already in the ring as the audio task opened the speaker, the first word played within
milliseconds of the amp; when it trickled in, the wait for 4 KB happened to give the amp time.
Now the start is the same every time (`tk_spk_may_start`, host-tested): the speaker opens, the amp
goes on, **silence is written until the amp has had 200 ms** (`TK_SPK_SETTLE_MS`) **and 150 ms of
speech is queued** (or the stream has ended); playback ends with 120 ms of silence so the DMA's last
buffers reach the speaker before the amp goes off. The debug `play_tone` takes the same settle and
tail. The snapshot's `audio` section reports the last reply's start: **`spk_settle_ms`** (amp on to
the first sample — want ≥ 200) and **`spk_start_fill`** (bytes queued then — want ≥ 4,800).

### The pieces

| Piece | What it is |
| --- | --- |
| `talk_core.c` | The JT2 reader (`tk_stream_*`), `tk_fit` (a copy cut on a character), the speaker's start gate. The JT1 frame reader is gone (the box always serves JT2 to a panel that asks). |
| `main/talk_link.c` | The stream, record by record: transcript and status into the talk's state, the answer, speech into the ring, END; the 45 s idle limit; the connection let go on a stop. |
| `svc_audio_talk.c` | What was heard and the status line, each with a change count so the UI redraws exactly when one moved; `svc_audio_status` (the line for a thread), `svc_audio_speaking`; the answer copied straight into the thread's whole-answer slot. |
| `esp/svc_audio_es.c` | The gated start, the pause on an underrun, the tail; `spk_settle_ms`, `spk_start_fill`. |
| `svc_messages` | `MSG_LONG_TEXT` (1 KB): one whole answer per agent thread beside its bubbles; `svc_messages_text`. |
| `convo` | The status row under the waiting question; a reader at the end stays at the end. |
| `shell_core.c` | `SH_ACT_STILL_WORKING`: KEY while an agent works. `speaking` decides when KEY/BOOT stop; BOOT while only thinking is BOOT. |
| `ui_kit` | `ui_hold.c` — the one long-press rule (the conversation menu at 600 ms, the Clock face at 2 s; `sh_long_press` is a thin wrapper); `ui_hold_ring`; `ui_status_row`; the Jerv mark (`ui_jerv_mark.c`, A8 masks at 52 and 23 px, generated by `tools/gen_jerv_mark.py` from the PWA's SVG — checked in, so clean builds stay byte-identical, and CI checks it against the SVG). |
| `app_clock` | The face's hold (`CLOCK_HOLD_MS` 2,000) and its ring; snapshot `holds`, `hold_fill`. |

### The memory budget

Static DIRAM (from the two ELFs, `.dram0.data` + `.dram0.bss`): 141,168 → 144,952 B, **+3,784 B**:
the two agent threads' whole answers (2 × 1,028), the answer buffer the main task fills (+768:
255 → 1 KB), the stream reader's text record (+~500 over JT1's frame reader), the status line and
the transcript's UI copy (moved off the UI task's stack). No heap is added per talk (the ring is
0.0.10's 16 KB); the status row's spinner, the hold ring and a 1 KB answer label come out of LVGL's
own static 64 KB pool. So expect **`int_min` ≈ 70 KB before talks** (0.0.11 read ≈ 74 KB on the
glass) and **≈ 52–54 KB after a talk** (0.0.10's talk dipped to 56.6 KB). If it reads under
~45 KB, the first lever is still 0.0.10's (2): an 8 KB talk ring.

### Debugging it

- The snapshot's `audio` gains `status` (while an agent works), `spk_settle_ms`,
  `spk_start_fill`; `shell` gains `speaking` and `keys.still_working`; `messages` gains each agent
  thread's `long_id` / `long_bytes`; the Clock's `app` section gains `holds` and `hold_fill`.
- The device log: `stream: N records (S status, K skipped)` and `reply: amp on N ms before the
  first sample, N B queued` after each talk.
- The box logs one `endpoint.converse` line per JT2 turn with `jt=2`, `statuses`, `tools`,
  `agent_ms`, `answer_bytes`, `cut`, `speech_bytes`, `groups`, `tts_ms`, and `cut_off` when the
  panel went away mid-answer.

```sh
scripts/debug-connect.sh panel-cmd c6 snapshot          # audio.spk_settle_ms, spk_start_fill
scripts/debug-connect.sh panel-cmd c6 open --arg app=clock
scripts/debug-connect.sh panel-cmd c6 inject --arg event=hold --arg x=240 --arg y=240 --arg value=2200
scripts/debug-connect.sh panel-shot c6 alarms.png        # the alarms list, after the hold
```

### What to check on the glass

1. Ask Jerv "find the high tide near Port St John, Florida". Your words replace "Transcribing…"
   within a few seconds; a status line names each step ("Searching the web: …"); the answer
   arrives on the panel and is read in full. The PWA's Jerv list shows the "Desk panel" chat.
2. Ask for something long ("tell me about the tides in the Indian River lagoon"): the voice runs
   past 30 s, pausing between sentences at most; KEY stops it at once.
3. While Jerv works, press KEY: "Still working on the last one", nothing records. Press BOOT: home;
   the answer is spoken when it lands, and is in the thread.
4. After a talk: `audio.spk_settle_ms` ≥ 200 and `spk_start_fill` ≥ 4,800, and the first word
   is whole; `play_tone` starts cleanly.
5. The Clock: a tap does nothing; hold two seconds — the ring fills, the alarms open. At night
   the ring is the night colour.
6. Home: the Jerv tile shows the PWA's mark in its blue.
7. Telemetry's `int_min` before and after a talk (above).

## What 0.0.13 adds: alarms that ring

The alarms wave (plan §1 "Background duties live in services", C3's "Clock + alarm service + PWA
alarm editor + box API"). Alarms are **set in the PWA** — jpanel → Panels → the desk panel's card →
Alarms (`../docs/mocks/jpanel-c6/PwaAlarms.dc.html`): time, repeat days, an optional label and a
sound — and on the panel they are **only switched on and off** (owner decision, mocks README). The
panel keeps its own copy and rings from its own clock, so an alarm sounds with the box unreachable.

### On the glass

- **The alarm list** (Clock, hold the face 2 s) is the box's list: each alarm's time, days and label
  with its switch, and the chip above it says what rings next ("Next 06:45 · in 9 h 4 min", or
  "Snoozed until 06:54 · in 9 min"). An empty list says where alarms are made. The last line says
  where the copy came from, and that a switch flipped while the box is away reaches it later.
- **The switch** takes effect at once (the next alarm moves with it) and is queued for the box: one
  write per alarm, the last one wins, retried every pass until the box answers, kept through a
  reboot. A one-off (no days) that has rung switches itself off the same way.
- **Ringing** is the mock's Ringing screen (bell, the time, the label, **Snooze 9 min** / **Stop**,
  "KEY snoozes · BOOT stops") over whatever is open — an app, the home pages, a notice, the talk
  ring, a dark screen. It lights the glass at no less than 40 % (the owner's brightness when that is
  higher) and gives his brightness back when it ends. **KEY snoozes nine minutes; BOOT — short or
  held — or Stop ends it.** Nobody answering: it **stops itself after 10 minutes** and the snapshot
  says `last_end: "timed_out"`.
- **The sound** is the alarm's own — **chime** (two bell strikes), **beeps** (four short ones) or
  **rise** (a sweep up) — synthesised a chunk at a time from the tone path's sine table
  (`svc_audio/alarm_sound.c`): no audio files. It loops with a volume ramp from 15 % to full over
  30 s, at a speaker volume of at least 70 whatever the reply volume is set to.

### Precedence: a ringing alarm beats everything

One rule in `shell_core.c` (`sh_route_button`, host-tested), checked before every other:

| While an alarm rings | What happens |
| --- | --- |
| KEY (down) | Snooze — never a recording; its release sends nothing |
| BOOT, BOOT held, Stop | Stop |
| PWR | Wakes a dark screen; never puts the glass out from under the ringing screen |
| A dark screen | The alarm wakes it; KEY and BOOT still snooze and stop (not "wake only") |
| The conversation menu | Closed when the alarm starts; any press is the alarm's |
| The talk ring (KEY held to Jerv) | Ended when the alarm starts (nothing sent); the release sends nothing |
| Jerv speaking a reply | The voice stops when the alarm starts; the answer stays in the thread |
| A notice | The alarm is the top overlay; BOOT stops the alarm, it does not dismiss the notice |
| A test tone or a talk wanting the speaker | The audio task gives the speaker to the alarm at once (`svc_audio_ring`); a talk is refused "An alarm is ringing" |

The shell does the takeover on `ALARM_DUE` (`SH_DO_ALARM_TAKES_OVER`: end the talk, close the menu,
stop the reply, light the glass, open the Clock's ringing view) and the handback on the new
`ALARM_ENDED` (snoozed, stopped or capped: the owner's brightness again, the face back).

### When an alarm rings: UTC, the zone, and daylight saving

`svc_alarms` schedules in **UTC** (`svc_rtc_epoch`), from the PCF85063-backed system clock, with the
zone chosen on the panel (default New York). The zone's POSIX rule is evaluated by the panel's own
code (`time_logic.c`: `tz_parse`, `tz_offset_at`, `tz_wall_to_utc`), not newlib's `localtime`,
because an alarm needs the UTC instant of a wall time on days that are not today; the host suite
checks it against glibc for every zone, every hour of 2026-2027.

- **Repeat days** are the panel's local days (bit 0 Monday … bit 6 Sunday); **0 = once**: the next
  occurrence, then it switches itself off.
- **The gap** (clocks forward): a time that does not exist rings when the gap ends — 02:30 rings at
  03:00. **The overlap** (clocks back): a time that happens twice rings the first time only.
- **Late is grace, not catch-up.** A check runs every UI pass; an alarm due up to 2 minutes behind the
  clock still rings (a reboot — an update — across its minute), further behind is counted `missed`,
  never rung late. A clock the box corrects backwards by up to 10 minutes rings nothing twice; a
  bigger step back is a clock that was wrong, and the schedule starts over from the corrected time.
- **Changed while snoozed:** a snooze ends when its alarm is deleted, switched off, or moved to another
  time or other days (`snooze_cancelled`); a new label or sound is carried into it.

### The protocol

`GET {api}/endpoint/alarms?etag=<what the panel holds>` on every pass of the update loop (the
manifest's minute; at once after a switch on the glass wakes the loop) and right after boot, on the
kept-alive session: **304 with no body** when nothing changed, otherwise
`{"etag":"…","alarms":[{"id","at","days","on","label","sound"}],"max":8}` (`at` minutes past
midnight in the panel's zone). The etag the panel sends moves only once a fetched list is stored,
and it is what the box reports to the PWA, so "On the panel" is never said of a list the panel
dropped. The answer is read into a buffer sized from the box's longest possible one
(`ALARM_JSON_MAX`, about 1 KB: eight full labels, every number at its widest); labels never hold
`"` or `\` (the box refuses them), so nothing in it is escaped and the bound holds — the host
suite builds that answer and checks it fits and parses. A switch flipped while the box is
unreachable waits in the queue and wins when it lands, even over a later PWA edit of that alarm's
on/off: the box takes switches in the order they arrive. Before the list, the queued switches go
up as
`PUT {api}/endpoint/alarms/{id}` `{"on":bool}` — a 2xx or a 404 (deleted in the PWA) settles one,
anything else keeps it. Both routes answer only a panel flashed as a `display` (the twins' S3 panels
are 403); the box's RLS lets a panel read only its own alarms and write only their `on` flag
(`../backend/src/jbrain/api/panel_alarms.py`, migration 0236).

### The pieces

| Piece | What it is |
| --- | --- |
| `svc_alarms/alarm_logic.c` | Pure, host-tested: the box's JSON (`alarm_parse`), the next occurrence (`alarm_next_at`), the ring / snooze / stop / cap machine (`alarm_sched_*`), the switch queue (`alarm_queue_*`). |
| `svc_alarms/svc_alarms.c` | The service on the UI task: applies the main task's hand-over (a sequence count, as the siblings'), fires, posts `ALARM_DUE` / `ALARM_ENDED`, starts and stops the sound, saves the cache 2 s after the last change. The schedule and the parsed zone are the UI task's alone: it publishes a copy (with what rings next) once a second and after every change, and telemetry and the debug snapshot read only that. The queue, the etag and that copy are shared under a critical section. |
| `svc_alarms/esp/alarm_port_nvs.c` | The cache: one NVS blob (`jpanel`/`alarms`, ~430 B) — the list, its etag and the switches not yet sent. |
| `svc_rtc/time_logic.c` | The zone rules (`tz_*`) and `svc_rtc_epoch`. |
| `svc_audio/alarm_sound.c` | The three sounds and the ramp, pure; `esp/svc_audio_es.c` plays them (`play_ring`) ahead of everything else. |
| `main/alarm_link.c` | The two box requests, main task only; `box_http_put`. |
| `shell_core.c` / `shell.c` | The precedence above; the takeover and handback; `inject event=alarm_test`. |
| `app_clock` | The real list, the next/snoozed chip, the ringing view leaving on `ALARM_ENDED`. |

### Debugging it

- The snapshot's **`alarms`** section: `count`, `source` (`none` / `cache` / `box`), `next` (`id`,
  local `at`, `in_min`, `snoozed`), `ringing` (the id, -1 when silent) with `ring_ms` and
  `ring_sound`, `ring_cap_min`, `last_fired` / `last_snoozed` / `last_dismissed` (local times),
  `last_end` (`stopped`, `snoozed`, `timed_out`), counts (`fired`, `snoozes`, `dismissed`,
  `timed_out`, `missed`, `snooze_cancelled`, `taken_over`), `fetch` (`last` ok/304/fail, `status`,
  `age_s`, `etag`, `puts`, `put_fails`), `queue` (switches not yet sent) and `alarms` (the list).
  `audio` gains `rings`, `ring_fail`, `ring_playing`; the Clock's `app` section `ringing_id`.
- Telemetry's note: `alarm=n:3,next:+545,fired:1,snz:0,stop:1,cap:0,miss:0,q:0,src:box,fetch:304/304`
  (`next:z9` is a snooze nine minutes away).
- **Fire one now, with nothing set:** `inject` `event=alarm_test` (`name` = `chime`, `beeps` or
  `rise`) rings a "Test alarm" through the whole path — the shell's takeover, the ringing view, the
  sound; KEY, BOOT or the cap end it, and it switches nothing off.

```sh
scripts/debug-connect.sh panel-cmd c6 inject --arg event=alarm_test --arg name=rise
scripts/debug-connect.sh panel-shot c6 ringing.png
scripts/debug-connect.sh panel-cmd c6 inject --arg event=key_down     # snooze
scripts/debug-connect.sh panel-cmd c6 snapshot                       # alarms.last_end, next.snoozed
```

### The memory budget

Static DIRAM (from the two ELFs, `.dram0.data` + `.dram0.bss`; 0.0.12 rebuilt from its tag to the
byte of its `dist/`): 144,952 → 148,336 B, **+3,384 B** (`.data` −232 — the fake's sample alarms
are gone). Most of it: the fetch buffer on the main task (1,001, sized from `ALARM_JSON_MAX`), the
schedule (`s_s`, 496), the view of it the UI task publishes for the other tasks and the debug
snapshot's copy of that (576 each), the hand-over (316), the NVS blob kept static rather than on
the UI task's stack (420), the switch queue (100), the etag and the parsed zone (52).
`.iram0.text` is unchanged. **No new task and no heap per ring:** the sound is the audio task's
existing chunk buffer, the fetch and the PUT ride the kept-alive session, the NVS write is a
transient handle. So **`int_min` should read about 3.4 KB lower than 0.0.12's** at the same point
(≈ 67 KB before a talk, ≈ 49–51 KB after one, on 0.0.12's estimate); an alarm ringing adds nothing
to it. App: 1,926,224 → 1,942,032 B (+16 KB; 54 % of the slot still free).

### What to check on the glass

1. In the PWA (jpanel → Panels → the desk panel), add an alarm two minutes ahead, sound Rise. The card
   says "Sending to panel…", then "On the panel" within about two minutes (the panel stores it
   on one poll and says so on the next).
2. Hold the Clock face 2 s: the alarm is in the list and the chip says "Next … · in 2 min".
3. Put the screen out with PWR. At the minute the glass lights at a readable brightness, the Ringing
   screen is up and the sweep starts soft and climbs. PWR does not put it out.
4. KEY: "Snoozed until …", the face comes back; the list's chip says "Snoozed until …". Nine minutes
   later it rings again; BOOT stops it.
5. Flip an alarm's switch on the panel with Wi-Fi off (Settings → Wi-Fi → Disconnect), reconnect: the
   PWA shows the change within a minute.
6. A one-off (no days): after Stop it is switched off on the panel and in the PWA.
7. Leave one ringing: it stops by itself at 10 minutes; the snapshot says `timed_out`.
8. `inject event=alarm_test` over a talk to Jerv: the talk ends, nothing is sent, the alarm rings.

## What 0.0.14 adds: the steps, a spinner that keeps turning, and a new chat from outside

Three things the owner asked for on 2026-10-12, and two on the box and in the PWA that travel with
them (plan §1 "KEY routing" and "Progress on the panel, and the whole answer").

### On the glass

- **The steps Jerv (or Dave) takes**, under the question: one dim line per tool in the tool's own
  shorthand and its target — "web_search · tide Port St John", "web_fetch · tide-forecast.com" —
  with a ✓ when it finished, a dim ✗ when it failed, and the spinner on the one running. Between
  tools the line is "Thinking… 0:42", its clock running from KEY's release. The last four show;
  older ones fold into "+N more" above them. When the answer lands they collapse to one dim line
  over it, "2 steps · 58 s"; an answer that ran no tool has none.
- **The spinner goes all the way round, and never starts over.** It is a fixed 90° arc turning once
  a second (`ui_spinner`), not LVGL's spinner, whose arc grows and shrinks on an eased sweep — a
  third of the way round it shrank back and read as a reset. And it was reset: every status line
  rebuilt the whole conversation, spinner included. Now a status, a step or the clock's second
  updates the working view where it stands and moves the one spinner between its rows; only a
  change to the messages rebuilds the list (`convo_redraw`, from `svc_messages_rev`). The spinner's
  angle is read from the tick, so even one made by a rebuild is exactly where the last one was.
- **KEY outside Jerv's thread starts a new conversation.** Home, the clock, Settings, Minecraft's
  menu (a person's thread keeps KEY as a message to her): once the hold is sent the Jerv thread opens holding just the new
  exchange, and the talk carries `new_thread=true`, so the box starts a fresh "Desk panel" chat
  (the old ones stay in the PWA). KEY inside Jerv's thread continues it; inside Dave's, Dave's. A
  cancelled or too-short hold changes nothing. One rule, `talk_key_route`, for the conversation
  view and the shell's own talk.

### The pieces

| Where | What |
| --- | --- |
| `svc_audio/talk_core.c` | JT2's `STEP_START` (0x07: index, label ≤ 60 B) and `STEP_DONE` (0x08: index, ok) read like ANSWER and END — a number byte, then the rest — in any split; `tk_steps_*`, the last four steps in place and the rest counted; `tk_steps_summary`. The converse path adds `&steps=1`. |
| `svc_audio/talk_policy.c` | `talk_key_route` (inside a conversation: continue it; anywhere else: a new Jerv one) and `talk_clock`. |
| `svc_audio/svc_audio_talk.c` | `TALK_TO_NEW_AGENT`: the hold asks for `new_thread`, and its send clears the panel's view of the thread first; `svc_audio_work` (the steps, the phase, the clock) replaces `svc_audio_status`; the answer's message carries the summary in its `status`, which grew from 16 to 24 bytes. |
| `svc_messages` | `svc_messages_rev`, a count that moves with every change to a thread. |
| `convo` | The working view built once per question and changed in place; `convo_refresh` asks `convo_redraw` whether the list or only the working view needs drawing. |
| `ui_kit` | `ui_spinner`, `ui_is_spinner`, `ui_step_row`, `ui_note_line` (replacing `ui_status_row`). |
| `main/talk_link.c` | Hands STEP_START / STEP_DONE to the audio service. |

### The memory budget

Static DIRAM (both ELFs, `.dram0.data` + `.dram0.bss`; 0.0.13 rebuilt from main to the byte of its
`dist/`): 148,336 → 148,968 B, **+632 B** (`.bss` +616, `.data` +16). The steps are fixed buffers:
`tk_steps_t` in the audio service, four steps of a 64-byte label plus counts (~270 B); each agent
conversation (`convo_t`, Jerv's and Dave's) holds its working view's widget pointers and the last
drawn signature (~2 × 90 B); a message's `status` grew from 16 to 24 bytes for "12 steps · 2:05"
(24 kept messages, +192 B); a revision count per thread and the send time. **No heap per step and
no new task:** drawing copies the view (`svc_work_t`, ~350 B) onto the UI task's stack for the
moment it draws, and the widgets — one row per shown step, made once per question — come out of
LVGL's own pool (the sim's peak for the steps states is 19.5 KB on its 64-bit host, the same as
0.0.12's `jerv-working` and under Settings' 36.9 KB). One LVGL timer (a second's tick) lives only
while the box works and goes with the working view. `int_min` should read about 0.6 KB lower than
0.0.13's. App: 1,942,032 → 1,944,608 B (+2.5 KB). `.iram0.text` is unchanged.

### What to check on the glass

1. From the home page, hold KEY and ask Jerv something that needs the web ("find the high tide near
   Port St John, Florida"). On release the Jerv thread opens with only that question; in the PWA a
   new "Desk panel" chat appears and the old one is still there.
2. Under the question: "Thinking… 0:03" with the clock counting, then a line per tool —
   "web_search · …" with the spinner on it, a ✓ when it ends. The spinner turns smoothly all the
   way round and does not jump as lines are added.
3. On a turn of more than four tools, "+N more" above the last four.
4. When the answer comes, the steps fold to "N steps · M s" over it.
5. Inside the Jerv thread, hold KEY again: the conversation continues (the PWA's chat gets the
   second exchange, no new chat).
6. Open Minecraft → Dave and talk: Dave's thread continues. Back on Minecraft's menu, KEY starts a
   new Jerv conversation, not a Dave one.

## What 0.0.15 adds: New conversation, Recent, and switching between conversations

The owner's request of 2026-10-12 — *"On long press of the Jerv [conversation] where it makes you
able to clear a conversation, it should also have another button so that you can click it and
view recent conversations to re-change what conversation we're actually in … change the clear
conversation to new session."* — drawn as the mock's Convo boards and DaveRecent and approved by
him the same day, with its four defaults (plan §1 "Switching conversations").

### On the glass

- **The thread's header names the conversation**: "Jerv" (or "Dave") and under it the box's
  title for the conversation the panel is in — "Shopping list", "Tides at Port St John" — or
  "Desk panel" until the box has one, "New conversation" for one not yet begun. Asked of the box
  when the thread opens and again when an answer lands (Jerv may have just named it).
- **The long press in Jerv's or Dave's thread** opens "This conversation" / "Jerv · Shopping list"
  with **New conversation** (the agent's colour, one tap: the thread starts over, the toast says
  "New conversation — the last one is in Recent", and the next talk asks the box for a new
  conversation; on one already new and empty it does nothing and says "Already a new
  conversation"), **Recent conversations ›** and **Cancel**. This replaces 0.0.11's Clear chat
  there. Quorra's and Tamsin's threads keep Clear chat and its two taps exactly as they were.
- **Recent** is its own screen over the thread: "Recent" / "Jerv's conversations", a "+ New
  conversation" row, then up to ten 84 px rows from the box — a microphone for a conversation
  begun at the panel, a phone for one begun in the app; the title, cut with "…"; when ("12 min
  ago", "Yesterday 19:40", "Sat 10:05", "5 Oct", on the panel's own 24-hour clock and zone); and
  the last line, "You: " before the owner's own. The current one has the 6 px accent bar, the tint
  and the "Current" chip; more below fades out at the bottom. While the box answers: "Loading…"
  over three grey rows; with none: "No other conversations yet"; with no answer: "Try again".
- **Tapping a row** puts "Opening…" in its corner and switches: the thread then holds that
  conversation's last three exchanges from the box, under its title, with the chip "Earlier
  messages are in the app" at the top, and the next KEY inside it continues THAT conversation. An
  answer still on its way for the conversation being left is let go first (the box finishes it
  there).
- **Buttons in Recent**: KEY closes it and talks in the current conversation (mid-switch it only
  says "One moment: opening that conversation"); BOOT and the back arrow return to the thread.
  KEY outside a thread is 0.0.14's `talk_key_route`, unchanged.

### The protocol

Three routes, all behind the talk's own gate (`panel_agent.agent_allowed`: a twin's panel, or a
display the owner has not switched on, gets 403 and nothing else), all answered for the live
owner principal on owner-scoped sessions (backend `api/panel_convo.py`):

| Request | Answer |
| --- | --- |
| `GET /endpoint/convo/{jerv\|dave}/recent` | `{"items":[{"id","title","at","origin","snippet","current"}…]}` — at most 10, the current first, then newest; `at` in UTC seconds; `origin` `panel` or `pwa`; title ≤ 40 B and snippet ≤ 80 B, cut with "…". |
| `POST /endpoint/convo/{agent}/select` `{"session_id"}` | `{"id","title"}`, or 404 for anything that is not the owner's own root chat with that agent from the panel or the app. |
| `GET /endpoint/convo/{agent}/messages?limit=N` | `{"id","title","messages":[{"mine","text"}…],"more"}` — the last N exchanges (N ≤ 4; the panel asks 3), the newest answer ≤ 1,000 B and the rest ≤ 255 B; `limit=0` is the title alone. |

A switch is `select` and then `messages`, one after the other on the main task. New conversation
has no request: it is the talk's own `new_thread=true` (0.0.11), so the box makes nothing until
something is said, and Recent leaves the box's current row un-marked meanwhile.

### The pieces

| Where | What |
| --- | --- |
| `svc_convo` (new) | The service: the Recent list on the heap only while Recent is open, the switch, each agent's title and "earlier" flag; requests queued for the main task (a repeat of the same ask replaces the waiting one), answers applied on the UI task's pass and announced as the thread's `MSG_ARRIVED`. `convo_wire.c`: the paths, the select body (a session id or nothing), and the two answers' readers — unknown fields skipped, rows past ten ignored, a row without an id or a known origin dropped, every string cut at a character with "…". |
| `main/convo_link.c` (new) | Runs the waiting request on the main loop, straight after a talk, over the kept session: one request in flight, an 8 s bound, the reply into a buffer allocated for it and handed to the UI task. |
| `convo` | The header's title line, the "Earlier messages" chip and the new-conversation page; `convo_menu` / `convo_new` / `convo_recent`; `convo_recent.c`, the Recent screen. Its rules are pure and host-tested in `convo_model.c`: `convo_new_decide`, `convo_subtitle`, `convo_row_is_current`, `convo_when`, and `rc_step`, Recent's state machine. One implementation, an instance per agent. |
| `shell` / `app_api` | The long-press menu has two kinds (`sh_menu_open(m, SH_MENU_CLEAR\|SH_MENU_CONVO)`): an app that offers `convo_menu`, `convo_new` and `convo_recent` gets New / Recent / Cancel; one that offers `can_clear` / `clear` keeps Clear chat. |
| `ui_kit` | `ui_header_sub`, `ui_phone`, `ui_menu_sub`, `ui_menu_row`, `ui_ghost_button`, `ui_convo_row`, `ui_skeleton_row`, `ui_empty_state`, `ui_app_chip`, `ui_fade_bottom`. A Recent row is four widgets on shared styles, its glyph and accent bar drawn in its own draw event — clipping a child to its corners would cost a layer the size of the strip. |
| `svc_audio` | `svc_audio_continue`: a switch forgets a new conversation asked for before it. |

### The memory budget

Static DIRAM (`.dram0.data` + `.dram0.bss`; 0.0.14 rebuilt from its source to the byte of its
`dist/`): 148,968 → 149,800 B, **+832 B** (`.bss` +776, `.data` +56) — `svc_convo`'s queue of four
requests and four answers, each agent's title (41 B) and flags, the counts; the two `convo_t`s'
Recent fields; four shared row styles. `.iram0.text` is unchanged. App: 1,944,608 → 1,963,008 B
(+18 KB: the service, the reader, Recent and the new widgets; 53% of the slot free).

**On the heap, only while it is needed:**

- **The Recent list, 1,768 B** (ten `convo_row_t` of 176 B: id 37, title 41, last line 81, the
  time and two flags), `calloc`ed when Recent opens and freed when it closes.
- **A reply buffer, 6,146 B** (`CONVO_BODY_MAX` + 2), allocated by the main task for one request
  and freed by the UI task once applied — milliseconds after it arrives. The box's longest answer
  (ten full rows, or a switch's six messages with the 1,000-byte answer) is under 4 KB; a reply
  that fills the buffer is taken as cut off.
- **1,024 B of scratch** on the UI task while a switch's messages are read into the thread; a
  switched thread lives in `svc_messages`' existing buffers (eight messages, the one long answer),
  so nothing grows there.

One request in flight, on the main task's existing loop: no new task and no new stack. LVGL's pool
peaks at 34.9 KB in the sim's busiest new state (`jerv-switched`, the thread under Recent just
closed; `jerv-recent` 33.8 KB) on its 64-bit host, under Settings' 36.9 KB: a Recent row is four
widgets on shared styles with its glyph drawn, not built (the first draft, a widget per stroke and
a corner-clipped bar, peaked at 81 KB). `int_min` should read about 0.8 KB lower than 0.0.14's; the
worst transient is the list and a reply buffer together, ~8 KB, for the moment a list lands.

### What to check on the glass

1. Open Jerv: under "Jerv" the header says what the PWA calls the current "Desk panel" chat (or
   "Desk panel" if Jerv has not named it).
2. Long-press the thread: New conversation, Recent conversations, Cancel — no Clear chat. Long-press
   Quorra's thread: Clear chat, as before.
3. Recent conversations: the panel's chats with a microphone, the PWA's with a phone, the current
   one barred and chipped, times in 24-hour. Tap a PWA chat: "Opening…", then its last exchanges
   under its title with "Earlier messages are in the app". Hold KEY and ask a follow-up: it lands
   in THAT chat in the PWA.
4. New conversation: the thread empties to "New conversation"; a second New says "Already a new
   conversation". Talk: a new chat appears in the PWA, and within the turn Jerv names it — the
   header shows the name once the answer lands.
5. In Recent, press KEY: Recent closes and the talk goes to the current conversation. BOOT in
   Recent returns to the thread.
6. Minecraft → Dave: the same menu and Recent, in Dave's green, listing only Dave's chats.

## What 0.0.16 adds: voice messages with the twins' panels

The owner, 2026-10-12: *"Am I able to receive voice messages now from the kids' other jpanel?"*
Until this image the answer was no: a twin's panel could already send "to Dad", but only the PWA
read those messages, and the two person threads here were stubs that said "Not sent". The desk
panel is now a second owner client for the same messages, next to the PWA (plan §1 "Voice
messages with the twins").

### On the glass

- **Her thread lists the real messages**: the box's last four with her, hers on the left with ▶,
  her length and the amber dot until it has been heard, his on the right with "Sent" or, once she
  has played it on her panel, "Played". The words are the box's transcript ("Voice message" when
  there is none). "Fetching messages…" until the box has answered once; "No messages yet. Hold KEY
  to send Quorra one." when there are none.
- **A tap on ▶ plays it** through the speaker, streamed from the box as it plays (no clip is held
  in RAM), with the reply's speaker start from 0.0.12 so its first word is not clipped. ⏸ (or KEY,
  or BOOT, the shell's stop) stops it. Played to its end with its digest checked, it is marked
  played here at once and its "played" stamp goes to the box, so the PWA shows it played too; a
  play stopped part way, or cut off by the radio, leaves it unplayed on both (0.0.17 changed this
  at the owner's word: heard once its audio starts, "What 0.0.17 adds"). A stamp the box does
  not take (no Wi-Fi, a box restart, a 5xx) waits in a list of up to eight kept in NVS (namespace
  `jpanel`, key `post_stamps`, written at most every 2 s), sent one at a time and retried after
  5 s, 15 s, 60 s and then every five minutes until the box answers 204; a 404 (the message is
  gone) is dropped. A reboot or a long outage loses none of them, and while one waits the message
  stays played on the panel whatever the box's list says, so its badge never comes back.
- **Hold KEY in her thread** to send her a voice message: the ring says "Message to Quorra", "0:30
  max" (the box keeps 30 s); let go and the bubble says "Sending…", then her panel has it exactly
  as if Dad had spoken into the PWA, and the bubble shows the box's transcript and "Sent". A
  failure says why ("Not sent: the upload was damaged", "…the box did not answer"); a hold of
  silence says "Nothing heard". KEY outside her thread is still Jerv's (0.0.14).
- **A new message from her**: the badge on her home tile, the clock face's corner badge (never a
  pop-up on the clock — the face's existing rule), the thread updating live if it is open, and
  one soft two-note chime — only with Notifications → chime on, outside the night window, and
  never over a hold, an answer, a play or an alarm. The messages waiting at boot are badged, not
  chimed.
- **No auto-play.** A message arriving never makes a sound but the chime; she is heard when he taps.
  Simple, and the same choice the PWA makes.
- Alarm and notice precedence are unchanged: a ringing alarm stops a play (as it stops a reply)
  and takes the speaker; a girl's message raises no overlay at all, so nothing in `shell_core.c`'s
  ordering moved.

### The protocol

Four routes behind the talk's own gate (`panel_agent.agent_allowed`: a `display` panel the owner
switched on — the twins' `jpet` panels get 403 and nothing else), each a thin door onto
`api/jpanel.py`'s own functions, on owner-scoped sessions (backend `api/panel_messages.py`):

| Request | Answer |
| --- | --- |
| `GET /endpoint/messages/{device}?limit=4` | `{"device_id","name","tag","messages":[{"id","mine","text","ms","played","voice"}…]}` — the owner's conversation with that twin (what she sent him, what he sent her; never twin-to-twin post), oldest first, text ≤ 255 B cut on a word. |
| `GET /endpoint/messages/{device}/{id}/pcm` | 16 kHz mono s16, with `X-Jpanel-Sha256` — the body a twin's panel replays. |
| `POST /endpoint/messages/{device}/{id}/played` | 204: the PWA's own "played" stamp. Only a message she sent him. |
| `POST /endpoint/messages/{device}/send` | Chunked 16 kHz s16 as it is recorded, `X-Jpanel-Sha256: trailer` and the 32 raw bytes of sha256(audio) after the audio (the digest cannot ride a header ahead of a stream) → 201 `{"id","text","ms"}`, filed through the PWA's `send_audio` path; 422 when the digest does not match, 413 past 30 s, 400 for silence. |

**The new-message signal rides the 3 s debug pickup**: `GET /endpoint/debug/next` carries
`X-Jpanel-Post: <8 hex>` for this panel, a tag that moves whenever the owner's post does (a message
filed, played or cleared). When it moves the panel fetches both threads — so a twin's message is on
the glass within a pickup or two, and an idle panel makes no extra request.

### The pieces

| Where | What |
| --- | --- |
| `svc_post` (new) | The service: the person threads from the box, each message's box id against its bubble, the unconfirmed "played" stamps (`post_stamps_*`: eight kept in NVS, deduplicated, one in flight, retried on `post_backoff_ms`, dropped only on 204 or 404), the pickup's tag, the chime decision. `post_logic.c` (host-tested): the paths (only a box id reaches one), the two answers' readers, the bubble, `post_arrivals` (new = hers, unplayed, not in the last list; the first list of a boot counts none), `post_chime`, `post_tag_feed`, `post_upload_room` (the upload stops at the box's 30 s). |
| `main/post_link.c` (new) | A thread's fetch and a "played" on the main loop, after a talk and the conversations. |
| `svc_audio` | Two new kinds of exchange through the same `tk_t`: `TK_KIND_POST` (recorded and uploaded like a talk, answered with an id and a transcript, no speech) and `TK_KIND_LISTEN` (no hold: `tk_listen` starts at the send, the audio goes through the reply's ring and speaker start). `svc_audio_play` and `svc_audio_chime` are real; 0.0.11's "Not sent" placeholder is gone. |
| `main/talk_link.c` | `post_exchange` (the digest kept with mbedTLS as the audio goes up, then sent as the trailer) and `listen_exchange` (a GET read in slices into the ring, hashed as it plays and compared with `X-Jpanel-Sha256`). |
| `main/box_http.c` | Reads `X-Jpanel-Post` and `X-Jpanel-Sha256` off a reply; a stream may carry the integrity header; `box_http_get_open`, a GET whose body is read in slices. |
| `svc_messages` | `svc_messages_fake_set`: a person thread replaced by the box's list, keeping the panel's own send on its way or failed. |
| `app_thread` | The list, ▶ and ⏸, the toasts, the 30 s ring, the open fetch. `ui_kit`'s ring says its own cap. |

### Debugging it

- The snapshot gains a **`post`** section: `tag`, `queued`, `arrivals`, `chimes`, `heard`,
  `stamps_waiting`, `stamp_fails`, `stamp_retries`, `stamps_dropped`, `last_status`, and per thread `device_id`, `loaded`, `listed`, `unplayed`. `audio` gains `kind`
  (`agent` / `message` / `play`), `playing`, `plays`, `play_failed`, `posted`, and the codec's
  `chimes`; an open thread's `app` section says `loaded`.
- Telemetry's note gains `post=rq:N,ok:N,fail:N,new:N,chimes:N,stamps:N,retries:N,tag:<tag>,last:<status>`.

```sh
scripts/debug-connect.sh panel-cmd c6 open --arg app=thread1
scripts/debug-connect.sh panel-cmd c6 snapshot           # post, audio.kind, app.loaded
scripts/debug-connect.sh panel-shot c6 thread.png
```

### The memory budget

Static DIRAM (`.dram0.data` + `.dram0.bss`; 0.0.15 rebuilt from its source to the byte of its
`dist/`, app `00b4074a…`): 149,800 → 152,208 B, **+2,408 B** (all `.bss`; `.data` and `.iram0.text`
unchanged): `svc_post`'s two slots (each its panel's id, the last list's four box ids and their
bubble ids), the eight unconfirmed "played" stamps (594 B, a box id and its panel's each), its queue of four requests and four answers
(968 B, each carrying its 112-byte path), the send's or play's box path on each side of the talk
(224 B), the SHA-256 context (116 B), counters. The person threads still keep four messages each in `svc_messages`' existing buffers, so
nothing grew there. The send's answer is read into the talk's own 1 KB slice buffer once the upload
is over. App: 1,963,008 → 1,976,880 B (+14 KB; 53% of the slot free).

**On the heap, only while it is needed:** a thread's reply buffer, 3,074 B, from its fetch until
the UI task has applied it, and the parsed list, ~1.2 KB, for that moment; a play uses the talk's
16 KB ring, allocated when it starts and freed when it ends, exactly as a talk does — never both,
since one exchange runs at a time. No new task, no new stack. LVGL's pool peaks at 22.0 KB in the
sim's busiest new state (`quorra-sending`, 64-bit host), under Settings' 36.6 KB. **`int_min` should
read about 2.5 KB lower than 0.0.15's**; the worst transient is a play's ring (16 KB, the same as a
talk's) and nothing else.

### What to check on the glass

1. On a twin's panel, record a message "to Dad". Within a few seconds the desk panel's home tile
   for her shows a badge (and the clock face its corner badge), and the panel chimes once if the
   chime is on and it is not night.
2. Open her thread: her message on the left with ▶ and its length. Tap ▶: it plays; the dot goes
   when it finishes; the PWA's thread shows it played.
3. In her thread, hold KEY and say something; let go. "Sending…", then the box's transcript and
   "Sent". Her panel shows Dad's message waiting; once she plays it, his bubble says "Played".
4. KEY outside her thread still talks to Jerv; KEY over a message playing stops it.
5. In the snapshot: `post.tag` moving with each message, `post.thread1.loaded` true,
   `audio.plays` and `audio.posted` counting.

## What 0.0.17 adds: a thread that holds still, a bar that moves, heard once its audio starts

The owner's first use of 0.0.16 on the panel, 2026-10-13: *"When I play back their audio the
playback works but the screen kind of jumps around depending on when I play. Also, when I hit play
the status bar as far as progress, I expect to go from left to right and that does not occur. A
message should no longer be considered unread if I even hit play once on it."* Three fixes in her
thread, and one memory change the same session's numbers asked for (`system.int_min` 44,084 B, under
the ~45 KB line this README has drawn since 0.0.11).

### On the glass

- **The thread holds still.** The cause: every press of ▶ or ⏸ rebuilt her whole thread — once from
  the button's own handler (`lv_async_call(refill)`) and again from the play's `MSG_ARRIVED` — and a
  play's end rebuilt it a third time. A rebuild is `lv_obj_clean` and a new list, and an emptied
  page clamps its scroll, so whoever had scrolled up to an older message was thrown to wherever the
  new list's scroll happened to land. Now the thread does what 0.0.14 made Jerv's do: it keeps the
  bubbles it drew (`drawn_t`: the message, its ▶/⏸ glyph, its bar, its dot) and a fingerprint of
  what the list is built from (`thread_shape_*`: ids, words, status, length, order — not whether a
  message was heard). A play's start, its progress and its stop change only the glyph, the bar's
  width and the dot, where they stand. The list is rebuilt only when the messages themselves change
  (a new one, a transcript, a send's status), and then it keeps the reader's scroll exactly as
  `convo.c` does, pinning to the end only on opening and when a new message arrives. The heard dot
  is now made clear rather than removed, so the ▶ does not shift sideways when it goes.
- **The bar fills left to right.** The cause: the thread passed a constant 0 as the play's progress
  (`thread_bubble(&m, svc_audio_playing(), 0, &tb)`) and nothing ever updated it, so a playing bar
  sat empty and jumped full when the play ended. Now `svc_audio_played_ms()` counts the samples the
  audio task actually wrote to the speaker (not the amp's pre-roll, not an underrun's zeros), and
  the bubble fills against the message's own length to the millisecond (`msg_t.dur_ms`, from the
  box's `ms`; the rounded seconds only when the box gave none), ten times a second, from a 100 ms
  LVGL timer that lives only while the thread is open. An underrun holds the bar where it is;
  stopped early, played out or failed, the message is heard, so its bar shows full like every heard
  message (the mock's rule). The time beside it stays the message's length, as the mock draws it.
- **Heard once its audio starts.** The dot goes as ▶ becomes ⏸; the moment the box answers the
  play with its audio — a fraction of a second after the tap — the message is heard: her tile's
  badge and the clock face's corner badge (within its one-second redraw) drop, and the "played"
  stamp is queued for the box through 0.0.16's NVS-kept retry list, so the PWA shows it played too.
  The stamp goes out on the main task's one connection right behind the play's stream. Stopped,
  cut off by the radio or failing its digest after that, it still counts as heard; the digest
  still judges the audio itself (a mismatch counts in `audio.play_failed`). A play the box never
  answers with audio (unreachable, not a 200, no memory for the ring) leaves it unread and sends
  no stamp, and so does a press the panel refuses (no Wi-Fi, a talk under way). A message already
  heard is never stamped again, so replays cannot fill the stamp list or push a waiting stamp out
  of it (`stamps_dropped` now counts only a real eviction).

### The memory: where 44 KB came from, and the lever pulled

`int_free` read 72 KB with the box's kept session up (the 3 s pickup keeps it open). The low point
is 28 KB under that, and a play is the one place two big things could meet:

| What | Internal RAM | When |
| --- | --- | --- |
| The play's ring | 16,384 B | 0.0.16: from the tap on ▶ until the play is over |
| A TLS session (mbedTLS in 16 KB + out 4 KB, context and the peer's certificate) | ~24 KB | while the connection is up; given back when it closes |
| A handshake's working set above the session (ECDHE, the bundle's chain check) | ~12 KB | for the handshake only |
| A thread's reply buffer and its parsed list | 3,074 + ~1,200 B | milliseconds, as a list lands |

A play stopped part way aborts its stream, which closes the connection (`box_http_stream_abort`);
so does the box's own idle close. The next ▶ then held the 16 KB ring from the tap while its GET
paid a fresh handshake: 72 + 24 (old session gone) − 24 (new session) − 12 (handshake) − 16 (ring)
≈ **44 KB**, which is what the panel read. Stopping one message and playing the next — the owner's
way through a thread — is exactly that.

**The lever: a play's ring is allocated after its GET has its answer, not at the tap**
(`talk_net_ring`, from `listen_exchange` once the box has said 200). The handshake and the ring now
never hold RAM at once: the play's low is the larger of the two, 72 − 16 ≈ **56 KB** during the
stream (the session up, the ring held) and ≈ 60 KB during a handshake. It costs nothing at the
speaker — the ring is still the full 16 KB and the speaker's start still waits for its 150 ms of
audio — and a GET refused, or a box that does not answer, now allocates nothing at all. The other
levers were weighed and not pulled: an 8 KB ring (0.0.10's (2)) halves the slack on the radio for
every talk and play to win 8 KB the ordering wins for free; the thread's 3 KB reply buffer is
already serialised behind a play (one connection, one request at a time, freed milliseconds after
it lands); and the new stamp at play start waits in the request queue behind the stream.

**What it does not cover:** a talk's ring must exist at KEY down, because the microphone records
into it while the upload's connection opens. A hold that starts within a few seconds of a dropped
connection (just after stopping a play, before the next pickup reconnects) can still meet a
handshake. If `int_min_at` (below) says `talk+handshake+ring` or `message+handshake+ring`, that is
the case, and the 8 KB ring is the next lever.

**Which phase set it, from now on.** The snapshot's `system` gains **`int_min_at`**, and telemetry's
note **`low=<bytes>@<what>`**: when the internal heap reaches a new low, the first task to see it —
the main task at each change of work, the UI task every pass — records what the main task was
doing (`pickup`, `report`, `manifest`, `ota`, `talk`, `message`, `play`, `post`, `convo`,
`siblings`, `alarms`, `idle`), `+handshake` inside a new connection's TLS handshake, and `+ring`
while a talk's or a play's ring is held. A reading of `low=…@play+ring` at or above ~52 KB is this
image working.

### The pieces

| Where | What |
| --- | --- |
| `app_thread` | `thread.c`: `drawn_t` per bubble, `refresh` (rebuild only on a new shape, else `update` in place), the 100 ms tick, a rebuild that keeps the scroll, `rebuilds` in its debug section. `thread_model.c` (host-tested): `thread_bubble` with the played milliseconds, `thread_play_pct`, `thread_shape_msg` / `thread_shape_end`. |
| `ui_kit` | `ui_bubble_parts_t` (the glyph, the bar's fill, the dot) filled by `ui_bubble`, and `ui_bubble_set`. |
| `svc_messages` | `msg_t.dur_ms`, in what was the struct's padding (no message grew). |
| `svc_audio` | `svc_audio_play` marks the message heard (`svc_post_heard`) as it starts; `svc_audio_played_ms` from `talk_audio_played`, counted by the audio task; `talk_net_ring` and a ring taken and freed under the lock from either task; `talk_ring_held`. |
| `main/talk_link.c` | The ring after the GET's 200; a play's kind named for the low-water mark. |
| `svc_debug` / `main` | `dbg_sys_doing`, `dbg_sys_handshake`, `dbg_sys_low_check`, `dbg_low_feed` (host-tested); `box_http` brackets a handshake; the UI task checks every pass. |
| `sim/` | `quorra-mid` (half way, the bar half full) and `quorra-heard` (one press and a stop: no dot, a full bar); `--thread`, run by `render.sh`: her four long messages, scrolled part way up, ▶ → progress → ⏸ → a play to its end, with the scroll, the button's place, the widgets and the rebuild count unchanged throughout. |

### The memory budget

Static DIRAM (`.dram0.data` + `.dram0.bss`): 152,208 → 152,800 B, **+592 B**: four thread
instances' drawn bubbles (four `drawn_t` of 28 B each) and their view state (~530 B in all), the
low-water attribution (~60 B), the play's sample count. `.iram0.text` unchanged. App: 1,976,880 →
1,979,632 B (+2.7 KB). No new task, no new stack, no new heap; the ring is the same 16 KB, taken
later. LVGL's pool peaks at 17.6 KB in the new states (`quorra-mid`, `quorra-heard`; 64-bit host),
under Settings'. **Expect `int_min` ≈ 56 KB after a session of plays** (`low=…@play+ring`), ~12 KB
better than 0.0.16's 44,084 B; a talk started just after a dropped connection is the case still
open (above).

### What to check on the glass

1. Open her thread with three or four messages, scroll up to an older one and tap ▶: the page does
   not move, the button becomes ⏸ in place, the dot and her tile's badge are gone at once.
2. The bar fills from the left as she speaks and reaches the right as she finishes. Tap ⏸ part way:
   the bar shows full (heard), the page still has not moved, and the message stays heard after the
   next list from the box and after a reboot.
3. The PWA shows the message played within seconds of the play ending.
4. Stop one, play the next, a few times; then the snapshot: `system.int_min` and `int_min_at`
   (expect ≈ 56 KB at `play+ring`), and the `app` section's `rebuilds` unchanged by the plays.

## What 0.0.18 adds: a play's own ring, auto-rotate, and touch calibration

The owner approved this scope on 2026-10-13 (*"Go ahead with 0.0.18."*): the memory 0.0.17 left at
a play, and the two Settings rows that had said "a later update" since 0.0.8 — Auto-rotate (plan
§2b, wave C2r) and Touch screen → Calibrate (the mock's `Calib.dc.html`).

### The memory: what a play holds, and the lever pulled

0.0.17 on the board, after a power-on and three plays of the twins' messages: `system.int_min`
**43,548 B at `play+ring`**, `int_free` 71.7 KB with the kept session up, `int_largest` 52 KB, the UI's
start low-water 63 KB. 0.0.17 had expected ~56 KB (72 − 16 for the ring), so about **12 KB more is
held during a play than it modelled** — and the label says when: inside `listen_exchange`, after its
handshake (no `+handshake`), with the ring held.

What is live during a play's stream, from the code:

| What | Internal RAM | During a play | Counted in 0.0.17's model? |
| --- | --- | --- | --- |
| The kept TLS session (mbedTLS in 16 KB + out 4 KB, context, the peer's chain) | ~24-28 KB | yes — the play rides it | yes, inside the 71.7 KB baseline |
| A second TLS session | — | **never**: the image has one `esp_http_client` (`box_http.c`); a play's GET goes on it, a dropped connection is closed (and its mbedTLS context freed) before the next opens on the same client, and the OTA download's own session opens only after `box_http_close` | — |
| mbedTLS record buffers growing | — | **no**: `CONFIG_MBEDTLS_DYNAMIC_BUFFER` is off, so in/out are allocated at the handshake at their full size and never grow | — |
| The play's ring | 16,384 B | yes | yes |
| **The receive window, full** | ~6.5 KB | **the whole play**: the box sends a stored clip as fast as TCP lets it and the reader is paced by the speaker, so lwIP's window (`TCP_WND` 5,760 B = 4 × 1,440) sits full of unread data, each segment in a Wi-Fi dynamic RX buffer sized to its frame (~1.6 KB with the 802.11 headers, the RX control block and the pbuf) | **no** |
| A loss in flight (AMPDU reorder, lwIP's out-of-order queue) | up to a few more frames | while a gap is filled | no |
| The "played" stamp's NVS write (UI task, ~2 s into the play) | a few hundred bytes | briefly | no |
| The JT2 decoder, the 1 KB slice buffer, the SHA-256 context | static | — | — |
| A thread's fetch buffer (3,074 B) and its parse | — | **not during a play**: the main task makes one request at a time, after `talk_link_run` returns, and the low would be labelled `post` | — |
| The speaker | — | the I2S DMA buffers are allocated at boot; opening the speaker reconfigures TX at the same size, and `esp_codec_dev_open` allocates nothing | — |

So the ~12 KB is what the network holds behind the ring: the full receive window for certain (~6.5
KB), the rest most likely loss-and-reorder frames and the stamp's write. The host cannot measure
Wi-Fi buffers, so this image measures them on the board: the snapshot's `audio` section now keeps
the last play's free heap **before its GET** (`play_before`), **once the box answered**
(`play_answered`), **with the ring taken** (`play_ring_free`) and **the lowest seen while it
streamed** (`play_low`). `play_ring_free − play_low` is exactly what lwIP and mbedTLS held behind the
ring.

**The lever: a play's ring is 6 KB, not a talk's 16 KB** (`tk_ring_bytes`, `TK_RING_PLAY`). The
network already buffers a stored message (the window, and mbedTLS's decrypted record), and an
underrun re-arms the speaker's start (0.0.12's gate: a pause, never a stutter), so the play's ring
needs only the speaker's 150 ms start (4,800 B) plus one network slice (1 KB). Talks and voice
messages keep 16 KB, which they need: a talk's ring is the microphone's while the upload connects.
The arithmetic, against the board's own numbers:

| | 0.0.17 (measured) | 8 KB ring | **0.0.18: 6 KB ring** |
| --- | --- | --- | --- |
| Free with the session up | 71.7 KB | 71.7 KB | 71.7 KB |
| The ring | − 16.4 KB | − 8.2 KB | **− 6.1 KB** |
| What the network held behind it (71.7 − 16.4 − 43.5) | − 11.8 KB | − 11.8 KB | − 11.8 KB |
| `int_min` at `play+ring` | **43.5 KB** | 51.7 KB — short of 52 | **≈ 53.8 KB** |

8 KB, the first number weighed, lands under the 52 KB target; 6 KB clears it by ~1.8 KB (by ~0.5 KB
reading every KB as 1,024 B). The buffered audio is ~0.19 s in the ring plus ~0.18 s in the window
plus whatever of mbedTLS's record is still unread. The other lever weighed — streaming the play over
the kept session rather than a new one — is what 0.0.16 already does: there is only ever one.

One more fix on the way: a KEY hold refused while a play was still being fetched took a 16 KB talk
ring before finding the exchange busy, and the play then adopted it. A hold now takes a ring only when
it can start (`tk_can_begin`), and a play takes its own size.

### Auto-rotate

- **Which way is down** comes from the QMI8658's accelerometer in the glass's plane (±2 g, 62.5 Hz,
  the gyroscope off), read at 8 Hz **on the UI task's own passes** — no task of its own — only while
  the screen is lit and the switch is on; dark or off, the accelerometer is powered down.
- **Hysteresis** (`orient_logic.c`, host-tested): a new edge counts only **25° past the 45° line**
  into its quadrant (within 20° of that edge's axis), and only after **700 ms** of every reading
  saying so. A lean near 45° holds whatever is showing. **Flat** (under 0.5 g in the glass's plane:
  within 30° of lying face up or down) changes nothing; **moving** (a total more than 0.2 g from 1 g)
  starts the dwell again.
- **How it turns: in the flush, not in LVGL and not in the CO5300.** LVGL keeps drawing the same
  upright 480×480 screen (the glass is square, so nothing is laid out again); each strip is turned on
  its way to the glass. **Upside down** reverses the strip where it lies and sends it to the mirrored
  window — no memory at all. **On its side**, the strip's columns are gathered into a 2.5 KB piece
  buffer and sent a few glass rows at a time (`disp_rot_piece`; an area LVGL draws narrow is tall, so
  the rows per piece are worked out per area). Every window stays even-aligned, as the CO5300 needs.
  Why not the alternatives: LVGL 9.2's own rotation in partial mode still leaves the flush to turn
  the pixels, with a second ~38 KB strip buffer for 90°/270° — RAM this board does not have; and the
  CO5300's MADCTL has no row/column exchange (HARDWARE.md §2), only mirrors, which would cover 180°
  alone and change the one init table proven on the glass.
- **Touch turns with it**, in the one place that maps it (`tp_xform`): raw → the 0.0.7 mapping → the
  calibration → the rotation undone. It follows the turn the **glass actually took**
  (`svc_display_on_rotated`), never the request: a quarter turn that finds no 2.5 KB for its piece
  buffer (most likely mid-play, the heap's low point) leaves the glass as it was and retries every
  pass, and touch, `svc_orient_rotation` and a calibration made meanwhile all stay with what is
  showing until it succeeds, then move together. The buttons do not move: KEY, BOOT and PWR mean
  what they did.
- **Off** is the panel's own upright, at once. **No sensor** (no answer at 0x6B, or not a QMI8658):
  the panel stays upright for the boot, Settings draws the switch dead with "The tilt sensor did not
  start, so the screen stays upright", and telemetry says `orient=failed:<step>`.
- **Screenshots stay upright**: the debug screenshot taps the strips before they are turned, so it is
  what LVGL drew; `display.rotation` and `orient.rotation` say how the glass shows it.
- **The chip's axes against the glass** are `imu_axes` = 6 on the owner's board (measured
  2026-10-13, HARDWARE.md §6). `imu_axes` (NVS, 0-7, the debug `set`) swaps the chip's X/Y and flips
  either axis; the default 0 assumes +X right, +Y down, which shows this board upright as 180°.

### Touch calibration

- **Settings → Touch screen → Calibrate** puts up the mock's screen: "Tap the cross", "1 of 4 · BOOT
  cancels", an amber cross with a ring and three dots, **one at each corner** (the mock's outer knots,
  44 px in) rather than the mock's sixteen. Three taps per cross that agree (each within 24 px of
  their mean) turn it green and bring the next; a tap more than 64 px from the cross is not counted
  ("Tap the centre of the cross"); three that disagree start that cross again ("Those taps disagreed:
  again"). **BOOT cancels** and stays in Settings with the old calibration (or none) in force; KEY
  still talks to Jerv. While it runs the screen holds whichever way up it is.
- **The fit** (`tp_cal_fit`, host-tested): an affine least-squares fit, x' = a·x + b·y + c and y' =
  d·x + e·y + f, **in the glass's own frame**, over the 0.0.7 measured mapping (x = 479 − rawB, y =
  rawA), from the four crosses' true places and the taps' means (both turned back into the glass's
  frame). Four points for six numbers leave a residual to judge it by. Refused, with "Not saved:
  those taps did not agree. Try again", when the taps span no plane (`few`), when it needs a scale
  past 15 %, a shear past 0.1 or a shift past 48 px anywhere on the glass (`wild`), or when a tap
  ends more than 12 px from its cross after it (`scattered`).
- **Kept** in NVS namespace `jpanel`, key `tp_cal` (a 32-byte blob: layout, the six numbers in Q16,
  the day), and applied to every touch from then on; **"Reset calibration"** (shown once there is
  one) erases it and puts the measured mapping back. The card's corner says Never, Today, or the day.
- **With rotation**: the calibration is applied in the glass's frame, then the rotation, so one
  calibration made in any orientation serves all four.

### The pieces

| Where | What |
| --- | --- |
| `svc_audio` | `tk_ring_bytes` (`TK_RING_TALK` 16 KB, `TK_RING_PLAY` 6 KB), `tk_can_begin`, a ring sized per exchange (`talk_ring_bytes`), `talk_net_mem` and the `audio.play_*` fields. |
| `main/talk_link.c` | The play's free heap before, answered, with its ring, and its low while streaming. |
| `svc_orient` (new) | `orient_logic.c` (pure: down from a raw reading, the margin, the dwell, flat and moving), `svc_orient.c` (the 8 Hz read on the UI task, the switch, the hold, the `orient` snapshot section and `orient=` note), `esp/orient_port_qmi8658.c` (id, reset, ±2 g at 62.5 Hz, power up/down, the 6-byte read, all through `svc_power_i2c_xfer`), `orient_port_fake.c` (the sim's and the host's tilt). |
| `svc_display` | `disp_rot_point`, `disp_rot_area`, `disp_reverse`, `disp_rot_piece`, `disp_rot_piece_rows` (pure, host-tested); the flush turning each strip, the turn applied between frames with the whole screen redrawn, `display.rotation`. |
| `svc_touch` | `tp_xform` (map → calibrate → unrotate), `tp_cal_fit` / `tp_cal_apply`, `tp_rotate` / `tp_unrotate` (pure); `svc_calib.c` (the fit's bookkeeping, the `calib` snapshot section); `esp/tp_cal_port_esp.c` (NVS); the touch task maps through a copy of the mapping each poll. |
| `svc_settings` | `imu_axes` (0-7, default 0). |
| `app_settings` | The Auto-rotate row live or dead; the calibration screen and its flow (`calib_flow_*`, pure); the Touch screen card's day and Reset calibration. |
| `main` | `svc_orient_start` after touch; the UI task's pass, the calibration loaded and touch following each turn; `low=` moved near the front of the note so a long note never cuts it. |
| `sim/` | Every strip also turned onto a glass framebuffer exactly as the flush turns it; `home-turned` (on its right edge) and `clock-upside-down` render the glass; `settings-no-imu`; `calib`, `calib-missed`, `calib-saved`; `--touch` runs a calibration, BOOT over it, and Reset. |

### Debugging it

- `orient`: `imu` (`ok` / `failed:<step>`), `auto`, `rotation` (degrees, what the glass shows) and
  `down` (the edge showing as the bottom), `asked` (only while a requested turn waits for memory),
  `seen` (`edge` / `between` / `flat` / `moving`) and `seen_edge`, `raw` (counts) and `raw_mg`
  (milli-g), `axes`, `axes_if_upright`, `axes_if_right_edge`, `powered`, `held`, `reads`,
  `read_errors`, `changes`, `chip` and `id` (WHO_AM_I / revision).
- **Settling `imu_axes` from the box takes two holds**, because one upright reading cannot tell the
  glass's x flip (and that bit is what tells 90° from 270°): (1) panel upright, still, a snapshot —
  `set imu_axes` to its `orient.axes_if_upright` before the second hold, since that helper starts from the
  current value; (2) panel on its **right** edge (turned a quarter turn
  clockwise, so the edge that was on your right is at the bottom), still, a snapshot — `set imu_axes` to its
  `orient.axes_if_right_edge`. That value is the whole answer; record it in HARDWARE.md §6. Telemetry: `orient=ok,rot:90,chg:N,rd:N,err:N,axes:N`.
- `calib`: `custom`, `capturing`, `date`, `fit` (a,b,c,d,e,f: scales and shears in thousandths,
  offsets in pixels — `1000,0,0,0,1000,0` is none), `fits`, `refused`, `last`. Telemetry's touch
  tokens gain `cal=custom|none` and `tp_rot=`; `tp_last`'s `x,y` are now after both.
- `display.rotation`, `rot_changes`, `rot_no_memory` (a quarter turn that found no 2.5 KB).
- `audio.ring` is the ring's real size; `audio.play_before` / `play_answered` / `play_ring_free` /
  `play_low` as above.
- A calibration can be driven from the box: `open` settings, then `inject` `touch` at each cross
  (44,44), (436,44), (436,436), (44,436), three times each; `inject boot` cancels.

```sh
scripts/debug-connect.sh panel-cmd c6 snapshot                       # orient, calib, audio.play_*
scripts/debug-connect.sh panel-cmd c6 set --arg name=imu_axes --arg value=4
scripts/debug-connect.sh panel-cmd c6 set --arg name=auto_rotate --arg value=0
```

### The memory budget

Static DIRAM (`.dram0.data` + `.dram0.bss`): 152,800 → 153,104 B, **+304 B**: the orientation state
and its describer, the calibration's coefficients and bookkeeping, the touch task's mapping, the
calibration screen's view, the play's heap readings. App: 1,979,632 → 1,992,656 B (+12.7 KB; 52% of
the slot free). No new task, no new stack. On the heap: a play's ring is **10 KB smaller** for its
whole length; the quarter-turn piece buffer is **2,560 B only while the panel is on its side**
(upright and upside down take none); the calibration's NVS write is a transient handle. CPU: one
6-byte bus read every 125 ms while lit (~0.3 ms each, well under 1 % of the UI task); a turned frame
adds the strip's reversal (180°) or gather plus a window per piece (90°/270°, ~15 pieces a full-width
strip). LVGL's pool peaks at 38.5 KB in `calib` (the screen over Settings; 64-bit host), under its
64 KB.

### What to check on the glass

1. **The play's memory.** Play three of her messages (stop one part way), then a snapshot:
   `system.int_min` at `play+ring` should read **≥ 52 KB** (expect ~53-54 KB); `audio.ring` 6144
   during a play; `play_ring_free − play_low` is what the network held behind the ring (expect
   ~6-12 KB). The sound should be as before; an underrun is a short pause, not a stutter.
2. **Auto-rotate.** With the panel standing on the desk, the screen must stay as it is. Turn it onto
   its right edge and hold it still: within a second the content turns to read upright; the same on
   its left edge and upside down. Lean it at about 45°: nothing flickers. Lay it flat: it keeps the
   last turn. Tap a tile in each orientation: it opens what is under the finger. If it turns while
   standing normally, or the right and left edges come out swapped (or upside down), settle the
   axes in two holds (Debugging it, above): **upright** → `set imu_axes` to `orient.axes_if_upright`,
   then **on its right edge** → `set imu_axes` to `orient.axes_if_right_edge`. Skipping the second
   hold leaves 90° and 270° swapped. Meanwhile Auto-rotate can be switched off in Settings — touch
   works whichever way the screen shows.
3. **Settings → Auto-rotate off**: the screen goes straight back to the panel's own upright.
4. **Touch calibration.** Settings → Touch screen → Calibrate: tap each cross until it turns green
   (three taps). The card then says Today and offers Reset calibration. Taps near the edges should
   land better than before; `calib.fit` in the snapshot shows the correction. Try it once with the
   panel turned, then upright: one calibration serves both. BOOT mid-way leaves the old one.

## How it talks to the box

The same protocol as the S3 panels, so the box needs no new route for it:

- **Provisioning** is the `jbrain` NVS namespace the box's flasher writes: `ssid`, `pass`,
  `api`, `token`, optional `ca`, `name` and `role`. An OTA rewrites only an app slot, so this
  survives every update.
- **Auth** is the unit's `device_key`, sent as `Authorization: Bearer …`.
- **TLS** trusts exactly one thing: the box's Caddy root when `ca` is present (LAN name), the
  public CA bundle when it is absent (public hostname). Never both.
- **`GET {api}/endpoint/firmware?board=c6-amoled-216`** is the manifest and the health signal,
  and since 0.0.8 the panel's clock: its reply's `Date` header sets the time ("What 0.0.8 adds").
  The image URL it returns carries the same board id and is fetched exactly as given. The id is
  one constant, `OTA_BOARD_ID` in `main/ota.h`; the box's per-board table must use the same
  string. Without it the box answers for the S3 panels.
- **`GET {api}/endpoint/debug/next?board=c6-amoled-216`** and **`POST
  {api}/endpoint/debug/{id}`** (since 0.0.9) are the debug relay's pickup and answer ("What
  0.0.9 adds"). The only routes that are the C6's own; the S3 panels never call them.
- **`POST {api}/endpoint/converse?persona=jerv|dave&voice=…&jt=2[&new_thread=true]`** (since
  0.0.10; Dave and `new_thread` since 0.0.11, `jt=2` since 0.0.12) is a talk to Jerv or Dave: the
  S3s' conversation route with the persona added, answered as the JT2 stream ("What 0.0.12 adds",
  The protocol). Uploaded chunked, as it is recorded; read until no byte has come for 45 s.
- **`GET {api}/endpoint/siblings`** (since 0.0.11) is the twins' panels' ids and names, for the
  two message threads ("What 0.0.11 adds").
- **`GET {api}/endpoint/alarms?etag=…`** and **`PUT {api}/endpoint/alarms/{id}`** `{"on":bool}`
  (since 0.0.13) are the owner's alarms and the switch on the glass ("What 0.0.13 adds").
- **`GET {api}/endpoint/convo/{agent}/recent`**, **`POST …/select`** and **`GET
  …/messages?limit=N`** (since 0.0.15) are the agent's Recent list, a switch, and the switched
  thread's last exchanges or the title alone ("What 0.0.15 adds").
- **`GET {api}/endpoint/messages/{device}`**, **`GET …/{id}/pcm`**, **`POST …/{id}/played`** and
  **`POST …/send`** (since 0.0.16) are the owner's voice post with each twin's panel: her thread, a
  message played, its "played", and a voice message to her; the debug pickup's `X-Jpanel-Post`
  says when to fetch the threads ("What 0.0.16 adds").
- **`GET {api}/endpoint/settings`** is not polled by this image. When a later one polls it, it
  must send the same `?board=` — the response's `fw_version` is per board.
- **`POST {api}/endpoint/telemetry`** is the heartbeat, in the S3's `TelemetryIn` shape:
  `version`, `uptime_ms`, `reset_reason`, the internal-heap readings (`free_heap`, `int_free`,
  `int_min`, `int_big`, `int_largest`), `wifi_reason` / `wifi_drops`, `ota_err` / `ota_tries`,
  `relinks`, `box_quiet_s`, and `note` — `board=jpanel-c6 image=display chip=esp32c6 rev=… dump=…`,
  because nothing else in the body says which board sent it, then each service's own tokens
  ("Reading 0.0.4 from the box" above). The note is bounded on its own, so a long one is cut
  rather than costing the whole report.

One request at a time, on one task, over **one HTTPS session kept open between requests** (since
0.0.9, `main/box_http.c`; until then each request was its own session, closed after). The S3
panels moved to one shared WebSocket (`../firmware/main/link.c`) because talk turns and polls
overlapped; this image still makes one request at a time, so a kept-alive HTTPS session is all
the sharing it needs. It is closed before the OTA download opens its own and whenever the radio
re-joins, so there is never a second.

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
AXP2101) and the display, and 0.0.6 the touch controller on the same bus; the rest are for later
waves.

| Function | Pins |
| --- | --- |
| BOOT button | GPIO9 |
| KEY button | GPIO10 |
| PWR button | GPIO18 |
| I2C (shared) | SDA GPIO8, SCL GPIO7 — AXP2101, PCF85063, QMI8658, CST9220, ES8311, ES7210 |
| IMU interrupts | INT1 GPIO16, INT2 GPIO17 |
| Touch | TP_RST GPIO11, TP_INT GPIO5 |
| Display (CO5300, QSPI) | CS GPIO15, SCL GPIO0, D0 GPIO1, D1 GPIO2, D2 GPIO3, D3 GPIO4; LCD reset is the AXP2101's ALDO3 |
| I2S | MCLK GPIO19, SCLK GPIO20, LRCK GPIO22, ASDOUT GPIO21, DSDIN GPIO23 (driven since 0.0.10); the speaker amp's enable is the AXP2101's ALDO2 (schematic + BSP; polarity unverified on the board, `HARDWARE.md` §5) |
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
                    svc_power, svc_display, svc_buttons, svc_touch, svc_settings (NVS), svc_rtc
                    (PCF85063 + the box's clock), svc_net, svc_box and svc_audio (ES8311 + ES7210,
                    the talk to Jerv and Dave, the alarm sounds), svc_siblings (the twins' panels'
                    names, NVS-cached), svc_alarms (the box's alarms, NVS-cached), svc_convo (an
                    agent's conversations), svc_post (the voice post with the twins) and svc_orient
                    (auto-rotate from the QMI8658, 0.0.18) are real: the device builds their esp/ file and pure logic, the sim and host
                    the top-level files
  convo/            the one conversation view every agent's app instantiates (Jerv, Dave)
  svc_debug/        the debug surface: describer registry, log ring, commands, stripe screenshots
  app_jerv/ app_thread/ app_clock/ app_settings/ app_minecraft/
                    each a descriptor + a pure *_model.c + a view; app_thread is one component
                    instantiated per sibling panel by slot; app_jerv and app_minecraft (Dave)
                    are each an instance of convo
host/               the pure modules' suite
sim/                every component against LVGL on the host, headless to PNG
tools/check_app_deps.py   an app may require app_api, ui_kit, convo and LVGL and nothing else
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
Fonts are LVGL's Montserrat plus the generated clock digits and punctuation fallbacks in
`components/ui_kit/fonts/` ("What 0.0.8 adds", Fonts). States the mock has no board for:
`settings-end`, `settings-armed` (the first Disconnect tap), `settings-usb`, `clock-badge`,
`clock-alarms-end`, `rec-anywhere` (KEY from Settings), `jerv-after-key` (the hold waiting for
its transcript), `jerv-reply` and `jerv-reply-text` (the box's answer, spoken and text-only),
`boot-long` (BOOT held in Settings), and from 0.0.11 `home-unnamed` / `thread-unnamed` (before the
box has sent the panels' names), `dave`, `rec-dave`, `dave-reply`, `compass`, and
`quorra-menu`, `quorra-menu-armed` and `quorra-cleared` (0.0.11's `chat-menu` states, in a
person's thread since 0.0.15), from 0.0.15 `jerv-menu`, `jerv-recent`, `jerv-recent-loading`,
`jerv-recent-end`, `jerv-recent-empty`, `jerv-switched`, `jerv-new`, `dave-menu` and
`dave-recent` (the box's conversations answered by the sim from the mock's sessions), from 0.0.16
`quorra-playing`, `quorra-sending`, `quorra-sent`, `quorra-new`, `home-new` and `clock-new`, and
from 0.0.17 `quorra-mid` and `quorra-heard`, from 0.0.18 `home-turned` and `clock-upside-down` (the
panel turned: the PNG is the glass, each strip turned the device's way), `settings-no-imu`, `calib`,
`calib-missed` and `calib-saved` (the
twins' threads answered by the sim as `api/panel_messages.py` does, with placeholder names and
words — her message playing, a voice message to her on its way and filed, a new one live in her
thread, on her tile's badge and on the clock face's), and from
0.0.13 `ringing-dark`,
`ringing-over-notice`, `alarm-snoozed` and `alarm-test`. The alarms come from the box's own JSON
through `svc_alarms_offer`, as the siblings' names do. The sim feeds the panels' names through the
box's own JSON and the panel's own path (`svc_siblings_offer`), with placeholder names. Each render also
prints LVGL's pool peak (64-bit pointers; the device's pool is 64 KB with 32-bit ones).

### Debugging a panel with no cable

`svc_debug` is how a session holding the owner's debug token reads and drives this panel through
the box (plan §4 has the box routes; the panel collects and answers from 0.0.9, "What 0.0.9
adds"). A **snapshot** is every describer — the shell (open app,
page, overlay, key owner, queue depth and drops, the last events, widget and timer counts, and
the open app's own `describe`), each service, and `system` (heap, low-water mark, largest block,
reset reason, crash breadcrumb, coredump present, watched tasks' stack high-water marks). The
**log ring** keeps the last ~3 KB of `ESP_LOGx` lines. **Commands** are a whitelist: `help`,
`snapshot`, `logs`, `log_level`, `open`, `home`, `set`, `inject` (KEY/BOOT/BOOT held/PWR, touch, swipe,
and since 0.0.11 `hold` — a finger held still, the long press),
`ota_check`, `report_now`, `screenshot`, and `reboot` — the only destructive one, refused
without `"confirm": true`. **Secrets cannot leave:** the writer every byte goes through refuses
the provisioning keys (`pass`, `token`, `ca`, any `*_key`/`*_token`…) and scrubs the registered
secret values from every string, log lines included; `host/tests.c` and `sim/render.sh` both
check a snapshot for the fake provisioning's password, device key and CA.

## Building and shipping

```sh
IDF_TARGETS=esp32c6 scripts/firmware-setup.sh     # once — ESP-IDF v5.5.5 + the RISC-V toolchain
echo 0.0.10 > firmware-c6/version.txt              # bump FIRST: the version is compiled in
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
UI adds a 38.4 KB strip buffer and a 12 KB task stack. 0.0.6 adds 3.3 KB of app and, at runtime,
the touch task's 3 KB stack. 0.0.8: app 1.82 MB (+73 KB, 54 KB of it the fonts; 57% of the slot
free), 243 KB of DIRAM static (+1.4 KB); no new task and no new runtime allocation beyond a
transient NVS handle, so the measured ~116 KB internal-heap minimum should hold within ~2 KB.
LVGL's pool peaks at 39 KB in the sim's busiest state (Settings under the talk ring, 64-bit).
0.0.10: app 1.91 MB (+78 KB, esp_codec_dev and the I2S driver most of it; 55% of the slot
free), 264 KB of DIRAM static (+9.4 KB); at runtime the audio task (3.5 KB stack) and ~9 KB of
I2S DMA and codec objects from boot, and the 16 KB talk ring per exchange ("What 0.0.10 adds",
The memory budget). 0.0.11: app 1.91 MB (+0.5 KB; 54% of the slot free), 265 KB of DIRAM static
(+1.7 KB), the audio stack
+768 B ("What 0.0.11 adds", From the first talks). 0.0.16: app 1.98 MB (+14 KB; 53% of the slot
free), 152 KB of DIRAM static (+2.4 KB; "What 0.0.16 adds", The memory budget). 0.0.17: app
1.98 MB (+2.7 KB), DIRAM static +592 B, and a play's 16 KB ring taken after its connection's
handshake rather than before it ("What 0.0.17 adds", The memory). 0.0.18: app 1.99 MB (+12.7 KB;
52% of the slot free), DIRAM static +304 B, a play's ring 6 KB instead of 16, and a 2.5 KB piece
buffer only while the panel is on its side ("What 0.0.18 adds", The memory budget).

## Recovery

The C6's ROM bootloader is mask ROM, so the S3's runbook (`../docs/runbooks/ENDPOINT_RECOVERY.md`)
holds: hold **BOOT (GPIO9)** while resetting and the chip enters download mode over the same
USB-C, whatever is in flash. The worst realistic outcome is "needs the cable again".
