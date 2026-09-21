/* Host tests for the firmware's pure-C modules.
 *
 * Mirrors `frontend/src/pet/{face,rig,variants}.test.ts`, because the panel runs a port of
 * those modules and a port that drifts from its reference is worse than no port. Each case
 * below asserts a property the source file's comments claim — not the values, which are
 * transcribed and would only be re-typed here, but the INVARIANTS, which are what a
 * transcription can silently break.
 */

#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "emotion.h"
#include "face.h"
#include "calib.h"
#include "gesture.h"
#include "rig.h"
#include "variants.h"

static int checks;
#define CHECK(c, msg)                                            \
    do {                                                         \
        checks++;                                                \
        if (!(c)) {                                              \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg)); \
            exit(1);                                             \
        }                                                        \
    } while (0)

/* ---- emotion ---------------------------------------------------------------------- */

static void test_emotion_mirror(void)
{
    face_params_t f;
    emotion_resolve(FACE_HAPPY, &f);
    /* A mirrored emotion flips the lid ANGLE and nothing else: flipping coverage too would
       undo the asymmetry that is the entire signal for curious and silly. */
    CHECK(f.l.ua == -f.r.ua, "happy mirrors the lid angle");
    CHECK(f.l.uy == f.r.uy, "happy does not mirror upper coverage");
    CHECK(f.l.ly == f.r.ly, "happy does not mirror lower coverage");
    CHECK(f.l.sx == f.r.sx, "happy does not mirror scale");
}

static void test_emotion_asymmetry(void)
{
    face_params_t c, s;
    emotion_resolve(FACE_CURIOUS, &c);
    emotion_resolve(FACE_SILLY, &s);
    /* Asymmetry IS the emotion here. If a refactor mirrors these, both stop reading. */
    CHECK(c.l.uy != c.r.uy, "curious is asymmetric in upper lid");
    CHECK(s.l.sx != s.r.sx, "silly is asymmetric in eye scale");
    CHECK(c.face_ang != 0.0f, "curious carries a head tilt");
}

static void test_emotion_distinct(void)
{
    /* Every face must differ from every other in GEOMETRY, because emotion is carried by lid
       geometry and never by colour (DESIGN.md). Two emotions that resolve to the same numbers
       are two names for one feeling. */
    face_params_t f[FACE_KEY_COUNT];
    for (int i = 0; i < FACE_KEY_COUNT; i++) emotion_resolve((face_key_t)i, &f[i]);
    for (int i = 0; i < FACE_KEY_COUNT; i++) {
        for (int j = i + 1; j < FACE_KEY_COUNT; j++) {
            CHECK(memcmp(&f[i], &f[j], sizeof(face_params_t)) != 0, "faces are distinct");
        }
    }
}

static void test_emotion_bad_key(void)
{
    face_params_t bad, happy;
    emotion_resolve((face_key_t)999, &bad);
    emotion_resolve(FACE_HAPPY, &happy);
    /* A panel that draws nothing is worse than one that smiles. */
    CHECK(memcmp(&bad, &happy, sizeof(face_params_t)) == 0, "unknown key falls back to happy");
    emotion_resolve(FACE_HAPPY, NULL); /* must not crash */
}

static void test_approach(void)
{
    float v = 0.0f;
    for (int i = 0; i < 40; i++) v = emotion_approach(v, 1.0f, 1.0f);
    CHECK(fabsf(v - 1.0f) < 0.001f, "approach converges");
    /* Clamped at 1, or a fast emotion would overshoot and ring. */
    float o = 0.0f;
    o = emotion_approach(o, 1.0f, 100.0f);
    CHECK(o == 1.0f, "approach never overshoots");
    float n = 0.5f;
    n = emotion_approach(n, 1.0f, -5.0f);
    CHECK(n == 0.5f, "a negative rate does not move backwards");
}

/* ---- rig -------------------------------------------------------------------------- */

static void test_idle_never_still(void)
{
    /* "A character that is ever perfectly still reads as dead." */
    rig_pose_t a, b;
    rig_for(ACT_NONE, 0.0f, 1.0f, 0, &a);
    rig_for(ACT_NONE, 0.0f, 1.0f, 2200, &b);
    CHECK(a.arm_l != b.arm_l, "the idle sway moves");

    figure_pose_t fa, fb;
    rig_figure(ACT_NONE, 0.0f, 1.0f, 0, 0.0f, &fa);
    rig_figure(ACT_NONE, 0.0f, 1.0f, 2200, 0.0f, &fb);
    CHECK(fa.sy != fb.sy, "breathing runs under the idle pose too");
}

static void test_gag_has_a_hold(void)
{
    /* The hold IS the punchline: anticipation, a short act, then a long hold. If the hold ever
       becomes shorter than the act, the pratfall becomes a twitch. */
    figure_pose_t f;
    int puff = 0, total = 0;
    for (int i = 0; i <= 100; i++) {
        rig_figure(ACT_FART, (float)i / 100.0f, 1.0f, 0, 0.0f, &f);
        if (f.extra == EXTRA_PUFF) puff++;
        total++;
    }
    CHECK(puff > total / 3, "the gag holds for a good fraction of its duration");
    rig_figure(ACT_FART, 0.05f, 1.0f, 0, 0.0f, &f);
    CHECK(f.extra == EXTRA_NONE, "the gag anticipates before it acts");
    CHECK(rig_spec(ACT_FART)->gag, "fart is a gag");
    CHECK(rig_spec(ACT_FART)->face == FACE_BEWILDERED, "gags resolve to bewildered");
}

static void test_peekaboo(void)
{
    /* Rise, hold, fall — and the hands must actually reach the eyes, or hide is the squat it
       used to be on the Wall. */
    rig_pose_t p;
    rig_for(ACT_HIDE, 0.0f, 1.0f, 0, &p);
    CHECK(p.hands_up < 0.05f, "hands start down");
    rig_for(ACT_HIDE, 0.4f, 1.0f, 0, &p);
    CHECK(p.hands_up > 0.99f, "hands are fully up in the middle");
    rig_for(ACT_HIDE, 1.0f, 1.0f, 0, &p);
    CHECK(p.hands_up < 0.05f, "hands come down again");
    for (int i = 0; i <= 100; i++) {
        rig_for(ACT_HIDE, (float)i / 100.0f, 1.0f, 0, &p);
        CHECK(p.hands_up >= 0.0f && p.hands_up <= 1.0f, "hands_up stays in range");
    }
}

