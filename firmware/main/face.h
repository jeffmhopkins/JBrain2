#pragma once

#include <stdbool.h>
#include <stdint.h>

#define FACE_W 368
#define FACE_H 448

/* How many colours the tap cycles through (the shipped palette plus the robot default). */
int face_colour_count(void);

/* Everything the rig can move, in one struct rather than a growing argument list.
 *
 * This is the start of W4's ~17 tweened floats. The caller owns the tweening; this file only
 * knows how to draw one instant of it, which is what keeps `face.c` free of ESP dependencies
 * and testable on a host. */
typedef struct {
    int bob;        /* vertical offset, px — see the note below; never constant */
    int lean;       /* horizontal offset, px: he slides downhill as the panel tilts */
    int dip;        /* extra downward px, the recoil from a poke */
    float open;     /* eyelids: 1 fully open, 0 shut */
    float startle;  /* 0 calm, 1 wide-eyed */
} face_state_t;

/* Render the robot at `colour` into `fb` (RGB565, already byte-swapped for the panel).
   `fb` must hold FACE_W * FACE_H pixels.

   `bob` exists because the panel will not hold an UNCHANGING image — see the plan's §10.4s.
   It is not decoration and it must never be constant.

   `lean` is the robot sliding downhill as the panel is tilted, proportional to the sideways
   component of gravity, so the flip at the end has something leading up to it rather than
   being a jump cut. */
void face_draw(uint16_t *fb, int colour, const face_state_t *st);
