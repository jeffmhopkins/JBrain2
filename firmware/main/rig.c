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
    /* A MEAL, NOT A NIBBLE (0.4.10, owner: *"the eat animation can probably be better with
       multiple bites being taken and kind of a longer animation and sound effect too"*): bites
       of a cookie, a crunch each, chewing between. Three of them, 0.96 s apart (0.4.11, owner:
       *"a little too long. Have it only three bites"* — five was 4.8 s). */
    [ACT_EAT] = {960 * EAT_BITES, FACE_HAPPY, 0},
    [ACT_KICK] = {900, FACE_EXCITED, 0},
    [ACT_SPIN] = {1200, FACE_EXCITED, 0},
    /* TWICE AS LONG AS 0.4.7's (owner, 0.4.10: *"the dizziness after being shaken. It should
       last twice as long. Go ahead and make the love animation twice as long also"*). Their
       motions are written against progress, so each doubles its cycle count with them: the
       head shakes and the hearts rise at the speed they did, for longer. */
    [ACT_DIZZY] = {6400, FACE_BEWILDERED, 0},
    [ACT_LOVE] = {5600, FACE_HAPPY, 0},
    [ACT_TICKLE] = {4800, FACE_HAPPY, 0},
    [ACT_POOP] = {2600, FACE_SILLY, 0},
    [ACT_BRACE] = {1600, FACE_EXCITED, 0},
    [ACT_TICKLED] = {1100, FACE_HAPPY, 0},
    [ACT_MEGAFART] = {7000, FACE_BEWILDERED, 0},
};

/* SITTING DOWN: in over the first eighth, held, up over the last. Shared by the legs and the
   drop so the feet stay on the floor while the body comes down onto them. */
#define DIZZY_SPLAY 72.0f

static float sit_amount(float p)
{
    /* The same 0.38 s down and 0.48 s up as at 3.2 s: he plonks, he does not settle. */
    if (p < 0.06f) return p / 0.06f;
    if (p > 0.925f) return (1.0f - p) / 0.075f;
    return 1.0f;
}

/* THE HEAD SHAKE of a dizzy pet: wide and fast at first, dying away — the shake of someone
   clearing their head, not a no. */
static float dizzy_shake(float p)
{
    return sinf(p * (float)M_PI * 22.0f) * (1.0f - p) * (1.0f - p);
}

/* THE TICKLE'S INTENSITY: up fast, held while he laughs, and dying away over the last quarter as
   he gets his breath back. Everything it drives is a fast oscillation times this. */
static float tickle_env(float p)
{
    if (p < 0.08f) return p / 0.08f;
    if (p > 0.75f) return (1.0f - p) / 0.25f;
    return 1.0f;
}

/* ONE BITE of the meal, `w` 0..1 through it: up to the mouth by a quarter, held while it
   bites, back down by 0.6, then chewing. The bite itself lands at BITE_AT. */
#define BITE_AT 0.25f

static float eat_reach(float w)
{
    if (w < BITE_AT) {
        const float t = w / BITE_AT;
        return t * t * (3.0f - 2.0f * t);
    }
    if (w < 0.4f) return 1.0f;
    if (w < 0.6f) return 1.0f - (w - 0.4f) / 0.2f;
    return 0.0f;
}

static float eat_chew(float w)
{
    if (w > 0.12f && w < BITE_AT) return (w - 0.12f) / (BITE_AT - 0.12f); /* opening wide */
    if (w >= 0.42f) return fabsf(sinf((w - 0.42f) / 0.58f * (float)M_PI * 3.0f)) * 0.45f;
    return 0.0f;
}

bool rig_cue_on_start(action_t a)
{
    return a != ACT_EAT && a != ACT_POOP;
}

food_t food_pick(food_t prev, uint32_t rnd)
{
    const unsigned p = (unsigned)prev < (unsigned)FOOD_COUNT ? (unsigned)prev : 0u;
    return (food_t)((p + 1u + rnd % (FOOD_COUNT - 1u)) % FOOD_COUNT);
}

