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
