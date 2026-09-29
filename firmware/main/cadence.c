#include "cadence.h"

uint32_t cadence_slice_ms(uint32_t remaining_ms, uint32_t period_ms)
{
    /* No short cadence asked for: one sleep, the whole remainder. */
    if (period_ms == 0) return remaining_ms;
    /* THE TAIL IS SHORT ON PURPOSE. Returning a full slice here would overshoot the period and
       push the long work late — fifteen minutes plus up to one slice, every cycle, forever. */
    if (remaining_ms < period_ms) return remaining_ms;
    return period_ms;
}

bool cadence_retry_due(uint32_t now_ms, uint32_t last_ms, uint32_t backoff_ms)
{
    if (last_ms == 0) return true;
    /* Unsigned arithmetic, so this stays correct across a rollover of `now_ms`. */
    return (uint32_t)(now_ms - last_ms) >= backoff_ms;
}

uint32_t cadence_backoff_ms(uint32_t base_ms, int fails, uint32_t cap_ms)
{
    if (fails <= 0) return base_ms;
    uint32_t ms = base_ms;
    for (int i = 0; i < fails; i++) {
        /* Stop before the shift rather than after it: doubling past the cap is the step that
           could overflow, and the answer is the cap either way. */
        if (ms >= cap_ms || ms > (uint32_t)0x7FFFFFFF) return cap_ms;
        ms *= 2;
    }
    return ms > cap_ms ? cap_ms : ms;
}
