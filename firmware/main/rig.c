#include "rig.h"

#include <math.h>

static const action_spec_t SPECS[ACT_COUNT] = {
    [ACT_NONE] = {0, FACE_HAPPY, 0},
    [ACT_WIGGLE] = {1100, FACE_HAPPY, 0},
    [ACT_GIGGLE] = {1300, FACE_HAPPY, 0},
    [ACT_BOING] = {900, FACE_EXCITED, 0},
    [ACT_BLUSH] = {1600, FACE_HAPPY, 0},
    [ACT_SNEEZE] = {1200, FACE_BEWILDERED, 0},
    [ACT_HICCUP] = {900, FACE_BEWILDERED, 0},
    [ACT_NOD] = {900, FACE_HAPPY, 0},
    [ACT_JUMP] = {900, FACE_EXCITED, 0},
    [ACT_WAVE] = {1400, FACE_HAPPY, 0},
    [ACT_DANCE] = {2600, FACE_EXCITED, 0},
    [ACT_BOP] = {2200, FACE_EXCITED, 0},
    [ACT_SHIMMY] = {2400, FACE_EXCITED, 0},
    [ACT_SLEEP] = {2000, FACE_SLEEPY, 0},
    [ACT_HIDE] = {2400, FACE_SILLY, 0},
    [ACT_FART] = {1500, FACE_BEWILDERED, 1},
    [ACT_BURP] = {1400, FACE_BEWILDERED, 1},
    [ACT_EAT] = {1600, FACE_HAPPY, 0},
    [ACT_KICK] = {900, FACE_EXCITED, 0},
    [ACT_SPIN] = {1200, FACE_EXCITED, 0},
};

const action_spec_t *rig_spec(action_t a)
{
    if ((unsigned)a >= (unsigned)ACT_COUNT) a = ACT_NONE;
    return &SPECS[a];
}

/* THE BIRD CHANNELS. Split out of the limb switch because the two disagree about what an
   action is: dance, bop and shimmy share one arm pose (they are three names for one robot
   animation), but on a bird they are three different dances — a sweep, a pump and a shimmy —
   and there is no reason the form that has no arms should inherit the form that does.

   Everything here is authored EXCEPT `crest`, which is the same curve evaluated a moment ago:
   light things trail heavy ones, and computing the lag rather than hand-keying it means every
   action gets follow-through for free, including the ones added later. */
#define BIRD_LAG 0.06f

