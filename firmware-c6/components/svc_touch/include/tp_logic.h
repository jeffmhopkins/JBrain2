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

typedef enum { TP_EV_NONE, TP_EV_DOWN, TP_EV_MOVE, TP_EV_UP } tp_ev_t;

typedef struct {
    bool down;
    uint8_t quiet;
    int16_t x, y;
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
