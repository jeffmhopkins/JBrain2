#include "disp_logic.h"

#include <stdio.h>

static int32_t clip(int32_t v, int32_t max)
{
    return v < 0 ? 0 : v > max ? max : v;
}

void disp_area_even(int32_t *x1, int32_t *y1, int32_t *x2, int32_t *y2, int32_t w, int32_t h)
{
    /* Clearing bit 0 floors the start to even and setting it rounds the end up to odd, so
     * end - start + 1 is even. Clipping first keeps an off-screen edge from rounding into a
     * window the controller would wrap. */
    *x1 = clip(*x1, w - 1) & ~(int32_t)1;
    *y1 = clip(*y1, h - 1) & ~(int32_t)1;
    *x2 = clip(*x2, w - 1) | 1;
    *y2 = clip(*y2, h - 1) | 1;
}

uint8_t disp_level(uint8_t pct, bool dark)
{
    if (dark) return 0;
    if (pct > 100) pct = 100;
    return (uint8_t)((pct * 255u + 50u) / 100u);
}

void disp_rot_point(uint8_t rot, int32_t n, int32_t lx, int32_t ly, int32_t *px, int32_t *py)
{
    switch (rot & 3) {
    case 1: /* the right edge down: the viewer's down is the glass's +x, their right its -y */
        *px = ly;
        *py = n - 1 - lx;
        break;
    case 2:
        *px = n - 1 - lx;
        *py = n - 1 - ly;
        break;
    case 3: /* the left edge down: down is -x, right is +y */
        *px = n - 1 - ly;
        *py = lx;
        break;
    default:
        *px = lx;
        *py = ly;
        break;
    }
}

disp_win_t disp_rot_area(uint8_t rot, int32_t n, disp_win_t l)
{
    int32_t ax, ay, bx, by;
    disp_rot_point(rot, n, l.x1, l.y1, &ax, &ay);
    disp_rot_point(rot, n, l.x2, l.y2, &bx, &by);
    return (disp_win_t){ax < bx ? ax : bx, ay < by ? ay : by, ax < bx ? bx : ax, ay < by ? by : ay};
}

void disp_reverse(uint16_t *px, uint32_t count)
{
    for (uint32_t i = 0, j = count ? count - 1 : 0; i < j; i++, j--) {
        const uint16_t t = px[i];
        px[i] = px[j];
        px[j] = t;
    }
}

disp_win_t disp_rot_gather(uint8_t rot, int32_t n, disp_win_t l, const uint16_t *px,
                           uint16_t *out)
{
    const int32_t w = l.x2 - l.x1 + 1, h = l.y2 - l.y1 + 1;
    /* Logical (x, y) is the area's glass row w-1-x, column y at 90°, and row x, column h-1-y at
       270°: each of LVGL's rows is read in order and written down one glass column. */
    const bool cw = (rot & 3) == 1;
    const int32_t step = cw ? -h : h;
    for (int32_t y = 0; y < h; y++) {
        const uint16_t *row = px + y * w;
        uint16_t *o = out + (cw ? (w - 1) * h + y : h - 1 - y);
        for (int32_t x = 0; x < w; x++, o += step) *o = (uint16_t)(row[x] << 8 | row[x] >> 8);
    }
    return disp_rot_area(rot, n, l);
}

static uint32_t ms(uint64_t us)
{
    return (uint32_t)((us + 500) / 1000);
}

void disp_stat_add(disp_stat_t *s, uint32_t flush_us, uint32_t wait_us, uint32_t wall_us,
                   bool full)
{
    s->frames++;
    s->sum_us += flush_us;
    s->last_ms = ms(flush_us);
    if (s->last_ms > s->max_ms) s->max_ms = s->last_ms;
    if (!full) return;
    s->full++;
    s->full_sum_us += flush_us;
    s->full_wait_us += wait_us;
    s->full_ms = s->last_ms;
    s->full_wall_ms = ms(wall_us);
    if (s->full_ms > s->full_max_ms) s->full_max_ms = s->full_ms;
}

uint32_t disp_stat_avg_ms(const disp_stat_t *s)
{
    return s->frames ? ms(s->sum_us / s->frames) : 0;
}

uint32_t disp_stat_full_avg_ms(const disp_stat_t *s)
{
    return s->full ? ms(s->full_sum_us / s->full) : 0;
}

void disp_stat_fmt(char *out, size_t len, const disp_stat_t st[4])
{
    size_t at = 0;
    if (len) out[0] = '\0';
    for (int r = 0; r < 4 && at < len; r++) {
        if (!st[r].full) continue;
        const int n = snprintf(out + at, len - at, "%s%d:%lu,%lu,%lu,%lu,%lu", at ? " " : "",
                               r * 90, (unsigned long)disp_stat_full_avg_ms(&st[r]),
                               (unsigned long)st[r].full_max_ms, (unsigned long)st[r].full,
                               (unsigned long)st[r].full_wall_ms,
                               (unsigned long)ms(st[r].full_wait_us / st[r].full));
        if (n < 0) break;
        at += (size_t)n;
    }
}
