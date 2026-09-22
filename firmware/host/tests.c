/* Host tests for the firmware's pure-C modules.
 *
 * Mirrors `frontend/src/pet/{face,rig,variants}.test.ts`, because the panel runs a port of
 * those modules and a port that drifts from its reference is worse than no port. Each case
 * below asserts a property the source file's comments claim — not the values, which are
 * transcribed and would only be re-typed here, but the INVARIANTS, which are what a
 * transcription can silently break.
 */

#include <assert.h>
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "calib.h"
#include "caption.h"
#include "emotion.h"
#include "face.h"
#include "font.h"
#include "gesture.h"
#include "orient.h"
#include "rig.h"
#include "variants.h"
#include "vocab.h"

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

/* ---- what is actually ON THE GLASS -------------------------------------------------
 *
 * Everything above this line compares POSE STRUCTS, and that is how four shipped actions
 * came to draw nothing at all. `wave` moved one float, so `test_every_action_moves` passed
 * while the arm it posed was hidden behind a head 216 px wide. `blush` set `fig.extra`, so
 * the same test passed while the cheeks were painted over by the head drawn after them —
 * ZERO pixels changed on the robot. `hide` drove `hands_up` to 1.0 while the hands it aimed
 * at the eyes were too small to cover them and the ostrich's wing was drawn behind the head
 * it was meant to be hiding.
 *
 * So these tests render frames and compare PIXELS. That is the only probe that can see the
 * difference between posing an action and performing one. */

static long frame_delta(const uint16_t *a, const uint16_t *b)
{
    long n = 0;
    for (long i = 0; i < (long)FACE_W * FACE_H; i++)
        if (a[i] != b[i]) n++;
    return n;
}

