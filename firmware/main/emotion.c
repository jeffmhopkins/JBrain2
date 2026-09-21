#include "emotion.h"

/* Values follow pycozmo's shipped `Expressive Eyes` set, mapped onto our six, exactly as
   `frontend/src/pet/face.ts` does. MIRROR there is `r == NULL` here. */
typedef struct {
    eye_params_t l;
    eye_params_t r;
    int mirror;
    float face_sy;
    float face_ang;
    float rate;
} spec_t;

static const spec_t SPECS[FACE_KEY_COUNT] = {
    [FACE_HAPPY] = {{0.0f, 0.0f, 0.40f, 0.40f, 1.00f, 1.00f}, {0}, 1, 1.00f, 0.0f, 1.0f},
    [FACE_EXCITED] = {{0.0f, 0.0f, 0.30f, 0.20f, 1.10f, 1.15f}, {0}, 1, 1.05f, 0.0f, 1.7f},
    /* Asymmetric on purpose: one eye half-lidded, one wide, plus a head tilt. Cozmo encodes
       confusion and skepticism the same way, and it costs nothing. */
    [FACE_CURIOUS] = {{0.30f, -10.0f, 0.20f, 0.20f, 1.00f, 1.00f},
                      {0.05f, 12.0f, 0.0f, 0.0f, 1.08f, 1.08f},
                      0, 1.00f, -7.0f, 0.8f},
    [FACE_SLEEPY] = {{0.48f, 4.0f, 0.45f, 0.0f, 1.00f, 0.90f}, {0}, 1, 0.95f, 0.0f, 0.35f},
    [FACE_SILLY] = {{0.10f, 0.0f, 0.10f, 0.30f, 1.25f, 1.25f},
                    {0.42f, -14.0f, 0.05f, 0.0f, 0.80f, 0.75f},
                    0, 1.00f, 9.0f, 1.5f},
    [FACE_SCARED] = {{0.0f, 26.0f, 0.35f, 0.10f, 1.20f, 1.30f}, {0}, 1, 1.02f, 0.0f, 1.9f},
    [FACE_BEWILDERED] = {{0.05f, -16.0f, 0.0f, 0.0f, 1.30f, 1.35f},
                         {0.30f, 18.0f, 0.15f, 0.15f, 0.95f, 1.10f},
                         0, 1.00f, -11.0f, 1.4f},
};

void emotion_resolve(face_key_t key, face_params_t *out)
{
    if (out == NULL) return;
    if ((unsigned)key >= (unsigned)FACE_KEY_COUNT) key = FACE_HAPPY;
    const spec_t *s = &SPECS[key];
    out->l = s->l;
    if (s->mirror) {
        out->r = s->l;
        /* Only the lid ANGLE mirrors. Flipping coverage too would undo the asymmetry that is
           the whole point of the two hand-written eyes. */
        out->r.ua = -s->l.ua;
    } else {
        out->r = s->r;
    }
    out->face_sy = s->face_sy;
    out->face_ang = s->face_ang;
    out->rate = s->rate;
}

float emotion_approach(float cur, float target, float rate)
{
    float k = 0.5f * rate;
    if (k > 1.0f) k = 1.0f;
    if (k < 0.0f) k = 0.0f;
    return cur + (target - cur) * k;
}

static void approach_eye(eye_params_t *c, const eye_params_t *t, float rate)
{
    c->uy = emotion_approach(c->uy, t->uy, rate);
    c->ua = emotion_approach(c->ua, t->ua, rate);
    c->ly = emotion_approach(c->ly, t->ly, rate);
    c->lb = emotion_approach(c->lb, t->lb, rate);
    c->sx = emotion_approach(c->sx, t->sx, rate);
    c->sy = emotion_approach(c->sy, t->sy, rate);
}

void emotion_approach_face(face_params_t *cur, const face_params_t *target, float rate)
{
    if (cur == NULL || target == NULL) return;
    approach_eye(&cur->l, &target->l, rate);
    approach_eye(&cur->r, &target->r, rate);
    cur->face_sy = emotion_approach(cur->face_sy, target->face_sy, rate);
    cur->face_ang = emotion_approach(cur->face_ang, target->face_ang, rate);
    /* The rate itself is NOT tweened: it governs the tween, and easing it would make a fast
       emotion arrive slowly on the way in and a slow one arrive fast. */
    cur->rate = target->rate;
}