static void bird_channels(action_t a, float p, float mag, uint32_t t_ms, rig_pose_t *out)
{
    /* Idle drift, on periods that do not divide into each other so the bird never repeats a
       pose exactly. A bird standing perfectly still reads as taxidermy. */
    const float t = (float)t_ms;
    out->neck = sinf(t / 1700.0f) * 4.0f;
    out->bob = sinf(t / 1100.0f) * 3.0f;
    out->tail = sinf(t / 2300.0f) * 5.0f;
    out->step = 0.0f;
    out->crest = 0.0f;
    if (a == ACT_NONE || (unsigned)a >= (unsigned)ACT_COUNT) return;

    switch (a) {
    case ACT_WIGGLE: {
        const float q = sinf(p * (float)M_PI * 9.0f) * mag;
        out->neck += q * 26.0f;
        out->tail += q * 16.0f;
        out->step += q * 9.0f;
        break;
    }
    case ACT_GIGGLE: {
        const float q = sinf(p * (float)M_PI * 7.0f) * mag;
        out->bob -= fabsf(q) * 14.0f;
        out->neck += q * 12.0f;
        out->tail += q * 20.0f;
        break;
    }
    case ACT_BOING:
        /* The neck IS the boing on this form — it is the only part long enough to stretch. */
        out->bob -= sinf(p * (float)M_PI * 3.0f) * 20.0f * mag;
        break;
    case ACT_NOD: {
        const float q = sinf(p * (float)M_PI * 5.0f) * mag;
        out->neck += q * 30.0f;
        out->bob += q * 8.0f;
        break;
    }
    case ACT_JUMP: {
        const float q = sinf(p * (float)M_PI);
        out->neck -= q * 18.0f * mag; /* head thrown back on the way up */
        out->bob -= q * 14.0f * mag;
        break;
    }
    case ACT_WAVE:
        /* A bird cannot wave an arm, so it waves the neck — which is why this action scored
           zero rendered pixels on the ostrich for three versions. */
        out->neck += sinf(p * (float)M_PI * 3.0f) * 34.0f * mag;
        out->bob -= fabsf(sinf(p * (float)M_PI * 3.0f)) * 8.0f * mag;
        break;
    case ACT_DANCE: {
        const float q = sinf(p * (float)M_PI * 6.0f) * mag;
        out->neck += q * 22.0f;
        out->tail += q * 18.0f;
        out->step += q * 26.0f; /* big alternating strides */
        break;
    }
    case ACT_BOP: {
        const float q = sinf(p * (float)M_PI * 8.0f) * mag;
        out->bob += q * 16.0f; /* the head pumps on the beat */
        out->neck += sinf(p * (float)M_PI * 4.0f) * 10.0f * mag;
        out->step += q * 8.0f;
        break;
    }
    case ACT_SHIMMY: {
        const float q = sinf(p * (float)M_PI * 14.0f) * mag;
        out->tail += q * 26.0f; /* fast tail, slow feet: that is a shimmy */
        out->neck -= q * 10.0f;
        out->step += sinf(p * (float)M_PI * 7.0f) * 14.0f * mag;
        break;
    }
    case ACT_SNEEZE:
    case ACT_HICCUP:
        if (p < 0.35f) {
            out->neck -= 20.0f * mag; /* rear back */
        } else if (p < 0.55f) {
            out->neck += 42.0f * mag; /* and snap forward */
            out->bob += 10.0f * mag;
        }
        break;
    case ACT_BLUSH: {
        const float q = sinf(p * (float)M_PI) * mag;
        out->neck -= q * 12.0f; /* turns away */
        out->bob += q * 6.0f;
        break;
    }
    case ACT_SLEEP: {
        const float q = fminf(1.0f, p * 3.0f);
        out->neck += q * 18.0f;
        out->bob += q * 22.0f; /* the neck folds and the head sinks */
        break;
    }
    case ACT_HIDE:
        out->bob += 8.0f * (p < 0.72f ? 1.0f : 0.0f); /* tucked under the wing */
        break;
    case ACT_FART:
        if (p > 0.12f && p < 0.32f) {
            out->neck -= 26.0f * mag;
            out->bob -= 10.0f * mag;
        }
        break;
    case ACT_BURP:
        if (p > 0.12f && p < 0.6f) out->neck += 30.0f * mag;
        break;
    case ACT_EAT: {
        /* A BIRD EATS BY PECKING THE GROUND, which is the single most recognisable thing an
           ostrich does and costs nothing this rig did not already have: the neck goes down
           and forward, three times, and the head follows it. */
        const float q = fabsf(sinf(p * (float)M_PI * 3.0f)) * mag;
        out->bob += q * 78.0f;
        out->neck += q * 30.0f;
        out->tail -= q * 14.0f; /* the tail comes up as the head goes down */
        break;
    }
    case ACT_KICK: {
        const float q = fabsf(sinf(p * (float)M_PI * 2.0f)) * mag;
        out->step -= q * 34.0f; /* the RIGHT leg, matching the limb pose */
        out->neck -= q * 14.0f; /* head back for balance */
        out->tail += q * 20.0f;
        break;
    }
    case ACT_SPIN:
        out->neck += sinf(p * (float)M_PI * 4.0f) * 10.0f * mag;
        break;
    default:
        break;
    }
}

