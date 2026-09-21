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

static void test_draw_produces_a_robot(void)
{
    face_state_t st;
    face_rest(&st);
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
                face_draw(fb, k % face_colour_count(), &st);
                CHECK(non_black() > 2000, "every face/action combination draws something");
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
    test_draw_produces_a_robot();
    test_every_face_and_action_draws();
    test_blink_is_a_line_not_a_hole();

    free(fb);
    printf("ok — %d checks\n", checks);
    return 0;
}
