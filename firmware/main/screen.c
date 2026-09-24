#include "screen.h"

#include <stdlib.h>

screen_stage_t screen_stage(uint32_t idle_ms)
{
    if (idle_ms >= SCREEN_DARK_MS) return SCREEN_DARK;
    if (idle_ms >= SCREEN_DIM_MS) return SCREEN_DIM;
    return SCREEN_AWAKE;
}

uint8_t screen_level(uint8_t configured, screen_stage_t stage)
{
    if (stage == SCREEN_DARK) return 0;
    if (stage != SCREEN_DIM) return configured;
    const uint8_t dim = (uint8_t)(configured >> SCREEN_DIM_SHIFT);
    if (dim >= SCREEN_DIM_FLOOR) return dim;
    /* A box that asked for a very dim screen gets that screen back rather than a BRIGHTER
       one: the floor is there to stop a quarter rounding to zero, not to raise anything. */
    return configured < SCREEN_DIM_FLOOR ? configured : SCREEN_DIM_FLOOR;
}

int screen_motion(const int16_t prev[3], const int16_t cur[3])
{
    int d = 0;
    for (int i = 0; i < 3; i++) d += abs((int)cur[i] - (int)prev[i]);
    return d;
}

bool screen_moved(int magnitude)
{
    return magnitude >= SCREEN_MOVE_COUNTS;
}

bool screen_dozing(screen_stage_t stage, bool conversing, bool speaking)
{
    return stage == SCREEN_DIM && !conversing && !speaking;
}
