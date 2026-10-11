# Waveshare ESP32-C6-Touch-AMOLED-2.16: hardware facts for ESP-IDF v5.5.5 firmware

> **Status:** Living · **Last verified:** 2026-10-11

The board facts the C2 drivers are built on, each with its source and a Confirmed / Unconfirmed
mark. Facts still open are settled on the real board by the 0.0.3 probe (`main/probe.c`); when
one is, its mark changes here in the same PR.

Research date: 2026-10-11. Downloaded code was read only. Nothing was built or run.

## Measured on the board (0.0.3 probe, 2026-10-11)

These override anything below that disagrees.

- **I2C bus** (SDA 8, SCL 7) answers at exactly `0x18` ES8311, `0x34` AXP2101, `0x40` ES7210,
  `0x51` PCF85063, `0x5A` touch, `0x6B` QMI8658. Confirmed.
- **AXP2101 as it powers up**, before any firmware writes it: chip id `0x4A`; DCDC1-4 on
  (`0x80=0x0F`), DCDC1 3.3 V (`0x82=0x12`); every ALDO/BLDO/DLDO1 enabled (`0x90=0xFF`) and
  DLDO2 (`0x91=0x01`); ALDO1-3 3.3 V (`0x1C`), **ALDO4 1.8 V** (`0x0D` — settles §1's
  conflict), BLDO1 1.2 V, BLDO2 2.8 V. So the LCD reset pull-up (ALDO3) and the speaker
  amplifier enable (ALDO2) are both **on from power-up**: the amp is live before any codec is
  configured, and a reset of the panel is ALDO3 off→on. Status `0x00=0x20` / `0x01=0x15`:
  on USB, no battery fitted, not charging.
- **Buttons, seen from the front, left → right: BOOT (GPIO9), PWR, KEY (GPIO10)** — the
  silkscreen order mirrored, as §4 suspected. BOOT and KEY idle high and read low while
  pressed (~0.4-0.5 s taps seen). Confirmed.
- **PWR works both ways (0.0.4, measured):** with GPIO18's *internal* pull-up on, it idles
  low and reads **high while PWR is pressed**, and the AXP2101's PEK flags latch the same
  press — every tap seen by both (`pwr=pek+gpio18`). With no pull (0.0.3) GPIO18 never moved,
  so §4's R11 pull-up is evidently not fitted: **the internal pull-up is required.** GPIO18 is
  the immediate source; PEK is the one that also sees presses while the CPU is busy.
- **Runtime brightness works (0.0.4, seen by the owner):** `0x51` sent framed as
  `(0x02<<24)|(0x51<<8)`, between frames, from the one task that owns the panel io, dims the
  glass. This is the write the twins' S3 panels silently discard; on this board it takes.
- **Display timings (0.0.4):** first full 480×480 frame 71 ms over QSPI at 40 MHz through one
  480×40 strip; later partial frames ~32 ms. Free internal heap with the UI up: ~156 KB,
  minimum ~120 KB.

## Sources and short names

| Short name | What it is | Where it is (paths are inside a clone of that source, not this repo) |
|---|---|---|
| **SCH** | Waveshare schematic, 3 pages (p1 = circuit, p2 = top silkscreen/assembly, p3 = bottom assembly) | https://files.waveshare.com/wiki/ESP32-C6-Touch-AMOLED-2.16/ESP32-C6-Touch-AMOLED-2.16-Schematic.pdf (`schem/sch.pdf`, rendered `schem/h-*.png`) |
| **DOC** | Waveshare product docs | https://docs.waveshare.com/ESP32-C6-Touch-AMOLED-2.16, `/ESP-IDF`, `/Resources-And-Documents` |
| **IDF** | Vendor ESP-IDF examples, repo `waveshareteam/ESP32-C6-Touch-AMOLED-2.16`, `main` @ `2945437` (2026-07-15) | `vendor216/02_Example/ESP-IDF-v5.5.3/…` |
| **ARD** | Vendor Arduino examples, same repo | `vendor216/02_Example/Arduino-v3.3.3/…` |
| **XZ** | XiaoZhi v2.2.5 board port, vendored in the same repo | `vendor216/02_Example/XiaoZhi-v2.2.5/main/boards/waveshare/esp32-c6-touch-amoled-2.16/` |
| **BSP** | Waveshare's new board BSP `waveshare/esp32_c6_touch_amoled_2_16` v1.0.0. It is not yet in the Component Registry. It is pinned by the repo's unmerged PR #1 (branch `chore/repository-modernization`) at commit `6dfe9e55` of `waveshareteam/Waveshare-ESP32-components`. Its README says CI tests **ESP-IDF v5.5.5**. | `wscomp/bsp/esp32_c6_touch_amoled_2_16/` |
| **CST** | Registry component `waveshare/esp_lcd_touch_cst9217` v1.0.4 (Apache-2.0) | `reg/waveshare__esp_lcd_touch_cst9217-v1.0.4/` |
| **CO-DS** | CO5300 datasheet V0.01 (Chipone), linked from DOC | https://files.waveshare.com/wiki/common/Co5300_Datasheet.pdf (`ds/co.txt`) |
| **CO-DRV** | Registry `espressif/esp_lcd_co5300` v2.2.0 (Apache-2.0). Reference only; the vendor does not use it. | `reg/espressif__esp_lcd_co5300-v2.2.0/` |
| **LVAD** | Registry `espressif/esp_lvgl_adapter` v0.3.2 (the version the IDF examples pin, `^0.3.0`) | `reg/lvad/` |

