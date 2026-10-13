#include "tp_logic.h"

#include <stddef.h>

tp_report_t tp_decode(const uint8_t d[TP_REPORT_LEN])
{
    tp_report_t r = {TP_NOACK, 0, 0};
    if (d[6] != TP_ACK) return r;
    r.kind = TP_UP;
    if ((d[5] & 0x7F) == 0 || (d[0] & 0x0F) != 0x06) return r;
    r.kind = TP_DOWN;
    r.raw_a = (uint16_t)((d[1] << 4) | (d[3] >> 4));
    r.raw_b = (uint16_t)((d[2] << 4) | (d[3] & 0x0F));
    return r;
}

static int16_t on_glass(uint16_t v)
{
    return (int16_t)(v >= TP_SIDE ? TP_SIDE - 1 : v);
}

void tp_map(uint16_t raw_a, uint16_t raw_b, int16_t *x, int16_t *y)
{
    *x = (int16_t)(TP_SIDE - 1 - on_glass(raw_b));
    *y = on_glass(raw_a);
}

void tp_cal_identity(tp_cal_t *c)
{
    *c = (tp_cal_t){TP_Q16, 0, 0, 0, TP_Q16, 0};
}

static int16_t clamp_glass(int64_t v)
{
    return (int16_t)(v < 0 ? 0 : v > TP_SIDE - 1 ? TP_SIDE - 1 : v);
}

/* Q16 to the nearest pixel, halves away from zero. */
static int64_t q16_round(int64_t v)
{
    return v >= 0 ? (v + TP_Q16 / 2) / TP_Q16 : -((-v + TP_Q16 / 2) / TP_Q16);
}

static void apply_free(const tp_cal_t *c, int32_t x, int32_t y, int32_t *ox, int32_t *oy)
{
    *ox = (int32_t)q16_round((int64_t)c->a * x + (int64_t)c->b * y + c->c);
    *oy = (int32_t)q16_round((int64_t)c->d * x + (int64_t)c->e * y + c->f);
}

void tp_cal_apply(const tp_cal_t *c, int16_t x, int16_t y, int16_t *ox, int16_t *oy)
{
    int32_t fx, fy;
    apply_free(c, x, y, &fx, &fy);
    *ox = clamp_glass(fx);
    *oy = clamp_glass(fy);
}

void tp_unrotate(uint8_t rot, int16_t px, int16_t py, int16_t *lx, int16_t *ly)
{
    switch (rot & 3) {
    case 1:
        *lx = (int16_t)(TP_SIDE - 1 - py);
        *ly = px;
        break;
    case 2:
        *lx = (int16_t)(TP_SIDE - 1 - px);
        *ly = (int16_t)(TP_SIDE - 1 - py);
        break;
    case 3:
        *lx = py;
        *ly = (int16_t)(TP_SIDE - 1 - px);
        break;
    default:
        *lx = px;
        *ly = py;
        break;
    }
}

void tp_rotate(uint8_t rot, int16_t lx, int16_t ly, int16_t *px, int16_t *py)
{
    /* A quarter turn's inverse is three more of them. */
    tp_unrotate((uint8_t)((4 - (rot & 3)) & 3), lx, ly, px, py);
}

void tp_xform(const tp_xf_t *xf, uint16_t raw_a, uint16_t raw_b, int16_t *x, int16_t *y)
{
    int16_t gx, gy;
    tp_map(raw_a, raw_b, &gx, &gy);
    if (!xf) {
        *x = gx;
        *y = gy;
        return;
    }
    if (xf->cal_on) tp_cal_apply(&xf->cal, gx, gy, &gx, &gy);
    tp_unrotate(xf->rot, gx, gy, x, y);
}

/* One axis of the least-squares fit: k0·x + k1·y + k2 ≈ t over the points, by Cramer's rule on the
   normal equations (centred, so the numbers stay small). */
static void fit_axis(const tp_pt_t *got, const int32_t *t, uint8_t n, double mx, double my,
                     double sxx, double sxy, double syy, double det, double k[3])
{
    double mt = 0, sxt = 0, syt = 0;
    for (uint8_t i = 0; i < n; i++) mt += t[i];
    mt /= n;
    for (uint8_t i = 0; i < n; i++) {
        sxt += (got[i].x - mx) * (t[i] - mt);
        syt += (got[i].y - my) * (t[i] - mt);
    }
    k[0] = (sxt * syy - syt * sxy) / det;
    k[1] = (syt * sxx - sxt * sxy) / det;
    k[2] = mt - k[0] * mx - k[1] * my;
}

static int32_t to_q16(double v)
{
    return (int32_t)(v >= 0 ? v * TP_Q16 + 0.5 : v * TP_Q16 - 0.5);
}

static int32_t dist2(int32_t ax, int32_t ay, int32_t bx, int32_t by)
{
    return (ax - bx) * (ax - bx) + (ay - by) * (ay - by);
}

