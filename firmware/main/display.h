#pragma once

#include <stdbool.h>

/* Bring up the AMOLED and draw the test pattern. NEVER fails fatally: a display fault must
   not stop this unit reaching the box, because reaching the box is how the fault gets
   fixed. Returns whether the panel came up; the caller logs it and carries on either way. */
bool display_start(void);