static void test_every_action_moves(void)
{
    /* On a panel a child is holding, "nothing happened" is indistinguishable from "broken".
       Every action must differ from the idle pose somewhere in its run. */
    for (int a = ACT_NONE + 1; a < ACT_COUNT; a++) {
        int moved = 0;
        rig_pose_t idle, p;
        figure_pose_t fi, fp;
        for (int i = 1; i < 100 && !moved; i++) {
            const float q = (float)i / 100.0f;
            rig_for(ACT_NONE, q, 1.0f, 0, &idle);
            rig_for((action_t)a, q, 1.0f, 0, &p);
            rig_figure(ACT_NONE, q, 1.0f, 0, 0.0f, &fi);
            rig_figure((action_t)a, q, 1.0f, 0, 0.0f, &fp);
            if (memcmp(&idle, &p, sizeof(p)) != 0 || memcmp(&fi, &fp, sizeof(fp)) != 0) moved = 1;
        }
        CHECK(moved, "every action visibly differs from idle");
        CHECK(rig_spec((action_t)a)->dur_ms > 0, "every action has a duration");
    }
}

/* ---- variants --------------------------------------------------------------------- */

static void test_pick_always_answers(void)
{
    /* "It can never fail." Hammer one instant so every cooldown is live, and it must still
       hand back an action a child can see. */
    pool_memory_t mem;
    variants_reset(&mem);
    for (int i = 0; i < 400; i++) {
        const int a = variants_pick(POOL_POKE, &mem, 1000, (uint32_t)(i * 2654435761u));
        CHECK(a > ACT_NONE && a < ACT_COUNT, "pick returns a real action");
    }
}

static void test_cooldown_suppresses_recency(void)
{
    /* Recency is suppressed WITHIN a pool: poke twice in a row and you should not get the
       same reaction, because that is what makes the toy stop being interesting. */
    pool_memory_t mem;
    variants_reset(&mem);
    int same = 0;
    int prev = variants_pick(POOL_POKE, &mem, 1000, 7u);
    for (int i = 1; i < 6; i++) {
        const int a = variants_pick(POOL_POKE, &mem, (uint32_t)(1000 + i * 200), 7u + (uint32_t)i * 13u);
        if (a == prev) same++;
        prev = a;
    }
    CHECK(same == 0, "a rapid burst never repeats a cooling variant");
}

static void test_pool_spreads(void)
{
    /* Over a long run with cooldowns expiring, more than one variant must actually appear —
       a weighted table that always returns its first entry is a pool of one. */
    pool_memory_t mem;
    variants_reset(&mem);
    int seen[ACT_COUNT];
    memset(seen, 0, sizeof(seen));
    for (int i = 0; i < 300; i++) {
        const int a = variants_pick(POOL_POKE, &mem, (uint32_t)(i * 60000), (uint32_t)(i * 1103515245u + 12345u));
        seen[a] = 1;
    }
    int n = 0;
    for (int i = 0; i < ACT_COUNT; i++) n += seen[i];
    CHECK(n >= 4, "the pool spreads over several variants");
}

static void test_penalty(void)
{
    pool_memory_t mem;
    variants_reset(&mem);
    /* The first poke of a burst lands at full magnitude, even at t=0 — 0 ms is a real time on
       this device, and reading it as "12 seconds ago" would soften the very first reaction. */
    CHECK(variants_penalty(POOL_POKE, &mem, 0) == 1.0f, "the first firing is full magnitude");
    float last = 1.0f;
    for (int i = 1; i < 20; i++) {
        const float m = variants_penalty(POOL_POKE, &mem, (uint32_t)(i * 300));
        CHECK(m <= last + 1e-6f, "a burst decays monotonically");
        CHECK(m >= PENALTY_FLOOR - 1e-6f, "the penalty never goes below the floor");
        last = m;
    }
    CHECK(last <= PENALTY_FLOOR + 1e-6f, "a long burst reaches the floor");
    /* "Leave it alone for 12 s and it is fresh again." */
    const float fresh = variants_penalty(POOL_POKE, &mem, (uint32_t)(20 * 300 + PENALTY_RESET_MS + 1));
    CHECK(fresh == 1.0f, "the penalty resets after a quiet gap");
}

/* ---- face ------------------------------------------------------------------------- */

static uint16_t *fb;

static long non_black(void)
{
    long n = 0;
    for (long i = 0; i < (long)FACE_W * FACE_H; i++)
        if (fb[i] != 0) n++;
    return n;
}

static void test_the_default_form_is_the_ostrich(void)
{
    /* The twins asked for it, so it is what a panel shows out of the box. A regression here
       is silent — the robot draws perfectly well — which is exactly why it is asserted. */
    face_state_t st;
    face_rest(&st);
    CHECK(st.form == FORM_OSTRICH, "rest is the ostrich");
}

static void test_both_forms_draw_and_differ(void)
{
    /* Two forms that render identically would mean the switch does nothing, and nothing in
       the rest of the suite would notice. */
    face_state_t st;
    face_rest(&st);
    uint16_t *other = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(other != NULL, "scratch frame allocated");
    st.form = FORM_OSTRICH;
    face_draw(fb, 0, &st);
    const long ostrich_lit = non_black();
    memcpy(other, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    st.form = FORM_ROBOT;
    face_draw(fb, 0, &st);
    CHECK(ostrich_lit > 8000, "the ostrich covers a real part of the panel");
    CHECK(non_black() > 8000, "and so does the robot");
    CHECK(memcmp(other, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t)) != 0,
          "the two forms are not the same picture");
    free(other);
}