static void test_every_action_changes_the_picture(void)
{
    /* The floor is deliberately low — `blush` is two 16 px discs and can never reach what a
       whole-figure squash reaches — but it is far above nothing, and the three broken
       actions scored 0, 0 and 1525 against it. */
    const long FLOOR = 900;
    uint16_t *idle = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(idle != NULL, "scratch frame allocated");

    for (int f = 0; f < FORM_COUNT; f++) {
        for (int a = ACT_NONE + 1; a < ACT_COUNT; a++) {
            long best = 0;
            for (int step = 0; step <= 20; step++) {
                const float q = (float)step / 20.0f;
                face_state_t st;
                face_rest(&st);
                st.form = (face_form_t)f;
                rig_for(ACT_NONE, q, 1.0f, 0, &st.rig);
                rig_figure(ACT_NONE, q, 1.0f, 0, 0.0f, &st.fig);
                face_draw(fb, 0, &st);
                memcpy(idle, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

                rig_for((action_t)a, q, 1.0f, 0, &st.rig);
                rig_figure((action_t)a, q, 1.0f, 0, 0.0f, &st.fig);
                face_draw(fb, 0, &st);
                const long d = frame_delta(idle, fb);
                if (d > best) best = d;
            }
            CHECK(best >= FLOOR, "every action changes real pixels on both forms");
        }
    }
    free(idle);
}

/* ---- the bird's own motion ---------------------------------------------------------- */

/* Render `a` on `form` at progress `q` with the clock at `t_ms`, into `dst`. */
static void render_at(uint16_t *dst, face_form_t form, action_t a, float q, uint32_t t_ms)
{
    face_state_t st;
    face_rest(&st);
    st.form = form;
    rig_for(a, q, 1.0f, t_ms, &st.rig);
    rig_figure(a, q, 1.0f, t_ms, 0.0f, &st.fig);
    face_draw(dst, 0, &st);
}

static void test_the_bird_moves_between_frames(void)
{
    /* `test_every_action_changes_the_picture` asks whether an action differs from IDLE, which
       a single frozen pose satisfies. A child watching does not compare against an idle they
       never see — they see one frame after another, and a pose that holds still for a second
       reads as a hang. So this measures FRAME TO FRAME.

       The ostrich failed it badly before the bird channels: `wave` moved 999 px between its
       biggest consecutive frames and `blush` 465, because everything on this form was driven
       off arm angles a bird does not have. The floor is set under the weakest action that now
       passes (`blush`, 1712) and over the strongest that did not (`wave`, 999).

       THE BIRD ONLY, and the floor is why. The robot's `blush` moves 1146 px between frames —
       it is nearly all static pink over a breathing body, which is what that action IS on that
       form. Holding the robot to a number calibrated for the bird would mean either loosening
       the floor until the pre-fix ostrich slips under it, or changing an approved robot
       animation to satisfy a test about a different form. The robot is unchanged by this work
       and `test_every_action_changes_the_picture` still covers it. */
    const long FLOOR = 1400;
    uint16_t *prev = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(prev != NULL, "scratch frame allocated");

    {
        const int f = FORM_OSTRICH;
        for (int a = ACT_NONE + 1; a < ACT_COUNT; a++) {
            const int dur = rig_spec((action_t)a)->dur_ms;
            long best = 0;
            for (int step = 0; step <= 40; step++) {
                const float q = (float)step / 40.0f;
                const uint32_t t = (uint32_t)(q * (float)dur);
                render_at(fb, (face_form_t)f, (action_t)a, q, t);
                if (step > 1) {
                    const long d = frame_delta(prev, fb);
                    if (d > best) best = d;
                }
                memcpy(prev, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
            }
            CHECK(best >= FLOOR, "every action moves the bird between consecutive frames");
        }
    }
    free(prev);
}

static void test_the_three_dances_differ_on_the_bird(void)
{
    /* `dance`, `bop` and `shimmy` share one arm pose in `rig_for` — three names for one robot
       animation, which is a decision about the ROBOT. The ostrich has no arms, so inheriting
       it meant the bird performed the identical animation under three different words: at a
       matched clock the frames were byte-for-byte equal. They are separate cases in
       `bird_channels` now, and this is what stops them merging again. */
    uint16_t *other = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(other != NULL, "scratch frame allocated");
    const action_t DANCES[] = {ACT_DANCE, ACT_BOP, ACT_SHIMMY};

    for (unsigned i = 0; i < 3; i++) {
        for (unsigned j = i + 1; j < 3; j++) {
            long best = 0;
            for (int step = 0; step <= 20; step++) {
                const float q = (float)step / 20.0f;
                render_at(fb, FORM_OSTRICH, DANCES[i], q, (uint32_t)(step * 100));
                memcpy(other, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
                render_at(fb, FORM_OSTRICH, DANCES[j], q, (uint32_t)(step * 100));
                const long d = frame_delta(other, fb);
                if (d > best) best = d;
            }
            CHECK(best > 3000, "the bird's three dances are three different dances");
        }
    }
    free(other);
}

/* The lit span on one figure-space row, as [lo, hi]; hi < lo when the row is empty. */
static void lit_span(int dy, int *lo, int *hi)
{
    const int y = FACE_H / 2 + dy;
    *lo = FACE_W;
    *hi = -1;
    for (int x = 0; x < FACE_W; x++) {
        if (fb[y * FACE_W + x] == 0) continue;
        if (x < *lo) *lo = x;
        *hi = x;
    }
}

static void test_the_bird_keeps_its_head_on_its_neck(void)
{
    /* The neck segments were drawn at a bare `ox` while the head sat at `ox + tilt`, so a head
       tilt slid the head sideways and left the neck behind — the join SMEARED rather than
       leaned. It is invisible to any test that counts pixels, because no pixels are lost; they
       move. It shows up as the width of the row at the join:

                          tilt 0        tilt +14
         before          170..206 (37)  170..218 (49)   left edge pinned, right edge dragged
         after           170..206 (37)  185..221 (37)   translates, keeps its width

       So: the join may LEAN, but it may not get wider. dy -60 is the topmost row that is neck
       rather than head — the head reaches down to about -70, which is the trap that made an
       earlier version of this test measure the head twice and conclude there was no bug. */
    int lo0, hi0;
    face_state_t st;
    face_rest(&st);
    st.form = FORM_OSTRICH;
    face_draw(fb, 0, &st);
    lit_span(-60, &lo0, &hi0);
    const int w0 = hi0 - lo0 + 1;
    CHECK(w0 > 20, "the neck is visible below the head");

    for (int sgn = -1; sgn <= 1; sgn += 2) {
        face_rest(&st);
        st.form = FORM_OSTRICH;
        rig_figure(ACT_NONE, 0.0f, 1.0f, 0, (float)sgn * 14.0f, &st.fig);
        face_draw(fb, 0, &st);
        int lo, hi;
        lit_span(-60, &lo, &hi);
        const int w = hi - lo + 1;
        CHECK(w <= w0 + 3, "a tilt leans the neck rather than smearing the join");
        CHECK(lo != lo0 || hi != hi0, "a tilt actually moves the neck");
    }
}

static void test_the_side_mounted_fit_stays_in_the_square(void)
{
    /* THE PANEL MOUNTED WITH ITS CABLE OUT THE SIDE. A quarter turn maps a SQUARE onto
       itself, which is the only reason the rotated blit needs no second framebuffer — so the
       figure renders into a 368x368 region of the 368x448 frame, scaled by 368/448.
       
       TWO DIFFERENT QUESTIONS, and the first version of this test asked only the strict one
       and failed. Nothing may clip AT REST — a pet that is cropped while standing still is
       simply drawn wrong. But eliminating clipping at the PEAK OF A GAG needs scale 0.66, a
       third smaller than portrait, and §10.4at already rejected that exact trade for the
       portrait figure: "shrinking the approved bird by a sixth to save an average of four
       pixels a frame, on a figure already 428 px tall in a 448 px panel, is the wrong trade
       on a 29 mm screen; a cropped toe at the peak of a gag reads as energy." Shrinking by a
       third to save a crest tip during a boing is the same trade and worse.
       
       So: strict at rest, and bounded by the tolerance portrait already accepts elsewhere. */
    const int SQ = FACE_W;
    const int SQ_Y0 = (FACE_H - SQ) / 2;
    face_set_fit((float)SQ / (float)FACE_H, SQ_Y0 + (int)(SQ * 0.545f));

    for (int f = 0; f < FORM_COUNT; f++) {
        face_state_t st;
        face_rest(&st);
        st.form = (face_form_t)f;
        face_draw(fb, 0, &st);
        int x0, x1, y0, y1;
        bbox(&x0, &x1, &y0, &y1);
        CHECK(y0 >= SQ_Y0 && y1 < SQ_Y0 + SQ, "at rest the figure is wholly inside the square");
    }

    int poses = 0, clipped = 0;
    for (int f = 0; f < FORM_COUNT; f++) {
        for (int a = ACT_NONE + 1; a < ACT_COUNT; a++) {
            for (int step = 0; step <= 16; step++) {
                const float q = (float)step / 16.0f;
                face_state_t st;
                face_rest(&st);
                st.form = (face_form_t)f;
                rig_for((action_t)a, q, 1.0f, (uint32_t)(step * 200), &st.rig);
                rig_figure((action_t)a, q, 1.0f, (uint32_t)(step * 200), st.eyes.face_ang,
                           &st.fig);
                face_draw(fb, 0, &st);
                int x0, x1, y0, y1;
                bbox(&x0, &x1, &y0, &y1);
                if (y1 < y0) continue;
                poses++;
                if (y0 < SQ_Y0 || y1 >= SQ_Y0 + SQ) clipped++;
            }
        }
    }
    face_set_fit(1.0f, -1); /* leave the renderer as every other test expects it */
    /* Measured at 11.8% when this landed. The gate is the 15% §10.4at accepted for the
       portrait figure's own clipping, so a change that makes the side-mounted pet visibly
       more cropped than the portrait one has to argue for itself here. */
    CHECK(poses > 500, "enough poses sampled to mean anything");
    CHECK(clipped * 100 <= poses * 15, "a side-mounted gag clips no worse than portrait does");
}

/* The quarter turn itself, as `display.c` performs it. Duplicated here rather than shared
   because that file is full of ESP headers and this suite is the only place the index maths
   can be CHECKED rather than reasoned about — and an off-by-one in a permutation is a
   mirrored pet, which looks deliberate. */
static void rotate_square(const uint16_t *src, uint16_t *dst, bool clockwise)
{
    const int SQ = FACE_W, SQ_Y0 = (FACE_H - SQ) / 2, CS = 16;
    for (int x0 = 0; x0 < FACE_W; x0 += CS) {
        for (int c = 0; c < CS; c++) {
            const int x = x0 + c;
            const uint16_t *s = clockwise ? &src[(size_t)(SQ_Y0 + x) * FACE_W + (SQ - 1)]
                                          : &src[(size_t)(SQ_Y0 + SQ - 1 - x) * FACE_W];
            const int step = clockwise ? -1 : 1;
            for (int r = 0; r < SQ; r++) dst[(SQ_Y0 + r) * FACE_W + x] = s[r * step];
        }
    }
}

static void test_a_quarter_turn_is_a_permutation(void)
{
    /* Two properties, and together they are the whole correctness argument for the rotated
       blit: every destination pixel comes from exactly one source pixel (nothing is invented
       and nothing is lost), and turning one way then the other returns the original. */
    const int SQ = FACE_W, SQ_Y0 = (FACE_H - SQ) / 2;
    uint16_t *cw = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    uint16_t *back = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    uint16_t *src = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(cw != NULL && back != NULL && src != NULL, "scratch frames allocated");

    /* A pattern where every pixel in the square is distinct, so a duplicate or a dropped
       pixel cannot hide behind a neighbour of the same colour. */
    for (int y = 0; y < FACE_H; y++) {
        for (int x = 0; x < FACE_W; x++) src[y * FACE_W + x] = (uint16_t)(y * FACE_W + x);
    }
    memset(cw, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    rotate_square(src, cw, true);
    rotate_square(cw, back, false);

    long same = 0;
    for (int y = SQ_Y0; y < SQ_Y0 + SQ; y++) {
        for (int x = 0; x < FACE_W; x++) {
            if (back[y * FACE_W + x] == src[y * FACE_W + x]) same++;
        }
    }
    CHECK(same == (long)SQ * FACE_W, "clockwise then anticlockwise is the identity");

    /* A corner, by hand, because "it round-trips" is also true of doing nothing. */
    CHECK(cw[SQ_Y0 * FACE_W + 0] == src[SQ_Y0 * FACE_W + (SQ - 1)],
          "the square's top-right corner turns into its top-left");
    free(cw);
    free(back);
    free(src);
}

/* The inverse of `rotate_square`, as `display.c`'s `panel_to_frame` computes it. Duplicated
   for the same reason the rotation is: this is the only place the index maths can be checked
   rather than reasoned about, and these two functions are only correct as a PAIR. */
static void panel_to_frame(int quarter, int px, int py, int *fx, int *fy)
{
    const int SQ = FACE_W, SQ_Y0 = (FACE_H - SQ) / 2;
    switch (quarter) {
    case 1:
        *fx = SQ_Y0 + SQ - 1 - py;
        *fy = SQ_Y0 + px;
        break;
    case 3:
        *fx = py - SQ_Y0;
        *fy = SQ_Y0 + SQ - 1 - px;
        break;
    default:
        *fx = px;
        *fy = py;
        break;
    }
}

static void test_a_tap_lands_where_the_pixel_it_touched_came_from(void)
{
    /* THE OWNER: "while horizontal the touch screen indicators do not indicate where I
       actually tapped, it's like rotated 90 degrees or something." They were rotated 90
       degrees, exactly — by the blit, on the way out. The touch controller reports a point on
       the GLASS; the figure lives in the frame; and for two releases everything downstream of
       a finger read the first as if it were the second.

       The property is stronger than "there is a mapping": the mapping has to be the exact
       inverse of the one the blit applies, so the test asks the rotation itself. For every
       pixel of glass inside the square, the frame pixel `panel_to_frame` names must be the one
       the rotated blit actually put there. A sign error, a transposition or an off-by-one all
       fail this and all of them look deliberate on a screen. */
    const int SQ = FACE_W, SQ_Y0 = (FACE_H - SQ) / 2;
    uint16_t *src = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    uint16_t *out = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(src != NULL && out != NULL, "scratch frames allocated");
    for (int y = 0; y < FACE_H; y++) {
        for (int x = 0; x < FACE_W; x++) src[y * FACE_W + x] = (uint16_t)(y * FACE_W + x);
    }

    for (int q = 1; q <= 3; q += 2) {
        memset(out, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
        rotate_square(src, out, q == 1);
        long agree = 0, checked = 0;
        for (int py = SQ_Y0; py < SQ_Y0 + SQ; py++) {
            for (int px = 0; px < FACE_W; px++) {
                int fx, fy;
                panel_to_frame(q, px, py, &fx, &fy);
                checked++;
                if (fx < 0 || fx >= FACE_W || fy < 0 || fy >= FACE_H) continue;
                if (out[py * FACE_W + px] == src[fy * FACE_W + fx]) agree++;
            }
        }
        CHECK(agree == checked, "every tap maps to the frame pixel the blit drew there");
    }

    /* Upright and upside down are the identity, the second only because the marker is drawn
       AFTER `flip_frame` and the panel is then physically turned over. Two reversals that
       cancel — the same argument the lean needed, and the same one that is easy to talk
       yourself out of. */
    for (int q = 0; q <= 2; q += 2) {
        int fx, fy;
        panel_to_frame(q, 17, 300, &fx, &fy);
        CHECK(fx == 17 && fy == 300, "upright and inverted need no mapping here");
    }
    free(src);
    free(out);
}

static void test_the_zones_follow_the_scaled_figure(void)
{
    /* The quiet half of the same complaint. `face_zone` answers WHERE ON THE FIGURE a finger
       landed, and side-mounted the figure is a sixth smaller and sits at a different origin —
       so a zone test that ignored the fit was asking about a figure that is not on the glass.
       Poking the bird's neck answered as a leg, and nothing said so.

       The invariant: a point and the figure move TOGETHER. Take a point in portrait, put it
       through the same transform `face_draw` puts the drawing through, and the zone must not
       change. These are the numbers `display.c` passes to `face_set_fit`. */
    const int SQ = FACE_W, SQ_Y0 = (FACE_H - SQ) / 2;
    const float fit = (float)SQ / (float)FACE_H;
    const int fit_oy = SQ_Y0 + (int)(SQ * 0.545f);
    const int OX = FACE_W / 2, OY = (int)(FACE_H * 0.545f);

    for (int f = 0; f < FORM_COUNT; f++) {
        long agree = 0, sampled = 0, on_the_figure = 0;
        for (int y = 0; y < FACE_H; y += 3) {
            for (int x = 0; x < FACE_W; x += 3) {
                face_set_fit(1.0f, -1);
                const face_zone_t up = face_zone((face_form_t)f, x, y, false, 0);
                /* Where `face_draw` puts that same bit of figure when it is scaled. */
                const int sx = OX + (int)lrintf((float)(x - OX) * fit);
                const int sy = fit_oy + (int)lrintf((float)(y - OY) * fit);
                face_set_fit(fit, fit_oy);
                const face_zone_t side = face_zone((face_form_t)f, sx, sy, false, 0);
                sampled++;
                if (up != ZONE_NONE) on_the_figure++;
                if (up == side) agree++;
            }
        }
        face_set_fit(1.0f, -1);
        CHECK(on_the_figure > 800, "enough of the grid lands on the figure to mean anything");
        /* Not 100%: the round trip through two lrintf calls moves a point by up to half a
           pixel, which flips the verdict for points sitting exactly on a zone boundary. The
           old code disagreed on 30% of the grid; a budget of 2% catches any return of that
           while tolerating the rounding. */
        CHECK((sampled - agree) * 100 <= sampled * 2,
              "a zone does not move when the figure is scaled into the square");
    }
}

/* The largest symmetric lean at which EVERY form is still drawn whole, under the fit already
   set. Measured rather than asserted, because the answer depends on the drawn silhouette and
   the silhouettes change. */
static int clean_lean_limit(void)
{
    for (int lean = 0; lean <= 240; lean++) {
        for (int f = 0; f < FORM_COUNT; f++) {
            for (int sign = -1; sign <= 1; sign += 2) {
                face_state_t st;
                face_rest(&st);
                st.form = (face_form_t)f;
                st.lean = sign * lean;
                face_draw(fb, 0, &st);
                int x0, x1, y0, y1;
                bbox(&x0, &x1, &y0, &y1);
                if (x0 <= 0 || x1 >= FACE_W - 1) return lean - 1;
            }
        }
    }
    return 240;
}

static void test_the_lean_limits_are_the_room_that_exists(void)
{
    /* The owner, side-mounted: "he should be able to tilt and slide all over to the right and
       I'll put it to the left, not restrained as much."

       WHAT THIS TEST HAD TO STOP ASSERTING. The first version demanded the figure be wholly
       on screen at full lean, and portrait failed it — the shipped ±60 is already 30% past
       the point where the ostrich's tail crosses the left edge, and has been since it landed.
       That is deliberate, and §10.4at is where it was argued: "a cropped toe at the peak of a
       gag reads as energy" on a 29 mm screen. An invariant the shipped product violates is
       not an invariant, it is a bug report about the test.

       So the real property is PROPORTION. Measure the room each fit actually has, and require
       the side-mounted limit to be as generous as portrait's and no more — which is what
       makes 110 a measurement (85 x 60/46) rather than a number that felt about right. */
    const int SQ = FACE_W, SQ_Y0 = (FACE_H - SQ) / 2;

    face_set_fit(1.0f, -1);
    const int room_up = clean_lean_limit();
    face_set_fit((float)SQ / (float)FACE_H, SQ_Y0 + (int)(SQ * 0.545f));
    const int room_side = clean_lean_limit();
    face_set_fit(1.0f, -1);

    CHECK(room_up > 20 && room_side > room_up,
          "scaling into the square really does buy sideways room");
    /* Portrait's shipped generosity, as a ratio, is the budget the side limit may spend. */
    CHECK(FACE_LEAN_MAX * room_side <= FACE_LEAN_MAX_SIDE * room_up + room_up,
          "the side-mounted limit is at least as generous as portrait's");
    CHECK(FACE_LEAN_MAX_SIDE * room_up <= FACE_LEAN_MAX * room_side + room_side,
          "and no more generous, so the pet is not cropped worse for being on its side");

    /* And the crop that generosity buys stays small. At full lean the figure still covers most
       of the width it covers at rest — a pet sliding off the edge is not a lean. */
    for (int side = 0; side < 2; side++) {
        face_set_fit(side ? (float)SQ / (float)FACE_H : 1.0f,
                     side ? SQ_Y0 + (int)(SQ * 0.545f) : -1);
        for (int f = 0; f < FORM_COUNT; f++) {
            face_state_t st;
            face_rest(&st);
            st.form = (face_form_t)f;
            face_draw(fb, 0, &st);
            int x0, x1, y0, y1;
            bbox(&x0, &x1, &y0, &y1);
            const int rest_w = x1 - x0;
            for (int sign = -1; sign <= 1; sign += 2) {
                face_rest(&st);
                st.form = (face_form_t)f;
                st.lean = sign * (side ? FACE_LEAN_MAX_SIDE : FACE_LEAN_MAX);
                face_draw(fb, 0, &st);
                bbox(&x0, &x1, &y0, &y1);
                CHECK((x1 - x0) * 100 >= rest_w * 88, "full lean crops a sliver, not a limb");
            }
        }
    }
    face_set_fit(1.0f, -1); /* leave the renderer as every other test expects it */
}

/* Gravity at `deg` around the XY plane, at one g, as the accelerometer would report it. */
static void gravity_at(float deg, int *ax, int *ay)
{
    const float r = deg * (float)M_PI / 180.0f;
    *ax = (int)lrintf(cosf(r) * 8192.0f);
    *ay = (int)lrintf(sinf(r) * 8192.0f);
}

static void test_the_orientation_needs_a_band_crossed_on_purpose(void)
{
    /* The owner: "the tilt going to 90 and causing an orientation change shouldn't happen right
       at 45. We should have like an extra 20 you should have to go, and then another 20 back
       past that 45 to go back the other way."

       THE OLD CODE HAD NO HYSTERESIS AT THE BOUNDARY, which is easy to miss because it looks
       like it does: `FLIP_THRESHOLD` gated how much gravity was in the plane, but WHICH quarter
       came from `|ax| > |ay|`, and that turns over at exactly 45 degrees. Held at 45 the two
       axes are equal and noise picks the orientation, several times a second. */
    int ax, ay;

    /* Upright stays upright well past the old boundary. */
    for (float d = 0.0f; d <= 64.0f; d += 4.0f) {
        gravity_at(d, &ax, &ay);
        CHECK(orient_quarter(0, ax, ay) == 0, "upright holds past 45 degrees");
    }
    gravity_at(70.0f, &ax, &ay);
    CHECK(orient_quarter(0, ax, ay) == 3, "and gives way once the band is crossed");

    /* AND IT IS STICKY THE OTHER WAY TOO, which is the half that stops the oscillation. Having
       landed in 3, coming back must go well past 45 before returning — at 40 degrees, which the
       old code would already have called upright, it stays. */
    gravity_at(40.0f, &ax, &ay);
    CHECK(orient_quarter(3, ax, ay) == 3, "the new orientation holds coming back");
    gravity_at(20.0f, &ax, &ay);
    CHECK(orient_quarter(3, ax, ay) == 0, "until it too has crossed the band");

    /* THE PROPERTY THAT MATTERS: sitting exactly on the boundary, nothing changes — whichever
       quarter you were in, you stay in. This is the check the old comparison fails outright. */
    gravity_at(45.0f, &ax, &ay);
    CHECK(orient_quarter(0, ax, ay) == 0, "held at 45 from upright, stay upright");
    CHECK(orient_quarter(3, ax, ay) == 3, "held at 45 from landscape, stay landscape");

    /* Noise on the boundary must not flip it either — the actual symptom. */
    int flips = 0, last = 0;
    for (int i = 0; i < 400; i++) {
        gravity_at(45.0f + (float)((i * 7919) % 41 - 20) * 0.05f, &ax, &ay);
        const int q = orient_quarter(last, ax, ay);
        if (q != last) flips++;
        last = q;
    }
    CHECK(flips == 0, "and jitter on the boundary never flips it");

    /* Flat on its back is not an orientation: hold whatever was being drawn. */
    CHECK(orient_quarter(2, 100, -80) == 2, "a flat panel keeps the orientation it had");
    CHECK(orient_quarter(1, 0, 0) == 1, "including a perfectly still one");

    /* All four are reachable, and each from its own centre. */
    for (int q = 0; q < 4; q++) {
        static const float CENTRE[4] = {0.0f, 270.0f, 180.0f, 90.0f};
        gravity_at(CENTRE[q], &ax, &ay);
        CHECK(orient_quarter(q, ax, ay) == q, "every quarter is stable at its own centre");
        /* And is reached from the opposite one, rather than stepping round through a
           neighbour: a panel set down and picked up the other way should land where it is. */
        const int opposite = (q + 2) % 4;
        CHECK(orient_quarter(opposite, ax, ay) == q, "and reachable from the far side");
    }
}

static void test_the_open_mouth_is_the_smile_opening(void)
{
    /* The owner: "when changing to the robot, when it speaks the happy face doesn't go away
       while the mouth appears, which looks really weird."

       The first version drew the opening as a rounded BOX under the smile arc, on the theory
       that the smile would read as its lip. It does not — a curved smile with a rectangle
       below it reads as two mouths, because that is what it is.

       THE PROPERTY THAT SEPARATES THEM IS THE CORNERS. A real mouth closes where the lips
       meet, so the opening tapers to nothing at each end; a box is full height right out to
       its edge. Comparing a column at the centre against one near the corner catches that,
       and would have caught it before the owner had to. */
    face_state_t st;
    face_rest(&st);
    st.form = FORM_ROBOT;
    face_draw(fb, 0, &st);
    uint16_t *shut = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(shut != NULL, "scratch frame allocated");
    memcpy(shut, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

    st.talk = 1.0f;
    face_draw(fb, 0, &st);

    /* Per-column change, across the whole frame — the mouth is the only thing that moved. */
    int col[FACE_W];
    memset(col, 0, sizeof(col));
    int widest = 0;
    for (int x = 0; x < FACE_W; x++) {
        for (int y = 0; y < FACE_H; y++) {
            if (fb[y * FACE_W + x] != shut[y * FACE_W + x]) col[x]++;
        }
        if (col[x] > col[widest]) widest = x;
    }
    CHECK(col[widest] > 10, "the mouth opens somewhere");

    /* Walk out to the edge of the opening and check it closed rather than stopped. */
    int edge = widest;
    while (edge + 1 < FACE_W && col[edge + 1] > 0) edge++;
    const int span = edge - widest;
    CHECK(span > 8, "the opening is wide enough to have corners at all");
    /* NINE TENTHS OF THE WAY OUT, and the fraction is measured rather than picked. The lens
       profile runs 23 px at the centre and 8 at 90% — a rounded box of the same width is
       still near full height there, because its corner radius only bites in the last fifth.
       Checked on BOTH sides, since a taper on one is a shape that slid rather than a mouth
       that opened. */
    CHECK(col[widest + (span * 9) / 10] * 2 < col[widest],
          "the opening tapers toward the corner, as lips do");
    CHECK(col[widest - (span * 9) / 10] * 2 < col[widest], "and toward the other corner");
    free(shut);
}

static void test_the_shuffle_is_driven_by_distance_not_by_a_clock(void)
{
    /* The owner: "the robot should kind of shuffle his legs back and forth as tilt causes him
       to move left and right."

       THE PROPERTY THAT MATTERS IS THE ONE A TIMER WOULD FAIL. A timed wiggle gated on "is he
       moving" looks the same in a screenshot and is wrong in the hand: it keeps stepping after
       he stops and takes the same number of steps to cross the panel slowly as quickly. So the
       phase advances with PIXELS TRAVELLED, and these checks are about that relationship
       rather than about the legs wiggling at all. */
    rig_pose_t base;
    rig_for(ACT_NONE, 0.0f, 1.0f, 0, &base);

    /* Standing still leaves the action's pose exactly alone. */
    rig_walk_t still = {0};
    rig_pose_t p = base;
    for (int i = 0; i < 40; i++) rig_walk(&still, 0.0f, &p);
    CHECK(p.leg_l == base.leg_l && p.leg_r == base.leg_r, "a stationary pet does not shuffle");
    CHECK(p.step == base.step, "and its feet stay down");

    /* Moving swings the legs, and in OPPOSITE directions — one stride, not two legs doing the
       same thing, which is what a careless `+=` on both would give. */
    rig_walk_t going = {0};
    p = base;
    float apart = 0.0f;
    for (int i = 0; i < 12; i++) {
        p = base;
        rig_walk(&going, 4.0f, &p);
        const float dl = p.leg_l - base.leg_l, dr = p.leg_r - base.leg_r;
        if (dl * dr < 0.0f) apart += 1.0f;
    }
    CHECK(apart >= 8.0f, "the legs alternate rather than swinging together");
    CHECK(p.step != base.step, "and the bird lifts a foot too");

    /* THE HEADLINE: the same ground covered in different numbers of frames reaches the same
       point in the stride. A clock-driven phase fails this outright. */
    rig_walk_t slow = {0}, fast = {0};
    rig_pose_t q = base;
    for (int i = 0; i < 20; i++) rig_walk(&slow, 1.0f, &q);
    q = base;
    for (int i = 0; i < 4; i++) rig_walk(&fast, 5.0f, &q);
    const float diff = fabsf(slow.phase - fast.phase);
    CHECK(diff < 0.001f, "twenty pixels is twenty pixels, however many frames it took");

    /* And the faster crossing is the bigger stride, because amplitude follows speed. */
    CHECK(fast.amp > slow.amp, "a scramble swings wider than a drift");

    /* HE HAS TO COME HOME, and the owner is the one who found this: "if we're static and not
       moving very fast or kind of just sitting there, the legs need to be back in the neutral
       position." Two separate failures, so two separate checks.

       A CRAWL IS NOT A WALK. `s_lean` is smoothed and integer, so it converges by ever-smaller
       steps and the accelerometer nudges it a pixel at rest — a trickle that, without a floor,
       is indistinguishable from a very slow walk and leaves the legs parked mid-stride
       forever. */
    rig_walk_t crawl = {0};
    rig_pose_t c = base;
    for (int i = 0; i < 200; i++) {
        c = base;
        rig_walk(&crawl, 0.4f, &c);
    }
    CHECK(c.leg_l == base.leg_l && c.leg_r == base.leg_r, "a crawl leaves the legs standing");
    CHECK(crawl.phase == 0.0f, "and does not creep the stride along");

    /* AND STOPPING PUTS THEM BACK, promptly. The legs are neutral once the amplitude is zero,
       so this is really a check on how long that takes: the release used to be slow enough
       that a pet set down on a shelf stood there with one leg out. Half a second at 25 fps. */
    rig_walk_t stopping = {0};
    rig_pose_t d = base;
    for (int i = 0; i < 30; i++) rig_walk(&stopping, 5.0f, &d);
    CHECK(stopping.amp > 0.5f, "walking first, or the next check proves nothing");
    for (int i = 0; i < 15; i++) {
        d = base;
        rig_walk(&stopping, 0.0f, &d);
    }
    CHECK(d.leg_l == base.leg_l && d.leg_r == base.leg_r,
          "and about half a second after stopping he is standing again");
    CHECK(d.step == base.step, "with both feet down");

    /* The phase must not grow without bound: a panel left tilting accumulates travel forever,
       and a float large enough that one frame's addition rounds away stops the legs dead. */
    rig_walk_t forever = {0};
    q = base;
    for (int i = 0; i < 5000; i++) rig_walk(&forever, 7.0f, &q);
    CHECK(forever.phase <= 2.0f * (float)M_PI + 0.001f, "the phase wraps rather than drifting");
    CHECK(forever.phase >= 0.0f, "and stays positive");
}

static void test_the_mouth_moves_only_while_talking(void)
{
    /* The owner, after the first real conversation: "we should make an animation of the robot
       talking as it talks."

       TWO PROPERTIES, and the second is the one that catches a careless channel. The mouth has
       to actually change the drawn face — a `talk` field nothing reads would pass any test
       that only checked it compiles. And at talk 0 the face must be EXACTLY what it was
       before this existed, because a channel that leaks at rest changes every frame the pet
       has ever drawn. Both forms: the ostrich opens a beak, the robot opens a mouth, and a
       change that reaches only one of them is half a feature. */
    for (int f = 0; f < FORM_COUNT; f++) {
        face_state_t st;
        face_rest(&st);
        st.form = (face_form_t)f;
        CHECK(st.talk == 0.0f, "a resting pet is not talking");
        face_draw(fb, 0, &st);
        uint16_t *shut = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
        CHECK(shut != NULL, "scratch frame allocated");
        memcpy(shut, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

        st.talk = 1.0f;
        face_draw(fb, 0, &st);
        long moved = 0;
        for (long i = 0; i < (long)FACE_W * FACE_H; i++) {
            if (fb[i] != shut[i]) moved++;
        }
        /* 500, NOT 80, AND THE FLOOR IS THE POINT. The first version of this asserted 80 px
           and passed happily on a mouth the owner could not see moving from across the room:
           *"definitely not big enough or obvious enough."* A test whose threshold sits below
           the smallest thing a person would accept is not testing the thing it is named for.
           Measured after widening: 721 px on the ostrich, 838 on the robot — so 500 catches a
           regression toward subtle while leaving room to restyle. */
        CHECK(moved > 500, "an open mouth is visibly different from a shut one");

        /* And it is the MOUTH that moved, not the whole figure: the change sits in the head,
           which is the top half. A `talk` wired to the wrong offset would still differ. */
        long high = 0;
        for (int y = 0; y < FACE_H / 2; y++) {
            for (int x = 0; x < FACE_W; x++) {
                if (fb[y * FACE_W + x] != shut[y * FACE_W + x]) high++;
            }
        }
        CHECK(high * 10 >= moved * 9, "and the change is on the face, not the feet");

        st.talk = 0.0f;
        face_draw(fb, 0, &st);
        long leaked = 0;
        for (long i = 0; i < (long)FACE_W * FACE_H; i++) {
            if (fb[i] != shut[i]) leaked++;
        }
        CHECK(leaked == 0, "and a shut mouth draws exactly what it always did");
        free(shut);
    }
}

static void test_the_case_geometry_is_the_case(void)
{
    /* §10.4bx: the enclosure rounds the display into a squircle, so a pixel can be drawn
       perfectly and still be behind plastic. `frontend/src/pet/scale.ts` has masked the PWA
       preview to that shape since day one; the firmware did not, and the microphone-open dot
       spent its whole life hidden in the bottom-left corner as a result.

       THE DOT IS GONE NOW — the owner had it removed (0.2.66) — but the geometry is not, and
       it is what keeps the version label and anything drawn next to it out of the corners. So
       this pins `face_inside_case` itself: a predicate that returned true everywhere would
       have passed the old test just as happily, and would have been the more dangerous bug. */
    CHECK(!face_inside_case(0, 0, FACE_W, FACE_H), "the corner pixel is behind the case");
    CHECK(!face_inside_case(FACE_W - 1, 0, FACE_W, FACE_H), "and the opposite top one");
    CHECK(!face_inside_case(0, FACE_H - 1, FACE_W, FACE_H), "and both bottom ones");
    CHECK(!face_inside_case(FACE_W - 1, FACE_H - 1, FACE_W, FACE_H), "including that one");
    CHECK(!face_inside_case(10, FACE_H - 15, FACE_W, FACE_H),
          "and where the microphone dot used to sit, which is how this was found");
    CHECK(face_inside_case(FACE_W / 2, 0, FACE_W, FACE_H), "the top edge's middle is visible");
    CHECK(face_inside_case(0, FACE_H / 2, FACE_W, FACE_H), "so is the left edge's");
    CHECK(face_inside_case(FACE_W / 2, FACE_H / 2, FACE_W, FACE_H), "and the centre");
    CHECK(!face_inside_case(-1, 10, FACE_W, FACE_H), "off the panel is not on the panel");
    CHECK(!face_inside_case(10, FACE_H, FACE_W, FACE_H), "in either direction");

    /* The corner is a quarter circle, not a diagonal cut: a point on the arc is in, one just
       outside it is out, and a predicate that got the sense backwards fails both. */
    const int r = FACE_CASE_CORNER_R;
    CHECK(face_inside_case(r, r, FACE_W, FACE_H), "the arc's own centre is inside");
    CHECK(face_inside_case(r - (r * 70) / 100, r - (r * 70) / 100, FACE_W, FACE_H),
          "and a point just inside the arc");
    CHECK(!face_inside_case(r - (r * 72) / 100, r - (r * 72) / 100, FACE_W, FACE_H),
          "but not one just outside it");
}

static void test_the_caption_does_not_black_out_the_pet(void)
{
    /* THE TICKER USED TO CLEAR A FULL-WIDTH BLACK STRIP before drawing, and the reason was
       real — the ostrich's feet reach y=435 while the ticker runs at 432, so "PLAY PEEKABOO"
       ran straight through its toes and the bar was what made the words legible.

       The owner: "the text scrolling on the bottom ... the black in. It should be
       transparent." On an AMOLED a cleared row is OFF, so that bar was not a tint over the
       pet, it was a hard-edged hole punched through it.

       The legibility now comes from a halo on the glyphs instead. What that has to mean,
       measurably, is: the figure's pixels in the ticker's band SURVIVE the caption being
       drawn over them. Measured at 330 lit without a caption and 359 with one — the feet
       still there, the letters added. Under the old strip it was the letters alone. */
    const int SCALE = 2, MARGIN = 8, ROW_H = 7 * SCALE;
    const int y = FACE_H - MARGIN - ROW_H;

    face_state_t st;
    face_rest(&st);
    st.form = FORM_OSTRICH;
    face_draw(fb, 0, &st);
    long bare = 0;
    for (int r = y - 3; r < y + ROW_H + 3 && r < FACE_H; r++) {
        for (int c = 0; c < FACE_W; c++) {
            if (r >= 0 && fb[r * FACE_W + c]) bare++;
        }
    }
    CHECK(bare > 100, "the figure really is in the ticker's band, or this proves nothing");

    caption_t cap;
    caption_reset(&cap);
    caption_say(&cap, "play peekaboo");
    for (int i = 0; i < 30; i++) caption_tick(&cap, (uint32_t)(i * 40), FACE_W);
    face_draw(fb, 0, &st);
    caption_draw(&cap, fb, FACE_W, FACE_H, 0xFFFF);
    long over = 0;
    for (int r = y - 3; r < y + ROW_H + 3 && r < FACE_H; r++) {
        for (int c = 0; c < FACE_W; c++) {
            if (r >= 0 && fb[r * FACE_W + c]) over++;
        }
    }
    /* The halo erodes a few of the figure's own pixels where a letter sits on a toe, which
       is the point of it; wholesale erasure is what this forbids. */
    CHECK(over * 10 >= bare * 9, "the pet survives the caption drawn over it");
}

static void test_peekaboo_covers_the_eyes(void)
{
    /* `hide` is peekaboo, and peekaboo that does not hide the eyes is the shipped dud this
       replaces — "arms up beside the head". Measured before the fix: the robot's hands hid
       35% of the eye white and the ostrich's wing 13%. The only acceptable number is all of
       it, on BOTH forms. */
    const uint16_t white = rgb565(0xF6, 0xF9, 0xFC);
    for (int f = 0; f < FORM_COUNT; f++) {
        face_state_t st;
        face_rest(&st);
        st.form = (face_form_t)f;
        face_draw(fb, 0, &st);
        CHECK(count_colour(white) > 500, "the form shows its eye whites at rest");

        rig_for(ACT_HIDE, 0.45f, 1.0f, 0, &st.rig);
        rig_figure(ACT_HIDE, 0.45f, 1.0f, 0, 0.0f, &st.fig);
        face_draw(fb, 0, &st);
        CHECK(count_colour(white) == 0, "mid-hide, no eye white is left showing");
    }
}

static void test_the_blush_lands_on_the_face(void)
{
    /* Drawn in the wrong ORDER it vanishes; drawn at the wrong ANCHOR it lands on the chest.
       Both happened. So: the pink must survive the frame, and it must sit inside the head. */
    const uint16_t pink = rgb565(0xFF, 0x7A, 0x9C);
    const uint16_t white = rgb565(0xF6, 0xF9, 0xFC);
    for (int f = 0; f < FORM_COUNT; f++) {
        face_state_t st;
        face_rest(&st);
        st.form = (face_form_t)f;
        face_draw(fb, 0, &st);
        CHECK(count_colour(pink) == 0, "nothing is pink at rest");

        /* Where the eyes are, so "on the face" can be asserted without hardcoding geometry
           that belongs to the renderer. */
        int eye_lo = FACE_H, eye_hi = -1;
        for (int y = 0; y < FACE_H; y++) {
            for (int x = 0; x < FACE_W; x++) {
                if (fb[y * FACE_W + x] != white) continue;
                if (y < eye_lo) eye_lo = y;
                if (y > eye_hi) eye_hi = y;
            }
        }

        rig_for(ACT_BLUSH, 0.5f, 1.0f, 0, &st.rig);
        rig_figure(ACT_BLUSH, 0.5f, 1.0f, 0, 0.0f, &st.fig);
        CHECK(st.fig.extra == EXTRA_BLUSH, "blush asks for the cheeks");
        face_draw(fb, 0, &st);
        CHECK(count_colour(pink) > 900, "the cheeks are actually on the glass");

        int lo = FACE_H, hi = -1;
        for (int y = 0; y < FACE_H; y++) {
            for (int x = 0; x < FACE_W; x++) {
                if (fb[y * FACE_W + x] != pink) continue;
                if (y < lo) lo = y;
                if (y > hi) hi = y;
            }
        }
        /* Cheeks: below the top of the eyes and not far below their bottom. The robot's old
           anchor put the ostrich's gag puff 90 px adrift on the chest; this is the check that
           would have said so. */
        CHECK(lo > eye_lo && hi < eye_hi + 70, "the cheeks are on the face, not the body");
    }
}

static void test_the_gag_puff_shows_on_both_forms(void)
{
    /* The puff is the whole fart gag; `EXTRA_PUFF` anchored off the HEAD put it in the middle
       of the ostrich's chest. Its colour is unique to it, so it can simply be counted. */
    const uint16_t puff = rgb565(0x8C, 0x9A, 0x8C);
    for (int f = 0; f < FORM_COUNT; f++) {
        face_state_t st;
        face_rest(&st);
        st.form = (face_form_t)f;
        face_draw(fb, 0, &st);
        CHECK(count_colour(puff) == 0, "no cloud at rest");

        rig_for(ACT_FART, 0.4f, 1.0f, 0, &st.rig);
        rig_figure(ACT_FART, 0.4f, 1.0f, 0, 0.0f, &st.fig);
        CHECK(st.fig.extra == EXTRA_PUFF, "the gag's hold carries the puff");
        face_draw(fb, 0, &st);
        CHECK(count_colour(puff) > 900, "the cloud clears the figure drawn over it");
    }
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

/* ---- the vocabulary -----------------------------------------------------------------
 *
 * `vocab.c` states three rules its phrases obey and this is where they are enforced, because
 * NOTHING ELSE CAN NOTICE. A phrase MultiNet refuses is dropped at boot with a log line
 * nobody is reading; from the room the panel is simply deaf to that one thing, which looks
 * exactly like a broken microphone. */

static void test_vocab_phrases_are_sayable(void)
{
    const vocab_t *v = vocab_all();
    CHECK(vocab_count() > 0, "there is a vocabulary");
    /* MultiNet7 English takes ~200 phrases; with no wake word every one of them is live, so
       the practical limit is much lower than the model's. */
    CHECK(vocab_count() <= 60, "the always-on vocabulary stays small enough to not misfire");
    for (int i = 0; i < vocab_count(); i++) {
        const char *p = v[i].phrase;
        int words = 1;
        CHECK(p != NULL && p[0] != '\0', "every entry has a phrase");
        CHECK(p[0] != ' ', "no phrase starts with a space");
        for (const char *c = p; *c; c++) {
            /* Lowercase a-z and spaces ONLY: the grapheme-to-phoneme pass takes words, and a
               digit, apostrophe or capital is refused silently. */
            CHECK((*c >= 'a' && *c <= 'z') || *c == ' ', "phrases are lowercase letters and spaces");
            if (*c == ' ') {
                CHECK(c[1] != ' ' && c[1] != '\0', "no double or trailing spaces");
                words++;
            }
        }
        /* Two words minimum, and the exceptions are a NAMED LIST rather than a loosened
           check. WakeNet is disabled, so a one-word phrase is always live and fires at the
           television; these ten are here because the owner asked for them in these words and
           they are what a four-year-old actually says. Naming them means the next single word
           has to be argued for rather than slipped in beside these — which is the whole value
           of a list over a loosened check, and the list has already grown once. */
        static const char *const SINGLES[] = {"burp", "fart",  "dance", "jump", "wave",
                                              "shake", "laugh", "eat",   "kick", "spin"};
        bool allowed_single = false;
        for (unsigned k = 0; k < sizeof(SINGLES) / sizeof(SINGLES[0]); k++) {
            if (strcmp(p, SINGLES[k]) == 0) allowed_single = true;
        }
        CHECK(words >= 2 || allowed_single,
              "a one-word phrase is one of the four the owner named");
    }
}

static void test_vocab_has_no_ambiguity(void)
{
    const vocab_t *v = vocab_all();
    for (int i = 0; i < vocab_count(); i++) {
        for (int j = 0; j < vocab_count(); j++) {
            if (i == j) continue;
            CHECK(strcmp(v[i].phrase, v[j].phrase) != 0, "no phrase is listed twice");
            /* Nor a prefix of another: the shorter becomes unreachable and the longer
               unreliable, and neither failure says which one it is. */
            const size_t n = strlen(v[i].phrase);
            CHECK(!(strlen(v[j].phrase) > n && strncmp(v[i].phrase, v[j].phrase, n) == 0 &&
                    v[j].phrase[n] == ' '),
                  "no phrase is a prefix of another");
        }
    }
}

static void test_vocab_arguments_are_real(void)
{
    const vocab_t *v = vocab_all();
    int forms = 0, actions = 0, colours = 0, named_colours = 0, listens = 0;
    for (int i = 0; i < vocab_count(); i++) {
        switch (v[i].kind) {
        case VOCAB_ACTION:
            CHECK(v[i].arg > ACT_NONE && v[i].arg < ACT_COUNT, "an action command names a real action");
            CHECK(rig_spec((action_t)v[i].arg)->dur_ms > 0, "and one with a duration");
            actions++;
            break;
        case VOCAB_FORM:
            CHECK(v[i].arg >= 0 && v[i].arg < FORM_COUNT, "a form command names a real form");
            forms++;
            break;
        case VOCAB_COLOUR:
            /* A named colour must land on a colour that EXISTS. "turn red" resolving past the
               end of the palette would wrap to some other colour and look like the recogniser
               mishearing — a bug that would be chased in the microphone for hours. */
            CHECK(v[i].arg < face_colour_count(), "a named colour is in the palette");
            if (v[i].arg >= 0) named_colours++;
            colours++;
            break;
        case VOCAB_LISTEN:
            /* THE NAME, and there must be EXACTLY ONE of it. Two wake phrases would give the
               panel two names and the twins no way to know which one worked; zero would leave
               the hands-free path unreachable with nothing to say so. Counted below. */
            listens++;
            /* And it must be more than one word. Every other phrase here costs an animation
               when it misfires; this one opens a microphone and calls a model, which is the
               whole argument in `vocab.c` for a carrier word in front of the name. */
            {
                const char *sp = v[i].phrase;
                bool spaced = false;
                while (*sp != '\0') {
                    if (*sp == ' ') spaced = true;
                    sp++;
                }
                CHECK(spaced, "the wake phrase carries a word in front of the name");
            }
            break;
        }
        CHECK(vocab_get(i) == &v[i], "ids are indices, which is what MultiNet hands back");
    }
    CHECK(vocab_get(-1) == NULL && vocab_get(vocab_count()) == NULL, "a bad id is NULL, not a read off the end");
    /* Both forms must be reachable BY VOICE, which is the request that started this: the
       ostrich is the default, so "change into robot" is the only way back without five taps. */
    CHECK(listens == 1, "the panel has exactly one name");
    /* AND THAT NAME IS WHAT THE GLASS SHOWS. The label defaults to it now, derived from the
       wake phrase rather than stored twice — so this pins the derivation: the word after the
       carrier, drawable in a font that has uppercase and digits and nothing else. */
    {
        const char *nm = vocab_name();
        CHECK(nm != NULL && nm[0] != '\0', "the panel's name is derivable from its phrase");
        bool spaced = false;
        for (const char *q = nm; *q != '\0'; q++) {
            if (*q == ' ') spaced = true;
            CHECK(*q >= 'a' && *q <= 'z', "and is plain lowercase letters, which the font can shout");
        }
        CHECK(!spaced, "the name is the last word, not the whole phrase");
    }
    CHECK(forms >= 2, "both bodies can be asked for");
    CHECK(actions >= 8 && colours >= 1, "there is something worth saying");
    /* The owner asked for "turn [color]" by name, so a palette command that only ever steps
       to the next colour no longer satisfies the request. */
    CHECK(named_colours >= 6, "colours can be asked for by name, not only cycled");
}

/* ---- the caption ticker -------------------------------------------------------------- */

static void test_caption_starts_empty_and_silent(void)
{
    caption_t c;
    caption_reset(&c);
    CHECK(caption_idle(&c), "nothing to show at boot");
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    caption_draw(&c, fb, FACE_W, FACE_H, 0xFFFF);
    CHECK(non_black() == 0, "and nothing drawn");
}

static void test_the_ticker_draws_nothing_of_its_own(void)
{
    /* THIS REPLACES `test_caption_indicator_tracks_the_microphone`, which asserted that an
       open microphone is always indicated. That was the right assertion for a feature that no
       longer exists: the owner had the dot removed (0.2.66), so the property to hold now is
       the opposite one — with no phrase to show, this row is empty, and the pet underneath it
       is untouched. A leftover pixel from a removed feature is exactly the kind of thing that
       survives a deletion. */
    caption_t c;
    caption_reset(&c);
    for (int i = 0; i < 60; i++) caption_tick(&c, (uint32_t)(i * 40), FACE_W);
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    caption_draw(&c, fb, FACE_W, FACE_H, 0xFFFF);
    CHECK(non_black() == 0, "an idle ticker draws nothing at all");

    /* And it still draws the thing it is for. */
    CHECK(caption_say(&c, "HELLO"), "a phrase is accepted");
    caption_tick(&c, 2400, FACE_W);
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    caption_draw(&c, fb, FACE_W, FACE_H, 0xFFFF);
    CHECK(non_black() > 0, "but a phrase still reaches the glass");
}

static void test_caption_scrolls_and_drains(void)
{
    caption_t c;
    caption_reset(&c);
    CHECK(caption_say(&c, "PLAY PEEKABOO"), "a phrase is accepted");
    CHECK(!caption_idle(&c), "and is now pending");

    /* It must ARRIVE, travel, and LEAVE. A ticker that never empties leaves a word parked
       across the bottom sixth of a 29 mm panel forever. */
    uint32_t t = 0;
    int seen = 0;
    for (int i = 0; i < 1000 && !caption_idle(&c); i++) {
        t += 40;
        caption_tick(&c, t, FACE_W);
        memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
        caption_draw(&c, fb, FACE_W, FACE_H, 0xFFFF);
        if (non_black() > 0) seen++;
    }
    CHECK(caption_idle(&c), "the ticker drains");
    CHECK(seen > 25, "and the text was on the glass long enough to read");
    CHECK(t < 40000u, "without taking the better part of a minute");
}

static void test_caption_is_bounded_by_a_talkative_room(void)
{
    /* Nothing here may grow without limit, and nothing may corrupt: this is fed by whatever
       a room says for as long as it says it. */
    caption_t c;
    caption_reset(&c);
    int taken = 0;
    for (int i = 0; i < 200; i++) {
        if (caption_say(&c, "MAKE A RUDE NOISE")) taken++;
        CHECK(c.len <= CAPTION_MAX, "the buffer never overruns");
        CHECK(c.buf[c.len] == '\0', "and stays a string");
    }
    CHECK(taken > 0 && taken < 200, "a full ticker refuses rather than dropping what is on screen");

    /* And it CATCHES UP: a backlog scrolls faster than a single phrase, or the last thing
       said arrives after the child has stopped looking. */
    caption_t one;
    caption_reset(&one);
    caption_say(&one, "WAVE HELLO");
    CHECK(caption_speed(&c, FACE_W) > caption_speed(&one, FACE_W),
          "a long backlog drains faster than a short one");
}

static void test_caption_survives_a_stalled_clock(void)
{
    /* `now_ms` comes from the render loop, which the OTA path and the calibration routine can
       both hold for seconds. A resumed clock must not teleport the text. */
    caption_t c;
    caption_reset(&c);
    caption_say(&c, "DO A DANCE");
    caption_tick(&c, 1000, FACE_W);
    const float before = c.scrolled;
    caption_tick(&c, 31000, FACE_W); /* thirty seconds later */
    CHECK(c.scrolled == before, "a long stall advances nothing");
    caption_tick(&c, 31040, FACE_W);
    CHECK(c.scrolled > before, "and the next ordinary frame resumes");
}

static void test_caption_ignores_nonsense(void)
{
    caption_t c;
    caption_reset(&c);
    CHECK(!caption_say(&c, ""), "an empty phrase is not a phrase");
    CHECK(!caption_say(&c, NULL), "nor is nothing at all");
    CHECK(caption_idle(&c), "and neither put anything in the ticker");
    caption_reset(NULL);
    caption_tick(NULL, 0, FACE_W);
    caption_draw(NULL, fb, FACE_W, FACE_H, 0);
    CHECK(caption_idle(NULL), "a null ticker is an empty one, not a crash");
}

static void test_the_font_can_spell_the_vocabulary(void)
{
    /* THE TICKER IS THE FEATURE. A glyph the font lacks renders as a blank of the right
       width, so a missing letter would show as a gap in the middle of a word — legible as
       "broken", never as the word. Every character the panel can be told to say must have
       one, uppercased as `speech.c` publishes it. */
    const vocab_t *v = vocab_all();
    for (int i = 0; i < vocab_count(); i++) {
        for (const char *p = v[i].phrase; *p; p++) {
            char up[2] = {(char)toupper((unsigned char)*p), '\0'};
            if (up[0] == ' ') continue;
            memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
            font_draw(fb, FACE_W, FACE_H, 10, 10, 2, up, 0xFFFF);
            CHECK(non_black() > 0, "every letter in the vocabulary has a glyph");
        }
    }
}

static void test_the_font_glyphs_are_distinct(void)
{
    /* Copy-paste is the failure mode of a hand-entered bitmap table, and two letters sharing
       a shape is invisible until someone reads a word on the glass. */
    static const char *SET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    uint16_t *seen = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    CHECK(seen != NULL, "scratch frame allocated");
    for (const char *a = SET; *a; a++) {
        char sa[2] = {*a, '\0'};
        memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
        font_draw(fb, FACE_W, FACE_H, 10, 10, 1, sa, 0xFFFF);
        memcpy(seen, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
        for (const char *b = a + 1; *b; b++) {
            char sb[2] = {*b, '\0'};
            memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
            font_draw(fb, FACE_W, FACE_H, 10, 10, 1, sb, 0xFFFF);
            CHECK(memcmp(seen, fb, (size_t)FACE_W * FACE_H * sizeof(uint16_t)) != 0,
                  "no two glyphs draw the same shape");
        }
    }
    free(seen);
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
    test_every_action_changes_the_picture();
    test_peekaboo_covers_the_eyes();
    test_the_caption_does_not_black_out_the_pet();
    test_the_side_mounted_fit_stays_in_the_square();
    test_a_quarter_turn_is_a_permutation();
    test_a_tap_lands_where_the_pixel_it_touched_came_from();
    test_the_zones_follow_the_scaled_figure();
    test_the_lean_limits_are_the_room_that_exists();
    test_the_case_geometry_is_the_case();
    test_the_mouth_moves_only_while_talking();
    test_the_open_mouth_is_the_smile_opening();
    test_the_shuffle_is_driven_by_distance_not_by_a_clock();
    test_the_orientation_needs_a_band_crossed_on_purpose();
    test_the_bird_moves_between_frames();
    test_the_three_dances_differ_on_the_bird();
    test_the_bird_keeps_its_head_on_its_neck();
    test_the_blush_lands_on_the_face();
    test_the_gag_puff_shows_on_both_forms();
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
    test_vocab_phrases_are_sayable();
    test_vocab_has_no_ambiguity();
    test_vocab_arguments_are_real();
    test_caption_starts_empty_and_silent();
    test_the_ticker_draws_nothing_of_its_own();
    test_caption_scrolls_and_drains();
    test_caption_is_bounded_by_a_talkative_room();
    test_caption_survives_a_stalled_clock();
    test_caption_ignores_nonsense();
    test_the_font_can_spell_the_vocabulary();
    test_the_font_glyphs_are_distinct();

    free(fb);
    printf("ok — %d checks\n", checks);
    return 0;
}
