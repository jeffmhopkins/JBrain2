#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "emotion.h"
#include "rig.h"

#define FACE_W 368
#define FACE_H 448

/* How many colours the tap cycles through (the shipped palette plus the robot default). */
int face_colour_count(void);

/* Everything the rig can move, in one struct rather than a growing argument list.
 *
 * The caller owns the tweening and the clock; this file only knows how to draw ONE INSTANT of
 * it. That is what keeps `face.c` free of ESP dependencies and renderable on a host. */
typedef struct {
    int bob;        /* vertical offset, px — see the note below; never constant */
    int lean;       /* horizontal offset, px: he slides downhill as the panel tilts */
    float open;     /* eyelids: 1 fully open, 0 shut. Blink, not emotion. */
    float startle;  /* 0 calm, 1 wide-eyed. The poke recoil, on top of whatever face is worn. */
    face_params_t eyes; /* the emotion, as lid geometry — already tweened by the caller */
    rig_pose_t rig;     /* limb angles for this instant */
    figure_pose_t fig;  /* whole-figure offset, squash and head tilt */
} face_state_t;

/* Render the robot at `colour` into `fb` (RGB565, already byte-swapped for the panel).
   `fb` must hold FACE_W * FACE_H pixels.

   `bob` exists because the panel will not hold an UNCHANGING image — see the plan's §10.4s.
   It is not decoration and it must never be constant.

   `lean` is the robot sliding downhill as the panel is tilted, proportional to the sideways
   component of gravity, so the flip at the end has something leading up to it rather than
   being a jump cut. */
void face_draw(uint16_t *fb, int colour, const face_state_t *st);

/* A rest state: happy, open-eyed, idle limbs, no figure transform. The caller starts here and
   tweens away from it, so nothing has to enumerate seventeen floats to get a first frame. */
void face_rest(face_state_t *st);
