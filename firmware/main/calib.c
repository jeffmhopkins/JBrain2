#include "calib.h"

#include <stddef.h>

#include "face.h" /* FACE_W, FACE_H — the same pure island */

/* Far enough out to measure the edge behaviour, far enough in to be hittable with a fingertip
   rather than a fingernail on the bezel. See the header for why these four. */
const float CAL_FRAC[CAL_KNOTS] = {0.06f, 0.35f, 0.65f, 0.94f};

int calib_target_x(int i)
{
    if (i < 0) i = 0;
    if (i >= CAL_KNOTS) i = CAL_KNOTS - 1;
    return (int)(CAL_FRAC[i] * (float)(FACE_W - 1));
}

int calib_target_y(int j)
{
    if (j < 0) j = 0;
    if (j >= CAL_KNOTS) j = CAL_KNOTS - 1;
    return (int)(CAL_FRAC[j] * (float)(FACE_H - 1));
}

/* One axis: piecewise linear through the knots, extended linearly beyond the outer two and
   then clamped to the panel.
 *
 * EXTENDED, NOT CLAMPED, AT THE ENDS. The outer 12% of the panel lies beyond the outermost
 * target — it has to, since a target on the bezel cannot be tapped — and clamping there would
 * flatten precisely the band the owner reported as skewed. Extending the outer segment keeps
 * correcting out to the edge; the clamp afterwards only stops a wild reading leaving the
 * panel. */
static int map_axis(const int16_t *raw, int n, int (*target)(int), int span, int v)
{
    /* Which segment: below the first knot, above the last, or between k and k+1. */
    int k = 0;
    if (v <= raw[0]) {
        k = 0;
    } else if (v >= raw[n - 1]) {
        k = n - 2;
    } else {
        while (k < n - 2 && v > raw[k + 1]) k++;
    }
    const int r0 = raw[k], r1 = raw[k + 1];
    const int t0 = target(k), t1 = target(k + 1);
    const int dr = r1 - r0;
    /* `calib_build` guarantees dr > 0; this is belt and braces against a hand-made struct. */
    if (dr == 0) return v;
    long out = (long)t0 + (long)(v - r0) * (long)(t1 - t0) / (long)dr;
    if (out < 0) out = 0;
    if (out > span - 1) out = span - 1;
    return (int)out;
}

void calib_apply(const calib_t *c, int raw_x, int raw_y, int *sx, int *sy)
{
    if (c == NULL || !c->valid) {
        if (sx != NULL) *sx = raw_x;
        if (sy != NULL) *sy = raw_y;
        return;
    }
    if (sx != NULL) *sx = map_axis(c->raw_x, CAL_KNOTS, calib_target_x, FACE_W, raw_x);
    if (sy != NULL) *sy = map_axis(c->raw_y, CAL_KNOTS, calib_target_y, FACE_H, raw_y);
}

bool calib_build(const int16_t mx[CAL_KNOTS][CAL_KNOTS], const int16_t my[CAL_KNOTS][CAL_KNOTS],
                 calib_t *out)
{
    if (mx == NULL || my == NULL || out == NULL) return false;
    calib_t c = {0};
    for (int i = 0; i < CAL_KNOTS; i++) {
        long sx = 0, sy = 0;
        for (int j = 0; j < CAL_KNOTS; j++) {
            sx += mx[j][i]; /* column i: every row aimed at the same x */
            sy += my[i][j]; /* row i: every column aimed at the same y */
        }
        c.raw_x[i] = (int16_t)(sx / CAL_KNOTS);
        c.raw_y[i] = (int16_t)(sy / CAL_KNOTS);
    }
    for (int i = 0; i + 1 < CAL_KNOTS; i++) {
        if (c.raw_x[i + 1] <= c.raw_x[i]) return false;
        if (c.raw_y[i + 1] <= c.raw_y[i]) return false;
    }
    c.valid = true;
    *out = c;
    return true;
}

#define CAL_MAGIC0 0xCA
#define CAL_MAGIC1 0x17

