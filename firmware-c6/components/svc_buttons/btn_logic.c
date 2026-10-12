#include "btn_logic.h"

int btn_db_step(btn_db_t *b, bool pressed_now)
{
    if (pressed_now == b->pressed) {
        b->run = 0;
        return 0;
    }
    if (++b->run < BTN_DB_SAMPLES) return 0;
    b->run = 0;
    b->pressed = pressed_now;
    return pressed_now ? 1 : -1;
}

btn_hold_ev_t btn_hold_step(btn_hold_t *h, int edge, uint32_t now_ms)
{
    if (edge > 0) {
        *h = (btn_hold_t){.down = true, .fired = false, .since = now_ms};
        return BTN_HOLD_NONE;
    }
    if (edge < 0) {
        const bool was_short = h->down && !h->fired;
        h->down = false;
        return was_short ? BTN_HOLD_SHORT : BTN_HOLD_NONE;
    }
    if (h->down && !h->fired && (uint32_t)(now_ms - h->since) >= BTN_LONG_MS) {
        h->fired = true;
        return BTN_HOLD_LONG;
    }
    return BTN_HOLD_NONE;
}

bool pwr_merge_accept(pwr_merge_t *m, pwr_src_t src, uint32_t now_ms)
{
    m->seen |= (uint8_t)src;
    /* Unsigned subtraction stays right across the 49-day wrap. */
    if (m->any && (uint32_t)(now_ms - m->last_ms) < PWR_MERGE_MS) return false;
    m->any = true;
    m->last_ms = now_ms;
    return true;
}

const char *pwr_seen_name(uint8_t seen)
{
    static const char *const NAMES[] = {"none", "gpio18", "pek", "pek+gpio18"};
    return NAMES[seen & 3];
}
