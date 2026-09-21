#pragma once

#include <stdbool.h>

/* THE REBOOT GESTURE: three short taps, then hold.
 *
 * The hold alone used to be the whole gesture, guarded by its length: five seconds, chosen
 * because 4-5 year olds were measured producing ORDINARY taps lasting up to 4.2 s, so five was
 * the first threshold outside a child's accidental press. A 0.8 s margin against a determined
 * four-year-old is not much, and both units are going to the twins.
 *
 * So the length is no longer doing the work — a RHYTHM is. Three deliberate taps in time, then
 * a hold. Two children mashing a panel will produce plenty of taps and plenty of holds; what
 * they will not produce is three short taps, each within half a second of the last, followed
 * immediately by a sustained press.
 *
 * BOTH FAILURE DIRECTIONS COST SOMETHING, which is why this is its own file with its own
 * tests. A false positive reboots a toy in a child's hands. A false negative strands an owner
 * who has no terminal (CLAUDE.md #10) with no way to force a firmware re-check.
 *
 * Pure C, no ESP headers: the caller supplies the edges and the frame interval.
 */

/* How many short taps arm the hold. */
#define GESTURE_TAPS 3
/* The rhythm. Each press must begin within this long of the previous release, or the count
   starts over — this is the owner's "within half a second of each other". */
#define GESTURE_GAP_MS 500
/* What makes a tap SHORT. Well under the 4.2 s an ordinary child's press can last, so a slow
   press cannot be mistaken for part of the sequence, and comfortably long for an adult doing
   it on purpose. */
#define GESTURE_TAP_MAX_MS 600
/* The hold, unchanged. The prefix is now the guard, so this is no longer the only thing
   standing between a curious four-year-old and a reboot. */
#define GESTURE_HOLD_MS 5000
/* From here the amber bar grows, full width at the moment it reboots, in time to let go. */
#define GESTURE_CUE_MS 1500

typedef struct {
    int taps;     /* short taps counted so far, 0..GESTURE_TAPS */
    int press_ms; /* how long the current press has lasted; 0 when nothing is down */
    int idle_ms;  /* since the last release */
    bool armed;   /* the current press is the hold, not a tap */
} gesture_t;

void gesture_reset(gesture_t *g);

/* Advance by one poll. `tapped` is the press EDGE, `down` the level, `dt_ms` the interval.
   Returns true on the frame the hold completes — once, and the caller should act on it. */
bool gesture_poll(gesture_t *g, bool tapped, bool down, int dt_ms);

/* 0..1 across the hold, for the cue bar. Zero unless the hold is armed and past the cue
   threshold, so an ordinary press never draws anything. */
float gesture_cue(const gesture_t *g);
