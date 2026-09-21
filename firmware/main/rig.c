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
};

const action_spec_t *rig_spec(action_t a)
{
    if ((unsigned)a >= (unsigned)ACT_COUNT) a = ACT_NONE;
    return &SPECS[a];
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
    if (a == ACT_NONE || (unsigned)a >= (unsigned)ACT_COUNT) return;

    switch (a) {
    case ACT_WAVE:
        out->arm_r = -150.0f + sinf(p * (float)M_PI * 6.0f) * 26.0f * mag;
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
