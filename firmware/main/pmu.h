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

/* Log the samples that survived the last restart, newest last, then reset the ring. Call once
   at boot, BEFORE the first sample, or the history is diluted by the present. */
void pmu_report_history(void);
