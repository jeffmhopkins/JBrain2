#pragma once

#include <stdbool.h>

/* WHAT HOLDS THE PANEL DARK, SAMPLED EVERY TEN SECONDS INTO MEMORY A RESTART CANNOT TOUCH.
 *
 * Two chips, because the bus scan (ROOM_ENDPOINT_PLAN.md §10.4x) named exactly two that could
 * hold state across `esp_restart()` and be cleared by pulling the plug: the AXP2101 PMU at
 * 0x34, and the TCA9554 IO expander at 0x20 that no firmware here has ever spoken to.
 *
 * THE POINT OF THIS FILE IS THE RECORDING, NOT THE READING. The dark state cannot be observed
 * live: reads over QSPI return zeros, and opening the console resets the chip before anything
 * can be seen. So samples go to RTC memory, which survives `esp_restart()` and is cleared only
 * by a power cycle — and the five-second hold is a reboot the owner can perform while looking
 * at a dark screen. Hold it, and the next telemetry carries the two minutes that preceded it.
 */

/* Adds both devices. False means the AXP2101 did not answer at 0x34, which would contradict
   the scan and is worth seeing. A missing expander is not fatal: its bytes read 0xff and the
   boot log says so, which is a different fact from "the expander said 0xff". */
bool pmu_start(void);

/* Take one sample into the RTC ring. Cheap: nine one-byte register reads. */
void pmu_sample(void);

/* PULSE THE PANEL'S HARDWARE RESET, before the display is brought up. Needs `pmu_start()`
   first (it owns the expander handle). False means the expander did not answer and the panel
   is being initialised exactly as it always was — a degradation, not a failure, so the caller
   carries on rather than refusing to boot.

   This is the line `.reset_gpio_num = GPIO_NUM_NC` says does not exist. It does; it hangs off
   the TCA9554, and nothing in this firmware has ever driven it. See the long comment in
   `pmu.c` for why that is the black screen after every OTA, and why the pin numbers come from
   Waveshare's own V2 samples rather than from probing.

   Note for anyone reading the PMU ring afterwards: the expander's config byte was 0xff on
   every sample ever taken here, meaning all-inputs. Once this runs it reads 0xf8, which is
   how telemetry confirms the reset actually happened. */
bool pmu_reset_panel(void);

/* Call ONCE at boot, before the first sample. Copies whatever survived the restart into plain
   RAM, logs it oldest-first, and then re-arms the ring for this boot.

   RE-ARMING IS THE WHOLE FUNCTION, AND ITS ABSENCE WAS A SILENT DEAD END. The validity magic
   used to be written only by `pmu_history_clear()`, which `main.c` calls only when a report
   already carried samples — which needs the magic. Nothing else ever set it, so once a power
   cycle randomised it the ring could never become readable again: every sample was written
   faithfully and every reader rejected the lot. That is exactly what happened when the panel
   was power-cycled on 2026-09-21, and it turned `pmu_history: []` into a permanent reading
   that looked identical to the honest "nothing survived" it was designed to report
   (ROOM_ENDPOINT_PLAN.md §10.4am). The copy is taken first so re-arming cannot destroy the
   evidence — the bug §10.4ai fixed by removing the clear, which is what broke the arming. */
void pmu_report_history(void);

/* Drop the copy taken at boot, once it has left the box. Until this is called the same history
   rides every telemetry post, so a failed POST costs nothing. */
void pmu_history_clear(void);

/* The surviving samples as hex, oldest first, for sending somewhere that is not a console.
   Writes at most `max` entries of `PMU_SAMPLE_CHARS` bytes into `out` and returns how many.
   Reads the boot-time copy, so it is stable no matter what the render task is sampling now.
   Six AXP2101 bytes then three TCA9554 bytes: st0 st1 id dcdc_en ldo_en0 ldo_en1 | in out cfg. */
#define PMU_SAMPLE_CHARS 32
int pmu_history_hex(char (*out)[PMU_SAMPLE_CHARS], int max);
