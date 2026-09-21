#pragma once

#include <stddef.h>

/* The six emotions as LID GEOMETRY — the C port of `frontend/src/pet/face.ts`, whose header
 * says outright that it exists to be this file: "the ESP32-S3 panel will run the same model in
 * C, so this file is the reference implementation". The numbers below are transcribed, not
 * re-derived, for the same reason `face.c` copies the mock's coordinates verbatim.
 *
 * Emotion is carried by lid geometry, whole-face motion and timing, NEVER by colour
 * (`docs/reference/DESIGN.md`). Colour is the tap toy; it must stay orthogonal to feeling.
 * Asymmetry between the two eyes is the entire signal for curious and silly.
 *
 * Pure C, no ESP headers, like `face.c` and `font.c` — so `firmware/host` can exercise it.
 */

/* One eye's geometry. `uy`/`ly` are lid coverage 0..1; `ua` is the upper lid's tilt in
   degrees; `lb` bends the lower lid into the Duchenne cheek-raise that makes "happy" read. */
typedef struct {
    float uy;
    float ua;
    float ly;
    float lb;
    float sx;
    float sy;
} eye_params_t;

typedef enum {
    FACE_HAPPY = 0,
    FACE_EXCITED,
    FACE_CURIOUS,
    FACE_SLEEPY,
    FACE_SILLY,
    FACE_SCARED,
    /* Not an emotion the server ships: the gag face. Four- and five-year-olds read a pratfall
       as funny when the character looks BEWILDERED, and as not funny when it looks pained or
       smug — so every gag resolves here and holds it. The hold is the punchline. */
    FACE_BEWILDERED,
    FACE_KEY_COUNT,
} face_key_t;

typedef struct {
    eye_params_t l;
    eye_params_t r;
    float face_sy;
    float face_ang;
    /* Transition-speed multiplier. The SAME shape at a different speed reads as a different
       feeling — the cheapest expressive channel there is, so it is part of the emotion. */
    float rate;
} face_params_t;

/* Resolve an emotion to drawable parameters. Mirroring is applied here rather than stored, so
   a symmetric emotion is one edit and an asymmetric one is explicit about both eyes. Out of
   range keys resolve to happy: a panel that draws nothing is worse than one that smiles. */
void emotion_resolve(face_key_t key, face_params_t *out);

/* Tween by halving: `cur += (target - cur) * min(1, 0.5*rate)`. ~90% of the way in 66 ms at
   50 fps with a natural ease-out, in one multiply — this is what makes a pose system read as
   an animation system, and it is cheap enough for this panel. */
float emotion_approach(float cur, float target, float rate);

/* Tween every field of a face, so the caller does not enumerate seventeen floats by hand. */
void emotion_approach_face(face_params_t *cur, const face_params_t *target, float rate);
