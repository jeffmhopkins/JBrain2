#include "orient_logic.h"

#include <stddef.h>

void orient_init(orient_t *o)
{
    *o = (orient_t){0};
}

void orient_down(uint8_t axes, const int16_t a[3], int32_t *dx, int32_t *dy)
{
    int32_t u = a[0], v = a[1];
    if (axes & 1) {
        u = a[1];
        v = a[0];
    }
    *dx = axes & 2 ? u : -u;
    *dy = axes & 4 ? v : -v;
}

static int64_t sq(int64_t v)
{
    return v * v;
}

/* Along the edge's axis, and across it, for the down vector: edge 0 is +y, 1 is +x, 2 -y, 3 -x. */
static void along(uint8_t edge, int32_t dx, int32_t dy, int32_t *on, int32_t *across)
{
    switch (edge) {
    case 1: *on = dx, *across = dy; break;
    case 2: *on = -dy, *across = dx; break;
    case 3: *on = -dx, *across = dy; break;
    default: *on = dy, *across = dx; break;
    }
}

static orient_seen_t classify(uint8_t axes, const int16_t a[3], uint8_t *edge)
{
    const int64_t total = sq(a[0]) + sq(a[1]) + sq(a[2]);
    const int64_t lo = ORIENT_1G - ORIENT_1G * ORIENT_STILL_MG / 1000;
    const int64_t hi = ORIENT_1G + ORIENT_1G * ORIENT_STILL_MG / 1000;
    if (total < lo * lo || total > hi * hi) return ORIENT_SEEN_MOVING;
    int32_t dx, dy;
    orient_down(axes, a, &dx, &dy);
    const int64_t flat = ORIENT_1G * ORIENT_FLAT_MG / 1000;
    if (sq(dx) + sq(dy) < flat * flat) return ORIENT_SEEN_FLAT;
    for (uint8_t e = 0; e < 4; e++) {
        int32_t on, across;
        along(e, dx, dy, &on, &across);
        const int64_t side = across < 0 ? -(int64_t)across : across;
        if (on > 0 && side * 1024 <= (int64_t)on * ORIENT_TAN_Q10) {
            *edge = e;
            return ORIENT_SEEN_EDGE;
        }
    }
    return ORIENT_SEEN_BETWEEN;
}

void orient_forget(orient_t *o)
{
    o->has_cand = false;
}

bool orient_feed(orient_t *o, uint8_t axes, const int16_t a[3], uint32_t now_ms)
{
    uint8_t edge = 0;
    o->seen = classify(axes, a, &edge);
    o->edge = edge;
    /* Only a clear edge that is not already showing starts or continues a dwell; anything else
       — the edge showing, a lean, flat, a hand moving it — starts the count again. */
    if (o->seen != ORIENT_SEEN_EDGE || edge == o->rot) {
        o->has_cand = false;
        return false;
    }
    if (!o->has_cand || o->cand != edge) {
        o->has_cand = true;
        o->cand = edge;
        o->cand_since_ms = now_ms;
        return false;
    }
    if ((uint32_t)(now_ms - o->cand_since_ms) < ORIENT_DWELL_MS) return false;
    o->rot = edge;
    o->has_cand = false;
    o->changes++;
    return true;
}

const char *orient_seen_name(orient_seen_t s)
{
    static const char *const N[] = {"none", "edge", "between", "flat", "moving"};
    return (unsigned)s < sizeof N / sizeof *N ? N[s] : "?";
}

const char *orient_edge_name(uint8_t rot)
{
    static const char *const N[] = {"bottom", "right", "top", "left"};
    return N[rot & 3];
}