void rig_for(action_t a, float p, float mag, uint32_t t_ms, rig_pose_t *out)
{
    if (out == NULL) return;
    const float sway = sinf((float)t_ms / 1400.0f) * 4.0f;
    out->arm_l = 12.0f + sway;
    out->arm_r = -12.0f - sway;
    out->leg_l = 4.0f;
    out->leg_r = -4.0f;
    out->hands_up = 0.0f;
    bird_channels(a, p, mag, t_ms, out);
    {
        /* Follow-through: the plumes are the lightest thing on the bird, so they show where
           the head WAS. One extra evaluation of the same curve, no state to keep. */
        rig_pose_t lag;
        bird_channels(a, p > BIRD_LAG ? p - BIRD_LAG : 0.0f,
                      mag, t_ms > 60u ? t_ms - 60u : 0u, &lag);
        out->crest = (lag.neck - out->neck) * 1.6f;
        out->tail += (lag.neck - out->neck) * 0.8f;
    }
    if (a == ACT_NONE || (unsigned)a >= (unsigned)ACT_COUNT) return;

    switch (a) {
    case ACT_WAVE:
        /* Arms are drawn BEHIND the head, whose 216 px width swallows a shoulder sitting at
           ox+66 — so the old -150 put the hand at x+103 against a head reaching x+108 and the
           wave was invisible, a third the rendered delta of any other action on either form.
           `face.c` now redraws an arm posed above the shoulder over the head; this swing is
           kept wholly past that threshold so the arm cannot flick in front and behind. */
        out->arm_r = -130.0f + sinf(p * (float)M_PI * 6.0f) * 24.0f * mag;
        break;
    case ACT_DANCE:
    case ACT_BOP:
    case ACT_SHIMMY: {
        const float q = sinf(p * (float)M_PI * 6.0f) * mag;
        out->arm_l = 40.0f + q * 55.0f;
        out->arm_r = -40.0f + q * 55.0f;
        out->leg_l = 10.0f + q * 10.0f;
        out->leg_r = -10.0f + q * 10.0f;
        break;
    }
    case ACT_JUMP:
    case ACT_BOING: {
        const float q = sinf(p * (float)M_PI);
        out->arm_l = 120.0f * q;
        out->arm_r = -120.0f * q;
        out->leg_l = 30.0f * q;
        out->leg_r = -30.0f * q;
        break;
    }
    case ACT_WIGGLE:
    case ACT_GIGGLE: {
        const float q = sinf(p * (float)M_PI * 9.0f) * mag;
        out->arm_l = 25.0f + q * 30.0f;
        out->arm_r = -25.0f + q * 30.0f;
        break;
    }
    case ACT_SLEEP:
        out->arm_l = 6.0f;
        out->arm_r = -6.0f;
        out->leg_l = 2.0f;
        out->leg_r = -2.0f;
        break;
    case ACT_FART:
        if (p > 0.12f && p < 0.72f) {
            out->arm_l = 95.0f;
            out->arm_r = -95.0f;
            out->leg_l = 26.0f;
        }
        break;
    case ACT_BURP:
        if (p > 0.12f && p < 0.6f) out->arm_r = -160.0f; /* hand to the mouth */
        break;
    case ACT_EAT: {
        /* THE ROBOT EATS WITH A HAND; THE BIRD PECKS (`bird_channels`). Same action, two
           anatomies, which is the whole reason the bird has channels of its own. Three trips
           to the mouth: one reads as a mistake, three read as a meal. */
        const float q = sinf(p * (float)M_PI * 6.0f);
        out->arm_r = -120.0f - 45.0f * fabsf(q) * mag;
        out->arm_l = 20.0f;
        break;
    }
    case ACT_KICK: {
        /* One leg, twice, and hard. Negative `leg_r` swings the foot out to the right — the
           limb's x offset is -sin(deg) — so this is a kick rather than a squat. The other
           leg stiffens to plant, or the whole figure reads as falling over. */
        const float q = fabsf(sinf(p * (float)M_PI * 2.0f)) * mag;
        out->leg_r = -4.0f - 66.0f * q;
        out->leg_l = 10.0f;
        out->arm_l = 30.0f + 40.0f * q; /* arms counterbalance, as a kicking child's do */
        out->arm_r = -30.0f - 20.0f * q;
        break;
    }
    case ACT_SPIN:
        /* The limbs stay near rest: the spin is carried entirely by the figure transform, and
           a limb swinging through a squash that is already near zero width just flickers. */
        out->arm_l = 30.0f;
        out->arm_r = -30.0f;
        break;
    case ACT_SNEEZE:
    case ACT_HICCUP:
        if (p > 0.35f && p < 0.6f) {
            out->arm_l = 140.0f;
            out->arm_r = -140.0f;
        }
        break;
    case ACT_HIDE:
        /* PEEKABOO. With hands, hide can actually hide — which is squarely on target for a
           four-year-old, and is the difference between this and squatting in a corner. */
        out->hands_up = p < 0.18f ? p / 0.18f
                        : p < 0.72f ? 1.0f
                                    : (1.0f - (p - 0.72f) / 0.28f);
        if (out->hands_up < 0.0f) out->hands_up = 0.0f;
        out->arm_l = 168.0f;
        out->arm_r = -168.0f;
        break;
    default:
        break;
    }
}