static void test_draw_produces_a_robot(void)
{
    face_state_t st;
    face_rest(&st);
    st.form = FORM_ROBOT;
    memset(fb, 0xAB, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    face_draw(fb, 0, &st);
    const long lit = non_black();
    CHECK(lit > 8000, "the robot covers a real part of the panel");
    CHECK(lit < (long)FACE_W * FACE_H, "the robot does not fill the whole panel");
}

static void test_every_face_and_action_draws(void)
{
    /* The renderer must survive every combination the rig can hand it. A NaN or a negative
       radius here is a panel that shows nothing, in a bedroom, with no console. */
    for (int k = 0; k < FACE_KEY_COUNT; k++) {
        for (int a = 0; a < ACT_COUNT; a++) {
            for (int step = 0; step <= 4; step++) {
                face_state_t st;
                face_rest(&st);
                emotion_resolve((face_key_t)k, &st.eyes);
                const float p = (float)step / 4.0f;
                rig_for((action_t)a, p, 1.0f, (uint32_t)(step * 400), &st.rig);
                rig_figure((action_t)a, p, 1.0f, (uint32_t)(step * 400), st.eyes.face_ang,
                           &st.fig);
                st.open = step == 2 ? 0.0f : 1.0f;
                st.startle = step == 1 ? 1.0f : 0.0f;
                for (int f = 0; f < FORM_COUNT; f++) {
                    st.form = (face_form_t)f;
                    face_draw(fb, k % face_colour_count(), &st);
                    CHECK(non_black() > 2000,
                          "every face/action/form combination draws something");
                }
            }
        }
    }
}

/* The renderer's own RGB565-byte-swapped packing, so the test can name a colour. Counting
   non-black pixels cannot see a blink at all: the eye is drawn OVER the head, so shutting it
   swaps one non-black colour for another and the silhouette is identical. */
static uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    const uint16_t c = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
    return (uint16_t)((c >> 8) | (c << 8));
}

/* The figure's extent on the panel. */
static void bbox(int *x0, int *x1, int *y0, int *y1)
{
    *x0 = FACE_W;
    *x1 = -1;
    *y0 = FACE_H;
    *y1 = -1;
    for (int y = 0; y < FACE_H; y++) {
        for (int x = 0; x < FACE_W; x++) {
            if (fb[y * FACE_W + x] == 0) continue;
            if (x < *x0) *x0 = x;
            if (x > *x1) *x1 = x;
            if (y < *y0) *y0 = y;
            if (y > *y1) *y1 = y;
        }
    }
}

static long count_colour(uint16_t want)
{
    long n = 0;
    for (long i = 0; i < (long)FACE_W * FACE_H; i++)
        if (fb[i] == want) n++;
    return n;
}

