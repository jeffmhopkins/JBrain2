#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "emotion.h"
#include "rig.h"

#define FACE_W 368
#define FACE_H 448

/* HOW FAR THE FIGURE MAY SLIDE, and the two numbers differ because the composition does.
 *
 * Both are measured, not chosen: render every form at rest and walk the lean until the
 * bounding box touches an edge. Portrait clips past 70, so 60 keeps a margin. Side-mounted the
 * figure is scaled by 368/448, which narrows it by a sixth and moves the wall out — it clips
 * past 110, and 110 is what the owner asked for: *"in landscape he should be able to tilt and
 * slide all over to the right and I'll put it to the left, not restrained as much."* Nearly
 * double the travel for the same tilt, because the same tilt has nearly double the room.
 *
 * Here rather than in `display.c` so the host harness can pin them against the actual drawn
 * geometry. A lean constant that is only a firmware `#define` is a number nothing checks. */
#define FACE_LEAN_MAX 60
#define FACE_LEAN_MAX_SIDE 110

/* How many colours the tap cycles through (the shipped palette plus the robot default). */
int face_colour_count(void);

/* Which body is drawn. The rig, the emotions and the tweening are shared: a form decides
   the SHAPES, never the behaviour, which is what stops a second body from becoming a second
   animation system.

   The ostrich is the default because that is what the twins asked for. `docs/mocks/
   room-endpoint/ostrich-mock.py` is its spec, at true geometry, drawn with these same
   primitives so the port is a transcription. */
typedef enum {
    FORM_OSTRICH = 0,
    FORM_ROBOT,
    FORM_COUNT,
} face_form_t;

/* Everything the rig can move, in one struct rather than a growing argument list.
 *
 * The caller owns the tweening and the clock; this file only knows how to draw ONE INSTANT of
 * it. That is what keeps `face.c` free of ESP dependencies and renderable on a host. */
typedef struct {
    face_form_t form; /* which body; the rest of this struct means the same thing for both */
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

/* Which part of the robot a panel coordinate lands on.
 *
 * The zones are the figure's own geometry, so they move with him: `lean` slides everything
 * downhill as the panel tilts, and `upside_down` is the 180 degree flip `flip_frame` performs
 * — a tap on his head while he is inverted is still his head, and getting that wrong would
 * make the zones feel random exactly when a child is holding the panel any which way.
 *
 * Transient action offsets are deliberately NOT applied: a hitbox that jumps with him during
 * a jump is one a child cannot learn. The rest silhouette is the target.
 *
 * PER FORM, because the shapes are: an ostrich's head is high and small and its legs are most
 * of its height, so the robot's boxes would put "head" over empty space and "leg" over a
 * body. A form whose zones were not updated would answer every poke from the wrong pool. */
typedef enum {
    ZONE_NONE = 0, /* off the figure entirely — the background */
    ZONE_HEAD,
    ZONE_BODY,
    ZONE_ARM,
    ZONE_LEG,
} face_zone_t;

face_zone_t face_zone(face_form_t form, int x, int y, bool upside_down, int lean);

/* Scale the whole figure and move its vertical origin, for a panel mounted on its side —
   see the comment on `s_fit` in `face.c`. `origin_y` below zero keeps the portrait default. */
void face_set_fit(float scale, int origin_y);

/* A rest state: happy, open-eyed, idle limbs, no figure transform. The caller starts here and
   tweens away from it, so nothing has to enumerate seventeen floats to get a first frame. */
void face_rest(face_state_t *st);