Reachability: all of the sources above were reached. Things that do **not** exist or could not be found:
- No Component Registry entry for `waveshare/esp32_c6_touch_amoled_2_16` (404). It exists only at the git commit above.
- No registry entry for `esp_lcd_touch_cst9220`. The vendor uses the CST9217 driver for the CST9220.
- DOC lists a `10_FactoryProgram` demo, but it is not in the repo (only the prebuilt `03_Firmware/01_Fac-v1.0.0.bin`).
- No datasheets were found for the CST9220, the ES7210 or the NS4150B amplifier.

Markers: **[C]** = Confirmed (read directly in a source, or two sources agree). **[U]** = Unconfirmed (inferred, sources conflict, or I read it off a schematic net trace that I could not double-check). Where sources conflict, both are given.

---

## Headline surprises (read these first)

1. **The vendor never uses an "esp_lcd_co5300" driver.** All vendor code (IDF, ARD, XZ, BSP) drives the CO5300 with the **`esp_lcd_sh8601`** driver and a custom init table [C] (IDF `09_LVGL_V9_Test/components/port_bsp/display_bsp.cpp:6,72`; BSP `idf_component.yml` → `waveshare/esp_lcd_sh8601 ^2.0.0`). Touch uses **`esp_lcd_touch_cst9217`**, even though DOC names the chip CST9220 [C].
2. **The LCD reset line is a PMIC rail, not a GPIO.** The panel RESET pin (FPC J4 pin 1, net `LCD_RESET`) is pulled up to **AXP2101 ALDO3** through R16 10 kΩ. The firmware resets the panel by turning ALDO3 off and on [C] (SCH p1, LCD connector block; IDF `display_bsp.cpp:80-88`; BSP README "AMOLED reset/power is controlled by AXP2101 ALDO3").
3. **The speaker amplifier enable is a PMIC rail.** NS4150B `CTRL` = `PA_CTRL` ← R14 10 kΩ ← **ALDO2** [C] (SCH p1, PA&SPEAKER&MIC block; BSP `esp32_c6_touch_amoled_2_16.c:313-315,398-401`).
4. **The vendor's own `user_config.h` has the wrong LCD CS and touch INT pins.** It says CS=GPIO5 and TP_INT=GPIO15. The code that actually runs uses the `DisplayPort` constructor defaults, **CS=GPIO15 and TP_INT=GPIO5**. SCH, XZ, ARD and BSP all agree with CS=15 / INT=5 [C].
5. **XiaoZhi's display init table is wrong for this panel.** It is a copy of the 2.06" board's table: `0x2A 00 16 01 AF`, `0x2B 00 00 01 F5`, `0x36 A0`. The same bytes appear in `…/esp32-c6-touch-amoled-2.06/*.cc:69-70`. Do not port it [C].
6. **IMU interrupts share pins with UART0.** QMI8658 INT1/INT2 go to **GPIO16/GPIO17** through 0 Ω links R4/R5. GPIO16/17 are also the ESP32-C6 U0TXD/U0RXD pins and are brought out to the TX/RX pads [C] (SCH p1 table and QMI8658 block). Keep the console on USB-Serial/JTAG (see §8).
7. **CO5300 MADCTL (0x36) has no row/column-exchange bit.** Only MY (bit 7), MX (bit 6) and RGB (bit 3) are defined; bits 5:4 are "don't care" (CO-DS §7.5.29). The vendor's `0x36=0x30` and its "rotate" toggle (0x30 vs 0x00) are therefore a no-op per the datasheet [U, datasheet vs. vendor code]. Rotate in software, or by MX/MY only.

---

## 1. AXP2101 PMIC

