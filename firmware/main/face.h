#pragma once

#include <stdbool.h>
#include <stdint.h>

#define FACE_W 368
#define FACE_H 448

/* How many colours the tap cycles through (the shipped palette plus the robot default). */
int face_colour_count(void);

/* Render the robot at `colour` into `fb` (RGB565, already byte-swapped for the panel).
   `fb` must hold FACE_W * FACE_H pixels. */
void face_draw(uint16_t *fb, int colour);
