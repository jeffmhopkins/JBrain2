#pragma once

#include <stdbool.h>

/* Bring up the AMOLED and draw the test pattern. NEVER fails fatally: a display fault must
   not stop this unit reaching the box, because reaching the box is how the fault gets
   fixed. Returns whether the panel came up; the caller logs it and carries on either way. */
bool display_start(void);

/* Redraw the test pattern, alternating it so a running panel is distinguishable from a
   frozen one by eye. Returns whether the SPI writes succeeded — which is NOT the same
   question as whether anything appeared. See display.c. */
bool display_repaint(void);
