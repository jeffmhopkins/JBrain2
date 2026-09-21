#pragma once

#include <stdbool.h>

/* Open the touch controller. False means no panel answered; the caller carries on without it. */
bool touch_start(void);

/* True once per press, on the edge. Never true twice for one finger held down. */
bool touch_tapped(void);

/* Whether a finger is down as of the last `touch_tapped()` — the LEVEL, which the edge
   deliberately hides. Cached rather than re-read so a caller wanting both does not cost two
   I2C transactions per poll. Only the maintenance hold uses it; the product gesture is still
   the edge (§10.4p). */
bool touch_is_down(void);