tp_fit_t tp_cal_fit(const tp_pt_t *want, const tp_pt_t *got, uint8_t n, tp_cal_t *out)
{
    if (n < TP_CAL_MIN_POINTS) return TP_FIT_DEGENERATE;
    if (n > 16) n = 16;
    double mx = 0, my = 0;
    for (uint8_t i = 0; i < n; i++) {
        mx += got[i].x;
        my += got[i].y;
    }
    mx /= n;
    my /= n;
    double sxx = 0, sxy = 0, syy = 0;
    for (uint8_t i = 0; i < n; i++) {
        sxx += (got[i].x - mx) * (got[i].x - mx);
        sxy += (got[i].x - mx) * (got[i].y - my);
        syy += (got[i].y - my) * (got[i].y - my);
    }
    /* The taps must span a plane: at least ~40 px of spread across every direction. */
    const double det = sxx * syy - sxy * sxy, floor = (double)n * 40 * 40;
    if (det < floor * floor) return TP_FIT_DEGENERATE;
    int32_t tx[16], ty[16];
    for (uint8_t i = 0; i < n; i++) {
        tx[i] = want[i].x;
        ty[i] = want[i].y;
    }
    double kx[3], ky[3];
    fit_axis(got, tx, n, mx, my, sxx, sxy, syy, det, kx);
    fit_axis(got, ty, n, mx, my, sxx, sxy, syy, det, ky);
    const tp_cal_t c = {to_q16(kx[0]), to_q16(kx[1]), to_q16(kx[2]),
                        to_q16(ky[0]), to_q16(ky[1]), to_q16(ky[2])};
    const int32_t lo = TP_Q16 * (100 - TP_CAL_SCALE_PCT) / 100, hi = TP_Q16 * (100 + TP_CAL_SCALE_PCT) / 100;
    const int32_t shear = TP_Q16 * TP_CAL_SHEAR_PCT / 100;
    if (c.a < lo || c.a > hi || c.e < lo || c.e > hi || c.b > shear || c.b < -shear ||
        c.d > shear || c.d < -shear)
        return TP_FIT_WILD;
    /* Nowhere on the glass moved further than a real panel's error: its corners and centre. */
    static const int16_t P[5][2] = {{0, 0}, {TP_SIDE - 1, 0}, {0, TP_SIDE - 1},
                                    {TP_SIDE - 1, TP_SIDE - 1}, {TP_SIDE / 2, TP_SIDE / 2}};
    for (int i = 0; i < 5; i++) {
        int32_t x, y;
        apply_free(&c, P[i][0], P[i][1], &x, &y);
        if (dist2(x, y, P[i][0], P[i][1]) > TP_CAL_MAX_SHIFT * TP_CAL_MAX_SHIFT) return TP_FIT_WILD;
    }
    for (uint8_t i = 0; i < n; i++) {
        int32_t x, y;
        apply_free(&c, got[i].x, got[i].y, &x, &y);
        if (dist2(x, y, want[i].x, want[i].y) > TP_CAL_MAX_RESIDUAL * TP_CAL_MAX_RESIDUAL)
            return TP_FIT_SCATTERED;
    }
    *out = c;
    return TP_FIT_OK;
}

tp_ev_t tp_track(tp_track_t *t, const tp_report_t *r)
{
    if (r && r->kind == TP_DOWN) {
        int16_t x, y;
        tp_xform(t->xf, r->raw_a, r->raw_b, &x, &y);
        const bool was = t->down, moved = x != t->x || y != t->y;
        t->down = true;
        t->quiet = 0;
        t->x = x;
        t->y = y;
        return !was ? TP_EV_DOWN : moved ? TP_EV_MOVE : TP_EV_NONE;
    }
    if (!t->down) return TP_EV_NONE;
    if (r && r->kind == TP_UP) {
        t->down = false;
        return TP_EV_UP;
    }
    if (++t->quiet < TP_HOLD_POLLS) return TP_EV_NONE;
    t->down = false;
    return TP_EV_UP;
}

void tp_ring_push(tp_ring_t *q, const tp_sample_t *s)
{
    if (q->count) {
        tp_sample_t *last = &q->s[(q->head + q->count - 1) % TP_RING];
        if (s->ev == TP_EV_MOVE && last->ev == TP_EV_MOVE) {
            *last = *s;
            return;
        }
    }
    if (q->count == TP_RING) {
        q->head = (uint8_t)((q->head + 1) % TP_RING);
        q->count--;
        q->dropped++;
    }
    q->s[(q->head + q->count) % TP_RING] = *s;
    q->count++;
}

bool tp_ring_pop(tp_ring_t *q, tp_sample_t *out)
{
    if (!q->count) return false;
    *out = q->s[q->head];
    q->head = (uint8_t)((q->head + 1) % TP_RING);
    q->count--;
    return true;
}
