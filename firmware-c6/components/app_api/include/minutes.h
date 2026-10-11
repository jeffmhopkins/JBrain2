/* Minute-of-day arithmetic shared by the clock's night face and the notice quiet hours, which
 * the plan says are the same window. Header-only so a service and an app can both use it
 * without one depending on the other. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define MIN_PER_DAY 1440

/* [from, to): a window that starts later than it ends wraps past midnight (21:30-06:30).
 * from == to is empty, not all day: an unset window must never silence anything. */
static inline bool min_window_contains(uint16_t now, uint16_t from, uint16_t to)
{
    if (from == to) return false;
    return from < to ? (now >= from && now < to) : (now >= from || now < to);
}

static inline uint16_t min_add(uint16_t at, int32_t delta)
{
    int32_t v = ((int32_t)at + delta) % MIN_PER_DAY;
    return (uint16_t)(v < 0 ? v + MIN_PER_DAY : v);
}
