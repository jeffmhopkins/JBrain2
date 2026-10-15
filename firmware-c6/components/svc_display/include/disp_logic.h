/* The arithmetic the CO5300 driver depends on, pure so the host suite checks it. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
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
 * has no row/column exchange (HARDWARE.md §2), and a second strip buffer for LVGL's own rotation
 * is RAM this board does not have, so the one 38.4 KB buffer is used as two 480×20 halves
 * (0.0.25):
 *   - upright and 180° LVGL draws into either half while the other is on the wire; 180° reverses
 *     its strip where it lies and sends it to the mirrored window;
 *   - 90° and 270° LVGL draws into the first half only, and each strip is gathered whole, turned,
 *     into the second, which goes out as one transfer while LVGL draws the next strip.
 * Every window stays even-aligned: a strip LVGL rounded to even starts and sizes maps to even
 * starts and sizes on a side of even length. */
typedef struct {
    int32_t x1, y1, x2, y2; /* inclusive */
} disp_win_t;

#define DISP_N 480
/* The strip buffer, 480×40 (38.4 KB, the README's budget), and the 480×20 halves LVGL draws in.
   A half holds any area LVGL draws (it fills its buffer by area, so a narrow area is tall), and so
   does that area turned: its pixel count is the same. */
#define DISP_BUF_ROWS 40
#define DISP_STRIP_ROWS (DISP_BUF_ROWS / 2)
#define DISP_STRIP_PX (DISP_N * DISP_STRIP_ROWS)
/* The CO5300's memory write (CO-DS §7.5.32): from the window's first pixel, row by row. */
#define DISP_RAMWR 0x2C

/* Where a logical pixel (lx, ly) lands on an n×n glass. */
void disp_rot_point(uint8_t rot, int32_t n, int32_t lx, int32_t ly, int32_t *px, int32_t *py);
/* The glass window a logical area lands in. */
disp_win_t disp_rot_area(uint8_t rot, int32_t n, disp_win_t logical);
/* 180°: the area's pixels reversed in place, which is the area turned half way round. */
void disp_reverse(uint16_t *px, uint32_t count);
/* 90° / 270°: the area (w × h logical pixels, row-major in `px`, in LVGL's byte order) is h wide
 * and w tall on the glass. All of it is gathered into `out` (w × h pixels, row-major as the glass
 * takes them) with each pixel's two bytes swapped for the wire, so a turned strip needs no swap
 * pass of its own. Returns the glass window it goes to. */
disp_win_t disp_rot_gather(uint8_t rot, int32_t n, disp_win_t logical, const uint16_t *px,
                           uint16_t *out);

/* ---- what a frame costs (0.0.22): the debug snapshot's `display.rot_ms` and telemetry ----
 * Per rotation, so a turned panel's frames are never averaged with upright ones. `flush` is the
 * UI task's time in the flush callback over a frame's strips: the swap or gather, the commands,
 * and `wait` (0.0.25), the part of it spent blocked on a transfer still on the wire. `wall` is
 * the frame from LVGL's render start to its last strip sent (drawing and sending). A full frame
 * is one that covered the whole glass — a turn, an app opened — the only kind whose numbers
 * compare across rotations. */
typedef struct {
    uint32_t frames, last_ms, max_ms;
    uint64_t sum_us;
    uint32_t full, full_ms, full_max_ms, full_wall_ms;
    uint64_t full_sum_us, full_wait_us;
} disp_stat_t;
void disp_stat_add(disp_stat_t *s, uint32_t flush_us, uint32_t wait_us, uint32_t wall_us,
                   bool full);
uint32_t disp_stat_avg_ms(const disp_stat_t *s);
uint32_t disp_stat_full_avg_ms(const disp_stat_t *s);
/* The snapshot's `display.rot_ms`: for each rotation that has had a whole-glass frame,
 * "<deg>:<mean>,<worst>,<count>,<last wall>,<mean wait>" in ms, space-separated, "" before any.
 * One short string because the snapshot's answer is capped at 6 KB (dbg_link's OUT_MAX) and
 * nearly full. */
void disp_stat_fmt(char *out, size_t len, const disp_stat_t st[4]);