static void test_blink_is_a_line_not_a_hole(void)
{
    /* "A blink drawn as nothing reads as the face breaking for a frame; a line reads as a
       blink." Two properties say that precisely: the whites of the eyes go away, and NOTHING
       ELSE ON THE FIGURE MOVES — a blink that shifted the silhouette would be the face
       breaking, whatever it drew in the eye. */
    const uint16_t white = rgb565(0xF6, 0xF9, 0xFC);
    uint16_t *shut = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(shut != NULL, "scratch frame allocated");

    face_state_t st;
    face_rest(&st);
    face_draw(fb, 0, &st);
    const long open_white = count_colour(white);
    const long open_lit = non_black();
    int ox0, ox1, oy0, oy1;
    bbox(&ox0, &ox1, &oy0, &oy1);
    CHECK(open_white > 500, "an open eye shows its white");

    st.open = 0.0f;
    face_draw(fb, 0, &st);
    memcpy(shut, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(count_colour(white) == 0, "a shut eye shows no white");
    /* The BOUNDING BOX, not the lit count. Lids are drawn black inside the eye — the same as
       the web renderer's `#000` fills — and black is the field colour, so a happy face's
       cheek-raise punches "unlit" pixels into the head and a shut eye covers them. Counting
       lit pixels therefore says a blink makes the robot BIGGER, which is an artefact of the
       probe rather than anything on the glass. The figure's extent is what must not move. */
    int bx0, bx1, by0, by1;
    bbox(&bx0, &bx1, &by0, &by1);
    CHECK(bx0 == ox0 && bx1 == ox1 && by0 == oy0 && by1 == oy1,
          "a blink does not change the figure's extent");
    (void)open_lit;

    /* And every changed pixel is in the eyes, not scattered across the robot. */
    face_draw(fb, 0, &st);
    st.open = 1.0f;
    face_draw(fb, 0, &st);
    int y_lo = FACE_H, y_hi = -1;
    for (int y = 0; y < FACE_H; y++) {
        for (int x = 0; x < FACE_W; x++) {
            if (fb[y * FACE_W + x] != shut[y * FACE_W + x]) {
                if (y < y_lo) y_lo = y;
                if (y > y_hi) y_hi = y;
            }
        }
    }
    CHECK(y_hi >= y_lo, "the blink changes something");
    CHECK(y_hi - y_lo < 80, "the blink is confined to the eyes");
    free(shut);
}

/* ---- zones ------------------------------------------------------------------------ */

/* The figure's geometry, as face.c lays it out: origin at (W/2, H*0.545). Written out rather
   than imported because a test that shares the constant under test only proves the constant
   equals itself; these are the coordinates a finger actually lands on. */
#define FIG_X (FACE_W / 2)
#define FIG_Y 244

static void test_zones_hit_the_right_parts(void)
{
    CHECK(face_zone(FORM_ROBOT, FIG_X, FIG_Y - 96, false, 0) == ZONE_HEAD, "the head is the head");
    CHECK(face_zone(FORM_ROBOT, FIG_X, FIG_Y - 96 - 110, false, 0) == ZONE_HEAD, "the antenna is his too");
    CHECK(face_zone(FORM_ROBOT, FIG_X, FIG_Y + 36, false, 0) == ZONE_BODY, "the belly is the body");
    CHECK(face_zone(FORM_ROBOT, FIG_X + 86, FIG_Y + 36, false, 0) == ZONE_ARM, "out to the side is an arm");
    CHECK(face_zone(FORM_ROBOT, FIG_X - 86, FIG_Y + 36, false, 0) == ZONE_ARM, "both arms");
    CHECK(face_zone(FORM_ROBOT, FIG_X, FIG_Y + 146, false, 0) == ZONE_LEG, "below the hips is a leg");
    CHECK(face_zone(FORM_ROBOT, 4, 4, false, 0) == ZONE_NONE, "the corner is background");
    CHECK(face_zone(FORM_ROBOT, FACE_W - 4, FACE_H - 4, false, 0) == ZONE_NONE, "so is the far corner");
}

static void test_zones_follow_the_flip(void)
{
    /* A tap on his head is his head whichever way up the panel is. Getting this wrong makes
       the zones feel random exactly when a child is holding the thing any which way. */
    for (int f = 0; f < FORM_COUNT; f++) {
        const face_form_t form = (face_form_t)f;
        for (int y = 0; y < FACE_H; y += 7) {
            for (int x = 0; x < FACE_W; x += 7) {
                const face_zone_t up = face_zone(form, x, y, false, 0);
                const face_zone_t flipped =
                    face_zone(form, FACE_W - 1 - x, FACE_H - 1 - y, true, 0);
                CHECK(up == flipped, "zones follow the 180 degree flip, in every form");
            }
        }
    }
}

static void test_zones_follow_the_lean(void)
{
    /* He slides downhill as the panel tilts; his hitboxes go with him. */
    const int lean = 40;
    CHECK(face_zone(FORM_ROBOT, FIG_X + lean, FIG_Y + 36, false, lean) == ZONE_BODY,
          "the belly moves with the lean");
    CHECK(face_zone(FORM_ROBOT, FIG_X, FIG_Y + 36, false, lean) == ZONE_BODY,
          "and is still wide enough to hit at centre");
}

static void test_every_zone_is_reachable(void)
{
    /* A zone nothing can hit is a pool that never plays. */
    for (int f = 0; f < FORM_COUNT; f++) {
        const face_form_t form = (face_form_t)f;
        int seen[8];
        memset(seen, 0, sizeof(seen));
        for (int y = 0; y < FACE_H; y++)
            for (int x = 0; x < FACE_W; x++) seen[face_zone(form, x, y, false, 0)] = 1;
        CHECK(seen[ZONE_HEAD] && seen[ZONE_BODY] && seen[ZONE_ARM] && seen[ZONE_LEG],
              "every part of every form can be tapped");
        CHECK(seen[ZONE_NONE], "and so can the background");
    }
}

static void test_every_pool_answers(void)
{
    /* Same contract as the poke pool, for all of them: never silent, and never a pool of one.
       A belly poke that always farts stops being funny on the third try. */
    for (int p = 0; p < POOL_COUNT; p++) {
        pool_memory_t mem;
        variants_reset(&mem);
        int seen[ACT_COUNT];
        memset(seen, 0, sizeof(seen));
        for (int i = 0; i < 300; i++) {
            const int a = variants_pick((pool_t)p, &mem, (uint32_t)(i * 60000),
                                        (uint32_t)(i * 1103515245u + 12345u));
            CHECK(a > ACT_NONE && a < ACT_COUNT, "every pool returns a real action");
            seen[a] = 1;
        }
        int n = 0;
        for (int i = 0; i < ACT_COUNT; i++) n += seen[i];
        CHECK(n >= 4, "every pool spreads over several variants");
    }
}

/* ---- calibration ------------------------------------------------------------------- */

/* The fault the owner actually reported: the middle reads true and the outer band is pulled
   in toward the centre. Modelled here as a cubic ease that is the identity at the centre and
   compresses increasingly toward both edges — sensed = f(true). Calibration must invert it. */
static int16_t squash(int v, int span, float k)
{
    const float c = (float)(span - 1) * 0.5f;
    const float t = ((float)v - c) / c; /* -1 .. +1 */
    const float s = t * (1.0f - k * (1.0f - t * t) * 0.0f) - k * t * t * t;
    float out = c + s * c;
    if (out < 0.0f) out = 0.0f;
    if (out > (float)(span - 1)) out = (float)(span - 1);
    return (int16_t)(out + 0.5f);
}

static void fill_grid(int16_t mx[CAL_KNOTS][CAL_KNOTS], int16_t my[CAL_KNOTS][CAL_KNOTS],
                      float k)
{
    for (int j = 0; j < CAL_KNOTS; j++) {
        for (int i = 0; i < CAL_KNOTS; i++) {
            mx[j][i] = squash(calib_target_x(i), FACE_W, k);
            my[j][i] = squash(calib_target_y(j), FACE_H, k);
        }
    }
}

static void test_samples_need_agreement_not_just_count(void)
{
    cal_samples_t s;
    calib_sample_reset(&s);
    calib_sample_add(&s, 100, 200);
    CHECK(!calib_sample_settled(&s), "one tap is never a measurement");
    calib_sample_add(&s, 102, 201);
    CHECK(!calib_sample_settled(&s), "nor are two");
    calib_sample_add(&s, 101, 199);
    CHECK(calib_sample_settled(&s), "three that agree settle");

    /* The owner's actual complaint: the contact patch moves the reading. A run that is
       merely NUMEROUS but scattered must not be accepted. */
    calib_sample_reset(&s);
    calib_sample_add(&s, 100, 200);
    calib_sample_add(&s, 100 + CAL_SPREAD_MAX + 6, 200);
    calib_sample_add(&s, 100, 200);
    CHECK(!calib_sample_settled(&s), "three that disagree do not settle");
}

static void test_samples_use_the_median_not_the_mean(void)
{
    /* One slip with the side of a finger drags a mean and cannot move a median. */
    cal_samples_t s;
    calib_sample_reset(&s);
    calib_sample_add(&s, 100, 300);
    calib_sample_add(&s, 104, 302);
    calib_sample_add(&s, 102, 301);
    calib_sample_add(&s, 260, 40); /* the slip */
    int16_t x = 0, y = 0;
    calib_sample_result(&s, &x, &y);
    CHECK(x >= 100 && x <= 104, "a wild sample does not move the x result");
    CHECK(y >= 300 && y <= 302, "nor the y result");
    CHECK(!calib_sample_settled(&s), "and it keeps the target unsettled");
}

static void test_samples_recover_from_a_bad_start(void)
{
    /* A target the owner kept tapping until it felt right is judged on the taps that felt
       right: the buffer keeps the most recent CAL_SAMPLES_MAX. */
    cal_samples_t s;
    calib_sample_reset(&s);
    for (int i = 0; i < 4; i++) calib_sample_add(&s, 40 + i * 30, 40 + i * 30);
    CHECK(!calib_sample_settled(&s), "a scattered start does not settle");
    for (int i = 0; i < CAL_SAMPLES_MAX; i++) calib_sample_add(&s, 200 + (i % 2), 300);
    CHECK(calib_sample_settled(&s), "a run that settles, settles");
    int16_t x = 0;
    calib_sample_result(&s, &x, NULL);
    CHECK(x >= 200 && x <= 201, "and reports where it settled");
}

static void test_samples_always_yield_something(void)
{
    /* A target that will not settle must not trap the owner on it. Past the cap the routine
       accepts the median anyway, so the result has to be defined for any n. */
    cal_samples_t s;
    calib_sample_reset(&s);
    for (int i = 0; i < CAL_SAMPLES_MAX + 4; i++) calib_sample_add(&s, 10 + i * 40, 500 - i * 30);
    CHECK(s.n == CAL_SAMPLES_MAX, "the buffer is bounded");
    int16_t x = -1, y = -1;
    calib_sample_result(&s, &x, &y);
    CHECK(x >= 0 && y >= 0, "a capped-out target still yields its best estimate");
    CHECK(calib_sample_spread(&s) > CAL_SPREAD_MAX, "and is honest that it never agreed");
}

/* One trial: build a grid either from single taps or from medians of agreeing taps, on a
   panel with both edge skew and per-tap contact noise, and return the total residual. */
static long noisy_trial(uint32_t *rnd, float k, int samples)
{
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    for (int j = 0; j < CAL_KNOTS; j++) {
        for (int i = 0; i < CAL_KNOTS; i++) {
            const int tx = calib_target_x(i), ty = calib_target_y(j);
            cal_samples_t s;
            calib_sample_reset(&s);
            for (int t = 0; t < samples; t++) {
                *rnd = *rnd * 1103515245u + 12345u;
                const int nx = (int)((*rnd >> 16) % 19u) - 9;
                *rnd = *rnd * 1103515245u + 12345u;
                const int ny = (int)((*rnd >> 16) % 19u) - 9;
                calib_sample_add(&s, squash(tx, FACE_W, k) + nx, squash(ty, FACE_H, k) + ny);
            }
            calib_sample_result(&s, &mx[j][i], &my[j][i]);
        }
    }
    calib_t c;
    if (!calib_build(mx, my, &c)) return -1;
    long err = 0;
    for (int tx = 8; tx < FACE_W - 8; tx++) {
        const int raw = squash(tx, FACE_W, k);
        int a = 0;
        calib_apply(&c, raw, 0, &a, NULL);
        err += a > tx ? a - tx : tx - a;
    }
    return err;
}

static void test_samples_beat_single_taps_on_a_noisy_panel(void)
{
    /* THE WHOLE POINT, end to end — and stated as a claim about MANY runs, because it is one.
       The median of three taps has roughly 70% of the noise of one, so a single seeded trial
       can go either way and asserting on one would be measuring the seed. */
    const float k = 0.18f;
    uint32_t r1 = 12345u, r3 = 12345u;
    long tot1 = 0, tot3 = 0;
    int wins = 0, trials = 0;
    for (int t = 0; t < 200; t++) {
        const long e1 = noisy_trial(&r1, k, 1);
        const long e3 = noisy_trial(&r3, k, CAL_SAMPLES_MIN);
        if (e1 < 0 || e3 < 0) continue; /* a noisy single-tap grid can fold; that is its own cost */
        tot1 += e1;
        tot3 += e3;
        if (e3 < e1) wins++;
        trials++;
    }
    CHECK(trials > 150, "most trials produced usable grids");
    CHECK(tot3 < tot1, "medians of agreeing taps beat single taps overall");
    CHECK(wins * 2 > trials, "and win the majority of individual runs");
}

/* Occasional gross misses, which is what the owner actually described: most taps tight, and
   one in six landing well off because of how much fingertip went down. Uniform jitter is the
   wrong model — against THAT, extra samples barely help, because the residual is dominated by
   the piecewise-linear model's own error rather than by noise. The tail is the whole story. */
static int slip_noise(uint32_t *r)
{
    *r = *r * 1103515245u + 12345u;
    if (((*r >> 16) % 6u) == 0) {
        *r = *r * 1103515245u + 12345u;
        return (int)((*r >> 16) % 81u) - 40;
    }
    *r = *r * 1103515245u + 12345u;
    return (int)((*r >> 16) % 7u) - 3;
}

static long slip_trial(uint32_t *r, float k, bool adaptive)
{
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    for (int j = 0; j < CAL_KNOTS; j++) {
        for (int i = 0; i < CAL_KNOTS; i++) {
            const int tx = calib_target_x(i), ty = calib_target_y(j);
            cal_samples_t s;
            calib_sample_reset(&s);
            int guard = 0;
            do {
                calib_sample_add(&s, squash(tx, FACE_W, k) + slip_noise(r),
                                 squash(ty, FACE_H, k) + slip_noise(r));
                guard++;
            } while (adaptive && !calib_sample_settled(&s) && guard < CAL_SAMPLES_MAX);
            calib_sample_result(&s, &mx[j][i], &my[j][i]);
        }
    }
    calib_t c;
    if (!calib_build(mx, my, &c)) return -1;
    long worst = 0;
    for (int tx = 8; tx < FACE_W - 8; tx++) {
        const int raw = squash(tx, FACE_W, k);
        int a = 0;
        calib_apply(&c, raw, 0, &a, NULL);
        const long d = a > tx ? a - tx : tx - a;
        if (d > worst) worst = d;
    }
    return worst;
}

static void test_agreement_removes_the_bad_tail(void)
{
    /* THE CLAIM THAT JUSTIFIES THE EXTRA TAPS, and it is about the tail, not the mean: a
       single tap per target leaves a real chance of a calibration that is WORSE than none in
       places, and requiring agreement removes it. */
    uint32_t r1 = 777u, r2 = 777u;
    int bad_single = 0, bad_adaptive = 0, runs = 0;
    long worst_single = 0, worst_adaptive = 0;
    for (int t = 0; t < 300; t++) {
        const long a = slip_trial(&r1, 0.18f, false);
        const long b = slip_trial(&r2, 0.18f, true);
        if (a < 0 || b < 0) continue;
        if (a > 15) bad_single++;
        if (b > 15) bad_adaptive++;
        if (a > worst_single) worst_single = a;
        if (b > worst_adaptive) worst_adaptive = b;
        runs++;
    }
    CHECK(runs > 250, "most trials produced usable grids");
    CHECK(bad_single > runs / 10, "single taps really do go badly wrong sometimes");
    CHECK(bad_adaptive == 0, "requiring agreement removes the bad tail entirely");
    CHECK(worst_adaptive * 2 < worst_single, "and halves the worst case");
}

static void test_calib_identity_until_built(void)
{
    /* An uncalibrated panel must behave EXACTLY as it did before this file existed. */
    int sx = -1, sy = -1;
    calib_apply(NULL, 123, 231, &sx, &sy);
    CHECK(sx == 123 && sy == 231, "no calibration is the identity");
    calib_t c;
    memset(&c, 0, sizeof(c));
    calib_apply(&c, 40, 400, &sx, &sy);
    CHECK(sx == 40 && sy == 400, "an invalid calibration is the identity");
}

static void test_calib_exact_at_the_knots(void)
{
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    fill_grid(mx, my, 0.18f);
    calib_t c;
    CHECK(calib_build(mx, my, &c), "the grid builds");
    for (int i = 0; i < CAL_KNOTS; i++) {
        int sx = 0, sy = 0;
        calib_apply(&c, c.raw_x[i], c.raw_y[i], &sx, &sy);
        CHECK(sx == calib_target_x(i), "a knot maps to its own target in x");
        CHECK(sy == calib_target_y(i), "a knot maps to its own target in y");
    }
}

static void test_calib_corrects_the_edges(void)
{
    /* The point of the whole exercise: error near the bezel must come DOWN, and the centre
       must not be made worse in the process. */
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    calib_t c;

    /* Swept across three distortion strengths rather than fitted to one. The real panel's
       curve is unknown, and a threshold tuned to the single model this test invented would
       measure the test rather than the code. */
    for (int s = 0; s < 3; s++) {
        const float k = 0.10f + 0.09f * (float)s;
        fill_grid(mx, my, k);
        CHECK(calib_build(mx, my, &c), "the grid builds at every strength");
        int worst_before = 0, worst_after = 0;
        for (int tx = 8; tx < FACE_W - 8; tx++) {
            const int raw = squash(tx, FACE_W, k);
            int sx = 0;
            calib_apply(&c, raw, 0, &sx, NULL);
            const int before = raw > tx ? raw - tx : tx - raw;
            const int after = sx > tx ? sx - tx : tx - sx;
            if (before > worst_before) worst_before = before;
            if (after > worst_after) worst_after = after;
        }
        CHECK(worst_before > 12, "the simulated panel really is skewed");
        CHECK(worst_after * 3 < worst_before, "calibration removes most of the error");
        CHECK(worst_after < 15, "and what is left is well inside a fingertip");
    }
}

static void test_calib_extends_past_the_outer_targets(void)
{
    /* The outer 12% lies beyond the outermost target — a target on the bezel cannot be
       tapped — so that band is extrapolated. Clamping there would flatten exactly the region
       the owner reported as wrong. */
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    fill_grid(mx, my, 0.18f);
    calib_t c;
    CHECK(calib_build(mx, my, &c), "the grid builds");

    int a = 0, b = 0;
    calib_apply(&c, c.raw_x[0] - 30, 0, &a, NULL);
    calib_apply(&c, c.raw_x[0] - 10, 0, &b, NULL);
    CHECK(a < b, "readings outside the first knot still separate");
    CHECK(a >= 0, "and stay on the panel");
    calib_apply(&c, c.raw_x[CAL_KNOTS - 1] + 400, 0, &a, NULL);
    CHECK(a <= FACE_W - 1, "a wild reading is clamped to the panel");
}

static void test_calib_is_monotone_everywhere(void)
{
    /* A map that folds puts two places on the panel at one coordinate, and the only way to
       undo it is the touchscreen it just broke. */
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    fill_grid(mx, my, 0.18f);
    calib_t c;
    CHECK(calib_build(mx, my, &c), "the grid builds");
    int prev = -1;
    for (int v = -50; v < FACE_W + 50; v++) {
        int sx = 0;
        calib_apply(&c, v, 0, &sx, NULL);
        CHECK(sx >= prev, "the x map never goes backwards");
        prev = sx;
    }
}

static void test_calib_rejects_a_folded_grid(void)
{
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    fill_grid(mx, my, 0.18f);
    /* One column tapped out of order — a slip, or a child helping. */
    for (int j = 0; j < CAL_KNOTS; j++) mx[j][2] = mx[j][0];
    calib_t c;
    CHECK(!calib_build(mx, my, &c), "a non-monotone grid is refused");
}

static void test_calib_round_trips_through_nvs(void)
{
    int16_t mx[CAL_KNOTS][CAL_KNOTS], my[CAL_KNOTS][CAL_KNOTS];
    fill_grid(mx, my, 0.18f);
    calib_t c, back;
    CHECK(calib_build(mx, my, &c), "the grid builds");
    uint8_t blob[CAL_BLOB_BYTES];
    CHECK(calib_save(&c, blob, sizeof(blob)) == CAL_BLOB_BYTES, "it serialises");
    CHECK(calib_load(blob, CAL_BLOB_BYTES, &back), "and loads");
    CHECK(memcmp(&c, &back, sizeof(c)) == 0, "unchanged by the round trip");

    CHECK(!calib_load(blob, CAL_BLOB_BYTES - 1, &back), "a short blob is refused");
    uint8_t bad[CAL_BLOB_BYTES];
    memcpy(bad, blob, sizeof(bad));
    bad[0] ^= 0xFF;
    CHECK(!calib_load(bad, sizeof(bad), &back), "a blob without the magic is refused");
    memcpy(bad, blob, sizeof(bad));
    bad[2] = 0x7F;
    bad[3] = 0x7F; /* first x knot enormous -> not ascending */
    CHECK(!calib_load(bad, sizeof(bad), &back), "a folded stored map is refused on load");
}

/* ---- gesture ---------------------------------------------------------------------- */

#define DT 40 /* the render loop's poll interval */

/* Drive the gesture for `ms` with the finger up, returning true if it ever fired (it must
   not). */
static gesture_action_t idle_for(gesture_t *g, int ms)
{
    gesture_action_t fired = GESTURE_NONE;
    for (int t = 0; t < ms; t += DT) {
        const gesture_action_t a = gesture_poll(g, false, false, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    return fired;
}

/* One press of `ms`, edge on the first frame. */
static gesture_action_t press_for(gesture_t *g, int ms)
{
    gesture_action_t fired = gesture_poll(g, true, true, DT);
    for (int t = DT; t < ms; t += DT) {
        const gesture_action_t a = gesture_poll(g, false, true, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    const gesture_action_t r = gesture_poll(g, false, false, DT); /* the release frame */
    return r != GESTURE_NONE ? r : fired;
}

static gesture_action_t do_sequence_n(gesture_t *g, int taps, int gap_ms, int hold_ms)
{
    gesture_action_t fired = GESTURE_NONE;
    for (int i = 0; i < taps; i++) {
        press_for(g, 120);
        idle_for(g, gap_ms);
    }
    gesture_poll(g, true, true, DT);
    for (int t = DT; t < hold_ms; t += DT) {
        const gesture_action_t a = gesture_poll(g, false, true, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    return fired;
}

static gesture_action_t do_sequence(gesture_t *g, int gap_ms, int hold_ms)
{
    return do_sequence_n(g, GESTURE_TAPS_REBOOT, gap_ms, hold_ms);
}

static void test_gesture_happy_path(void)
{
    gesture_t g;
    gesture_reset(&g);
    CHECK(do_sequence(&g, 200, GESTURE_HOLD_MS + 200) == GESTURE_REBOOT,
          "three taps then a hold reboots");
}

static void test_gesture_hold_alone_does_nothing(void)
{
    /* The old gesture. It must no longer be enough on its own, or nothing has changed. */
    gesture_t g;
    gesture_reset(&g);
    gesture_action_t fired = gesture_poll(&g, true, true, DT);
    for (int t = DT; t < 20000; t += DT) {
        const gesture_action_t a = gesture_poll(&g, false, true, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    CHECK(fired == GESTURE_NONE, "a long hold with no taps never reboots");
    CHECK(gesture_cue(&g) == 0.0f, "and it draws no cue");
}

static void test_gesture_slow_taps_do_not_count(void)
{
    /* "Within half a second of each other." A lazy rhythm is a new attempt, not a failure. */
    gesture_t g;
    gesture_reset(&g);
    CHECK(do_sequence(&g, GESTURE_GAP_MS + 200, GESTURE_HOLD_MS + 200) == GESTURE_NONE,
          "taps spaced too far apart never arm the hold");
}

static void test_gesture_long_taps_do_not_count(void)
{
    /* Mashing produces presses of every length; only SHORT ones are part of the sequence. */
    gesture_t g;
    gesture_reset(&g);
    gesture_action_t fired = GESTURE_NONE;
    for (int i = 0; i < GESTURE_TAPS_REBOOT; i++) {
        press_for(&g, GESTURE_TAP_MAX_MS + 200);
        idle_for(&g, 200);
    }
    gesture_poll(&g, true, true, DT);
    for (int t = DT; t < GESTURE_HOLD_MS + 200; t += DT) {
        const gesture_action_t a = gesture_poll(&g, false, true, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    CHECK(fired == GESTURE_NONE, "slow presses do not count as taps");
}

static void test_gesture_release_abandons(void)
{
    /* Letting go mid-hold must abandon the WHOLE sequence. Leaving the panel one press from
       rebooting, indefinitely, is exactly the accident this change exists to stop. */
    gesture_t g;
    gesture_reset(&g);
    for (int i = 0; i < GESTURE_TAPS_REBOOT; i++) {
        press_for(&g, 120);
        idle_for(&g, 200);
    }
    gesture_action_t fired = gesture_poll(&g, true, true, DT);
    for (int t = DT; t < GESTURE_HOLD_MS / 2; t += DT) {
        const gesture_action_t a = gesture_poll(&g, false, true, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    const gesture_action_t rel = gesture_poll(&g, false, false, DT); /* let go */
    CHECK(fired == GESTURE_NONE && rel == GESTURE_NONE, "an abandoned hold does not reboot");
    CHECK(g.taps == 0, "an abandoned hold clears the taps");
    /* And a fresh press right afterwards is just a tap. */
    fired = gesture_poll(&g, true, true, DT);
    for (int t = DT; t < GESTURE_HOLD_MS + 200; t += DT) {
        const gesture_action_t a = gesture_poll(&g, false, true, DT);
        if (a != GESTURE_NONE) fired = a;
    }
    CHECK(fired == GESTURE_NONE, "the next hold alone does not reboot either");
}

static void test_gesture_fires_once(void)
{
    /* The finger is still down after it fires. It must not fire again every frame. */
    gesture_t g;
    gesture_reset(&g);
    int count = 0;
    if (do_sequence(&g, 200, GESTURE_HOLD_MS + 200) != GESTURE_NONE) count++;
    for (int t = 0; t < 10000; t += DT)
        if (gesture_poll(&g, false, true, DT) != GESTURE_NONE) count++;
    CHECK(count == 1, "a single hold reboots exactly once");
}

static void test_gesture_child_mashing(void)
{
    /* THE PROPERTY THE OWNER ASKED FOR, measured against the gesture it replaces.
     *
     * The model has to include LEANS — a child resting a hand on the panel for several
     * seconds — or it proves nothing: a stream of short presses could never reach a five
     * second hold under either scheme. So 15% of presses here are 3-9 s, which is what the
     * old single-hold gesture was defenceless against.
     *
     * The assertion is a COMPARISON, not a magic number: the old gesture fired on every lean
     * past five seconds, and the new one must be far below that. Not zero — three short taps
     * in rhythm followed by a long press is a reachable pattern, and a gesture that could
     * never occur by accident could not be performed on purpose either. */
    gesture_t g;
    gesture_reset(&g);
    uint32_t r = 12345u;
    int fired = 0, old_would_fire = 0;
    for (int i = 0; i < 20000; i++) {
        r = r * 1103515245u + 12345u;
        const int lean = ((r >> 16) % 100u) < 15u;
        r = r * 1103515245u + 12345u;
        const int press = lean ? 3000 + (int)((r >> 16) % 6000u) : 40 + (int)((r >> 16) % 800u);
        r = r * 1103515245u + 12345u;
        const int gap = 40 + (int)((r >> 16) % 1200u);
        if (press >= GESTURE_HOLD_MS) old_would_fire++;
        if (press_for(&g, press) != GESTURE_NONE) fired++;
        if (idle_for(&g, gap) != GESTURE_NONE) fired++;
    }
    CHECK(old_would_fire > 500, "the model actually contains long holds");
    CHECK(fired * 50 < old_would_fire, "the tap prefix blocks the overwhelming majority");
}

static void test_gesture_selects_by_tap_count(void)
{
    /* The point of the generalisation: the same rhythm, a different count, a different
       action — and a count that means nothing does nothing. */
    gesture_t g;
    gesture_reset(&g);
    CHECK(do_sequence_n(&g, GESTURE_TAPS_REBOOT, 200, GESTURE_HOLD_MS + 200) == GESTURE_REBOOT,
          "three taps then hold reboots");
    gesture_reset(&g);
    CHECK(do_sequence_n(&g, GESTURE_TAPS_FORM, 200, GESTURE_HOLD_MS + 200) == GESTURE_FORM,
          "four taps then hold swaps the body");
    gesture_reset(&g);
    CHECK(do_sequence_n(&g, GESTURE_TAPS_CALIBRATE, 200, GESTURE_HOLD_MS + 200) ==
              GESTURE_CALIBRATE,
          "five taps then hold calibrates");
    /* Every count maps to at most one action, and an unassigned count does nothing at all. */
    for (int n = 1; n <= GESTURE_TAPS_MAX; n++) {
        if (n == GESTURE_TAPS_REBOOT || n == GESTURE_TAPS_FORM || n == GESTURE_TAPS_CALIBRATE) {
            continue;
        }
        gesture_reset(&g);
        CHECK(do_sequence_n(&g, n, 200, GESTURE_HOLD_MS + 200) == GESTURE_NONE,
              "a count that selects nothing does nothing");
    }
    CHECK(GESTURE_TAPS_REBOOT != GESTURE_TAPS_FORM &&
              GESTURE_TAPS_FORM != GESTURE_TAPS_CALIBRATE &&
              GESTURE_TAPS_REBOOT != GESTURE_TAPS_CALIBRATE,
          "no two actions share a tap count");
}

static void test_gesture_five_taps_survive_the_reboot_threshold(void)
{
    /* The trap this design avoids: if the hold armed on the PRESS EDGE, the fourth tap would
       arm a reboot and its release would clear the count, so five taps could never be
       reached at all. Deciding on press DURATION instead is what makes both live together. */
    gesture_t g;
    gesture_reset(&g);
    for (int i = 0; i < GESTURE_TAPS_CALIBRATE; i++) {
        CHECK(press_for(&g, 120) == GESTURE_NONE, "a short tap never fires anything");
        idle_for(&g, 200);
    }
    CHECK(g.taps == GESTURE_TAPS_CALIBRATE, "all five taps counted");
}

static void test_gesture_no_cue_for_a_count_that_does_nothing(void)
{
    /* Growing a bar promises an action. Four taps then a hold has none, so it must not. */
    gesture_t g;
    gesture_reset(&g);
    /* Every count from one to the maximum that is NOT assigned an action. Hard-coding a
       number here was wrong the moment four stopped being dead. */
    for (int n = 1; n <= GESTURE_TAPS_MAX; n++) {
        if (n == GESTURE_TAPS_REBOOT || n == GESTURE_TAPS_FORM || n == GESTURE_TAPS_CALIBRATE) {
            continue;
        }
        gesture_reset(&g);
        for (int i = 0; i < n; i++) {
            press_for(&g, 120);
            idle_for(&g, 200);
        }
        gesture_poll(&g, true, true, DT);
        for (int t = DT; t < GESTURE_CUE_MS + 500; t += DT) gesture_poll(&g, false, true, DT);
        CHECK(gesture_cue(&g) == 0.0f, "no cue for a hold that will do nothing");
    }
}

static void test_gesture_cue(void)
{
    gesture_t g;
    gesture_reset(&g);
    for (int i = 0; i < GESTURE_TAPS_REBOOT; i++) {
        press_for(&g, 120);
        idle_for(&g, 200);
    }
    gesture_poll(&g, true, true, DT);
    CHECK(gesture_cue(&g) == 0.0f, "no cue before the threshold");
    for (int t = DT; t < GESTURE_CUE_MS + 200; t += DT) gesture_poll(&g, false, true, DT);
    const float c = gesture_cue(&g);
    CHECK(c > 0.0f && c < 1.0f, "the cue grows during the hold");
}

int main(void)
{
    fb = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(fb != NULL, "framebuffer allocated");

    test_emotion_mirror();
    test_emotion_asymmetry();
    test_emotion_distinct();
    test_emotion_bad_key();
    test_approach();
    test_idle_never_still();
    test_gag_has_a_hold();
    test_peekaboo();
    test_every_action_moves();
    test_pick_always_answers();
    test_cooldown_suppresses_recency();
    test_pool_spreads();
    test_penalty();
    test_the_default_form_is_the_ostrich();
    test_both_forms_draw_and_differ();
    test_draw_produces_a_robot();
    test_every_face_and_action_draws();
    test_blink_is_a_line_not_a_hole();
    test_samples_need_agreement_not_just_count();
    test_samples_use_the_median_not_the_mean();
    test_samples_recover_from_a_bad_start();
    test_samples_always_yield_something();
    test_samples_beat_single_taps_on_a_noisy_panel();
    test_agreement_removes_the_bad_tail();
    test_calib_identity_until_built();
    test_calib_exact_at_the_knots();
    test_calib_corrects_the_edges();
    test_calib_extends_past_the_outer_targets();
    test_calib_is_monotone_everywhere();
    test_calib_rejects_a_folded_grid();
    test_calib_round_trips_through_nvs();
    test_zones_hit_the_right_parts();
    test_zones_follow_the_flip();
    test_zones_follow_the_lean();
    test_every_zone_is_reachable();
    test_every_pool_answers();
    test_gesture_happy_path();
    test_gesture_hold_alone_does_nothing();
    test_gesture_slow_taps_do_not_count();
    test_gesture_long_taps_do_not_count();
    test_gesture_release_abandons();
    test_gesture_fires_once();
    test_gesture_child_mashing();
    test_gesture_selects_by_tap_count();
    test_gesture_five_taps_survive_the_reboot_threshold();
    test_gesture_no_cue_for_a_count_that_does_nothing();
    test_gesture_cue();

    free(fb);
    printf("ok — %d checks\n", checks);
    return 0;
}
