#include "tickle.h"

#include <string.h>

void tickle_reset(tickle_t *t)
{
    memset(t, 0, sizeof(*t));
}

void tickle_arm(tickle_t *t, uint32_t now)
{
    t->armed = true;
    t->until = now + TICKLE_WAIT_MS;
    t->pokes = 0;
}

bool tickle_armed(const tickle_t *t, uint32_t now)
{
    return t->armed && (int32_t)(t->until - now) > 0;
}

tickle_poke_t tickle_poke(tickle_t *t, uint32_t now)
{
    if (!tickle_armed(t, now)) {
        t->armed = false;
        return TICKLE_NO;
    }
    if (++t->pokes >= TICKLE_POKES) {
        t->armed = false;
        return TICKLE_BIG;
    }
    t->until = now + TICKLE_WAIT_MS;
    return TICKLE_SMALL;
}
