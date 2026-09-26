#include "confirm.h"

#include <stdbool.h>
#include <math.h>

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

bool confirm_hit_centre(int fx, int fy, int over_h)
{
    return within(fx, fy, CONFIRM_CX_CENTRE, confirm_cy(over_h));
}

/* A filled axis-aligned rectangle, clipped. */
static void rect(uint16_t *fb, int w, int h, int x0, int y0, int x1, int y1, uint16_t colour)
{
    for (int y = y0; y <= y1; y++) {
        if (y < 0 || y >= h) continue;
        for (int x = x0; x <= x1; x++) {
            if (x >= 0 && x < w) fb[y * w + x] = colour;
        }
    }
}

void confirm_draw_stop(uint16_t *fb, int w, int h, int over_h)
{
    const int cy = confirm_cy(over_h);
    disc(fb, w, h, CONFIRM_CX_CENTRE, cy, CONFIRM_R, CONFIRM_RED);
    /* A SQUARE, not an X. The cross already means "throw this away" on the recording screen,
       and a control that stops a message playing must not read as one that destroys it — the
       message stays in the queue either way. Square is what every transport control on earth
       uses for stop, including the ones these children have already seen on a television. */
    const int a = CONFIRM_R / 2;
    rect(fb, w, h, CONFIRM_CX_CENTRE - a, cy - a, CONFIRM_CX_CENTRE + a, cy + a, CONFIRM_GLYPH);
}

/* An arc of the circle centred on (cx,cy), swept between two angles in degrees, drawn with the
   same walked disc the strokes use so its ends and its thickness match them. */
static void arc(uint16_t *fb, int w, int h, int cx, int cy, int r, int deg0, int deg1,
                uint16_t colour)
{
    const int steps = (deg1 - deg0 > 0 ? deg1 - deg0 : deg0 - deg1) * 2;
    for (int i = 0; i <= steps; i++) {
        const double t = (double)deg0 + (double)(deg1 - deg0) * (double)i / (double)steps;
        const double rad = t * M_PI / 180.0;
        disc(fb, w, h, cx + (int)lround(cos(rad) * r), cy + (int)lround(sin(rad) * r),
             CONFIRM_STROKE / 2, colour);
    }
}

/* A filled triangle, by half-plane test. Three points, no winding assumption. */
static void tri(uint16_t *fb, int w, int h, const int x[3], const int y[3], uint16_t colour)
{
    int xmin = x[0], xmax = x[0], ymin = y[0], ymax = y[0];
    for (int i = 1; i < 3; i++) {
        if (x[i] < xmin) xmin = x[i];
        if (x[i] > xmax) xmax = x[i];
        if (y[i] < ymin) ymin = y[i];
        if (y[i] > ymax) ymax = y[i];
    }
    for (int py = ymin; py <= ymax; py++) {
        if (py < 0 || py >= h) continue;
        for (int px = xmin; px <= xmax; px++) {
            if (px < 0 || px >= w) continue;
            const int d0 = (x[1] - x[0]) * (py - y[0]) - (y[1] - y[0]) * (px - x[0]);
            const int d1 = (x[2] - x[1]) * (py - y[1]) - (y[2] - y[1]) * (px - x[1]);
            const int d2 = (x[0] - x[2]) * (py - y[2]) - (y[0] - y[2]) * (px - x[2]);
            const bool neg = (d0 < 0) || (d1 < 0) || (d2 < 0);
            const bool pos = (d0 > 0) || (d1 > 0) || (d2 > 0);
            if (!(neg && pos)) fb[py * w + px] = colour;
        }
    }
}

void confirm_draw_repeat(uint16_t *fb, int w, int h, int over_h)
{
    const int cy = confirm_cy(over_h);
    disc(fb, w, h, CONFIRM_CX_CENTRE, cy, CONFIRM_R, CONFIRM_BLUE);

    /* Three quarters of a circle with an arrowhead on the open end — the refresh symbol, which
       is the one circular arrow an adult reads instantly and a child has seen on every remote
       and tablet in the house. The gap is at the top right so the head sits where the eye
       finishes the sweep. */
    const int r = (CONFIRM_R * 55) / 100;
    arc(fb, w, h, CONFIRM_CX_CENTRE, cy, r, 300, 580, CONFIRM_GLYPH);

    /* The head, tangent to the arc at its end. Pointing anticlockwise-onward, which is the
       direction the sweep was travelling. */
    const double end = 300.0 * M_PI / 180.0;
    const int hx = CONFIRM_CX_CENTRE + (int)lround(cos(end) * r);
    const int hy = cy + (int)lround(sin(end) * r);
    const int a = CONFIRM_STROKE + 5;
    const int tx[3] = {hx - a, hx + a, hx + (a / 2)};
    const int ty[3] = {hy - (a / 2), hy - (a / 2), hy + a};
    tri(fb, w, h, tx, ty, CONFIRM_GLYPH);
}
