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
 *   - 90° and 270° (0.0.22) give LVGL only DISP_SIDE_ROWS of its strip buffer, and gather each
 *     strip's columns into rows of two pieces carved from the tail it no longer draws in. The
 *     strip's whole turned window is set once and the pieces stream into it, RAMWR then RAMWRC,
 *     one gathered while the other is on the wire: no memory beyond the strip buffer either.
 * Every window stays even-aligned: a strip LVGL rounded to even starts and sizes maps to even
 * starts and sizes on a side of even length. */
typedef struct {
    int32_t x1, y1, x2, y2; /* inclusive */
} disp_win_t;

#define DISP_N 480
/* LVGL's strip upright and upside down: 480×40, the whole 38.4 KB buffer. */
#define DISP_STRIP_ROWS 40
/* On its side LVGL draws 480×32 strips (30 KB) and the last 8 rows' 7.5 KB are the two pieces. */
#define DISP_SIDE_ROWS 32
#define DISP_ROT_PIECES 2
#define DISP_ROT_PIECE_PX ((DISP_STRIP_ROWS - DISP_SIDE_ROWS) * DISP_N / DISP_ROT_PIECES)
/* The CO5300's memory writes (CO-DS §7.5.32-33): RAMWR starts at the window's first pixel and
   RAMWRC carries on from where the last write stopped, so a window can be filled piece by piece
   with no CASET/RASET between them. Both ride the QSPI pixel opcode 0x32. */
#define DISP_RAMWR 0x2C
#define DISP_RAMWRC 0x3C

/* Glass rows per piece for an area `h` logical rows tall: as many as fit DISP_ROT_PIECE_PX,
   always even (the CO5300's windows), never under 2 — h is at most the glass's 480, a quarter of
   DISP_ROT_PIECE_PX. An area LVGL draws narrow is taller than a strip (it fills its buffer by
   area, not by rows), so this is worked out per area. */
int32_t disp_rot_piece_rows(int32_t h);

/* The i-th piece of a quarter-turned area `w` logical columns wide (so `w` glass rows tall), `k`
 * glass rows a piece: its first glass row, its rows and its memory-write command — RAMWR for the
 * first, RAMWRC for the rest — and which of the DISP_ROT_PIECES buffers it is gathered into.
 * False once i is past the last piece. */
typedef struct {
    int32_t r0, rows;
    uint8_t cmd, buf;
} disp_piece_t;
bool disp_rot_step(int32_t w, int32_t k, int32_t i, disp_piece_t *out);

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

/* ---- what a frame costs (0.0.22): the debug snapshot's `display.flush` and telemetry ----
 * Per rotation, so a turned panel's frames are never averaged with upright ones. `flush` is the
 * time spent sending a frame's strips (the turn included), `wall` the frame from LVGL's render
 * start to its last strip sent (drawing and sending). A full frame is one that covered the whole
 * glass — a turn, an app opened — the only kind whose numbers compare across rotations. */
typedef struct {
    uint32_t frames, last_ms, max_ms;
    uint64_t sum_us;
    uint32_t full, full_ms, full_max_ms, full_wall_ms;
    uint64_t full_sum_us;
} disp_stat_t;
void disp_stat_add(disp_stat_t *s, uint32_t flush_us, uint32_t wall_us, bool full);
uint32_t disp_stat_avg_ms(const disp_stat_t *s);
uint32_t disp_stat_full_avg_ms(const disp_stat_t *s);
