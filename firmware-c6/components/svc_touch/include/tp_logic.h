/* The CST9220's report and what a finger means, pure so the host suite checks every byte.
 *
 * The report layout is from Waveshare's `esp_lcd_touch_cst9217` v1.0.4 (Apache-2.0,
 * © 2015-2024 Espressif Systems, © 2025 Waveshare; HARDWARE.md §3), read and re-expressed here,
 * not copied: register 0xD000, ten bytes, byte 6 == 0xAB on a valid report, the point count in
 * byte 5, point 0's status in byte 0's low nibble (0x06 pressed) and two 12-bit axes. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define TP_SIDE 480
#define TP_REPORT_LEN 10
#define TP_ACK 0xAB
/* Polls a held finger may go unreported before it counts as lifted (~60 ms at 20 ms a poll).
   The vendor's IDF driver keeps the last state on a report without the ack byte, its Arduino
   reader calls the same report a release; holding briefly, then releasing, is neither a stroke
   cut in two by one quiet read nor a finger stuck down forever. */
#define TP_HOLD_POLLS 3

typedef enum { TP_NOACK, TP_UP, TP_DOWN } tp_kind_t;

typedef struct {
    tp_kind_t kind;
    uint16_t raw_a, raw_b; /* the chip's two axes before mapping; set only for TP_DOWN */
} tp_report_t;

tp_report_t tp_decode(const uint8_t d[TP_REPORT_LEN]);

/* Measured on the board (0.0.6, four corner taps): display x = 479 - rawB, display y = rawA.
 * The vendor's swap_xy + mirror_y (HARDWARE.md §3) put every tap in the opposite corner, because
 * this firmware draws the panel 180° from the vendor's demos. Clamped to the glass, so a reading
 * past an edge lands on that edge rather than wrapping to the far one. */
void tp_map(uint16_t raw_a, uint16_t raw_b, int16_t *x, int16_t *y);

/* ---- 0.0.18: calibration, and the panel turned ----
 * A finger goes raw → the measured mapping above (the glass's own frame) → the owner's
 * calibration, still in the glass's frame, so it means the same whichever way up the panel is →
 * the rotation undone, into the upright screen LVGL draws (disp_logic.h turns that screen onto the
 * glass the opposite way). */

/* An affine correction in Q16: x' = (a·x + b·y + c) / 65536, y' = (d·x + e·y + f) / 65536, the
 * offsets c and f in Q16 pixels. Identity is {65536, 0, 0, 0, 65536, 0}. */
#define TP_Q16 65536
typedef struct {
    int32_t a, b, c, d, e, f;
} tp_cal_t;

void tp_cal_identity(tp_cal_t *c);
/* Applied, rounded, and clamped to the glass. */
void tp_cal_apply(const tp_cal_t *c, int16_t x, int16_t y, int16_t *ox, int16_t *oy);

/* A point on the glass of a panel turned `rot` quarter turns clockwise, as the upright screen has
 * it: the inverse of disp_rot_point. */
void tp_unrotate(uint8_t rot, int16_t px, int16_t py, int16_t *lx, int16_t *ly);
/* And back: a point of the upright screen, on the turned panel's glass (disp_rot_point). */
void tp_rotate(uint8_t rot, int16_t lx, int16_t ly, int16_t *px, int16_t *py);

typedef struct {
    tp_cal_t cal;
    bool cal_on; /* off: the owner's calibration is not in force, or is being measured afresh */
    uint8_t rot;
} tp_xf_t;

/* The whole path for one raw point. */
void tp_xform(const tp_xf_t *xf, uint16_t raw_a, uint16_t raw_b, int16_t *x, int16_t *y);

/* The fit, over the targets' true positions and where the taps landed, both in the glass's frame
 * with no calibration in force: least squares, so four corners leave a residual to judge it by. */
typedef struct {
    int16_t x, y;
} tp_pt_t;

typedef enum {
    TP_FIT_OK,
    TP_FIT_DEGENERATE, /* too few taps, or all in a line or a heap: no plane to fit */
    TP_FIT_WILD,       /* a scale, a shear or a shift no real panel needs */
    TP_FIT_SCATTERED,  /* the taps disagree with every straight correction */
} tp_fit_t;

#define TP_CAL_MIN_POINTS 3
/* Scales within 15%, shears within 0.1, and nowhere on the glass moved more than 48 px: the
 * 0.0.7 mapping is already close (corners within ~20 px), so anything past these is a bad tap,
 * not a panel. */
#define TP_CAL_SCALE_PCT 15
#define TP_CAL_SHEAR_PCT 10
#define TP_CAL_MAX_SHIFT 48
/* Every tap within this of where the fit puts its target. */
#define TP_CAL_MAX_RESIDUAL 12

tp_fit_t tp_cal_fit(const tp_pt_t *want, const tp_pt_t *got, uint8_t n, tp_cal_t *out);

typedef enum { TP_EV_NONE, TP_EV_DOWN, TP_EV_MOVE, TP_EV_UP } tp_ev_t;

typedef struct {
    bool down;
    uint8_t quiet;
    int16_t x, y;
    /* The mapping a press takes (0.0.18); NULL is the measured mapping alone. */
    const tp_xf_t *xf;
} tp_track_t;

/* One poll's outcome into the stroke. `r` NULL is a failed bus read, which counts as a quiet
 * poll: a stroke ends by a report saying so or after TP_HOLD_POLLS quiet polls, never by being
 * forgotten. */
tp_ev_t tp_track(tp_track_t *t, const tp_report_t *r);

/* What the touch task hands the UI task. Moves coalesce, so the ring keeps every press and
 * every release however late LVGL reads, and drops (counted) only when eight edges pile up. */
typedef struct {
    int16_t x, y;
    tp_ev_t ev;
    uint32_t at_ms;
} tp_sample_t;

#define TP_RING 8

typedef struct {
    tp_sample_t s[TP_RING];
    uint8_t head, count;
    uint32_t dropped;
} tp_ring_t;

void tp_ring_push(tp_ring_t *q, const tp_sample_t *s);
bool tp_ring_pop(tp_ring_t *q, tp_sample_t *out);
