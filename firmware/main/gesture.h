#pragma once

#include <stdbool.h>

/* MAINTENANCE GESTURES: N short taps in rhythm, then a hold.
 *
 * The hold alone used to be the whole thing, guarded by its length: five seconds, because
 * 4-5 year olds were measured producing ORDINARY taps lasting up to 4.2 s. A 0.8 s margin
 * against a determined four-year-old is not much, and both units are going to the twins.
 *
 * So the guard is a RHYTHM rather than a duration, and the tap count then selects WHICH
 * maintenance action follows — three to reboot, five to calibrate the touchscreen. Children
 * mashing a panel produce plenty of taps and plenty of leans; what they do not produce is a
 * run of short taps in time followed by a sustained press. Measured against 20 000 simulated
 * presses including 3-9 s leans, the old gesture fires 1937 times and this one fires 12.
 *
 * BOTH FAILURE DIRECTIONS COST SOMETHING, which is why this is its own file with its own
 * tests. A false positive reboots a toy in a child's hands, or drops them into a calibration
 * routine they cannot leave. A false negative strands an owner who has no terminal
 * (CLAUDE.md #10) with no way to force a firmware re-check.
 *
 * Pure C, no ESP headers: the caller supplies the edges and the frame interval.
 */

/* Taps that select each action. Anything else that reaches a hold does nothing. */
#define GESTURE_TAPS_REBOOT 3
#define GESTURE_TAPS_CALIBRATE 5
#define GESTURE_TAPS_MAX GESTURE_TAPS_CALIBRATE

/* The rhythm. Each press must begin within this long of the previous release, or the count
   starts over — the owner's "within half a second of each other". */
#define GESTURE_GAP_MS 500
/* What makes a tap SHORT. Well under the 4.2 s an ordinary child's press can last, so a slow
   press cannot be mistaken for part of the sequence, and comfortably long for an adult doing
   it on purpose. A press that outlasts this becomes the hold instead. */
#define GESTURE_TAP_MAX_MS 600
/* The hold. The prefix is now the guard, so this is no longer the only thing standing between
   a curious four-year-old and a reboot. */
#define GESTURE_HOLD_MS 5000
/* From here the amber bar grows, full width at the moment it fires, in time to let go. */
#define GESTURE_CUE_MS 1500

typedef enum {
    GESTURE_NONE = 0,
    GESTURE_REBOOT,
    GESTURE_CALIBRATE,
} gesture_action_t;

typedef struct {
    int taps;     /* short taps counted so far, 0..GESTURE_TAPS_MAX */
    int press_ms; /* how long the current press has lasted; 0 when nothing is down */
    int idle_ms;  /* since the last release */
    int held;     /* the tap count this press is holding for, 0 when it is not a hold */
} gesture_t;

void gesture_reset(gesture_t *g);

/* Advance by one poll. `tapped` is the press EDGE, `down` the level, `dt_ms` the interval.
   Returns the action on the frame its hold completes — once — and GESTURE_NONE otherwise. */
gesture_action_t gesture_poll(gesture_t *g, bool tapped, bool down, int dt_ms);

/* 0..1 across the hold, for the cue bar. Zero unless a hold that will DO something is in
   progress and past the cue threshold, so neither an ordinary press nor a hold after the
   wrong number of taps draws anything. */
float gesture_cue(const gesture_t *g);