| Fact | Value | Marker / citation |
|---|---|---|
| I2C address | **0x34** (7-bit) on I2C0, SCL=GPIO7, SDA=GPIO8 | [C] IDF `09…/main/main.cpp:18,43`; BSP header `:74`; XPowersLib `AXP2101Constants.h:3` |
| I2C pull-ups | 2.2 kΩ R42/R43 to VCC3V3 (Codec block). The vendor also enables internal pull-ups. | [C] SCH p1; IDF `i2c_bsp.cpp:18` |
| DCDC1 | **3.3 V = `VCC3V3`**, the main system rail. It feeds the ESP32-C6, flash, LCD FPC (J4 pins 11/12/15/16), QMI8658, PCF85063 pull-ups, SD card, ES8311 DVDD/PVDD (via R46 0 Ω), NS4150B VDD, key pull-ups and USB. | [C] SCH p1 (rail table and each block); IDF `power_bsp.cpp:71-74`; BSP `.c:235,240` |
| DCDC2–5 | No load on the schematic. XZ turns them off (`0x80=0x01`). BSP only ORs in the DCDC1 enable bit. | [C] XZ `.cc:35-36`; BSP `.c:240` |
| ALDO1 | **3.3 V = net `A3V3`** (analog). It feeds the ES7210 (VDDP/VDDD) and the ES8311 AVDD (via R37 0 Ω). Mic bias comes from ES7210 MICBIAS12. | [C] SCH p1 AXP2101 block (ALDO1→A3V3) and ADC/Codec blocks; XZ comment "ALDO1(MIC)" `.cc:50` |
| ALDO2 | **3.3 V → PA_CTRL** (NS4150B enable, via 10 kΩ). Off = speaker amp muted/shut down. | [C] SCH p1 PA block; BSP `.c:313-315` (`bsp_pmu_set_audio_power` = LDO bit 1) |
| ALDO3 | **3.3 V → LCD_RESET pull-up**: the "display power/reset" control. | [C] SCH p1; IDF `display_bsp.cpp:80-88`; BSP `.c:308-311` (LDO bit 2), `.c:568-571` |
| ALDO4 | Sources disagree. SCH rail table says **1.8 V** ("VL3_1.8V") and BSP programs 1.8 V. The older IDF/ARD examples and XZ program **3.3 V**. I found no load on ALDO4 in the schematic. **Use 1.8 V (BSP + schematic).** | [U] SCH p1 rail table; BSP `.c:239` vs IDF `power_bsp.cpp:87-90`, XZ `.cc:48` |
| BLDO1/2, CPUSLDO, DLDO1/2 | Rail table names 1.2 V / 2.8 V / 1.2 V, but nothing is connected. XZ disables them (`0x91=0x00`). | [U] SCH p1 (pins left unconnected) |
| RTCLDO / VBACKUP | RTCLDO → `VCC-RTC`, which powers the PCF85063 (and the AXP_IRQ pull-up). VBACKUP has a 2.2 µF cap. DOC: RTC is "powered by the AXP2101 with battery backup". | [C] SCH p1; DOC |
| LDO enable mask (BSP) | `0x90 \|= 0x09` (ALDO1+ALDO4 on). ALDO2 (amp) and ALDO3 (display) are switched on demand. XZ writes `0x90=0x0F` (ALDO1-4 all on). | [C] BSP `.c:241`; XZ `.cc:51` |
| VBUS input limit | 2000 mA | [C] IDF `power_bsp.cpp:69`; BSP `.c:234` |
| Charge settings | IDF/ARD/BSP: precharge 50 mA, constant current **500 mA**, termination 50 mA. XZ: precharge 50 mA (`0x61=0x02`), CC **400 mA** (`0x62=0x0A`), term 25 mA (`0x63=0x01`), CV 4.1 V (`0x64=0x02`, from code comment). | [C] IDF `power_bsp.cpp:92-94`; BSP `.c:242-244`; XZ `.cc:53-58` |
| Battery ADC | BSP enables the battery-voltage ADC channel (`0x30` bit0) and battery detection (`0x68` bit0), then reads 0x34/0x35 (14-bit mV). | [C] BSP `.c:245-246,273-282` |
| PWROK | AXP2101 PWROK → ESP32-C6 **CHIP_PU**. The PMIC holds the MCU in reset until the rails are good. | [C] SCH p1 |
| PWR key | Key2 (silkscreen "PWR") pulls AXP2101 **PWRON** (pin 30) to GND through RP7 510 Ω. DOC: "powers on by default when power is connected; long press to power off, short press to power on". | [C] SCH p1 KEYS; DOC |
| Long-press config (XZ only) | `0x22=0b110`: long-press shutdown enabled + over-temperature power-off, long press = power off (not restart). `0x27=0x10`: IRQLEVEL 1.5 s, OFFLEVEL 4 s, ONLEVEL 128 ms. IDF/BSP leave these at OTP defaults. | [C] XZ `.cc:32-33`; bit meanings from XPowersLib `XPowersAXP2101.tpp:826-858,1045-1070` |
| Must firmware hold power on battery? | **No.** There is no hold/latch GPIO; the AXP2101 latches power itself. Software power-off goes through the PMIC (XZ calls `pmic_->PowerOff()`). | [C] SCH p1 (no hold net); XZ `.cc:171` |
| AXP IRQ | `AXP_IRQ` has only a 10 kΩ pull-up to VCC-RTC. **It does not go to any ESP32 GPIO.** Poll the PMIC instead. | [C] SCH p1 (net appears once, at the AXP2101) |
| Chip ID | Register 0x03 = 0x4A | [C] BSP `.c:55,71,223` |
| Init order in all vendor code | Order: I2C init → PMU init → ALDO3 cycle (off 100 ms, on 100 ms) → SPI bus → panel init. IDF does on→off→on with 100 ms steps. BSP does off→100 ms→on→100 ms. | [C] IDF `display_bsp.cpp:80-88`; BSP `.c:567-571` |

## 2. CO5300 AMOLED (480×480, QSPI)