bool rig_holds_pokes(action_t a)
{
    /* The fit is the payoff for five pokes; the seven seconds are the joke. */
    return a == ACT_TICKLE || a == ACT_MEGAFART;
}

static const action_t TICKLED_OUT[] = {ACT_TICKLE, ACT_FART, ACT_DIZZY, ACT_NONE};

const action_t *rig_run_tickled_out(void)
{
    return TICKLED_OUT;
}

action_t rig_run_next(const action_t **run, action_t ended)
{
    if (*run == NULL || **run != ended || ended == ACT_NONE) {
        *run = NULL;
        return ACT_NONE;
    }
    (*run)++;
    if (**run == ACT_NONE) *run = NULL;
    return *run != NULL ? **run : ACT_NONE;
}

bool rig_recue_continuous(action_t a, float p)
{
    /* Not in the last moment: a piece started there would run on after he has finished. */
    return a == ACT_MEGAFART && p < 0.93f;
}

action_t rig_fart_pick(uint32_t rnd)
{
    return rnd % 10u == 0u ? ACT_MEGAFART : ACT_FART;
}

/* THE MEGA FART's effort: up over the first moment, held, let go at the very end. */
static float mega_amount(float p)
{
    if (p < 0.06f) return p / 0.06f;
    if (p > 0.92f) return (1.0f - p) / 0.08f;
    return 1.0f;
}

/* THE SQUAT: down over the first fifth, held while he strains and through the plop, up after.
   The legs splay by SQUAT_SPLAY and `face.c` lowers him by what that takes off the legs. */
#define SQUAT_SPLAY 40.0f

static float squat_amount(float p)
{
    if (p < 0.2f) return p / 0.2f;
    if (p > 0.78f) return (1.0f - p) / 0.22f;
    return 1.0f;
}

/* Straining: a fast shiver building towards the plop, gone after it. */
static float strain(float p)
{
    if (p < 0.2f || p >= POOP_AT) return 0.0f;
    return sinf(p * (float)M_PI * 44.0f) * (p - 0.2f) / (POOP_AT - 0.2f);
}

int rig_eat_bites(float p, float *since)
{
    const float b = p * (float)EAT_BITES - BITE_AT;
    if (b < 0.0f) {
        if (since != NULL) *since = -1.0f;
        return 0;
    }
    const int n = (int)b + 1;
    if (since != NULL) *since = b - floorf(b);
    return n > EAT_BITES ? EAT_BITES : n;
}

