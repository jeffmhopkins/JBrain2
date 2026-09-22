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
