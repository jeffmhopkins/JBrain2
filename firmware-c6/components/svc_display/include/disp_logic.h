/* The arithmetic the CO5300 driver depends on, pure so the host suite checks it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* The CO5300 takes a window only if its start column and width are both even, and likewise its
 * start row and height (CASET/RASET: "SC and EC-SC+1 must be divisible by 2", HARDWARE.md §2).
 * An odd window is not refused, it is drawn shifted. Grows the inclusive area [x1..x2]×[y1..y2]
 * outward to even starts and even sizes, clipped to a w×h screen whose sides are even. */
void disp_area_even(int32_t *x1, int32_t *y1, int32_t *x2, int32_t *y2, int32_t w, int32_t h);

/* The 0x51 byte for a brightness in percent. Dark is 0 — the controller stays initialised and
 * waking is one write, never display-off (svc_display.h). */
uint8_t disp_level(uint8_t pct, bool dark);
