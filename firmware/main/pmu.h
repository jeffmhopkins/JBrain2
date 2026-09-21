#pragma once

#include <stdbool.h>

/* The AXP2101 at 0x34 — confirmed present by the bus scan, after being named as a suspect and
 * ruled out by inference three releases ago (ROOM_ENDPOINT_PLAN.md §10.4n, §10.4x).
 *
 * THE POINT OF THIS FILE IS THE RECORDING, NOT THE READING. The panel goes dark and the dark
 * state cannot be observed: reads over QSPI return zeros, and opening the console resets the
 * chip before anything can be seen. So the samples are kept in RTC memory, which survives
 * `esp_restart()` and is cleared only by a power cycle — and the five-second hold is a reboot
 * the owner can perform while looking at a dark screen. Hold it, and the next boot log
 * contains the two minutes of PMU state leading up to the fault.
 *
 * That is the first instrument in this investigation that does not destroy what it measures.
 */

/* False means nothing answered at 0x34, which would contradict the scan and is worth seeing. */
bool pmu_start(void);

/* Take one sample into the RTC ring. Cheap: six one-byte register reads. */
void pmu_sample(void);

/* Log the samples that survived the last restart, oldest first. Call once at boot, BEFORE the
   first sample, or the history is diluted by the present.

   DOES NOT CLEAR. It used to, and that silently broke the whole capture: `display_start()`
   calls this long before the first telemetry POST, so by the time `pmu_history_hex()` ran the
   ring was already empty and every report carried `pmu_history: []`. The evidence looked like
   "nothing survived the restart" and was actually "we threw it away before sending it".
   Clearing is now the caller's, after the history has left the box. */
void pmu_report_history(void);

/* Drop the surviving samples so the ring holds only what happens from here. Call after the
   history has been reported somewhere that outlives this boot. */
void pmu_history_clear(void);

/* The surviving samples as hex, oldest first, for sending somewhere that is not a console.
   Writes at most `max` entries of `PMU_SAMPLE_CHARS` bytes into `out` and returns how many.
   Valid only until `pmu_report_history()` clears the ring, so take the copy first. */
#define PMU_SAMPLE_CHARS 24
int pmu_history_hex(char (*out)[PMU_SAMPLE_CHARS], int max);
