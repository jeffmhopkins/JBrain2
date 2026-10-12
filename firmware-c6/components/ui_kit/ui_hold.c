#include "ui_hold.h"

ui_hold_state_t ui_hold_feed(ui_hold_t *h, bool pressed, int x, int y, uint32_t now_ms,
                             uint32_t need_ms)
{
    if (!pressed) {
        h->down = false;
        return UI_HOLD_IDLE;
    }
    if (!h->down) {
        *h = (ui_hold_t){.down = true, .x0 = x, .y0 = y, .t0 = now_ms};
        return need_ms ? UI_HOLD_GROWING : UI_HOLD_IDLE;
    }
    if (h->done) return UI_HOLD_IDLE;
    const int dx = x - h->x0, dy = y - h->y0;
    if ((dx < 0 ? -dx : dx) >= UI_HOLD_SLOP || (dy < 0 ? -dy : dy) >= UI_HOLD_SLOP) {
        /* A scroll or a swipe: this stroke is never a hold, even if it comes back. */
        h->done = true;
        return UI_HOLD_IDLE;
    }
    if ((uint32_t)(now_ms - h->t0) < need_ms) return UI_HOLD_GROWING;
    h->done = true;
    return UI_HOLD_FIRE;
}

uint16_t ui_hold_permille(const ui_hold_t *h, uint32_t now_ms, uint32_t need_ms)
{
    if (!h->down || h->done || need_ms <= UI_HOLD_SHOW_MS) return 0;
    const uint32_t held = now_ms - h->t0;
    if (held <= UI_HOLD_SHOW_MS) return 0;
    const uint32_t p = (held - UI_HOLD_SHOW_MS) * 1000u / (need_ms - UI_HOLD_SHOW_MS);
    return (uint16_t)(p > 1000 ? 1000 : p);
}
