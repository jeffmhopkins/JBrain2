/* Which way is down, from the QMI8658's accelerometer, with the hysteresis that keeps a panel
 * leaned near 45° from flickering (plan §2b, owner 2026-10-13). Pure C, host-tested: the twins'
 * S3 `orient.c` was extracted the same way after its first version shipped backwards.
 *
 * A rotation is how many quarter turns CLOCKWISE the panel has been turned from upright, which is
 * the edge now at the bottom: 0 its own bottom, 1 its right edge, 2 its top, 3 its left edge
 * (disp_logic.h turns the content to match). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* The accelerometer at ±2 g: 16384 counts a g. */
#define ORIENT_1G 16384
/* A new edge counts as down only this far past the 45° line into its quadrant, i.e. within
 * 45 - 25 = 20° of that edge's own axis. Between those, whatever is showing holds. */
#define ORIENT_MARGIN_DEG 25
/* tan(20°) in Q10: |sideways| ≤ this × along, the test for "within 20° of the axis". */
#define ORIENT_TAN_Q10 373
/* ...and it must stay there this long, every reading of it, before the screen turns. */
#define ORIENT_DWELL_MS 700
/* Under half a g in the glass's plane the panel is lying (within 30° of) flat, face up or face
 * down: there is no "down" on the glass to follow, so nothing changes. */
#define ORIENT_FLAT_MG 500
/* A total more than this far from 1 g is a hand moving the panel, not gravity. */
#define ORIENT_STILL_MG 200
/* How often it is read while the screen is on: 8 Hz. After a failed transfer, once a second. */
#define ORIENT_PERIOD_MS 125
#define ORIENT_RETRY_MS 1000

/* What one reading said. */
typedef enum {
    ORIENT_SEEN_NONE,   /* nothing read yet */
    ORIENT_SEEN_EDGE,   /* clearly one edge down (`edge` says which) */
    ORIENT_SEEN_BETWEEN, /* leaning between two edges: hold */
    ORIENT_SEEN_FLAT,
    ORIENT_SEEN_MOVING,
} orient_seen_t;

typedef struct {
    uint8_t rot;      /* what the screen shows */
    uint8_t cand;     /* the edge waiting out its dwell */
    bool has_cand;
    uint32_t cand_since_ms;
    orient_seen_t seen;
    uint8_t edge;     /* the last reading's edge when seen == EDGE */
    uint32_t changes;
} orient_t;

void orient_init(orient_t *o);

/* How the chip's X/Y sit against the glass. Bit 0: swap them; bit 1: flip the glass's x; bit 2:
 * flip its y. 0 assumes the chip's +X points to the glass's right and +Y to its bottom. Only the
 * board can settle it (HARDWARE.md §6), so it is the `imu_axes` setting, changeable from the debug
 * `set` without a new image. */
#define ORIENT_AXES_MAX 7
/* Gravity's direction ("down") on the glass, from one raw reading: a resting accelerometer reads
 * +1 g along the axis that points UP, so down is the reading's opposite. */
void orient_down(uint8_t axes, const int16_t a[3], int32_t *dx, int32_t *dy);

/* One reading, `axes` as above, at `now_ms`. True when the rotation changed. */
bool orient_feed(orient_t *o, uint8_t axes, const int16_t a[3], uint32_t now_ms);

/* A change of mind that is not the accelerometer's — the screen went dark, auto-rotate was
 * switched — forgets any edge waiting out its dwell. */
void orient_forget(orient_t *o);

const char *orient_seen_name(orient_seen_t s);
/* "bottom", "right", "top", "left". */
const char *orient_edge_name(uint8_t rot);
