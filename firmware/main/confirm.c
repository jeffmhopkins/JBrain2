#include "confirm.h"

#include <stdbool.h>

int confirm_cy(int over_h)
{
    return over_h - CONFIRM_MARGIN - CONFIRM_R;
}

static bool within(int fx, int fy, int cx, int cy)
{
    const int dx = fx - cx, dy = fy - cy;
    return dx * dx + dy * dy <= CONFIRM_HIT_R * CONFIRM_HIT_R;
}

confirm_hit_t confirm_hit(int fx, int fy, int over_h)
{
    const int cy = confirm_cy(over_h);
    /* Cancel is tested first only so the order is fixed and readable; the two targets cannot
       overlap (see the centres in the header), so no finger can satisfy both. */
    if (within(fx, fy, CONFIRM_CX_CANCEL, cy)) return CONFIRM_CANCEL;
    if (within(fx, fy, CONFIRM_CX_SEND, cy)) return CONFIRM_SEND;
    return CONFIRM_NONE;
}

/* A filled disc, clipped to the buffer. */
static void disc(uint16_t *fb, int w, int h, int cx, int cy, int r, uint16_t colour)
{
    for (int dy = -r; dy <= r; dy++) {
        const int py = cy + dy;
        if (py < 0 || py >= h) continue;
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx;
            if (px >= 0 && px < w) fb[py * w + px] = colour;
        }
    }
}

/* A thick stroke between two points: a disc walked along the line rather than a thin line
   widened afterwards, which is what keeps the elbow of the tick and the crossing of the cross
   solid instead of showing a notch where two strokes meet. */
static void stroke(uint16_t *fb, int w, int h, int x0, int y0, int x1, int y1, uint16_t colour)
{
    const int dx = x1 - x0, dy = y1 - y0;
    const int ax = dx < 0 ? -dx : dx, ay = dy < 0 ? -dy : dy;
    int steps = ax > ay ? ax : ay;
    if (steps < 1) steps = 1;
    for (int i = 0; i <= steps; i++) {
        disc(fb, w, h, x0 + dx * i / steps, y0 + dy * i / steps, CONFIRM_STROKE / 2, colour);
    }
}

void confirm_draw(uint16_t *fb, int w, int h, int over_h)
{
    const int cy = confirm_cy(over_h);
    const int a = CONFIRM_R / 2;

    /* CANCEL, left. */
    disc(fb, w, h, CONFIRM_CX_CANCEL, cy, CONFIRM_R, CONFIRM_RED);
    stroke(fb, w, h, CONFIRM_CX_CANCEL - a, cy - a, CONFIRM_CX_CANCEL + a, cy + a,
           CONFIRM_GLYPH);
    stroke(fb, w, h, CONFIRM_CX_CANCEL + a, cy - a, CONFIRM_CX_CANCEL - a, cy + a,
           CONFIRM_GLYPH);

    /* SEND, right. Both arms are drawn OUTWARDS FROM THE ELBOW so they meet exactly there;
       drawing the long arm as one stroke through the joint would thin the corner. */
    disc(fb, w, h, CONFIRM_CX_SEND, cy, CONFIRM_R, CONFIRM_GREEN);
    const int ex = CONFIRM_CX_SEND - a / 3, ey = cy + a * 2 / 3;
    stroke(fb, w, h, ex, ey, CONFIRM_CX_SEND - a, cy - a / 4, CONFIRM_GLYPH);
    stroke(fb, w, h, ex, ey, CONFIRM_CX_SEND + a, cy - a, CONFIRM_GLYPH);
}
