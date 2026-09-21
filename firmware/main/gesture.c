#include "gesture.h"

#include <stddef.h>

void gesture_reset(gesture_t *g)
{
    if (g == NULL) return;
    g->taps = 0;
    g->press_ms = 0;
    g->idle_ms = 0;
    g->armed = false;
}

bool gesture_poll(gesture_t *g, bool tapped, bool down, int dt_ms)
{
    if (g == NULL) return false;

    if (tapped) {
        /* The rhythm is checked on the PRESS, against the gap since the last release. A late
           press is not a failure — it is the first tap of a new attempt. */
        if (g->taps > 0 && g->idle_ms > GESTURE_GAP_MS) g->taps = 0;
        if (g->taps >= GESTURE_TAPS) g->armed = true;
        g->press_ms = 0;
    }

    if (down) {
        g->press_ms += dt_ms;
        if (g->armed && g->press_ms >= GESTURE_HOLD_MS) {
            /* Consumed here so one hold cannot fire twice while the finger is still down. */
            gesture_reset(g);
            return true;
        }
        return false;
    }

    if (g->press_ms > 0) {
        /* The frame after a release. */
        if (g->armed) {
            /* Let go before the end: the whole sequence is abandoned, not just the hold. A
               half-finished reboot gesture must not leave the panel one press from rebooting
               however long the child waits. */
            g->armed = false;
            g->taps = 0;
        } else if (g->press_ms <= GESTURE_TAP_MAX_MS) {
            if (g->taps < GESTURE_TAPS) g->taps++;
        } else {
            g->taps = 0;
        }
        g->press_ms = 0;
        g->idle_ms = 0;
        return false;
    }

    g->idle_ms += dt_ms;
    if (g->taps > 0 && g->idle_ms > GESTURE_GAP_MS) g->taps = 0;
    return false;
}

float gesture_cue(const gesture_t *g)
{
    if (g == NULL || !g->armed || g->press_ms < GESTURE_CUE_MS) return 0.0f;
    const float p = (float)g->press_ms / (float)GESTURE_HOLD_MS;
    return p > 1.0f ? 1.0f : p;
}
