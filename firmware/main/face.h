#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "emotion.h"
#include "rig.h"
#include "game.h"

#define FACE_W 368
#define FACE_H 448

/* THE CASE HIDES THE CORNERS, AND FOR TWO WAVES ONLY THE PWA KNEW IT.
 *
 * §10.4c, from the first photograph of the hardware: "the enclosure hides its corners —
 * anything drawn there is invisible to whoever is holding it." `frontend/src/pet/scale.ts`
 * acted on it the same day (`CASE_CORNER_FRACTION = 0.13`, so 48 px of a 368 px width) and
 * clips the preview to the case shape. THE FIRMWARE NEVER DID. It has been drawing into the
 * full 368x448 rectangle ever since, which is the same fault the preview was fixed for —
 * "it shows pixels that cannot be seen, and invites a design that loses content."
 *
 * What it cost: the microphone-open indicator sat at x=10 on the caption row, about 50 px
 * from the corner's centre of curvature against a radius of 48, so the one mark on this panel
 * that is a promise to a room rather than a decoration was BEHIND THE CASE. Nobody could have
 * noticed from a screenshot — the framebuffer is perfectly correct — and it took a quarter
 * turn, which moves a corner of the square into the middle of an edge of the glass, for the
 * owner to see a dot that had been there all along.
 *
 * Kept in sync with `CASE_CORNER_FRACTION` by hand; the two live in different languages and
 * neither can import the other. 0.13 * 368 = 47.8. */
#define FACE_CASE_CORNER_R 48

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

/* True when (x, y) is inside the case opening of a w x h panel — see `FACE_CASE_CORNER_R`.
   Drawn is not the same as visible, and the difference is a 48 px radius at each corner. */
bool face_inside_case(int x, int y, int w, int h);

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
    /* Degrees the HEAD turns back toward true up while the panel is tilted, + = clockwise on
       the glass. The body slides downhill (`lean`); the head rights itself, as a bird's does
       on a slope. The owner: *"when we angled the head can kind of self-align to point up a
       little more."* `display.c` sets it from the accelerometer, partial and eased. */
    float upright;
    /* THE POOP ON THE FLOOR (`poop.h`), drawn behind him: `poop_x` in figure units from the
       floor's centre, `poop_size` 0 (none) .. 1, `poop_t` seconds since it landed, which
       moves its stink lines. */
    food_t food; /* what the meal is (`food_pick`); the bites come out of it the same way */
    float poop_x;
    float poop_size;
    float poop_t;
    int poop_kind; /* which of the eight (`poop_look`) */
    /* THE GAME (`game.h`), when one is on: it places and sizes the figure and draws over it.
       NULL, or a game that is off, is the pet as usual. `game_ms` is the game's clock. */
    const game_t *game;
    uint32_t game_ms;
    float open;     /* eyelids: 1 fully open, 0 shut. Blink, not emotion. */
    float startle;  /* 0 calm, 1 wide-eyed. The poke recoil, on top of whatever face is worn. */
    /* 0 shut, 1 fully open — the mouth while the pet is SPEAKING. The owner: "we should make
       an animation of the robot talking as it talks."
     *
       A channel rather than an action, because it has to ride on top of whatever the figure is
       already doing: a reply can arrive mid-wave, and a talking pet that stops waving to talk
       reads as two pets. `display.c` drives it from `audio_playing()`. */
    float talk;
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

/* The game's field (`game.h`): its half extents in pixels, for `game_step`; and where the
   figure stands now in field pixels, given his lean, for `game_start` to walk him from and
   back to. Both follow `face_set_fit`'s orientation. */
void face_game_field(float *hw, float *hh);
void face_game_home(int lean, float *x, float *y);

/* A rest state: happy, open-eyed, idle limbs, no figure transform. The caller starts here and
   tweens away from it, so nothing has to enumerate seventeen floats to get a first frame. */
void face_rest(face_state_t *st);

/* The two primitives most of a frame is made of, public so `host/tests.c` can hold each against
   the per-pixel loop it replaced (0.4.4): a filled disc, and a limb (a round-capped stroke of
   `len` px at `deg` with a ball on the end). Same pixels, written as one run per row. */
void face_fill_circle(uint16_t *fb, int cx, int cy, int r, uint16_t c);
void face_draw_limb(uint16_t *fb, int x, int y, float deg, int len, int w, uint16_t c);