bool rig_recue(action_t a, float p0, float p)
{
    /* The tickle: two more laughs after the first, 1.6 s apart, the last well before he winds
       down. The meal: a crunch on every bite, the first included (`rig_cue_on_start`). */
    static const float TICKLE[] = {0.33f, 0.62f};
    if (a == ACT_TICKLE) {
        for (unsigned i = 0; i < sizeof(TICKLE) / sizeof(TICKLE[0]); i++) {
            if (p0 < TICKLE[i] && p >= TICKLE[i]) return true;
        }
    } else if (a == ACT_EAT) {
        for (int k = 0; k < EAT_BITES; k++) {
            const float at = ((float)k + BITE_AT) / (float)EAT_BITES;
            if (p0 < at && p >= at) return true;
        }
    } else if (a == ACT_POOP) {
        return p0 < POOP_AT && p >= POOP_AT; /* the plop, not the squat */
    }
    return false;
}

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
        /* A BIRD EATS BY PECKING, which is the single most recognisable thing an ostrich does:
           the neck goes down and forward to the cookie on its wing, once a bite, and the head
           follows it. */
        const float w = p * (float)EAT_BITES - floorf(p * (float)EAT_BITES);
        const float q = eat_reach(w) * mag;
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
    case ACT_DIZZY:
        /* The head droops and swings on the neck; the tail sags. */
        out->neck += dizzy_shake(p) * 34.0f * mag;
        out->bob += sit_amount(p) * 10.0f;
        out->tail -= sit_amount(p) * 12.0f;
        break;
    case ACT_LOVE: {
        /* A slow happy sway, the head nuzzling side to side, tail going. */
        const float q = sinf(p * (float)M_PI * 8.0f) * mag;
        out->neck += q * 14.0f;
        out->bob -= fabsf(q) * 6.0f;
        out->tail += sinf(p * (float)M_PI * 20.0f) * 14.0f * mag;
        break;
    }
    case ACT_MEGAFART: {
        /* Head back, tail up and shaking with it, the whole bird rumbling. */
        const float k = mega_amount(p) * mag;
        out->neck -= 18.0f * k + sinf(p * (float)M_PI * 90.0f) * 4.0f * k;
        out->tail += 30.0f * k + sinf(p * (float)M_PI * 120.0f) * 10.0f * k;
        out->bob += sinf(p * (float)M_PI * 70.0f) * 3.0f * k;
        break;
    }
    case ACT_BRACE:
        /* UH-OH: head pulled down into the shoulders, a nervous shiver, tail tucked. */
        out->bob += squat_amount(p) * 10.0f;
        out->neck += sinf(p * (float)M_PI * 28.0f) * 5.0f * mag;
        out->tail -= squat_amount(p) * 10.0f;
        break;
    case ACT_POOP:
        /* Head down and straining, tail up out of the way; a shake of relief after. */
        out->neck += strain(p) * 6.0f * mag;
        if (p > POOP_AT) out->neck += sinf(p * (float)M_PI * 12.0f) * 8.0f * mag;
        out->bob += squat_amount(p) * 8.0f;
        out->tail += squat_amount(p) * 22.0f;
        break;
    case ACT_TICKLED: {
        /* ONE TICKLE: the fit's squirm, short and at `mag` — which rises tickle by tickle, so
           the four build to the fifth rather than repeating. */
        const float k = tickle_env(p) * mag;
        out->neck += sinf(p * (float)M_PI * 14.0f) * 16.0f * k;
        out->bob -= fabsf(sinf(p * (float)M_PI * 12.0f)) * 10.0f * k;
        out->tail += sinf(p * (float)M_PI * 18.0f) * 24.0f * k;
        break;
    }
    case ACT_TICKLE: {
        /* HELPLESS LAUGHTER: the head tosses, he bounces with every "ha", the tail wags flat
           out and the feet kick. Each channel at its own rate so it never settles into one
           rhythm, which is what makes it squirming rather than dancing. */
        const float k = tickle_env(p) * mag;
        out->neck += sinf(p * (float)M_PI * 30.0f) * 18.0f * k;
        out->bob -= fabsf(sinf(p * (float)M_PI * 26.0f)) * 12.0f * k;
        out->tail += sinf(p * (float)M_PI * 38.0f) * 26.0f * k;
        out->step += sinf(p * (float)M_PI * 18.0f) * 16.0f * k; /* in time with the kicks */
        break;
    }
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
           anatomies, which is the whole reason the bird has channels of its own. A trip to the
           mouth a bite: one reads as a mistake, three read as a meal. */
        const float w = p * (float)EAT_BITES - floorf(p * (float)EAT_BITES);
        out->arm_r = -120.0f - 45.0f * eat_reach(w) * mag;
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
    case ACT_DIZZY: {
        /* PLONKED DOWN: legs splayed out sideways, arms flopped. `face.c` lowers the body by
           what the splay takes off this form's legs (`figure_pose_t.sit`). */
        const float k = sit_amount(p);
        out->leg_l = 4.0f + (DIZZY_SPLAY - 4.0f) * k;
        out->leg_r = -out->leg_l;
        out->arm_l = 12.0f + 30.0f * k;
        out->arm_r = -out->arm_l;
        break;
    }
    case ACT_LOVE: {
        /* Arms up and swaying: delighted. A 2D rig cannot hug itself. */
        const float q = sinf(p * (float)M_PI * 8.0f) * mag;
        out->arm_l = 120.0f + q * 12.0f;
        out->arm_r = -120.0f + q * 12.0f;
        break;
    }
    case ACT_MEGAFART: {
        /* The fart's own pose — arms out, a leg up — held for seven seconds, trembling. */
        const float k = mega_amount(p);
        const float j = sinf(p * (float)M_PI * 110.0f) * 4.0f * k;
        out->arm_l = 12.0f + 83.0f * k + j;
        out->arm_r = -out->arm_l;
        out->leg_l = 4.0f + 22.0f * k;
        break;
    }
    case ACT_BRACE: {
        /* HUNCHED: arms pulled in to guard his sides, knees together. */
        const float k = squat_amount(p);
        out->arm_l = 12.0f - 22.0f * k + sinf(p * (float)M_PI * 28.0f) * 4.0f * k;
        out->arm_r = -out->arm_l;
        out->leg_l = 4.0f - 6.0f * k;
        out->leg_r = -out->leg_l;
        break;
    }
    case ACT_POOP: {
        /* SQUATTING: legs out, arms in tight with the effort. */
        const float k = squat_amount(p);
        out->leg_l = 4.0f + (SQUAT_SPLAY - 4.0f) * k;
        out->leg_r = -out->leg_l;
        out->arm_l = 12.0f - 8.0f * k;
        out->arm_r = -out->arm_l;
        break;
    }
    case ACT_TICKLED: {
        /* Arms snapping in to guard the tickled side, and back. */
        const float k = tickle_env(p) * mag;
        out->arm_l = 25.0f + sinf(p * (float)M_PI * 12.0f) * 40.0f * k;
        out->arm_r = -(25.0f + sinf(p * (float)M_PI * 12.0f + 1.3f) * 40.0f * k);
        break;
    }
    case ACT_TICKLE: {
        /* Arms flailing, out of step with each other; legs kicking OUT in turn, never across
           (a kick through the middle crossed his legs). */
        const float k = tickle_env(p) * mag;
        const float kick = p * (float)M_PI * 18.0f;
        out->arm_l = 70.0f + sinf(p * (float)M_PI * 26.0f) * 35.0f * k;
        out->arm_r = -(70.0f + sinf(p * (float)M_PI * 26.0f + 1.3f) * 35.0f * k);
        out->leg_l = 4.0f + fmaxf(0.0f, sinf(kick)) * 30.0f * k;
        out->leg_r = -(4.0f + fmaxf(0.0f, -sinf(kick)) * 30.0f * k);
        break;
    }
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
    out->extra_p = p;
    out->sit = 0.0f;
    out->chew = 0.0f;
    out->reach = 0.0f;

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
        case ACT_EAT: {
            /* Down to the food and back, under the pecking; the cookie, the mouth's work. */
            const float w = p * (float)EAT_BITES - floorf(p * (float)EAT_BITES);
            out->oy += eat_reach(w) * 14.0f * mag;
            out->reach = eat_reach(w);
            out->chew = eat_chew(w);
            out->extra = EXTRA_FOOD;
            break;
        }
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
        case ACT_DIZZY:
            out->sit = sit_amount(p);
            out->tilt += dizzy_shake(p) * 16.0f * mag;
            out->extra = EXTRA_STARS;
            break;
        case ACT_LOVE:
            out->tilt += sinf(p * (float)M_PI * 8.0f) * 7.0f * mag;
            out->extra = EXTRA_HEARTS;
            break;
        case ACT_MEGAFART: {
            /* RUMBLING: a fast shake of the whole figure, eyes wide, the clouds coming. */
            const float k = mega_amount(p) * mag;
            out->ox += sinf(p * (float)M_PI * 130.0f) * 3.0f * k;
            out->oy += sinf(p * (float)M_PI * 96.0f) * 2.0f * k;
            out->tilt -= 6.0f * k;
            out->extra = EXTRA_GAS;
            break;
        }
        case ACT_BRACE:
            /* Shrinking down and leaning away from the hand that is coming, shivering. */
            out->sy -= squat_amount(p) * 0.06f;
            out->oy += squat_amount(p) * 8.0f;
            out->tilt += sinf(p * (float)M_PI * 28.0f) * 3.0f * mag - squat_amount(p) * 5.0f;
            break;
        case ACT_POOP:
            out->sit = squat_amount(p);
            out->tilt += strain(p) * 3.0f * mag;
            /* Up on his toes with relief once it is out. */
            if (p > POOP_AT) out->oy -= sinf((p - POOP_AT) / (1.0f - POOP_AT) * (float)M_PI) * 10.0f;
            break;
        case ACT_TICKLED: {
            const float k = tickle_env(p) * mag;
            out->ox += sinf(p * (float)M_PI * 10.0f) * 9.0f * k;
            out->oy -= fabsf(sinf(p * (float)M_PI * 12.0f)) * 8.0f * k;
            out->tilt += sinf(p * (float)M_PI * 8.0f) * 9.0f * k;
            out->extra = EXTRA_TICKLE;
            break;
        }
        case ACT_TICKLE: {
            /* Wriggling side to side and bouncing on the laughs. */
            const float k = tickle_env(p) * mag;
            out->ox += sinf(p * (float)M_PI * 22.0f) * 9.0f * k;
            out->oy -= fabsf(sinf(p * (float)M_PI * 26.0f)) * 10.0f * k;
            out->tilt += sinf(p * (float)M_PI * 16.0f) * 10.0f * k;
            out->extra = EXTRA_TICKLE;
            break;
        }
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

