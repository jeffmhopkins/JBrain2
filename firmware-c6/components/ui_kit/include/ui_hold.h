/* A finger held still: the one rule behind every long press on the panel — the conversation menu
 * (shell, 600 ms) and the Clock face's alarms list (2 s, owner 2026-10-12: "that needs to be a
 * long press, maybe 2 seconds or more"). Pure C, no LVGL, host-tested: each caller passes its own
 * threshold, so there is one copy of the rule, not one per screen. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* How far a finger may wander and still be "still": ~1.6 mm on this glass, the tap's own slop,
 * and LVGL's scroll start — so a scroll moves past it before it could ever be a hold. */
#define UI_HOLD_SLOP 20
/* A hold shows nothing for this long: a tap, and the start of every scroll, stay quiet. */
#define UI_HOLD_SHOW_MS 300

typedef struct {
    bool down, done;
    int x0, y0;
    uint32_t t0;
} ui_hold_t;

typedef enum {
    UI_HOLD_IDLE,    /* no finger, or a finger that is not (or no longer) a hold */
    UI_HOLD_GROWING, /* held still, under the threshold */
    UI_HOLD_FIRE,    /* crossed it on this sample: exactly once per stroke */
} ui_hold_state_t;

/* Every pointer sample. A finger that moves past UI_HOLD_SLOP is not a hold for the rest of its
 * stroke, even if it comes back; lifting starts over. */
ui_hold_state_t ui_hold_feed(ui_hold_t *h, bool pressed, int x, int y, uint32_t now_ms,
                             uint32_t need_ms);
/* The feedback ring's fill, 0-1000: 0 until UI_HOLD_SHOW_MS, then filling over the rest of the
 * threshold. 0 for a stroke that is not a hold. */
uint16_t ui_hold_permille(const ui_hold_t *h, uint32_t now_ms, uint32_t need_ms);
