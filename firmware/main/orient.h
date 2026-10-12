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

/* ON ITS SIDE the figure renders into a SQUARE of the portrait frame — `ORIENT_SQ` wide, at
   rows `ORIENT_SQ_Y0`.. — which a quarter turn maps onto itself (`display.c`'s
   `blit_frame_rotated`). The glass is 448 long, so the square has 40 px of band either side,
   and it SLIDES into them with the lean, `win` panel rows from centre: the owner's *"his
   feathers are cutting off before we even get into the left extent of the screen."* */
#define ORIENT_SQ 368
#define ORIENT_SQ_Y0 40
#define ORIENT_WIN_MAX 40

/* The board axes in viewer terms for `quarter`: `tilt` is the component along the viewer's
   RIGHT (positive = right side down, so the figure slides right), `down` the component along
   gravity's own axis. Each quarter turn walks the same circle; see `update_orientation`. */
void orient_axes(int quarter, int ax, int ay, int *tilt, int *down);

/* Where the square sits for a figure leaned `lean` of `lean_max`: signed panel rows, EVEN (the
   controller takes windows on even rows), zero unless on its side. Viewer-right is panel -Y
   clockwise and +Y anticlockwise. */
int orient_window(int quarter, int lean, int lean_max);

/* Panel (px, py) to the portrait frame, for the square at `win`. The ONE mapping: the rotated
   blit reads its source through it, and a touch is mapped back through it, so the two cannot
   disagree. Upright and upside down are the identity (`flip_frame` and the panel's own turn
   cancel). */
void orient_panel_to_frame(int quarter, int win, int px, int py, int *fx, int *fy);

/* The degrees the head turns back toward true up (+ = clockwise on the glass): `share` of the
   angle the panel is turned, clamped to `max_deg`. Tilting right side down turns the glass
   clockwise, so true up appears turned ANTICLOCKWISE on it — hence the sign. */
float orient_upright(int tilt, int down, float share, float max_deg);
/* Partial, because a head that stays perfectly level while the body tilts under it reads as a
   gyroscope, not a bird; past 45 the panel is about to turn a quarter and `orient_quarter`
   takes over. */
#define ORIENT_UPRIGHT_SHARE 0.6f
#define ORIENT_UPRIGHT_MAX_DEG 45.0f