| Fact | Value | Marker / citation |
|---|---|---|
| Bus | SPI2_HOST in quad mode, SPI mode 0 | [C] IDF `display_bsp.cpp:48-59`; BSP header `:53` |
| SCLK | **GPIO0** | [C] SCH J4 pin 5; IDF/ARD/XZ/BSP |
| D0..D3 | **GPIO1, GPIO2, GPIO3, GPIO4** | [C] SCH J4 pins 7/6/23/22; BSP header `:55-58` |
| CS | **GPIO15** (see headline 4) | [C] SCH J4 pin 4; XZ `config.h:25`; ARD `user_config.h:19`; BSP header `:59` |
| Reset | No GPIO; **ALDO3** rail (see §1). BSP also calls `esp_lcd_panel_reset()`. With `reset_gpio_num = NC`, the sh8601 driver then sends SWRESET 0x01 and waits 80 ms. | [C] SCH; BSP `.c:593-595`; sh8601 driver `panel_sh8601_reset` (ARD `src/externLib/esp_lcd_sh8601.c`, and the registry's `espressif/esp_lcd_sh8601` 2.0.1 that 0.0.4 builds with) |
| TE pin | `LCD_TE` (J4 pin 3) goes **only to test pad TP20**, not to a GPIO. Software TE sync is not possible without a bodge wire. | [C] SCH p1 LCD block |
| DSI_PWR_EN (J4 pin 24) | Pulled up to VCC3V3 through R13 10 kΩ. Not controlled by firmware. | [C] SCH p1 |
| Display supplies | J4 power pins tie to **VCC3V3 (DCDC1)**. No separate VCI/VDDIO switch was found. The panel's own boost converters are on the FPC/COG. | [U] SCH p1 (FPC pins 11/12/15/16 = VCC3V3; the module's internal VCI/VDDIO split is not shown) |
| Pixel format | RGB565: `0x3A 0x55`, 16 bpp, RGB element order. LVGL needs byte swap (`CONFIG_LV_COLOR_16_SWAP=y` in IDF sdkconfig; ARD calls `lv_draw_sw_rgb565_swap`). | [C] IDF `display_bsp.cpp:16,68-69`; IDF `sdkconfig.defaults:18`; ARD `bsp_lvgl_port.cpp:45`; BSP `display.h:13` (BIGENDIAN) |
| SPI clock | **40 MHz** in every vendor source. Datasheet minimum write cycle is 20 ns (**50 MHz max**); reads need ≥100 ns (≤10 MHz). | [C] IDF `display_bsp.cpp:52`; CO-DRV `esp_lcd_co5300.h:136`; CO-DS timing table (txt ~L5385-5409) |
| Column/row gap | **None.** CASET/RASET are 0..0x1DF (0..479). `x_gap/y_gap` are left at 0. | [C] IDF `display_bsp.cpp:22-23`; BSP `.c:112-113` |
| Partial-update alignment | **Start column and width must both be even, and so must start row and height.** CO-DS CASET/RASET restriction: "SC and EC-SC+1 must be divisible by 2". All vendor code installs an LVGL rounder that floors x1/y1 to even and makes x2/y2 odd. | [C] CO-DS (txt ~L7994, L8088); IDF `lvgl_bsp.cpp:24-57`; BSP `.c:658-674` |
| Runtime command framing | Each command is a 32-bit "cmd" word: `(opcode<<24) \| (reg<<8)`. Opcode **0x02** = write command/params (1-line), **0x32** = pixel write (quad, used with RAMWR 0x2C), 0x03 = read. Example: brightness is `esp_lcd_panel_io_tx_param(io, (0x02<<24)\|(0x51<<8), &val, 1)`. | [C] sh8601 driver `LCD_OPCODE_*` + `tx_param/tx_color`; IDF `display_bsp.cpp:114-123`; BSP `.c:538` |
| Brightness 0x51 | 0x00–0xFF. Vendor maps 0–100 % linearly to 0–255. Needs `0x53 0x20` (BCTRL on) set in init. AMOLED: 0 is near-black, there is no backlight GPIO. The HBM brightness register 0x63 is set to 0xFF in init. | [C] IDF `display_bsp.cpp:19-21,114-123`; CO-DS command table (53h BC_EN, 63h WRHBMDISBV) |
| sh8601 driver quirk | `panel_sh8601_init` sends **MADCTL and COLMOD before** the vendor table (that is, before Sleep-Out). The table then re-sends 0x36/0x3A and the driver logs "overwritten" warnings. Harmless, but if you write your own driver, send the vendor table verbatim. | [C] sh8601 driver `panel_sh8601_init` |
| MADCTL | See headline 7. The CO5300 datasheet defines only MY/MX/RGB. | [U] CO-DS §7.5.29 vs vendor `0x36 0x30` |
| Max transfer size | IDF: `max_transfer_sz = 480*50*2` (48 000 B), queue depth 1. ARD/BSP/XZ: full frame `480*480*2` (460 800 B; this is a DMA-descriptor limit only, not an allocation). | [C] IDF `display_bsp.cpp:35,53`; BSP `.c:323` |

**The vendor init sequence for this 2.16" 480×480 panel.** IDF, ARD and BSP use the same table, byte for byte. Format: `{cmd, data…, delay_ms after}` [C] (IDF `09_LVGL_V9_Test/components/port_bsp/display_bsp.cpp:9-25`; ARD `09_LVGL_V9_Test/bsp_lvgl_port.cpp:25-41`; BSP `esp32_c6_touch_amoled_2_16.c:100-114`):

