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

/* Where the last press landed, in panel pixels. Captured on the edge and held, so a caller
   that asks after the finger has lifted still gets the point that was pressed.

   THE ORIENTATION IS NOT ASSUMED. The CST820 reports in its own frame, and whether that
   matches the display's is exactly the sort of thing §10.4af spent three releases getting
   wrong about the accelerometer by reasoning instead of measuring. So the raw values go out
   in telemetry and a marker is drawn where the firmware THINKS the finger was — if the dot
   is not under the finger, the mapping is wrong and the numbers say how. */
void touch_point(int *x, int *y);
