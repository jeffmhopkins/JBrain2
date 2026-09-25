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