```
0x11 (no data)            delay 600 ms   // Sleep Out
0xFE 0x20                                // CMD page switch -> manufacturer page 0x20
0x19 0x10                                // manufacturer reg (undocumented here)
0x1C 0xA0                                // manufacturer reg (undocumented here)
0xFE 0x00                                // back to user command page
0xC4 0x80                                // Set SPI mode (QSPI)
0x3A 0x55                                // COLMOD RGB565
0x35 0x00                                // TE on (V-blank) - pin not wired to MCU
0x36 0x30                                // MADCTL (bits 5:4 are "don't care" per CO-DS)
0x53 0x20                                // WRCTRLD: brightness control on
0x51 0xFF                                // brightness max
0x63 0xFF                                // HBM brightness max
0x2A 0x00 0x00 0x01 0xDF                 // CASET 0..479
0x2B 0x00 0x00 0x01 0xDF                 // RASET 0..479
0x29 (no data)            delay 100 ms   // Display On
```

Before this table: ALDO3 off for ≥100 ms, then on, then wait 100 ms. BSP additionally sends SWRESET (+80 ms) before the table and DISPON afterwards [C].

Caveat [U]: the meaning of page-0x20 registers 0x19 and 0x1C is not documented in what I read. Treat them as opaque panel tuning and copy them verbatim. Espressif's generic CO5300 default table (CO-DRV `esp_lcd_co5300_spi.c:212-223`) is for a different panel (466-wide, x offset 6). **Do not use it.**

## 3. CST9220 touch (driven as CST9217)

| Fact | Value | Marker / citation |
|---|---|---|
| I2C address | **0x5A** on the shared I2C0 bus (GPIO7/8), 400 kHz | [C] CST `include/esp_lcd_touch_cst9217.h:24`; ARD `bsp_lvgl_port.cpp:130`; BSP header `:80` |
| RST | **GPIO11**, active low (driver: low 10 ms → high, wait 50 ms; ARD: high/low/high with 200 ms steps) | [C] SCH J4 pin 18; CST `.c:199-207`; ARD `lcd_touch.cpp:80-89` |
| INT | **GPIO5**, active low (falling edge) | [C] SCH J4 pin 17; IDF/XZ/BSP `levels.interrupt = 0` |
| Supply | Through the LCD FPC from VCC3V3 | [U] SCH (no separate touch rail visible) |
| Read protocol | Write the 2-byte register **0xD000**, then read **10 bytes** (as two separate I2C transactions: tx, 2 ms delay, rx). Validate `data[6] == 0xAB`. `points = data[5] & 0x7F` (the driver caps it at 1). For point 0: `status = data[0] & 0x0F` must be `0x06` (pressed); `rawA = (data[1]<<4) \| (data[3]>>4)`, `rawB = (data[2]<<4) \| (data[3]&0x0F)`. Retries 5×, then pulses a hardware reset. | [C] CST `.c:24-33,119-151,244-265` |
| Config registers | Enter command mode by writing `0xD101` with `{0xD1,0x01}`. Checkcode at 0xD1FC, resolution at 0xD1F8 (LE u16 x,y), chip type/project ID at 0xD204. | [C] CST `.c:24-28,213-240` |
| Orientation vs display | With the vendor's `0x36=0x30` panel setup, every vendor ESP-IDF source uses `swap_xy=1, mirror_x=0, mirror_y=1` on the CST9217 driver output (driver x=rawA, y=rawB). Result: **display_x = rawB, display_y = 479 − rawA**. ARD's hand-rolled reader instead computes `y = rawA` and `x = 480 − (data[2]<<4) \| (data[3]&0x0F)`. That line has a precedence bug (the `\|` binds after the subtraction), so do not copy it. | [C] IDF `display_bsp.cpp:96-105`; XZ `.cc:263-279`; BSP `.c:626-632`; ARD `lcd_touch.cpp:107-108` |

## 4. Buttons