void rig_figure(action_t a, float p, float mag, uint32_t t_ms, float face_tilt,
                figure_pose_t *out)
{
    if (out == NULL) return;
    const float breathe = sinf((float)t_ms / 1400.0f) * 0.018f + 1.0f;
    out->ox = 0.0f;
    out->oy = 0.0f;
    out->sx = 1.0f;
    out->sy = 1.0f;
    out->tilt = face_tilt;
    out->facing = 1.0f;
    out->extra = EXTRA_NONE;

    if (a != ACT_NONE && (unsigned)a < (unsigned)ACT_COUNT) {
        switch (a) {
        case ACT_WIGGLE:
            out->tilt += sinf(p * (float)M_PI * 8.0f) * 11.0f * mag;
            break;
        case ACT_GIGGLE:
            out->oy += fabsf(sinf(p * (float)M_PI * 7.0f)) * -13.0f * mag;
            out->tilt += sinf(p * (float)M_PI * 9.0f) * 6.0f * mag;
            break;
        case ACT_BOING: {
            const float q = sinf(p * (float)M_PI * 3.0f);
            out->sy += q * 0.18f * mag;
            out->sx -= q * 0.11f * mag;
            out->oy -= fabsf(q) * 18.0f * mag;
            break;
        }
        case ACT_BLUSH:
            out->extra = EXTRA_BLUSH;
            break;
        case ACT_DANCE:
        case ACT_BOP:
        case ACT_SHIMMY:
            out->ox += sinf(p * (float)M_PI * 6.0f) * 22.0f * mag;
            out->tilt += sinf(p * (float)M_PI * 6.0f) * 9.0f * mag;
            break;
        case ACT_JUMP:
            /* The lift is applied OUTSIDE the figure scale, so keep it modest or the antenna
               leaves the top of a 448 px panel at the peak. */
            out->oy -= fabsf(sinf(p * (float)M_PI)) * 32.0f * mag;
            break;
        case ACT_NOD:
            out->oy += sinf(p * (float)M_PI * 5.0f) * 12.0f * mag;
            break;
        case ACT_EAT:
            /* Down to the food and back, under the pecking. */
            out->oy += fabsf(sinf(p * (float)M_PI * 3.0f)) * 14.0f * mag;
            break;
        case ACT_KICK: {
            const float q = fabsf(sinf(p * (float)M_PI * 2.0f)) * mag;
            out->ox -= q * 14.0f; /* the body shifts away from the kicking leg */
            out->tilt -= q * 9.0f;
            break;
        }
        case ACT_SPIN: {
            /* TWO TURNS, AS A HORIZONTAL SQUASH — see `facing` in rig.h for why this rig does
               not rotate. Never all the way to zero: a figure one pixel wide is a gap in the
               middle of the screen, not a character edge-on. */
            const float a = p * (float)M_PI * 4.0f;
            const float c = cosf(a);
            float w = fabsf(c);
            if (w < 0.08f) w = 0.08f;
            out->sx *= 1.0f - (1.0f - w) * mag;
            out->facing = c >= 0.0f ? 1.0f : -1.0f;
            break;
        }
        case ACT_SNEEZE:
        case ACT_HICCUP:
            if (p < 0.35f) {
                out->sx += 0.05f;
                out->sy -= 0.04f;
            } else if (p < 0.5f) {
                out->sx -= 0.17f;
                out->sy += 0.21f;
                out->oy -= 12.0f;
            }
            break;
        case ACT_HIDE:
            out->sy -= fminf(0.16f, p * 0.4f);
            out->oy += fminf(26.0f, p * 70.0f);
            break;
        case ACT_SLEEP:
            out->sy -= fminf(0.1f, p * 0.26f);
            out->oy += fminf(16.0f, p * 36.0f);
            break;
        default:
            break;
        }
        if (SPECS[a].gag) {
            /* THE GAG SKELETON: anticipation -> two to four frames of act -> LONG HOLD ->
               settle. The hold is the joke; the act is only the setup, so it is short and the
               hold is half the duration. Getting this ratio wrong is the difference between a
               pratfall and a twitch. */
            if (p < 0.12f) {
                out->sx += 0.05f;
                out->sy -= 0.04f;
            } else if (p < 0.22f) {
                out->sx -= 0.22f * mag;
                out->sy += 0.26f * mag;
                out->oy -= 9.0f;
                out->extra = EXTRA_PUFF;
            } else if (p < 0.72f) {
                out->extra = EXTRA_PUFF;
            }
        }
    }
    /* Breathing runs under everything, always, including the idle pose. */
    out->sx *= breathe;
    out->sy *= 2.0f - breathe;
}
