#pragma once

#include <stdbool.h>
#include <stdint.h>

#define FACE_W 368
#define FACE_H 448

/* How many colours the tap cycles through (the shipped palette plus the robot default). */
int face_colour_count(void);

/* Render the robot at `colour` into `fb` (RGB565, already byte-swapped for the panel),
   shifted `bob` pixels down and `lean` pixels right. `fb` must hold FACE_W * FACE_H pixels.

   `bob` exists because the panel will not hold an UNCHANGING image — see the plan's §10.4s.
   It is not decoration and it must never be constant.

   `lean` is the robot sliding downhill as the panel is tilted, proportional to the sideways
   component of gravity, so the flip at the end has something leading up to it rather than
   being a jump cut. */
void face_draw(uint16_t *fb, int colour, int bob, int lean);
