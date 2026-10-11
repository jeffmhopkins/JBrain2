#include "svc_audio.h"

talk_outcome_t talk_policy_end(uint32_t held_ms, bool send)
{
    if (!send) return TALK_CANCELLED;
    /* A blip is a KEY knocked, not a message: nothing that short says anything. */
    if (held_ms < TALK_MIN_MS) return TALK_TOO_SHORT;
    if (held_ms >= TALK_MAX_MS) return TALK_CAPPED;
    return TALK_SENT;
}