/* THE SHUFFLE. See `rig.h` for why the phase is driven by distance rather than by a clock. */
void rig_walk(rig_walk_t *w, float dlean_px, rig_pose_t *p)
{
    if (w == NULL || p == NULL) return;
    /* A DEADBAND FIRST, and the owner's report is what put it here: *"if we're static and not
       moving very fast or kind of just sitting there, the legs need to be back in the neutral
       position."* They were not, for two reasons that both live in this line.
     *
       `s_lean` is smoothed and integer, so it approaches its target by ever-smaller steps and
       the accelerometer keeps nudging it by a pixel at rest. Without a floor, that trickle is
       indistinguishable from a very slow walk: the phase creeps, the amplitude never quite
       reaches zero, and the legs sit forever at some arbitrary point in a stride.
     *
       Below the floor nothing moves AT ALL — not the amplitude and not the phase — so a panel
       sitting on a shelf is standing still rather than walking imperceptibly. It also gets the
       physics right for free: the tail of every real movement falls under the floor as the
       lean converges, so he decelerates into a stop instead of being cut off at one. */
    float travel = fabsf(dlean_px);
    if (travel < RIG_WALK_DEADBAND_PX) travel = 0.0f;

    w->phase += travel * (float)M_PI / RIG_WALK_PX_PER_STEP;
    /* Wrapped, or a panel left tilting for an hour accumulates a float big enough that adding
       a frame's travel to it changes nothing and the legs quietly stop. */
    if (w->phase > 2.0f * (float)M_PI) w->phase -= 2.0f * (float)M_PI;

    float want = travel / RIG_WALK_FULL_PX;
    if (want > 1.0f) want = 1.0f;
    /* Asymmetric on purpose: legs pick up quickly and settle back deliberately. The release
       used to be 0.08, which took about two seconds to reach neutral — long enough that a pet
       set down on a shelf stood there with one leg out, which is what the owner saw. 0.25 is
       still visibly a settle rather than a snap, and it is done in under half a second. */
    w->amp += (want - w->amp) * (want > w->amp ? 0.35f : 0.25f);
    if (w->amp < 0.02f) {
        w->amp = 0.0f;
        /* Home the stride too. The legs are already neutral once the amplitude is zero — the
           pose is amplitude times the swing — but leaving the phase wherever it stopped means
           the next step begins mid-stride from a standing start. */
        w->phase = 0.0f;
        return; /* standing still is the action's pose, untouched */
    }

    const float swing = sinf(w->phase) * w->amp * 15.0f;
    p->leg_l += swing;
    p->leg_r -= swing;
    /* The bird cannot step fore-and-aft head-on, so its stride reads as a lift — and it has to
       be in phase with the swing or the raised foot is the one taking the weight. */
    p->step += sinf(w->phase) * w->amp * 24.0f;
}
