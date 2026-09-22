#pragma once

/* WHICH WAY UP, WITH A BAND YOU HAVE TO MEAN TO CROSS.
 *
 * The owner: *"the tilt going to 90 and causing an orientation change shouldn't happen right
 * at 45. We should have like an extra 20 you should have to go in order to cause the
 * orientation change, and then another 20 back past that 45 to go back the other way."*
 *
 * THE OLD TEST HAD NO HYSTERESIS AT THE BOUNDARY AT ALL, which is the bug and is easy to miss
 * because it *looks* like it does. `FLIP_THRESHOLD` gates how much gravity has to be in the XY
 * plane before the reading is trusted — a panel lying flat has almost none and must not flip on
 * noise. But WHICH quarter you get came from `|ax| > |ay|`, and that comparison turns over at
 * exactly 45 degrees. Hold a panel at 45 and the two axes are equal, so a millivolt of
 * accelerometer noise picks the orientation, over and over, several times a second.
 *
 * So the quarter is chosen from the ANGLE of gravity in the XY plane, and the current quarter
 * keeps it until the angle is more than 45 + `ORIENT_HYST_DEG` away from that quarter's own
 * centre. Turning from upright toward landscape, the flip lands at 65 degrees; coming back, you
 * are then 65 degrees from the landscape centre at 25 degrees from upright. A 40 degree band
 * either side of the boundary, which is exactly what was asked for.
 *
 * Pure C, no ESP headers, because `display.c` cannot be linked by the host suite and an
 * orientation rule that is only reasoned about is how the first flip shipped backwards.
 */

/* How far PAST the 45 degree boundary the panel must turn before the orientation follows. */
#define ORIENT_HYST_DEG 20

/* Below this much gravity in the XY plane the panel is lying flat and the angle means nothing,
   so the orientation is held. About half a gravity, the figure the two-way flip already used. */
#define ORIENT_MIN_MAG 4000

/* 0 upright, 1 clockwise, 2 upside down, 3 anticlockwise — the quarter to draw in, given the
   one currently drawn and a raw accelerometer reading. Returns `quarter` unchanged whenever the
   reading is too flat to trust or has not left the band. */
int orient_quarter(int quarter, int ax, int ay);
