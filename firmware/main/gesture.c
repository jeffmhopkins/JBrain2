#include "gesture.h"

#include <stddef.h>

/* Which action a given tap count selects, or GESTURE_NONE. */
static gesture_action_t action_for(int taps)
{
    if (taps == GESTURE_TAPS_REBOOT) return GESTURE_REBOOT;
    if (taps == GESTURE_TAPS_FORM) return GESTURE_FORM;
    if (taps == GESTURE_TAPS_CALIBRATE) return GESTURE_CALIBRATE;
    return GESTURE_NONE;
}

void gesture_reset(gesture_t *g)
{
    if (g == NULL) return;
    g->taps = 0;
    g->press_ms = 0;
    g->idle_ms = 0;
    g->held = 0;
}

gesture_action_t gesture_poll(gesture_t *g, bool tapped, bool down, int dt_ms)
{
    if (g == NULL) return GESTURE_NONE;

    if (tapped) {
        /* The rhythm is checked on the PRESS, against the gap since the last release. A late
           press is not a failure — it is the first tap of a new attempt. */
        if (g->taps > 0 && g->idle_ms > GESTURE_GAP_MS) g->taps = 0;
        g->press_ms = 0;
        g->held = 0;
    }

    if (down) {
        g->press_ms += dt_ms;
        /* A press that outlasts a tap BECOMES the hold, carrying whatever count preceded it.
           Deciding this on duration rather than on the press edge is what lets five taps
           exist at all: arming at the fourth press would clear the count before the fifth. */
        if (g->held == 0 && g->press_ms > GESTURE_TAP_MAX_MS) g->held = g->taps;
        if (g->held != 0 && g->press_ms >= GESTURE_HOLD_MS) {
            const gesture_action_t act = action_for(g->held);
            /* Consumed here so one hold cannot fire twice while the finger is still down. */
            gesture_reset(g);
            return act;
        }
        return GESTURE_NONE;
    }

    if (g->press_ms > 0) {
        /* The frame after a release. */
        if (g->press_ms <= GESTURE_TAP_MAX_MS) {
            if (g->taps < GESTURE_TAPS_MAX) g->taps++;
        } else {
            /* Let go of a hold — or just a slow press. Either way the whole sequence is
               abandoned, not just the hold: leaving the panel one press from acting, however
               long a child waits, is exactly the accident this exists to stop. */
            g->taps = 0;
        }
        g->press_ms = 0;
        g->held = 0;
        g->idle_ms = 0;
        return GESTURE_NONE;
    }

    g->idle_ms += dt_ms;
    if (g->taps > 0 && g->idle_ms > GESTURE_GAP_MS) g->taps = 0;
    return GESTURE_NONE;
}

float gesture_cue(const gesture_t *g)
{
    /* Only for a hold that will actually do something. A hold after four taps is a mistake,
       and growing a bar for it would promise an action that never comes. */
    if (g == NULL || action_for(g->held) == GESTURE_NONE) return 0.0f;
    if (g->press_ms < GESTURE_CUE_MS) return 0.0f;
    const float p = (float)g->press_ms / (float)GESTURE_HOLD_MS;
    return p > 1.0f ? 1.0f : p;
}
