#include "orient.h"

#include <math.h>

/* Where gravity sits, in degrees of atan2(ay, ax), for each quarter. Read off the branch this
   replaces: quarter 0 was `ax > +T`, 2 was `ax < -T`, 1 was `ay < -T` and 3 was `ay > +T`. */
static const float CENTRE_DEG[4] = {0.0f, 270.0f, 180.0f, 90.0f};

/* Signed difference between two angles, wrapped to (-180, 180]. */
static float delta(float a, float b)
{
    float d = a - b;
    while (d > 180.0f) d -= 360.0f;
    while (d <= -180.0f) d += 360.0f;
    return d;
}

int orient_quarter(int quarter, int ax, int ay)
{
    if (quarter < 0 || quarter > 3) quarter = 0;
    const float fx = (float)ax, fy = (float)ay;
    /* Squared, in float, because two int16 products overflow int32 between them. */
    if (fx * fx + fy * fy < (float)ORIENT_MIN_MAG * (float)ORIENT_MIN_MAG) return quarter;

    const float ang = atan2f(fy, fx) * 180.0f / (float)M_PI;
    const float held = delta(ang, CENTRE_DEG[quarter]);
    if (held <= 45.0f + ORIENT_HYST_DEG && held >= -(45.0f + ORIENT_HYST_DEG)) return quarter;

    /* Out of the band: take whichever quarter the panel is actually nearest, rather than the
       neighbour it happened to leave through. A panel turned a half circle in one frame — set
       down, picked up the other way — should land where it IS, not one step around. */
    int best = quarter;
    float best_off = 360.0f;
    for (int q = 0; q < 4; q++) {
        float off = delta(ang, CENTRE_DEG[q]);
        if (off < 0.0f) off = -off;
        if (off < best_off) {
            best_off = off;
            best = q;
        }
    }
    return best;
}

void orient_axes(int quarter, int ax, int ay, int *tilt, int *down)
{
    switch (quarter) {
    case 1:
        *tilt = -ax;
        *down = -ay;
        break;
    case 2:
        *tilt = ay;
        *down = -ax;
        break;
    case 3:
        *tilt = ax;
        *down = ay;
        break;
    default:
        *tilt = -ay;
        *down = ax;
        break;
    }
}

int orient_window(int quarter, int lean, int lean_max)
{
    if ((quarter != 1 && quarter != 3) || lean_max <= 0) return 0;
    if (lean > lean_max) lean = lean_max;
    if (lean < -lean_max) lean = -lean_max;
    const int v = lean * ORIENT_WIN_MAX / lean_max;
    return ((quarter == 1 ? -v : v) / 2) * 2;
}

void orient_panel_to_frame(int quarter, int win, int px, int py, int *fx, int *fy)
{
    switch (quarter) {
    case 1:
        *fx = ORIENT_SQ_Y0 + win + ORIENT_SQ - 1 - py;
        *fy = ORIENT_SQ_Y0 + px;
        break;
    case 3:
        *fx = py - ORIENT_SQ_Y0 - win;
        *fy = ORIENT_SQ_Y0 + ORIENT_SQ - 1 - px;
        break;
    default:
        *fx = px;
        *fy = py;
        break;
    }
}

float orient_upright(int tilt, int down, float share, float max_deg)
{
    /* `down` is gravity's own axis and positive in the quarter that is held; flat or worse,
       the angle is not this quarter's to give. */
    float turned = atan2f((float)tilt, (float)(down > 0 ? down : 1)) * 180.0f / (float)M_PI;
    if (turned > max_deg) turned = max_deg;
    if (turned < -max_deg) turned = -max_deg;
    return -turned * share;
}
