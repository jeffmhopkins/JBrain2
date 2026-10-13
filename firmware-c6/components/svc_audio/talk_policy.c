#include <stdio.h>

#include "svc_audio.h"
#include "talk_core.h"

talk_outcome_t talk_policy_end(uint32_t held_ms, bool send)
{
    if (!send) return TALK_CANCELLED;
    /* A blip is a KEY knocked, not a message: nothing that short says anything. */
    if (held_ms < TALK_MIN_MS) return TALK_TOO_SHORT;
    if (held_ms >= TALK_MAX_MS) return TALK_CAPPED;
    return TALK_SENT;
}

void talk_clock(uint32_t ms, char *out, size_t cap)
{
    const uint32_t sec = ms / 1000;
    snprintf(out, cap, "%lu:%02lu", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
}

talk_route_t talk_key_route(const char *conversation)
{
    tk_agent_t a;
    if (conversation && tk_agent_from(conversation, &a))
        return (talk_route_t){TALK_TO_AGENT, tk_agent_id(a)};
    return (talk_route_t){TALK_TO_NEW_AGENT, tk_agent_id(TK_AGENT_JERV)};
}
