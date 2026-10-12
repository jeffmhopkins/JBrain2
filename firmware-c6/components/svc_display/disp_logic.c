#include "disp_logic.h"

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