int calib_save(const calib_t *c, uint8_t *buf, int cap)
{
    if (c == NULL || buf == NULL || cap < CAL_BLOB_BYTES || !c->valid) return 0;
    int n = 0;
    buf[n++] = CAL_MAGIC0;
    buf[n++] = CAL_MAGIC1;
    for (int i = 0; i < CAL_KNOTS; i++) {
        buf[n++] = (uint8_t)(c->raw_x[i] & 0xFF);
        buf[n++] = (uint8_t)((c->raw_x[i] >> 8) & 0xFF);
    }
    for (int i = 0; i < CAL_KNOTS; i++) {
        buf[n++] = (uint8_t)(c->raw_y[i] & 0xFF);
        buf[n++] = (uint8_t)((c->raw_y[i] >> 8) & 0xFF);
    }
    return n;
}

bool calib_load(const uint8_t *buf, int len, calib_t *out)
{
    if (buf == NULL || out == NULL || len < CAL_BLOB_BYTES) return false;
    if (buf[0] != CAL_MAGIC0 || buf[1] != CAL_MAGIC1) return false;
    calib_t c = {0};
    int n = 2;
    for (int i = 0; i < CAL_KNOTS; i++) {
        c.raw_x[i] = (int16_t)((uint16_t)buf[n] | ((uint16_t)buf[n + 1] << 8));
        n += 2;
    }
    for (int i = 0; i < CAL_KNOTS; i++) {
        c.raw_y[i] = (int16_t)((uint16_t)buf[n] | ((uint16_t)buf[n + 1] << 8));
        n += 2;
    }
    /* Monotonicity is re-checked on the way IN, not just on the way out: NVS is the one place
       a bad calibration could survive a reboot, and a folded map is the failure whose only
       remedy is the touchscreen it broke. */
    for (int i = 0; i + 1 < CAL_KNOTS; i++) {
        if (c.raw_x[i + 1] <= c.raw_x[i]) return false;
        if (c.raw_y[i + 1] <= c.raw_y[i]) return false;
    }
    c.valid = true;
    *out = c;
    return true;
}

void calib_sample_reset(cal_samples_t *s)
{
    if (s != NULL) s->n = 0;
}

void calib_sample_add(cal_samples_t *s, int x, int y)
{
    if (s == NULL) return;
    if (s->n < CAL_SAMPLES_MAX) {
        s->x[s->n] = (int16_t)x;
        s->y[s->n] = (int16_t)y;
        s->n++;
        return;
    }
    /* Full: drop the oldest. A target the owner kept tapping until it felt right should be
       judged on the taps that felt right. */
    for (int i = 1; i < CAL_SAMPLES_MAX; i++) {
        s->x[i - 1] = s->x[i];
        s->y[i - 1] = s->y[i];
    }
    s->x[CAL_SAMPLES_MAX - 1] = (int16_t)x;
    s->y[CAL_SAMPLES_MAX - 1] = (int16_t)y;
}

static int spread_of(const int16_t *v, int n)
{
    int lo = v[0], hi = v[0];
    for (int i = 1; i < n; i++) {
        if (v[i] < lo) lo = v[i];
        if (v[i] > hi) hi = v[i];
    }
    return hi - lo;
}

int calib_sample_spread(const cal_samples_t *s)
{
    if (s == NULL || s->n < 2) return 0;
    const int sx = spread_of(s->x, s->n), sy = spread_of(s->y, s->n);
    return sx > sy ? sx : sy;
}

bool calib_sample_settled(const cal_samples_t *s)
{
    if (s == NULL || s->n < CAL_SAMPLES_MIN) return false;
    return calib_sample_spread(s) <= CAL_SPREAD_MAX;
}

static int16_t median_of(const int16_t *v, int n)
{
    /* Insertion sort of a copy: n is at most CAL_SAMPLES_MAX, and a sort nobody can get
       wrong is worth more here than one nobody can measure the speed of. */
    int16_t t[CAL_SAMPLES_MAX];
    for (int i = 0; i < n; i++) t[i] = v[i];
    for (int i = 1; i < n; i++) {
        const int16_t k = t[i];
        int j = i - 1;
        while (j >= 0 && t[j] > k) {
            t[j + 1] = t[j];
            j--;
        }
        t[j + 1] = k;
    }
    if (n % 2 == 1) return t[n / 2];
    return (int16_t)(((int)t[n / 2 - 1] + (int)t[n / 2]) / 2);
}

void calib_sample_result(const cal_samples_t *s, int16_t *x, int16_t *y)
{
    if (s == NULL || s->n <= 0) return;
    if (x != NULL) *x = median_of(s->x, s->n);
    if (y != NULL) *y = median_of(s->y, s->n);
}