| Button | Net / GPIO | Electrical | Marker / citation |
|---|---|---|---|
| **BOOT** (Key1) | **GPIO9** | 10 kΩ pull-up (R8) to 3V3 + 100 nF, **active low**. Strapping pin: held low at reset = download mode. | [C] SCH p1 KEYS; XZ `config.h:22`; DOC |
| **KEY** (Key3, silkscreen "IO10") | **GPIO10** | 10 kΩ pull-up (R18), cap C22 not fitted, **active low**. The SCH p1 GPIO table lists GPIO10 as "LCD_RESET", but the drawn nets put LCD_RESET on ALDO3 and GPIO10 on Key3. The silkscreen says IO10. | [C] SCH p1 KEYS + p2 silkscreen; table conflict noted |
| **PWR** (Key2) | **AXP2101 PWRON** directly, and inverted onto **GPIO18** | Key2 pulls PWRON low. PWRON also drives the gate of BSS138 Q T1 (via R28 1 kΩ). The drain is GPIO18 with a 10 kΩ pull-up (R11). So **GPIO18 reads LOW when idle and HIGH while PWR is pressed** (FET turns off). | [U] SCH p1 KEYS (net trace read off the schematic; the PWRON idle-high level is assumed from the AXP2101's internal pull-up). Vendor code never reads GPIO18 as a key. |
| PWR via PMIC | The AXP2101 also latches the power key's events in IRQ status 2 (**0x49**): bit 0 positive edge (release), bit 1 negative edge (press), bit 2 long press, bit 3 short press, enabled by the same bits of 0x41 and cleared by writing 1s back. IRQ_N is not wired to a GPIO, so they are polled; 0.0.4 does, every 50 ms, and reports `pwr=`. | Bit layout [C] XPowersLib `XPowersParams.hpp:342-345` (IRQ2 = reg 41H/49H), `clearIrqStatus`; whether they fire on this board [U] until 0.0.4's telemetry |
| BSP conflict | BSP header defines `BSP_RTC_INT = GPIO18`. SCH shows RTC_INT going only to a test pad and GPIO18 = PWR-key sense. **Treat BSP's RTC_INT=18 as an error.** | [U] BSP header `:79` vs SCH p1 |
| Physical position | Top-silkscreen (SCH p2) top edge, left→right: **Key3 "IO10", Key2 "PWR", Key1 "BOOT"**. DOC says all three are side-mounted. Page 2 is the component-side view. If the AMOLED faces the user from the other side, the order seen from the front is mirrored: **BOOT, PWR, KEY**. Check on the device. | [U] SCH p2; DOC |

## 5. Audio

| Fact | Value | Marker / citation |
|---|---|---|
| ES8311 (DAC/speaker) | I2C **0x18** (CE pin pulled to AGND via R44 10 kΩ). AVDD = A3V3 (ALDO1). DVDD/PVDD = VCC3V3. OUTP/OUTN → NS4150B (150 kΩ input resistors) → speaker pads P10. | [C] IDF `07_Audio_Test/components/port_bsp/codec_bsp.h:393`; SCH p1; BSP header `:81` |
| ES7210 (mic ADC) | I2C **0x40** (AD0/AD1 = 0 Ω to GND). VDD = A3V3. | [C] IDF `codec_bsp.h:394`; SCH p1 ("0x40") |
| Mics | MIC1 → ES7210 MIC1, MIC2 → ES7210 MIC2 (analog, biased from MICBIAS12) | [C] SCH p1 MIC block |
| **AEC reference** | ES8311 **OUTP/OUTN → ES7210 MIC3P/MIC3N** through R25/R33 0 Ω ("AEC" label). MIC3 is the loopback reference; MIC4 is unused. | [C] SCH p1 PA&SPEAKER&MIC block |
| Amp | NS4150B. Enable = ALDO2 (see §1). VDD = VCC3V3. There is no PA GPIO (`pa_pin = -1 / NC` everywhere). | [C] SCH; IDF `board_cfg.txt` (Board `C6_AMOLED_2_16`, `pa: -1`); BSP `.c:477` |
| I2S pins | **MCLK GPIO19, BCLK GPIO20, WS/LRCK GPIO22, DOUT (to ES8311 DSDIN) GPIO23, DIN (from ES7210 SDOUT1) GPIO21** | [C] SCH table + ADC/Codec blocks; XZ `config.h:10-14`; BSP header `:46-50` |
| Single I2S, full duplex | The C6 has one I2S. Every vendor source creates **both TX and RX on I2S0 in one `i2s_new_channel()` call**, sharing MCLK/BCLK/WS. | [C] BSP `.c:409-411`; XZ `box_audio_codec.cc:97-106`; codec_board `codec_init.c:191-193` |
| Vendor I2S modes (they differ) | **BSP:** STD MSB, 32-bit slots, stereo, both directions, default **16 kHz**, MCLK = 256×fs (`I2S_STD_CLK_DEFAULT_CONFIG`). **IDF 07 demo:** TDM 4 slots × 32-bit, 16 kHz, playback/record opened at 16 kHz/2 ch/16-bit; ES7210 MIC1+MIC3 (+MIC2+MIC4 in TDM). **XZ:** 24 kHz, TX = STD 16-bit stereo, RX = TDM slots 0-3, `mclk_multiple = 256`, `bclk_div = 8`; AEC reference disabled (`AUDIO_INPUT_REFERENCE false`). | [C] BSP `.c:92-97,414`; IDF `codec_init.c:151-189`, `07_Audio_Test/main/main.cpp:61-68`, `codec_bsp.cpp:279-302`; XZ `config.h:6-9`, `box_audio_codec.cc:108-161` |
| Gains used | Speaker volume 80-100; mic gain 25-35 dB | [C] IDF `main.cpp:67-68`, `codec_bsp.cpp:248,265-266` |
| Note | To capture the AEC reference you need ES7210 in **TDM** (4 slots, MIC3 = reference). BSP's STD-stereo default captures only two channels. I did not check which ES7210 channels the default `es7210_codec_new` selects. | [U] |

## 6. RTC and IMU

| Fact | Value | Marker / citation |
|---|---|---|
| PCF85063A address | **0x51** | [C] BSP header `:78`; IDF `03_I2C_PCF85063` uses `PCF85063A_ADDRESS` (component `waveshare/pcf85063a ^1.1.1`) |
| PCF85063 supply | VCC-RTC (AXP2101 RTCLDO, battery-backed). 32.768 kHz crystal Y1. | [C] SCH p1 RTC block |
| RTC INT | `RTC_INT` → **test pad only**, not wired to the MCU. BSP's GPIO18 claim conflicts (see §4). | [U, conflict] SCH p1 vs BSP header `:79` |
| QMI8658 address | **0x6B** (SDO/SA0 pulled high, schematic note "0X6B") | [C] SCH p1; IDF `02_I2C_QMI8658/main/main.cpp:39` (`QMI8658_ADDRESS_HIGH`); BSP header `:75` |
| QMI8658 INT1 / INT2 | **GPIO16 / GPIO17** via R4/R5 0 Ω, shared with UART0 TX/RX (see headline 6) | [C] SCH p1; BSP header `:76-77` |
| QMI8658 supply | VCC3V3 (VDD + VDDIO) | [C] SCH p1 |
| Vendor IMU config | ±8 g @1 kHz, ±512 dps @1 kHz | [C] IDF `02_I2C_QMI8658/main/main.cpp:43-46` |

## 7. TF card

| Fact | Value | Marker / citation |
|---|---|---|
| Mode | SDSPI (1-bit SPI). D1/D2 are not connected. | [C] SCH p1 SD block; DOC; IDF `04_SD_Card` |
| Pins | **CLK GPIO0, CMD/MOSI GPIO1, D0/MISO GPIO2, CS (CD/D3) GPIO6**. 10 kΩ pull-ups (R7/R12/R15/R17) to VCC3V3. | [C] SCH p1; IDF `sdcard_bsp.h:18`; BSP header `:68-71` |
| Shares the display bus? | **Yes.** CLK and two data lines are the QSPI display's SCLK/D0/D1 on SPI2_HOST. The vendor IDF SD example inits the bus by itself, with no display running. The BSP initializes **one** quad-capable SPI2 bus and adds both the panel IO (CS 15) and the SDSPI device (CS 6) to it. | [C] IDF `sdcard_bsp.cpp:30-53` (commented-out shared variant at :5-28); BSP README "Shared SPI2 bus", `.c:318-330,360-386` |
| Implication | SD access and display flushes serialize on SPI2. A flush in progress blocks SD I/O and vice versa. The SD card runs at SDSPI speeds (default 20 MHz). | [U] inference |

## 8. Strapping pins, console, LVGL and buffers

| Fact | Value | Marker / citation |
|---|---|---|
| Flash / RAM | 16 MB external NOR (XM25QH128D). 512 KB HP SRAM + 16 KB LP SRAM. **No PSRAM.** | [C] DOC; SCH p1 U3; IDF `sdkconfig.defaults:6` |
| USB | GPIO12/GPIO13 = native USB D-/D+ to the Type-C port (22 Ω series). DOC: "ESP32-C6 USB port for programming and log output", i.e. **USB-Serial/JTAG**. | [C] SCH p1 USB block; DOC |
| Console setting | The vendor sdkconfig sets no console option, so the IDF default applies: primary UART0 on GPIO16/17 with USB-Serial/JTAG as the secondary console. Given the IMU-INT conflict, set `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` (or keep UART0 but never enable the QMI8658 INT outputs). | [U] inference from IDF defaults + SCH |
| C6 strapping pins in use | GPIO9 = BOOT (pulled up, good). GPIO8 = I2C SDA with a 2.2 kΩ pull-up (must be high for download mode; it is). GPIO15 = LCD CS. GPIO4 = LCD D3 (MTMS). GPIO5 = touch INT (MTDI). Vendor docs say nothing about strapping. Make sure no peripheral drives GPIO4/5/8/15 at reset. | [U] ESP32-C6 datasheet knowledge + SCH; DOC silent |
| Panel pins vs IOMUX | The SPI2 IOMUX pins on the C6 are GPIO2/4/5/6/7/16. The panel uses GPIO0-4/15, so the bus goes through the **GPIO matrix**. 40 MHz is what the vendor runs; I have not verified whether 50 MHz works through the matrix. | [U] |
| LVGL versions | IDF examples: `lvgl/lvgl ^8.4.0` (08) and `^9.5.0` (09), via `espressif/esp_lvgl_adapter ^0.3.0` and `espressif/esp_lcd_sh8601 ^1.0.0`. ARD bundles LVGL 8.4.0 / 9.3.0. BSP: LVGL `>=8,<10`, `esp_lvgl_adapter ~0.6`, `waveshare/esp_lcd_sh8601 ^2.0.0`, `waveshare/esp_lcd_touch_cst9217 ^2.0.0`, `esp_codec_dev ~1.5`. | [C] IDF `main/idf_component.yml`; BSP `idf_component.yml`; modernization branch `THIRD_PARTY.md` |
| Buffer strategy (no PSRAM) | **Partial render, full-width strips in internal RAM.** IDF v8: 480×50 px (48 000 B). IDF v9 and BSP: 480×**100** px (**96 000 B**, Kconfig `BSP_DISPLAY_LVGL_BUF_HEIGHT`, range 20-480). Both use the adapter profile `use_psram=false, require_double_buffer=false`, which I read as one buffer. ARD v9: **two** 480×50 DMA buffers (2×48 000 B), `LV_DISPLAY_RENDER_MODE_PARTIAL`. LVGL heap: `CONFIG_LV_MEM_SIZE_KILOBYTES=64`. LVGL task 20 KB stack, priority 10. | [C] IDF `08…/lvgl_bsp.cpp:71`, `09…/lvgl_bsp.cpp:61-76`; BSP `.c:686-696` + Kconfig; ARD `bsp_lvgl_port.cpp:23,174-178`; LVAD `esp_lv_adapter_display.h:182-189`; buffer count [U] from LVAD `display_manager.c:1043-1080` |
| Tear avoidance | BSP: `ESP_LV_ADAPTER_TEAR_AVOID_MODE_NONE` (TE is not wired) | [C] BSP `.c:719` |
| Flush rates achieved | **Not documented anywhere** (DOC, README, code). Theoretical: 40 MHz × 4 lines = 160 Mb/s, so a full 480×480×16 bpp frame (3.69 Mb) takes ~23 ms of wire time, ≈ 40 fps upper bound before LVGL render cost. A 100-row strip is ~4.8 ms. | [U] computed |
| ESP-IDF version | DOC: examples developed on IDF **v5.5.2** (folder name says 5.5.3). BSP CI: v5.5.5 and v6.0.2. | [C] DOC ESP-IDF page; BSP README |

## 9. Licences

| Code | Licence | Marker / citation |
|---|---|---|
| Vendor repo `main` (examples, init tables, BSP glue) | **No LICENSE file on `main`.** The unmerged PR #1 / branch `chore/repository-modernization` adds `LICENSE.txt` = **Apache-2.0** and labels the README "Apache-2.0". | [C] `vendor216` tree at `2945437`; branch @ `ac2513f` `LICENSE.txt` |
| BSP `waveshare/esp32_c6_touch_amoled_2_16` (has the identical init table) | **Apache-2.0** (`LICENSE` in the component dir) | [C] `wscomp/bsp/…/LICENSE` |
| `esp_lcd_sh8601` driver (vendored copy) | Apache-2.0, © Espressif | [C] SPDX header in ARD `src/externLib/esp_lcd_sh8601.c:1-5` |
| `esp_lcd_touch_cst9217` | Apache-2.0 | [C] `reg/…cst9217…/license.txt` |
| `esp_lcd_co5300` | Apache-2.0 | [C] |
| XPowersLib | MIT (Lewis He) | [C] `01_Arduino_Libraries/XPowersLib/LICENSE` |
| `codec_board` (used by audio demo) | "Espressif Modified MIT", use only with Espressif products (the C6 qualifies) | [C] `07_Audio_Test/components/ExternLib/codec_board/LICENSE` |
| XiaoZhi | MIT | [C] `XiaoZhi-v2.2.5/LICENSE` |

Bottom line: port the init table and pin map from the **Apache-2.0 BSP** (keep its notice/NOTICE obligations). The same bytes are in the unlicensed `main` examples, so cite the BSP as the source. Whether a register table is copyrightable at all is a legal question; I am not answering it here.

---

## What firmware C2 should do first

1. **Bring up I2C0 and the PMIC before anything else.** Use SDA=GPIO8, SCL=GPIO7, 400 kHz (PMU at 100-400 kHz). Probe AXP2101 at 0x34 and check chip ID reg 0x03 = 0x4A. Program DCDC1 = 3.3 V, ALDO1 = 3.3 V (codecs), ALDO4 = 1.8 V. Enable ALDO1 (+ALDO4). Set charge to 500 mA CC / 50 mA pre / 50 mA term and the VBUS limit to 2 A. Enable the battery ADC. Optionally set long-press-off (`0x22` bit1, `0x27`) as XZ does. Copy the register sequence from BSP `.c:198-255`.
2. **Display:** ALDO3 off → 100 ms → on → 100 ms. Init SPI2 in quad mode (SCLK 0, D0-D3 = 1-4, CS 15, mode 0, 40 MHz). Size `max_transfer_sz` for SD sharing, and add the SDSPI device (CS 6) to the same bus later. Use `esp_lcd_sh8601` (or a small equivalent driver with 0x02/0x32 opcode framing). Send the §2 init table verbatim, then fill a solid colour to confirm. Then test brightness 0x51.
3. **Enforce even-aligned flush rectangles** (x, y, width and height all even). Use the rounder from BSP `.c:658-674` or the equivalent in your own flush path.
4. **Touch:** CST9217 driver @0x5A, RST 11, INT 5 (falling edge). Map display_x = rawB and display_y = 479 − rawA (= `swap_xy=1, mirror_y=1`). Verify on the device by tapping the corners.
5. **Buttons:** BOOT = GPIO9 and KEY = GPIO10, both active-low with external pull-ups. PWR: read **GPIO18** (expected high while pressed, inverted via BSS138). Confirm the polarity on hardware, then decide whether to also poll the AXP2101 PEK IRQ flags.
6. **Console on USB-Serial/JTAG.** Leave the QMI8658 INT pins (GPIO16/17 = UART0) unconfigured until the console choice is settled.
7. **Audio last:** one I2S0 with full-duplex TX+RX, MCLK 19 / BCLK 20 / WS 22 / DOUT 23 / DIN 21, MCLK = 256×fs, 16 kHz to start. ES8311 at 0x18, ES7210 at 0x40. Enable ALDO2 only while playing (amp enable). Use TDM capture if you need the MIC3 AEC reference.
8. **Verify on hardware before relying on these [U] items:** button left/right order, GPIO18 PWR polarity, ALDO4 voltage (1.8 V vs 3.3 V), RTC INT routing, and whether 50 MHz QSPI is stable through the GPIO matrix.
