#pragma once

#include <stddef.h>
#include <stdint.h>

#include "emotion.h"

/* The body rig: limb poses per action and the whole-figure transform. The C port of
 * `frontend/src/pet/rig.ts`, which is "in the same figure-space the panel will use, so the
 * firmware port is a transcription rather than a redesign".
 *
 * The owner chose a SMALL BODY over eyes-only (`docs/reference/DESIGN.md`), and the reasoning
 * decides what belongs here: the emotions never needed a body — `emotion.c` carries those —
 * but the GAGS do. Wave, jump and above all peekaboo need arms.
 *
 * WHAT THIS PORT LEAVES OUT, AND WHY. The web rig rotates the whole figure (`ang`). Rotating
 * a 368x448 framebuffer per frame is a per-pixel resample this panel should not spend 25 times
 * a second, and source-space rotation tears holes in filled shapes. So `ang` becomes a HEAD
 * TILT — the head and eyes offset horizontally against the torso — which is the cue curious
 * and silly actually need, and `spin` is left out of the pools rather than faked badly.
 */

typedef enum {
    ACT_NONE = 0,
    ACT_WIGGLE,
    ACT_GIGGLE,
    ACT_BOING,
    ACT_BLUSH,
    ACT_SNEEZE,
    ACT_HICCUP,
    ACT_NOD,
    ACT_JUMP,
    ACT_WAVE,
    ACT_DANCE,
    ACT_BOP,
    ACT_SHIMMY,
    ACT_SLEEP,
    ACT_HIDE,
    ACT_FART,
    ACT_BURP,
    ACT_EAT,
    ACT_KICK,
    ACT_SPIN,
    ACT_COUNT,
} action_t;

typedef struct {
    int dur_ms;
    face_key_t face;
    int gag;
} action_spec_t;

/* The table, for the caller that needs a duration or the face an action wears. */
const action_spec_t *rig_spec(action_t a);

/* Limb angles in degrees, 0 = hanging straight down, negative swings forward/up.
   `hands_up` 0..1 drives peekaboo, where the hands are aimed at the EYES rather than posed by
   angle — which is the whole difference between hiding and squatting. */
typedef struct {
    float arm_l;
    float arm_r;
    float leg_l;
    float leg_r;
    float hands_up;
    /* BIRD CHANNELS. The ostrich has no arms, so every action's energy had to be read off the
       arm angles — and an action that moves both arms the same way (dance, wiggle) moved the
       bird not at all. A bird's expressive parts are the neck, the head, the tail and the
       legs, so they get channels of their own. The robot ignores all five; they cost this
       struct 20 bytes and remove the need for a parallel pose type. */
    float neck;  /* degrees the neck leans, + = toward the beak side */
    float bob;   /* figure-space px the head rides, + = down */
    float tail;  /* degrees of tail flap, on top of the arm-driven swing */
    float step;  /* + lifts the left leg, - the right; magnitude is the lift */
    float crest; /* degrees the plumes trail — follow-through, never authored per action */
} rig_pose_t;

/* Whole-figure transform. `extra` is a one-shot the renderer draws on top. */
typedef enum { EXTRA_NONE = 0, EXTRA_PUFF, EXTRA_BLUSH } extra_t;

typedef struct {
    float ox;
    float oy;
    float sx;
    float sy;
    float tilt; /* degrees, drawn as a head offset — see the file header */
    /* WHICH WAY THE FIGURE IS FACING: 1 front, -1 turned away. The file header explains why
       this rig has no rotation — resampling 165,000 pixels 25 times a second is not a thing
       this panel can afford, and source-space rotation tears holes in filled shapes. So a
       SPIN is the 2D trick instead: squash the figure horizontally to nearly nothing and back
       while flipping this, and the renderer drops the eyes and the mouth for the half-turn
       the figure is facing away. A silhouette with no face on it reads as a back, and it
       costs one multiply on a scale the renderer already applies. */
    float facing;
    extra_t extra;
} figure_pose_t;

/* Limb pose for `a` at progress `p` (0..1), scaled by `mag` (the repetition penalty), with
   `t_ms` driving the idle sway that runs underneath everything: a character that is ever
   perfectly still reads as dead. */
void rig_for(action_t a, float p, float mag, uint32_t t_ms, rig_pose_t *out);

/* The whole-figure transform. Comic actions come from squashing and offsetting the WHOLE
   character rather than from new artwork, which is why this panel can afford them. */
void rig_figure(action_t a, float p, float mag, uint32_t t_ms, float face_tilt,
                figure_pose_t *out);
