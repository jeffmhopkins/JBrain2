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

/* ---- auto-rotate (0.0.18): the content turned in the flush, the panel's registers never ----
 * `rot` is how many quarter turns CLOCKWISE the panel itself has been turned from upright, which
 * is the edge now at the bottom: 0 its own bottom, 1 its right edge, 2 its top, 3 its left edge.
 * LVGL draws the same upright 480×480 screen whatever the panel does (the glass is square, so
 * nothing is laid out again); each strip it draws is turned on its way to the glass. The CO5300
 * has no row/column exchange (HARDWARE.md §2), and a second 38 KB strip buffer for LVGL's own
 * rotation is RAM this board does not have, so:
 *   - 180° reverses the strip where it lies and sends it to the mirrored window: no memory;
 *   - 90° and 270° gather the strip's columns into rows of a small piece buffer
 *     (DISP_ROT_PIECE_PX pixels, 2.5 KB) and send it a piece at a time.
 * Every window stays even-aligned: a strip LVGL rounded to even starts and sizes maps to even
 * starts and sizes on a side of even length. */
typedef struct {
    int32_t x1, y1, x2, y2; /* inclusive */
} disp_win_t;

/* A quarter turn's piece buffer: 32 glass rows of a full 40-row strip. An area LVGL draws narrow
 * is taller than 40 rows (it fills its 480×40 buffer by area, not by rows), so the rows a piece
 * holds are worked out per area (disp_rot_piece_rows). */
#define DISP_ROT_PIECE_PX (32 * 40)
/* Glass rows per piece for an area `h` logical rows tall: as many as fit, always even (the
   CO5300's windows), never under 2 — h is at most the glass's 480, under DISP_ROT_PIECE_PX / 2. */
int32_t disp_rot_piece_rows(int32_t h);

/* Where a logical pixel (lx, ly) lands on an n×n glass. */
void disp_rot_point(uint8_t rot, int32_t n, int32_t lx, int32_t ly, int32_t *px, int32_t *py);
/* The glass window a logical area lands in. */
disp_win_t disp_rot_area(uint8_t rot, int32_t n, disp_win_t logical);
/* 180°: the area's pixels reversed in place, which is the area turned half way round. */
void disp_reverse(uint16_t *px, uint32_t count);
/* 90° / 270°: the area (w × h logical pixels, row-major in `px`) is h wide and w tall on the
 * glass. Glass rows [r0, r0 + k) of it are gathered into `out` (k × h pixels, row-major), and
 * the window they go to is returned. */
disp_win_t disp_rot_piece(uint8_t rot, int32_t n, disp_win_t logical, const uint16_t *px,
                          int32_t r0, int32_t k, uint16_t *out);
