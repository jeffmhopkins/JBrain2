/* The robot, drawn at the geometry the mock settled on.
 *
 * Every number here is lifted from `docs/mocks/room-endpoint/pet-face.html`, whose canvas is
 * 368x448 — the panel exactly — so its coordinates need no mapping. That is the point of
 * having built the mock at true geometry: the design was measured against this screen before
 * the screen existed, and copying the numbers is more faithful than re-deriving them.
 *
 * IT IS NOW THE RIG, not the rest pose it started as. Emotion arrives as lid geometry
 * (`emotion.c`), limbs and the figure transform as poses (`rig.c`), and this file draws one
 * instant of whatever the caller has tweened. The eye is the only part that needed new
 * rasterising: the web version clips a pupil and fills a quadratic cheek-arc, and both have a
 * closed form here, so the lids cost a per-pixel pass over two 60x70 boxes and nothing else.
 */

#include "face.h"
#include "font.h"
#include "game.h"
#include "orient.h"
#include "poop.h"

#include <math.h>
#include <stdio.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* The panel takes RGB565 big-endian; storing swapped means the blit is a straight memcpy. */
static inline uint16_t rgb(uint8_t r, uint8_t g, uint8_t b)
{
    const uint16_t c = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
    return (uint16_t)((c >> 8) | (c << 8));
}

/* The 10 shipped colours (intents.py PET_COLORS), then the robot's own default. The mock's
   `rainbow` is a per-frame hue and belongs with the animation, not here. */
static const uint32_t PALETTE[] = {
    0x7FA7C9, /* default — the robot's own steel, first so a tap LEAVES it rather than arriving */
    0x3BF0FF, 0xFF4FD8, 0xFFD23F, 0xFFB03A, 0x6A7BFF,
    0xFF477E, 0x49F08A, 0xFF8AD0, 0xB06AFF, 0xFFFFFF,
    /* NAMED RED AND BLUE, APPENDED, and appended is the point: every index above is a colour
       the tap cycle already visits in an approved order, and inserting would renumber them.
       These two exist because the owner asked for "turn red" and "turn blue" and this palette
       had neither. Its nearest to red was 0xFF477E, which a four-year-old calls pink, and its
       nearest to blue was 0x6A7BFF, a periwinkle. A named colour command that produces a
       colour the child would give a different name to is worse than no command. */
    0xFF3B30, /* red */
    0x3B82F6, /* blue */
};

int face_colour_count(void)
{
    return (int)(sizeof(PALETTE) / sizeof(PALETTE[0]));
}

/* The mock's `shade(hex, f)` — one silhouette tinted at draw time, so a colour change is a
   multiply rather than eleven hand-drawn robots. */
static uint16_t shade(uint32_t hex, float f)
{
    const float r = (float)((hex >> 16) & 0xFF) * f;
    const float g = (float)((hex >> 8) & 0xFF) * f;
    const float b = (float)(hex & 0xFF) * f;
    return rgb((uint8_t)r, (uint8_t)g, (uint8_t)b);
}

static void px(uint16_t *fb, int x, int y, uint16_t c)
{
    if (x >= 0 && x < FACE_W && y >= 0 && y < FACE_H) fb[y * FACE_W + x] = c;
}

static void fill_rect(uint16_t *fb, int x, int y, int w, int h, uint16_t c)
{
    for (int j = y; j < y + h; j++)
        for (int i = x; i < x + w; i++) px(fb, i, j, c);
}

/* ROWS, NOT PIXELS (0.4.4). The disc is the same set of pixels it always was, `i*i + j*j <= r*r`,
   but each row is found once and written as one run: no per-pixel test, no per-pixel bounds check.
   The framebuffer is in PSRAM, where a run of neighbouring writes is what the cache is good at. */

/* The largest i with i*i + j*j <= r*r, or -1 when the row misses the disc. */
static int disc_hw(int r, int j)
{
    const int rem = r * r - j * j;
    if (rem < 0) return -1;
    int i = (int)sqrtf((float)rem);
    while (i * i > rem) i--;
    while ((i + 1) * (i + 1) <= rem) i++;
    return i;
}

/* The rows a span may write: the whole frame, except while Jetpack's world is drawn into the
   square on its side (`draw_jet_back`). */
static int s_clip0 = 0, s_clip1 = FACE_H;

static void hspan(uint16_t *fb, int y, int x0, int x1, uint16_t c)
{
    if (y < s_clip0 || y >= s_clip1) return;
    if (x0 < 0) x0 = 0;
    if (x1 >= FACE_W) x1 = FACE_W - 1;
    uint16_t *row = &fb[y * FACE_W];
    for (int x = x0; x <= x1; x++) row[x] = c;
}

void face_fill_circle(uint16_t *fb, int cx, int cy, int r, uint16_t c)
{
    for (int j = -r; j <= r; j++) {
        const int hw = disc_hw(r, j);
        if (hw >= 0) hspan(fb, cy + j, cx - hw, cx + hw, c);
    }
}

static void fill_circle(uint16_t *fb, int cx, int cy, int r, uint16_t c)
{
    face_fill_circle(fb, cx, cy, r, c);
}

/* Centre rectangle plus four quarter-discs. Everything the mock draws is a roundRect. */
static void fill_round_rect(uint16_t *fb, int x, int y, int w, int h, int r, uint16_t c)
{
    if (r * 2 > w) r = w / 2;
    if (r * 2 > h) r = h / 2;
    fill_rect(fb, x + r, y, w - 2 * r, h, c);
    fill_rect(fb, x, y + r, r, h - 2 * r, c);
    fill_rect(fb, x + w - r, y + r, r, h - 2 * r, c);
    fill_circle(fb, x + r, y + r, r, c);
    fill_circle(fb, x + w - r - 1, y + r, r, c);
    fill_circle(fb, x + r, y + h - r - 1, r, c);
    fill_circle(fb, x + w - r - 1, y + h - r - 1, r, c);
}

/* THE SAME ROUNDED RECT, TURNED `deg` clockwise about (pvx, pvy). A rect turned by moving its
   corner and nothing else stays square to the glass: the beak came apart into a stack of level
   bars on a head righting itself (owner's photo, 0.4.7). Per pixel over the turned box, each one
   turned back into the rect's own frame. At zero it IS `fill_round_rect`, pixel for pixel. */
static void fill_turned_round_rect(uint16_t *fb, int x, int y, int w, int h, int r, int pvx,
                                   int pvy, float deg, uint16_t c)
{
    if (deg == 0.0f) {
        fill_round_rect(fb, x, y, w, h, r, c);
        return;
    }
    if (r * 2 > w) r = w / 2;
    if (r * 2 > h) r = h / 2;
    const float a = deg * (float)M_PI / 180.0f, ca = cosf(a), sa = sinf(a);
    /* The rect's centre and half sizes, in its own frame about the pivot. */
    const float hw = (float)w * 0.5f, hh = (float)h * 0.5f;
    const float lx = (float)(x - pvx) + hw - 0.5f, ly = (float)(y - pvy) + hh - 0.5f;
    const float tx = (float)pvx + lx * ca - ly * sa, ty = (float)pvy + lx * sa + ly * ca;
    const float ex = fabsf(hw * ca) + fabsf(hh * sa) + 1.0f;
    const float ey = fabsf(hw * sa) + fabsf(hh * ca) + 1.0f;
    const float rr = (float)r;
    for (int j = (int)floorf(ty - ey); j <= (int)ceilf(ty + ey); j++) {
        for (int i = (int)floorf(tx - ex); i <= (int)ceilf(tx + ex); i++) {
            const float dx = (float)i - tx, dy = (float)j - ty;
            const float ax = fabsf(dx * ca + dy * sa), ay = fabsf(-dx * sa + dy * ca);
            if (ax > hw || ay > hh) continue;
            if (ax > hw - rr && ay > hh - rr) {
                const float cx = ax - (hw - rr), cy = ay - (hh - rr);
                if (cx * cx + cy * cy > rr * rr) continue;
            }
            px(fb, i, j, c);
        }
    }
}

/* A round-capped stroke IS a run of circles along the path, which is exactly how canvas
   renders `lineCap="round"` — so the smile needs no curve rasteriser. */
static void arc_stroke(uint16_t *fb, int cx, int cy, int r, float a0, float a1, int width,
                       uint16_t c)
{
    const int steps = 48;
    for (int i = 0; i <= steps; i++) {
        const float a = a0 + (a1 - a0) * ((float)i / (float)steps);
        fill_circle(fb, cx + (int)lrintf(cosf(a) * (float)r),
                    cy + (int)lrintf(sinf(a) * (float)r), width / 2, c);
    }
}

/* A limb is a rotated capsule with a ball on the end — the mock's `drawLimb`. Canvas rotates
   clockwise with y down, so its (0,len) lands at (-len*sin, +len*cos); getting that sign
   backwards would splay the arms inward, which reads as a completely different pose. */
/* AND A LIMB IS THEIR UNION, ONE RUN PER ROW (0.4.4). It was a full disc stamped at every pixel of
   its length, about six writes for every pixel that ended up lit, and the limbs were most of the
   frame: the ostrich's legs, toes, tail and crest are ~80% of its pixel writes. Every disc of a limb
   is one colour, so drawing order inside it never mattered and the union is the whole picture.
   Each row's leftmost and rightmost covered pixel is gathered first, then the row is written once;
   `host/tests.c` holds the old stamping loop and checks the two agree pixel for pixel. */
void face_draw_limb(uint16_t *fb, int x, int y, float deg, int len, int w, uint16_t c)
{
    const float a = deg * (float)M_PI / 180.0f;
    const float ex = -sinf(a), ey = cosf(a);
    const int r = w / 2;
    const int rb = (int)(w * 0.62f);
    const int bx = x + (int)lrintf(ex * (float)len), by = y + (int)lrintf(ey * (float)len);
    int y0 = (y < by ? y : by) - r, y1 = (y > by ? y : by) + r;
    if (y0 < 0) y0 = 0;
    if (y1 >= FACE_H) y1 = FACE_H - 1;
    if (y0 > y1 || len < 0) {
        face_fill_circle(fb, bx, by, rb, c);
        return;
    }
    /* On the stack: 1.8 KB at most, for the length of one limb. */
    int16_t lo[FACE_H], hi[FACE_H];
    for (int row = y0; row <= y1; row++) {
        lo[row] = INT16_MAX;
        hi[row] = INT16_MIN;
    }
    /* One disc's row widths, worked out once: every stroke disc of a limb has the same radius. */
    int16_t hw_r[2 * 64 + 1];
    const bool table = r <= 64;
    if (table) {
        for (int j = -r; j <= r; j++) hw_r[j + r] = (int16_t)disc_hw(r, j);
    }
    /* The stroke's discs at 0..len. Their centres move at most a pixel per step, so each row's
       coverage is one run. */
    for (int i = 0; i <= len; i++) {
        const int cx = x + (int)lrintf(ex * (float)i), cy = y + (int)lrintf(ey * (float)i);
        for (int j = -r; j <= r; j++) {
            const int row = cy + j;
            if (row < y0 || row > y1) continue;
            const int hw = table ? hw_r[j + r] : disc_hw(r, j);
            if (hw < 0) continue;
            if (cx - hw < lo[row]) lo[row] = (int16_t)(cx - hw);
            if (cx + hw > hi[row]) hi[row] = (int16_t)(cx + hw);
        }
    }
    for (int row = y0; row <= y1; row++) {
        if (lo[row] <= hi[row]) hspan(fb, row, lo[row], hi[row], c);
    }
    /* THE BALL ON ITS OWN. It is wider than the stroke, so near the end it can cover part of a row
       the stroke left off further back, and one run per row would fill the gap between. */
    face_fill_circle(fb, bx, by, rb, c);
}

static void draw_limb(uint16_t *fb, int x, int y, float deg, int len, int w, uint16_t c)
{
    face_draw_limb(fb, x, y, deg, len, w, c);
}

/* Mock geometry, verbatim. Origin is translate(W/2, H*0.545) for the small-body variant. */
/* THE WHOLE FIGURE, SHRUNK TO FIT A DIFFERENT SHAPE OF PANEL.
 *
 * The owner mounts a unit with the cable out the side, so the panel is 448 wide and 368 tall
 * to the viewer rather than the other way round. The figure is composed for 448 of height and
 * is 428 px of it; in landscape it has 368. So everything scales by 368/448 and renders into
 * a 368x368 SQUARE, which is the shape a quarter turn maps onto itself.
 *
 * A scalar rather than a second set of constants: every number in this file was measured
 * against the mock, and a second hand-tuned layout would be a second thing to keep true. */
static float s_fit = 1.0f;
static int s_fit_oy = -1;

void face_set_fit(float scale, int origin_y)
{
    s_fit = scale > 0.05f ? scale : 0.05f;
    s_fit_oy = origin_y;
}

#define OX (FACE_W / 2)
#define OY ((int)(FACE_H * 0.545f))
#define HEAD_Y (-96)
#define HW 108
#define HH 88
#define SHOULDER_Y 6
#define HIP_Y 98
#define ARM_L 74
#define LEG_L 54

/* One eye, with lids.
 *
 * The web renderer (`frontend/src/pet/draw.ts:drawEye`) clips the pupil to the eye, fills a
 * rotated rect for the upper lid and a quadratic-capped region for the lower. Both reduce to
 * arithmetic, so this is one pass over the eye's box rather than a scanline rasteriser:
 *
 *   upper lid — a half-plane in a frame rotated by `ua` about the eye's TOP CENTRE, covering
 *               the first `h*uy` of it. Inverse-rotating the point is two multiplies.
 *   lower lid — the quadratic Bezier from (-bw, ly) through (0, ly-bend) to (+bw, ly) has
 *               x(t) = bw(2t-1), which is LINEAR in t. So t is recoverable from x directly
 *               and the curve is y(x) = ly - 2t(1-t)*bend with no root-finding at all.
 *
 * Lids are drawn in the field colour (black) rather than skipped, because an eye is drawn over
 * the head and "not drawn" would show the head through it.
 */
static void draw_eye(uint16_t *fb, int cx, int cy, uint16_t dark, const eye_params_t *p,
                     float open, float startle, float scale, float turn)
{
    /* 52x62 in the mock, scaled 0.78 because the eyes shrink with the head on a body. */
    const float grow = 1.0f + 0.20f * startle;
    float w = 52.0f * 0.78f * p->sx * grow * scale;
    float h = 62.0f * 0.78f * p->sy * grow * scale * open;
    /* The eye WIDENS as it closes — squash-and-stretch on a blink, straight from Cozmo. */
    const float bw = w * (1.0f + 0.35f * (1.0f - open));
    w = bw;

    /* Shut: a lid LINE, not an absent eye. A blink drawn as nothing reads as the face
       breaking for a frame; a line reads as a blink. */
    if (h < 5.0f) {
        fill_turned_round_rect(fb, cx - (int)(w / 2), cy - 2, (int)w, 5, 2, cx, cy, turn, dark);
        return;
    }

    const uint16_t white = rgb(0xF6, 0xF9, 0xFC);
    const uint16_t glint = rgb(0xFF, 0xFF, 0xFF);
    const uint16_t field = rgb(0x00, 0x00, 0x00);
    const float hw = w * 0.5f, hh = h * 0.5f;
    const float rr = (w < h ? w : h) * 0.38f;

    float pr = 17.0f * 0.78f * scale * (1.0f - 0.30f * startle);
    if (pr > hh) pr = hh;
    const float gx = -6.0f * 0.78f * scale, gy = -7.0f * 0.78f * scale;
    const float gr = 5.0f * 0.78f * scale;

    const float ua = p->ua * (float)M_PI / 180.0f;
    const float ca = cosf(ua), sa = sinf(ua);
    const float upper = h * p->uy;
    const float lower_y = hh - h * p->ly; /* eye-local y where the lower lid starts */
    const float bend = h * p->lb;

    /* TURNED WITH THE HEAD (`turn`, clockwise). Moving the eye's centre was not enough: the
       lids and the pupil's glint stayed square to the glass, so on a head righting itself the
       happy cheek arc sat beside the eye instead of under it. Each pixel is turned back into
       the eye's own frame, and at zero that is the identity, so the upright eye is unchanged. */
    const float ta = turn * (float)M_PI / 180.0f, tc = cosf(ta), ts = sinf(ta);
    const float ex = fabsf(hw * tc) + fabsf(hh * ts), ey = fabsf(hw * ts) + fabsf(hh * tc);
    const int x0 = (int)(-ex) - 1, x1 = (int)(ex) + 1;
    const int y0 = (int)(-ey) - 1, y1 = (int)(ey) + 1;
    for (int j = y0; j <= y1; j++) {
        for (int i = x0; i <= x1; i++) {
            const float fx = (float)i * tc + (float)j * ts;
            const float fy = -(float)i * ts + (float)j * tc;

            /* Rounded-rect membership: outside the corner discs is outside the eye. */
            const float ax = fabsf(fx), ay = fabsf(fy);
            if (ax > hw || ay > hh) continue;
            if (ax > hw - rr && ay > hh - rr) {
                const float dx = ax - (hw - rr), dy = ay - (hh - rr);
                if (dx * dx + dy * dy > rr * rr) continue;
            }

            uint16_t c = white;
            const float dpx = fx - gx * 0.0f, dpy = fy; /* pupil is centred; gaze is the glint */
            if (dpx * dpx + dpy * dpy <= pr * pr) c = dark;
            if (pr > 5.0f) {
                const float lx = fx - gx, ly2 = fy - gy;
                if (lx * lx + ly2 * ly2 <= gr * gr) c = glint;
            }

            /* Upper lid, in the frame rotated about the eye's top centre. */
            if (p->uy > 0.01f) {
                const float X = fx, Y = fy + hh;
                const float py = -X * sa + Y * ca;
                const float px2 = X * ca + Y * sa;
                if (py >= 0.0f && py <= upper && fabsf(px2) <= hw * 2.0f) c = field;
            }
            /* Lower lid / cheek arc: the Duchenne raise, and what makes "happy" read at all. */
            if (p->ly > 0.01f) {
                const float t = (fx / hw + 1.0f) * 0.5f;
                const float cyv = lower_y - 2.0f * t * (1.0f - t) * bend;
                if (fy >= cyv) c = field;
            }
            px(fb, cx + i, cy + j, c);
        }
    }
}

/* The gag puff, BEHIND the figure, and the cheek blush, ON the face.
 *
 * These were one function called before anything else, which meant the body and the head were
 * then painted straight over both. Measured: `ACT_BLUSH` changed ZERO pixels on the robot and
 * 1525 on the ostrich — a shipped action that did nothing, invisible to every test in the
 * suite because they all compare pose structs rather than rendered frames.
 *
 * The ostrich's BLUSH is the sharper case: the old anchor was the head's y plus a fixed
 * `+-78` in x, which on a head 128 wide instead of 216 put both cheeks in empty space
 * BESIDE the bird's head. So anchors come from the form, because they must — the shared
 * one cannot be right for two silhouettes this different. */
static void draw_puff(uint16_t *fb, int ax, int ay, float scale)
{
    /* Deliberately a cloud and not a colour: the gag has to read on a dark panel from across
       a room, and a tinted robot reads as a new robot. */
    const uint16_t puff = rgb(0x8C, 0x9A, 0x8C);
    const int r = (int)(18.0f * scale);
    fill_circle(fb, ax, ay, r, puff);
    fill_circle(fb, ax - (int)(28.0f * scale), ay - (int)(14.0f * scale), (int)(r * 0.7f), puff);
    fill_circle(fb, ax - (int)(24.0f * scale), ay + (int)(16.0f * scale), (int)(r * 0.6f), puff);
}

/* THE MEGA FART's clouds: a new one puffing out behind him every so often, swelling as it
   drifts away and up, so the trail grows for as long as it goes on. `p` is the action's
   progress. */
static void draw_gas(uint16_t *fb, int ax, int ay, float scale, float p)
{
    const float every = 0.09f;
    for (int k = 0; k < 6; k++) {
        const float age = fmodf(p, every) + (float)k * every; /* 0 is the newest */
        if (age > p) break;
        const float t = age / (every * 6.0f);
        const float grow = 0.6f + 1.1f * t;
        const int x = ax - (int)lrintf(150.0f * t * scale);
        const int y = ay - (int)lrintf((50.0f * t + 12.0f * sinf(age * 40.0f)) * scale);
        draw_puff(fb, x, y, scale * grow * (1.0f - 0.35f * t));
    }
}

/* The cheeks sit at +-(dx, dy) from the centre: along the head's own across, which is level
   only while the head is. */
static void draw_blush(uint16_t *fb, int cx, int cy, int dx, int dy, float scale)
{
    const uint16_t pink = rgb(0xFF, 0x7A, 0x9C);
    const int r = (int)(16.0f * scale);
    fill_circle(fb, cx - dx, cy - dy, r, pink);
    fill_circle(fb, cx + dx, cy + dy, r, pink);
}

/* A HEART, by the curve (x^2 + y^2 - 1)^3 <= x^2 y^3 — two lobes and a point, which no pair
   of discs and a triangle quite manage at 20 px. Per pixel, but over a box this small. */
static void draw_heart(uint16_t *fb, int cx, int cy, int r, uint16_t c)
{
    if (r < 2) return;
    for (int dy = -r; dy <= (int)(r * 1.25f); dy++) {
        const int y = cy + dy;
        if (y < 0 || y >= FACE_H) continue;
        const float fy = -(float)dy / (float)r + 0.15f;
        for (int dx = -(int)(r * 1.25f); dx <= (int)(r * 1.25f); dx++) {
            const float fx = (float)dx / (float)r;
            const float q = fx * fx + fy * fy - 1.0f;
            if (q * q * q - fx * fx * fy * fy * fy <= 0.0f) px(fb, cx + dx, y, c);
        }
    }
}

/* A four-point sparkle: two thin diamonds crossed. */
static void draw_star(uint16_t *fb, int cx, int cy, int r, uint16_t c)
{
    if (r < 2) return;
    const int w = r / 3 > 1 ? r / 3 : 1;
    for (int dy = -r; dy <= r; dy++) {
        const int ady = dy < 0 ? -dy : dy;
        const int tall = w - w * ady / r;        /* the vertical diamond's half-width here */
        const int wide = r - r * ady / (w + 1);  /* the horizontal one's, inside its height */
        const int half = tall > wide ? tall : wide;
        for (int dx = -half; dx <= half; dx++) px(fb, cx + dx, cy + dy, c);
    }
}

/* THE ONE-SHOTS THAT LIVE ABOVE THE HEAD, drawn last so nothing paints over them. `cx` is the
   head's centre, `top` its top edge and `half` its half-width, so each form puts them where its
   own head is.
 *
 * STARS (dizzy): three, round the head on a flat ellipse — the cartoon shorthand a four-year-
 * old already reads as "shaken up". The far side of the orbit is drawn smaller, which is all
 * the depth a flat ellipse needs.
 *
 * HEARTS (love): twelve, rising from beside the head in turn, swaying as they go and shrinking
 * away at the top rather than vanishing. */
/* THE FOOD'S OWN PIXEL: whether (u, v) — the food's radius is 1, y down — is food, and which
 * colour. Eight simple shapes a four-year-old names at a glance, each in its own colours so
 * that two meals running look different from across a room. Bites are cut out afterwards,
 * the same way for all of them. */
static bool food_px(food_t f, float u, float v, int dx, int dy, uint16_t *c)
{
    switch (f) {
    case FOOD_APPLE: {
        if (fabsf(u - 0.04f) < 0.07f && v > -1.05f && v < -0.62f) { /* the stem */
            *c = rgb(0x6B, 0x42, 0x20);
            return true;
        }
        const float lu = (u - 0.27f) / 0.24f, lv = (v + 0.84f) / 0.11f; /* the leaf */
        if (lu * lu + lv * lv <= 1.0f) {
            *c = rgb(0x4C, 0xB0, 0x40);
            return true;
        }
        const float au = u / 0.95f, av = (v - 0.1f) / 0.9f;
        if (au * au + av * av > 1.0f || (v < -0.62f && fabsf(u) < 0.16f)) return false;
        const float hu = u + 0.42f, hv = v + 0.25f;
        *c = hu * hu + hv * hv < 0.03f ? rgb(0xFF, 0x9A, 0x8A) : rgb(0xE0, 0x30, 0x30);
        return true;
    }
    case FOOD_BANANA: {
        const float d = sqrtf(u * u + (v + 1.3f) * (v + 1.3f));
        if (d < 1.38f || d > 2.05f || fabsf(u) > 0.98f) return false;
        *c = fabsf(u) > 0.86f ? rgb(0x6B, 0x4A, 0x20)
             : d > 1.92f      ? rgb(0xD8, 0xA8, 0x20)
                              : rgb(0xF5, 0xD0, 0x40);
        return true;
    }
    case FOOD_PIZZA: {
        if (v < -0.9f || v > 0.95f) return false;
        if (fabsf(u) > 0.92f * (0.95f - v) / 1.85f) return false;
        if (v < -0.64f) {
            *c = rgb(0xD9, 0x9A, 0x4E); /* the crust */
            return true;
        }
        static const float PEP[3][2] = {{-0.26f, -0.3f}, {0.26f, -0.28f}, {0.0f, 0.22f}};
        for (int i = 0; i < 3; i++) {
            const float pu = u - PEP[i][0], pv = v - PEP[i][1];
            if (pu * pu + pv * pv < 0.026f) {
                *c = rgb(0xC0, 0x2A, 0x2A);
                return true;
            }
        }
        *c = rgb(0xF6, 0xC6, 0x45);
        return true;
    }
    case FOOD_DONUT: {
        const float d = sqrtf(u * u + v * v);
        if (d > 1.0f || d < 0.34f) return false;
        const float wav = 0.05f * sinf(atan2f(v, u) * 9.0f);
        if (d > 0.42f && d < 0.9f + wav) {
            const int sp = (dx * 13 + dy * 7 + 600) % 41;
            *c = sp == 0   ? rgb(0x4A, 0xA8, 0xF0)
                 : sp == 9 ? rgb(0xF6, 0xE0, 0x40)
                 : sp == 20 ? rgb(0xFF, 0xFF, 0xFF)
                            : rgb(0xF0, 0x7C, 0xB0);
        } else {
            *c = rgb(0xD9, 0x9A, 0x5B);
        }
        return true;
    }
    case FOOD_CARROT: {
        if (v < -0.55f) { /* three blades of top */
            if (v < -1.02f) return false;
            const float t = (v + 1.02f) / 0.47f;
            for (int k = -1; k <= 1; k++) {
                if (fabsf(u - (float)k * 0.22f * (1.0f - t)) < 0.07f + 0.04f * t) {
                    *c = rgb(0x4C, 0xB0, 0x40);
                    return true;
                }
            }
            return false;
        }
        if (v > 1.0f || fabsf(u) > 0.44f * (1.0f - v) / 1.55f) return false;
        *c = fmodf((v + 2.0f) * 6.0f, 1.0f) < 0.14f ? rgb(0xC8, 0x60, 0x10)
                                                    : rgb(0xF0, 0x86, 0x1E);
        return true;
    }
    case FOOD_WATERMELON: {
        const float vv = v + 0.3f, d = sqrtf(u * u + vv * vv);
        if (vv < 0.0f || d > 1.0f) return false;
        if (d > 0.86f) {
            *c = rgb(0x2E, 0x8B, 0x3A);
        } else if (d > 0.78f) {
            *c = rgb(0xD8, 0xF0, 0xB0);
        } else {
            static const float SEED[4][2] = {{-0.35f, 0.15f}, {0.0f, 0.3f}, {0.35f, 0.15f},
                                             {0.0f, 0.02f}};
            *c = rgb(0xF0, 0x4A, 0x5A);
            for (int i = 0; i < 4; i++) {
                const float su = (u - SEED[i][0]) / 0.06f, sv = (v - SEED[i][1]) / 0.1f;
                if (su * su + sv * sv <= 1.0f) *c = rgb(0x22, 0x18, 0x18);
            }
        }
        return true;
    }
    case FOOD_STRAWBERRY: {
        if (v < -0.55f) { /* the leaves: a jagged crown */
            if (v < -0.9f || fabsf(u) > 0.62f) return false;
            if (v < -0.9f + 0.3f * fabsf(sinf(u * 9.0f))) return false;
            *c = rgb(0x4C, 0xB0, 0x40);
            return true;
        }
        const float bv = v + 0.12f;
        const bool top = u * u + bv * bv <= 0.56f;
        const bool tip = v > -0.12f && v < 1.0f && fabsf(u) < 0.75f * (1.0f - v) / 1.12f;
        if (!top && !tip) return false;
        *c = (dx * 11 + dy * 17 + 900) % 37 == 0 ? rgb(0xF6, 0xE0, 0x6A) : rgb(0xE8, 0x2E, 0x3C);
        return true;
    }
    case FOOD_COOKIE:
    default: {
        if (u * u + v * v > 1.0f) return false;
        /* Chips: a fixed scatter, so they are eaten with the dough rather than moving. */
        const bool chip = ((dx * 7 + dy * 13 + 400) % 23 == 0) ||
                          ((dx * 11 - dy * 5 + 400) % 29 == 0);
        *c = chip ? rgb(0x5A, 0x34, 0x1C) : rgb(0xD9, 0xA0, 0x5B);
        return true;
    }
    }
}

/* Its crumbs are the colour you would remember it by. */
static uint16_t food_crumb(food_t f)
{
    static const uint32_t CRUMB[FOOD_COUNT] = {0xD9A05B, 0xE03030, 0xF5D040, 0xF6C645,
                                               0xF07CB0, 0xF0861E, 0xF04A5A, 0xE82E3C};
    const uint32_t h = CRUMB[(unsigned)f < (unsigned)FOOD_COUNT ? f : 0];
    return rgb((uint8_t)(h >> 16), (uint8_t)(h >> 8), (uint8_t)h);
}

/* THE MEAL, with `bites` bites gone out of it. Per pixel over its box: food (`food_px`) and in
 * none of the bites. Bites are taken from the side facing the eater (`toward`, radians, y
 * down), each one further round, so it is eaten down to nothing; the last finishes it. Crumbs
 * fall from the bite for a moment after each (`since`, a fraction of a bite's time). */
static void draw_food(uint16_t *fb, food_t f, int cx, int cy, float s, int bites, float since,
                      float toward)
{
    /* Three bites big enough that the third finishes it: each one is most of a third. */
    const float r = 24.0f * s, br = 0.8f * r;
    static const float ROUND[EAT_BITES] = {0.0f, 0.9f, -0.9f};
    if (bites < EAT_BITES) {
        float bx[EAT_BITES], by[EAT_BITES];
        for (int k = 0; k < bites; k++) {
            bx[k] = cosf(toward + ROUND[k]) * r;
            by[k] = sinf(toward + ROUND[k]) * r;
        }
        const int ir = (int)(r * 1.1f) + 1;
        for (int dy = -ir; dy <= ir; dy++) {
            for (int dx = -ir; dx <= ir; dx++) {
                const float fx = (float)dx, fy = (float)dy;
                uint16_t c;
                if (!food_px(f, fx / r, fy / r, dx, dy, &c)) continue;
                bool gone = false;
                for (int k = 0; k < bites && !gone; k++) {
                    const float ex = fx - bx[k], ey = fy - by[k];
                    gone = ex * ex + ey * ey <= br * br;
                }
                if (!gone) px(fb, cx + dx, cy + dy, c);
            }
        }
    }
    if (since >= 0.0f && since < 0.5f) {
        const float t = since / 0.5f;
        const uint16_t crumb = food_crumb(f);
        for (int k = 0; k < 4; k++) {
            const float a = toward + ROUND[(bites - 1 + EAT_BITES) % EAT_BITES] +
                            ((float)k - 1.5f) * 0.5f;
            const int x = cx + (int)lrintf(cosf(a) * r + ((float)k - 1.5f) * 9.0f * s * t);
            const int y = cy + (int)lrintf(sinf(a) * r + 70.0f * s * t * t);
            fill_circle(fb, x, y, (int)lrintf(3.0f * s), crumb);
        }
    }
}

/* THE POOP: one of eight (`poop_look`): the swirl every child draws, a tower, a puddle, a
 * pile of pellets or a log, in its own colour, with a face (a four-year-old will want it to
 * have one), stink lines rising off it and flies round it — as many as the kind says. `cx` is
 * its centre, `floor` the floor it sits on, `k` its size (it plops in and shrinks away). */
static int lift(int c)
{
    return c + 40 > 255 ? 255 : c + 40;
}

static void draw_poop_face(uint16_t *fb, int face, int fx, int ey, float u, float t)
{
    const uint16_t white = rgb(0xF6, 0xF9, 0xFC), ink = rgb(0x1E, 0x12, 0x08);
    const int ed = (int)lrintf(9.0f * u), er = (int)lrintf(4.5f * u) + 1;
    const int pr = (int)lrintf(2.2f * u) + 1, w = (int)lrintf(2.6f * u) + 1;
    const int my = ey + (int)lrintf(1.0f * u), mr = (int)lrintf(8.0f * u);
    const float pi = (float)M_PI;
    /* The eyes, by kind. */
    for (int side = -1; side <= 1; side += 2) {
        const int ex = fx + side * ed;
        const bool shut = face == POOP_SLEEPY || (face == POOP_WINK && side > 0);
        if (shut) {
            arc_stroke(fb, ex, ey - er / 2, er, pi * 0.15f, pi * 0.85f, w, ink);
        } else if (face == POOP_COOL) {
            fill_round_rect(fb, ex - er - 1, ey - er + 1, 2 * er + 2, er + er / 2 + 1, er / 2,
                            ink);
        } else {
            const int r = face == POOP_SHOCKED ? er + er / 3 : er;
            fill_circle(fb, ex, ey, r, white);
            const int look = face == POOP_QUEASY ? -pr : (int)lrintf(1.0f * u);
            fill_circle(fb, ex + (face == POOP_QUEASY ? 0 : look), ey + look,
                        face == POOP_SHOCKED ? pr - pr / 3 : pr, ink);
        }
        if (face == POOP_GRUMPY) {
            /* A brow sloping down to the middle: a short run of discs. */
            for (int i = 0; i <= 4; i++) {
                const int bx = ex - side * er + side * (er * i) / 2;
                fill_circle(fb, bx, ey - er - 2 - ((4 - i) * er) / 8, w / 2 + 1, ink);
            }
        }
    }
    if (face == POOP_COOL) fill_rect(fb, fx - ed + er, ey - er + 2, 2 * (ed - er), w, ink);
    /* The mouth. */
    switch (face) {
    case POOP_SHOCKED:
        fill_circle(fb, fx, my + mr / 2 + 2, mr / 2, ink);
        break;
    case POOP_SLEEPY:
        fill_circle(fb, fx, my + mr / 2, mr / 3 + 1, ink);
        break;
    case POOP_TONGUE:
        arc_stroke(fb, fx, my, mr, pi * 0.1f, pi * 0.9f, w, ink);
        fill_circle(fb, fx + mr / 3, my + mr, mr / 2, rgb(0xF2, 0x6B, 0x86));
        break;
    case POOP_GRUMPY:
    case POOP_QUEASY:
        arc_stroke(fb, fx, my + mr + mr / 2, mr, pi * 1.25f, pi * 1.75f, w, ink);
        if (face == POOP_QUEASY) {
            /* The sweat drop, sliding down and starting over. */
            const float d = fmodf(t * 0.7f, 1.0f);
            fill_circle(fb, fx + ed + er + 2, ey - er + (int)lrintf(d * 10.0f * u), er - er / 4,
                        rgb(0x8F, 0xD3, 0xF5));
        }
        break;
    case POOP_COOL:
        arc_stroke(fb, fx + mr / 3, my, mr - mr / 4, pi * 0.15f, pi * 0.6f, w, ink);
        break;
    default:
        arc_stroke(fb, fx, my, mr, pi * 0.2f, pi * 0.8f, w, ink);
        break;
    }
}

static void draw_poop(uint16_t *fb, int kind, int cx, int floor, float s, float k, float t)
{
    if (k <= 0.01f) return;
    const poop_look_t *lk = poop_look(kind);
    const uint16_t body = rgb(lk->r, lk->g, lk->b);
    const uint16_t shine = rgb(lift(lk->r), lift(lk->g), lift(lk->b));
    const float u = s * k;
#define U(v) ((int)lrintf((float)(v) * u))
    /* Tiers bottom up, each {width, height, rise}; and where the face sits and how high. */
    static const struct {
        int n;
        int w[4], h[4], y[4];
        int face_y, top;
    } TIERS[] = {
        [POOP_SWIRL] = {3, {58, 44, 30}, {20, 18, 16}, {0, 15, 28}, 24, 48},
        [POOP_TOWER] = {4, {58, 48, 38, 28}, {18, 16, 15, 14}, {0, 13, 25, 36}, 22, 58},
        [POOP_BLOB] = {2, {78, 40}, {22, 18}, {0, 12}, 14, 34},
        [POOP_LOG] = {1, {92}, {26}, {0}, 13, 30},
    };
    int fx = cx, face_y = 24, top = 48;
    if (lk->shape == POOP_PELLETS) {
        static const int PEL[][3] = {{-24, 11, 11}, {0, 12, 12}, {24, 11, 11}, {-12, 31, 11},
                                     {12, 31, 11}, {0, 49, 10}};
        for (unsigned i = 0; i < sizeof(PEL) / sizeof(PEL[0]); i++) {
            const int x = cx + U(PEL[i][0]), y = floor - U(PEL[i][1]), r = U(PEL[i][2]);
            fill_circle(fb, x, y, r, body);
            fill_circle(fb, x - r / 3, y - r / 3, r / 4 + 1, shine);
        }
        face_y = 13;
        top = 58;
    } else {
        const int sh = lk->shape < POOP_SHAPE_COUNT && lk->shape != POOP_PELLETS ? lk->shape : 0;
        for (int i = 0; i < TIERS[sh].n; i++) {
            const int w = U(TIERS[sh].w[i]), h = U(TIERS[sh].h[i]);
            const int y = floor - U(TIERS[sh].y[i]) - h;
            fill_round_rect(fb, cx - w / 2, y, w, h, h / 2, body);
            fill_round_rect(fb, cx - w / 2 + w / 5, y + h / 5, w / 4, h / 4 + 1, h / 8 + 1, shine);
        }
        if (lk->shape == POOP_SWIRL || lk->shape == POOP_TOWER) {
            fill_circle(fb, cx + U(5), floor - U(TIERS[sh].top), U(6), body); /* the curl */
        } else if (lk->shape == POOP_LOG) {
            /* Segments, so it reads as a log and not a sausage; the face at the front end, so
               the seams stay behind it, clear of its eyes (review). */
            const uint16_t seam = rgb(lk->r * 3 / 5, lk->g * 3 / 5, lk->b * 3 / 5);
            for (int j = -1; j <= 1; j += 2) {
                fill_rect(fb, cx + j * U(11) - U(19), floor - U(22), U(3) + 1, U(18), seam);
            }
            fx = cx + U(20);
        }
        face_y = TIERS[sh].face_y;
        top = TIERS[sh].top;
    }
    draw_poop_face(fb, lk->face, fx, floor - U(face_y), u, t);
#undef U
    if (k < 0.99f) return;
    /* Stink: more lines, rising higher and faster, the ranker it is; each on its own clock. */
    const uint16_t stink = rgb(0x9C, 0xC8, 0x6A);
    const int lines = lk->stink == 0 ? 0 : lk->stink + 1;
    const float speed = 0.5f + 0.25f * (float)lk->stink, rise = 50.0f + 18.0f * (float)lk->stink;
    for (int j = 0; j < lines; j++) {
        const float life = fmodf(t * speed + (float)j / (float)lines, 1.0f);
        const int bx = cx + (int)lrintf(((float)j - (float)(lines - 1) * 0.5f) * 16.0f * s);
        for (int d = 0; d < 8; d++) {
            const float q = life + (float)d * 0.035f;
            if (q > 1.0f) break;
            const int x = bx + (int)lrintf(sinf(q * 12.0f + (float)j) * 5.0f * s);
            const int y = floor - (int)lrintf(((float)top + 12.0f + q * rise) * s);
            fill_circle(fb, x, y, (int)lrintf(2.5f * s * (1.0f - q)) + 1, stink);
        }
    }
    /* Flies: each on its own figure of eight round the top, wings beating. A grey body, not
       a black one: black on the black floor is just the wings, and those read as eyes. */
    const uint16_t fly = rgb(0x6A, 0x70, 0x7C), wing = rgb(0xD8, 0xE6, 0xF0);
    for (int f = 0; f < lk->flies; f++) {
        const float a = t * (2.6f + 0.7f * (float)f) + (float)f * 2.1f;
        const int x = cx + (int)lrintf(sinf(a) * (38.0f + 6.0f * (float)f) * s);
        const int y = floor - (int)lrintf(((float)top + 6.0f + sinf(2.0f * a) * 12.0f) * s);
        const int r = (int)lrintf(3.0f * s) + 1;
        const int flap = (int)(t * 30.0f + (float)f) % 2 ? r : r / 2;
        fill_circle(fb, x - r, y - flap, r, wing);
        fill_circle(fb, x + r, y - flap, r, wing);
        fill_circle(fb, x, y, r, fly);
        fill_circle(fb, x + r + r / 2, y + r / 3, r - r / 3, fly); /* its head, leading */
    }
}

/* TICKLE MARKS: two arcs either side of the body, the way a comic draws a squirm, flickering
 * out of step side to side so they read as something happening TO him. `cx, cy` is the body's
 * centre and `half` its half width; both forms' bodies are 144 wide. */
static void draw_tickle(uint16_t *fb, const face_state_t *st, int cx, int cy, int half, float s)
{
    if (st->fig.extra != EXTRA_TICKLE) return;
    const float p = st->fig.extra_p;
    if (p > 0.8f) return; /* gone as he gets his breath back */
    const uint16_t mark = rgb(0xFF, 0xF0, 0xA0);
    const int w = (int)lrintf(7.0f * s);
    for (int side = -1; side <= 1; side += 2) {
        const float flick = sinf(p * (float)M_PI * 34.0f + (side > 0 ? 0.0f : 1.6f));
        if (flick < -0.3f) continue; /* each side blinks off now and then */
        const float a0 = side > 0 ? -0.55f : (float)M_PI - 0.55f;
        for (int k = 0; k < 2; k++) {
            const int r = half + (int)lrintf((14.0f + 16.0f * (float)k + 4.0f * flick) * s);
            arc_stroke(fb, cx, cy, r, a0, a0 + 1.1f, w, mark);
        }
    }
}

/* A FILLED TRIANGLE, by rows: what the party hat, the game's things and their rotations need,
   since a 2D rig with no rotation draws turned shapes as triangles turned in source space. */
static void fill_tri(uint16_t *fb, float x0, float y0, float x1, float y1, float x2, float y2,
                     uint16_t c)
{
    float t;
    if (y1 < y0) { t = y0; y0 = y1; y1 = t; t = x0; x0 = x1; x1 = t; }
    if (y2 < y0) { t = y0; y0 = y2; y2 = t; t = x0; x0 = x2; x2 = t; }
    if (y2 < y1) { t = y1; y1 = y2; y2 = t; t = x1; x1 = x2; x2 = t; }
    if (y2 - y0 < 0.5f) return;
    int ya = (int)ceilf(y0 - 0.5f), yb = (int)floorf(y2 - 0.5f);
    if (ya < 0) ya = 0;
    if (yb >= FACE_H) yb = FACE_H - 1;
    for (int y = ya; y <= yb; y++) {
        const float fy = (float)y + 0.5f;
        const float xl = x0 + (x2 - x0) * (fy - y0) / (y2 - y0);
        float xr;
        if (fy < y1) xr = y1 - y0 > 0.0f ? x0 + (x1 - x0) * (fy - y0) / (y1 - y0) : x1;
        else xr = y2 - y1 > 0.0f ? x1 + (x2 - x1) * (fy - y1) / (y2 - y1) : x1;
        const float lo = xl < xr ? xl : xr, hi = xl < xr ? xr : xl;
        hspan(fb, y, (int)lrintf(lo), (int)lrintf(hi) - 1, c);
    }
}

/* A SHAPE IN ITS OWN TURNED FRAME: centre, rotation and size, so the hat and each of the
   game's things can be drawn in their own units and come out turned. */
typedef struct {
    float cx, cy, c, s, r;
} xf_t;

static xf_t xf(float cx, float cy, float a, float r)
{
    const xf_t t = {cx, cy, cosf(a), sinf(a), r};
    return t;
}

static float xx(const xf_t *t, float u, float v)
{
    return t->cx + (u * t->c - v * t->s) * t->r;
}

static float xy(const xf_t *t, float u, float v)
{
    return t->cy + (u * t->s + v * t->c) * t->r;
}

static void t_tri(uint16_t *fb, const xf_t *t, float u0, float v0, float u1, float v1, float u2,
                  float v2, uint16_t c)
{
    fill_tri(fb, xx(t, u0, v0), xy(t, u0, v0), xx(t, u1, v1), xy(t, u1, v1), xx(t, u2, v2),
             xy(t, u2, v2), c);
}

static void t_quad(uint16_t *fb, const xf_t *t, float u, float v, float w, float h, uint16_t c)
{
    t_tri(fb, t, u, v, u + w, v, u + w, v + h, c);
    t_tri(fb, t, u, v, u + w, v + h, u, v + h, c);
}

static void t_circ(uint16_t *fb, const xf_t *t, float u, float v, float r, uint16_t c)
{
    fill_circle(fb, (int)lrintf(xx(t, u, v)), (int)lrintf(xy(t, u, v)), (int)lrintf(r * t->r), c);
}

/* THE PARTY HAT (0.4.14): a striped cone with spots, a white brim and a pompom, dropped onto
   the head over the first tenth of the song with a bounce, then worn at a jaunty angle. Sized
   from the head it sits on, so it fits the robot's 216 px head and the bird's 128 alike. */
static void draw_party_hat(uint16_t *fb, int cx, int top, int half, float s, float p)
{
    const float k = p < 0.1f ? p / 0.1f : 1.0f;
    const float e = k * k * (3.0f - 2.0f * k);
    const float drop = (1.0f - e) * -260.0f * s - sinf(k * (float)M_PI) * 30.0f * s;
    const float w = 1.04f * (float)half, h = 1.18f * w;
    const xf_t t = xf((float)cx + 0.24f * (float)half, (float)top + 0.09f * (float)half + drop,
                      0.22f + 0.03f * sinf(p * 40.0f), 1.0f);
    const uint16_t pink = rgb(0xFF, 0x5F, 0xA2), yellow = rgb(0xFF, 0xE1, 0x4D);
    const uint16_t blue = rgb(0x4D, 0xD2, 0xFF), white = rgb(0xF6, 0xF9, 0xFC);
    t_tri(fb, &t, -w / 2, 0, w / 2, 0, 0, -h, pink);
    /* Two stripes across the cone: each a slice of the triangle between two heights. */
    for (int band = 0; band < 2; band++) {
        const float f0 = 0.22f + 0.3f * (float)band, f1 = f0 + 0.13f;
        const float a0 = w / 2 * (1.0f - f0), a1 = w / 2 * (1.0f - f1);
        t_tri(fb, &t, -a0, -h * f0, a0, -h * f0, a1, -h * f1, yellow);
        t_tri(fb, &t, -a0, -h * f0, a1, -h * f1, -a1, -h * f1, yellow);
    }
    t_circ(fb, &t, -w * 0.14f, -h * 0.12f, w * 0.06f, blue);
    t_circ(fb, &t, w * 0.12f, -h * 0.44f, w * 0.05f, blue);
    t_quad(fb, &t, -w / 2 - w * 0.04f, -w * 0.07f, w * 1.08f, w * 0.14f, white);
    t_circ(fb, &t, 0, -h, w * 0.12f, white);
}

/* CONFETTI over everything, a pure function of the action's progress: each piece has its own
   column, start, speed, sway and flutter from its index, so the same moment draws the same
   picture and nothing is stored. All of it has landed by the end. */
static void draw_confetti(uint16_t *fb, float p, float scale)
{
    static const uint8_t COLS[6][3] = {{0xFF, 0x5F, 0xA2}, {0xFF, 0xE1, 0x4D}, {0x4D, 0xD2, 0xFF},
                                       {0x7C, 0xF0, 0x8B}, {0xB9, 0x8C, 0xFF}, {0xFF, 0x8A, 0x3D}};
    const float secs = p * 7.0f;
    for (int i = 0; i < 70; i++) {
        uint32_t hsh = (uint32_t)i * 2654435761u;
        const float r1 = (float)((hsh >> 8) & 0xFFFF) / 65535.0f;
        hsh = hsh * 1103515245u + 12345u;
        const float r2 = (float)((hsh >> 8) & 0xFFFF) / 65535.0f;
        hsh = hsh * 1103515245u + 12345u;
        const float r3 = (float)((hsh >> 8) & 0xFFFF) / 65535.0f;
        const float t = secs - r2 * 2.1f;
        if (t < 0.0f) continue;
        const float y = -16.0f + t * (110.0f + 70.0f * r3) * scale;
        if (y > (float)FACE_H + 12.0f) continue;
        const float x = r1 * (float)FACE_W + sinf(t * (1.5f + 2.0f * r3) + r1 * 6.0f) * 14.0f;
        const float flip = fabsf(cosf(t * (4.0f + 5.0f * r2) + r3 * 6.0f));
        const int w = 2 + (int)lrintf(6.0f * flip * scale), h = (int)lrintf((8.0f + 5.0f * r3) * scale);
        const uint8_t *c = COLS[i % 6];
        fill_rect(fb, (int)lrintf(x) - w / 2, (int)lrintf(y) - h / 2, w, h, rgb(c[0], c[1], c[2]));
    }
}

static void draw_overhead(uint16_t *fb, const face_state_t *st, int cx, int top, int half,
                          float s)
{
    if (st->fig.extra == EXTRA_PARTY) draw_party_hat(fb, cx, top, half, s, st->fig.extra_p);
    const float p = st->fig.extra_p;
    if (st->fig.extra == EXTRA_STARS) {
        const uint16_t star = rgb(0xFF, 0xD8, 0x4A);
        /* Eight turns over 6.4 s: the speed four turns had over 3.2 s (0.4.10). */
        const float in = p < 0.05f ? p / 0.05f : p > 0.94f ? (1.0f - p) / 0.06f : 1.0f;
        for (int k = 0; k < 3; k++) {
            const float a = p * 2.0f * (float)M_PI * 8.0f + (float)k * 2.0f * (float)M_PI / 3.0f;
            const float depth = sinf(a);
            const int x = cx + (int)lrintf(cosf(a) * ((float)half + 16.0f * s));
            const int y = top - (int)(10.0f * s) + (int)lrintf(depth * 14.0f * s);
            draw_star(fb, x, y, (int)lrintf((17.0f + 6.0f * depth) * s * in), star);
        }
    } else if (st->fig.extra == EXTRA_HEARTS) {
        const uint16_t heart = rgb(0xFF, 0x4F, 0x7B);
        /* Twelve, each rising for the 1.2 s and following the last by the 0.36 s the five did
           over 2.8 s: the love lasts twice as long, not twice as slow (0.4.10). */
        for (int k = 0; k < 12; k++) {
            const float t = (p - (float)k * 0.065f) / 0.21f; /* this heart's own life, 0..1 */
            if (t <= 0.0f || t >= 1.0f) continue;
            const int side = (k % 2) ? 1 : -1;
            const float grow = t < 0.15f ? t / 0.15f : t > 0.75f ? (1.0f - t) / 0.25f : 1.0f;
            const float out = ((float)half + 6.0f * s) * (0.75f + 0.1f * (float)(k % 5));
            const int x = cx + side * (int)lrintf(out) +
                          (int)lrintf(sinf(t * 2.0f * (float)M_PI * 1.5f) * 10.0f * s);
            const int y = top + (int)lrintf((90.0f - 190.0f * t) * s);
            draw_heart(fb, x, y, (int)lrintf((15.0f + 4.0f * (float)(k % 3)) * s * grow), heart);
        }
    }
}

/* THE OSTRICH. Every number is transcribed from `docs/mocks/room-endpoint/ostrich-mock.py`,
 * whose helpers mirror the primitives above — so this is a port, not a redesign, in exactly
 * the way `face.c`'s robot was a port of the canvas mock.
 *
 * Two traps the mock records, because this code would hit both:
 *   * `draw_limb` takes 0 as straight DOWN and POSITIVE as swinging LEFT. Negative angles put
 *     the tail plumes behind the body, invisible.
 *   * the legs must be LONG. Leg length is the ostrich silhouette; short reads as a duck.
 */
#define OS_HEAD_Y (-186) /* everything below is relative to `oy`, as the robot's numbers are */
#define OS_HEAD_W 128
#define OS_HEAD_H 98
#define OS_EYE_Y (-144)
#define OS_EYE_X 31
#define OS_BODY_Y (-22)
#define OS_BODY_W 144
#define OS_BODY_H 112
#define OS_LEG_Y 74
#define OS_LEG_L 92
#define OS_HEAD_DX 7 /* the head sits slightly forward of the body, as a bird's does */

/* The ostrich's wing, drawn wherever the peekaboo lerp has put it. Separated out because it
 * has to be drawn in two different PLACES in the order: behind the body at rest, and over the
 * head once it is covering the eyes. */
static void draw_wing(uint16_t *fb, int x, int y, int w, int h, float s, uint16_t wing,
                      uint16_t col)
{
    fill_round_rect(fb, x, y, w, h, (int)(30.0f * s), wing);
    /* Scalloped line-work: the detail that says "this bird" rather than "a bird". */
    for (int r = 20; r <= 48; r += 14) {
        arc_stroke(fb, x + (int)(2.0f * s), y + (int)(8.0f * s), (int)((float)r * s),
                   (float)M_PI * 0.06f, (float)M_PI * 0.44f, (int)(3.0f * s), col);
    }
}

static face_zone_t zone_robot(int dx, int dy)
{
    /* Head: the rounded box the head is drawn in, plus the antenna above it, which is part of
       him and is the most obvious thing to poke. */
    if (dy <= HEAD_Y + HH && dy >= HEAD_Y - HH - 48) {
        if (dx >= -HW && dx <= HW) return ZONE_HEAD;
    }
    if (dy >= -26 && dy <= 106 && dx >= -72 && dx <= 72) return ZONE_BODY;
    /* Arms hang either side of the torso from the shoulder, so anything outside the torso's
       width but within the arm's reach is an arm. */
    if (dy >= SHOULDER_Y - 20 && dy <= SHOULDER_Y + ARM_L + 20) {
        if ((dx < -50 && dx >= -110) || (dx > 50 && dx <= 110)) return ZONE_ARM;
    }
    if (dy > 106 && dy <= HIP_Y + LEG_L + 24 && dx >= -80 && dx <= 80) return ZONE_LEG;
    return ZONE_NONE;
}

static face_zone_t zone_ostrich(int dx, int dy)
{
    /* Head first, and it owns the crest above it and the NECK below it. A neck is the most
       inviting thing on a bird to touch and it belongs with the head, not the body. */
    if (dy >= OS_HEAD_Y - 52 && dy <= OS_HEAD_Y + OS_HEAD_H && dx >= -60 && dx <= 74) {
        return ZONE_HEAD;
    }
    if (dy > OS_HEAD_Y + OS_HEAD_H && dy < OS_BODY_Y && dx >= -34 && dx <= 40) {
        return ZONE_HEAD; /* the neck */
    }
    /* Wing and tail BEFORE the body, because both sit inside the body's box and a poke that
       lands on the wing should answer as a wing. */
    if (dy >= -4 && dy <= 70 && dx >= 0 && dx <= 80) return ZONE_ARM;       /* the wing */
    if (dy >= -58 && dy <= 18 && dx <= -44 && dx >= -148) return ZONE_ARM;  /* the tail */
    if (dy >= OS_BODY_Y && dy <= OS_BODY_Y + OS_BODY_H && dx >= -OS_BODY_W / 2 &&
        dx <= OS_BODY_W / 2) {
        return ZONE_BODY;
    }
    if (dy > OS_BODY_Y + OS_BODY_H && dy <= OS_LEG_Y + OS_LEG_L + 26 && dx >= -56 &&
        dx <= 56) {
        return ZONE_LEG;
    }
    return ZONE_NONE;
}

/* Whether (x, y) can actually be SEEN, given the case's rounded corners. The straight edges
   are all inside; only the four corner quadrants curve away. Used by whatever must be visible
   rather than merely drawn — see `FACE_CASE_CORNER_R`. */
bool face_inside_case(int x, int y, int w, int h)
{
    const int r = FACE_CASE_CORNER_R;
    if (x < 0 || y < 0 || x >= w || y >= h) return false;
    const int cx = x < r ? r : (x >= w - r ? w - 1 - r : x);
    const int cy = y < r ? r : (y >= h - r ? h - 1 - r : y);
    const int dx = x - cx, dy = y - cy;
    return dx * dx + dy * dy <= r * r;
}

face_zone_t face_zone(face_form_t form, int x, int y, bool upside_down, int lean)
{
    /* The frame the child sees is the framebuffer rotated 180 degrees when inverted, so undo
       that before asking where on the FIGURE the finger landed. */
    if (upside_down) {
        x = FACE_W - 1 - x;
        y = FACE_H - 1 - y;
    }
    /* THE SAME TRANSFORM `face_draw` USES, INVERTED — and it has to be the same one or the
       zones drift away from the drawing. Side-mounted, `s_fit` shrinks the figure by a sixth
       and `s_fit_oy` moves its origin into the square; a zone test that ignored both asked
       where the finger landed on a figure that is not the one on the glass, which is the
       quiet half of the owner's "the indicators do not indicate where I actually tapped". */
    const int ox = OX + (int)lrintf((float)lean * s_fit);
    const int oy = s_fit_oy >= 0 ? s_fit_oy : OY;
    const int dx = (int)lrintf((float)(x - ox) / s_fit);
    const int dy = (int)lrintf((float)(y - oy) / s_fit);
    return form == FORM_ROBOT ? zone_robot(dx, dy) : zone_ostrich(dx, dy);
}

/* THE FIGURE TRANSFORM, shared by every form. Scaling is applied to coordinates as they are
   computed rather than to a finished bitmap, which is why squash and stretch cost nothing
   here beyond the multiply. */
#define SX(v) ((int)lrintf((float)(v) * sx))
#define SY(v) ((int)lrintf((float)(v) * sy))
#define LERPI(a, b, t) ((int)lrintf((float)(a) + ((float)(b) - (float)(a)) * (t)))

static void draw_robot(uint16_t *fb, uint32_t hex, const face_state_t *st, int ox, int oy,
                       float sx, float sy, float s, int tilt)
{
    const uint16_t col = shade(hex, 1.0f);
    const uint16_t dark = shade(hex, 0.22f);
    const uint16_t torso = shade(hex, 0.88f);
    const uint16_t plate = shade(hex, 0.55f);
    const uint16_t limb = shade(hex, 0.78f);

    const int hy = oy + SY(HEAD_Y);
    if (st->fig.extra == EXTRA_PUFF) draw_puff(fb, ox - SX(96), oy + SY(104), s);
    if (st->fig.extra == EXTRA_GAS) draw_gas(fb, ox - SX(96), oy + SY(104), s, st->fig.extra_p);

    /* Drawing order is the mock's: legs behind everything, arms behind the torso. */
    draw_limb(fb, ox - SX(34), oy + SY(HIP_Y), st->rig.leg_l, SY(LEG_L), SX(30), limb);
    draw_limb(fb, ox + SX(34), oy + SY(HIP_Y), st->rig.leg_r, SY(LEG_L), SX(30), limb);
    draw_limb(fb, ox - SX(66), oy + SY(SHOULDER_Y), st->rig.arm_l, SY(ARM_L), SX(28), limb);
    draw_limb(fb, ox + SX(66), oy + SY(SHOULDER_Y), st->rig.arm_r, SY(ARM_L), SX(28), limb);

    fill_round_rect(fb, ox - SX(72), oy - SY(26), SX(144), SY(132), (int)(40 * s), torso);
    fill_round_rect(fb, ox - SX(26), oy + SY(10), SX(52), SY(40), (int)(12 * s), plate);

    /* Antenna first: it sits behind the head, as the mock's silhouette pass does. */
    fill_rect(fb, ox + tilt - SX(4), hy - SY(HH + 30), SX(8), SY(30), col);
    fill_circle(fb, ox + tilt, hy - SY(HH + 36), (int)(11 * s), col);

    fill_round_rect(fb, ox + tilt - SX(HW), hy - SY(HH), SX(HW * 2), SY(HH * 2),
                    (int)(40 * s), col);

    /* ON the face, after it, or the head paints over it — which is exactly what used to
       happen. Cheeks: outboard of the eyes and level with the smile. */
    if (st->fig.extra == EXTRA_BLUSH) draw_blush(fb, ox + tilt, hy + SY(26), SX(78), 0, s);

    /* NO FACE ON THE BACK OF A HEAD. `facing` goes negative for the half of a spin the
       figure is turned away (rig.h), and the squash alone does not read as a turn — a face
       that stays put while the body narrows reads as the body being crushed. Dropping the
       eyes and the smile for that half is what makes it a spin. */
    const bool front = st->fig.facing >= 0.0f;
    const int ex = SX((int)(HW * 0.43f)), ey = hy - SY((int)(HH * 0.17f));
    if (front) {
        draw_eye(fb, ox + tilt - ex, ey, dark, &st->eyes.l, st->open, st->startle, s, 0.0f);
        draw_eye(fb, ox + tilt + ex, ey, dark, &st->eyes.r, st->open, st->startle, s, 0.0f);
    }

    /* Smile: arc(cx, cy-16, 30) from 0.15pi to 0.85pi, stroked 9 wide. */
    if (front) {
        const int mx = ox + tilt, my = hy + SY((int)(HH * 0.52f)) - SY(16);
        const int mr = (int)(30 * s);
        /* THE SMILE IS THE LOWER LIP, NOT A SECOND MOUTH.
         *
           The owner: *"when changing to the robot, when it speaks the happy face doesn't go
           away while the mouth appears, which looks really weird."* Correct, and the first
           version earned it — the open mouth was a rounded box drawn UNDER the smile arc, on
           the theory that the smile would read as its lip. It does not. A curved smile with a
           rectangle below it reads as two mouths, because that is what it is.
         *
           So the opening is bounded BY the arc: for every column across the smile, fill from
           the arc's own y upward by the gape, tapered to nothing at the corners so the hole is
           a lens rather than a band. That is the cartoon convention — the mouth opens out of
           the smile line, and the smile becomes the bottom of it — and it collapses exactly to
           the untouched arc as the gape goes to zero, with no pop and no second shape. */
        const int gape = (int)lrintf(fmaxf(st->talk, st->fig.chew) * 26.0f * s);
        if (gape > 0) {
            /* 0.15pi is where the arc starts, so its half-width is r*cos(0.15pi). */
            const int xmax = (int)(cosf((float)M_PI * 0.15f) * (float)mr);
            for (int dx = -xmax; dx <= xmax; dx++) {
                const float t = (float)dx / (float)xmax;
                const int lip = my + (int)lrintf(sqrtf((float)(mr * mr - dx * dx)));
                const int open = (int)lrintf((float)gape * sqrtf(1.0f - t * t));
                for (int y = lip - open; y <= lip; y++) px(fb, mx + dx, y, dark);
            }
        }
        /* The lip last, so the opening never eats its own edge. */
        arc_stroke(fb, mx, my, mr, (float)M_PI * 0.15f, (float)M_PI * 0.85f, (int)(9 * s),
                   dark);
    }

    /* A RAISED ARM IS REDRAWN OVER THE HEAD. The head is 216 px wide and the shoulder sits
       INSIDE it at ox+66, so an arm posed above the shoulder has only 42 px of head to clear
       and disappears behind the face — which is why `wave` changed a third as many pixels as
       any other action on this form. Redrawing the same limb is idempotent, so the arm simply
       stops being occluded by the head it is waving beside. */
    if (fabsf(st->rig.arm_l) > 95.0f) {
        draw_limb(fb, ox - SX(66), oy + SY(SHOULDER_Y), st->rig.arm_l, SY(ARM_L), SX(28), limb);
    }
    if (fabsf(st->rig.arm_r) > 95.0f) {
        draw_limb(fb, ox + SX(66), oy + SY(SHOULDER_Y), st->rig.arm_r, SY(ARM_L), SX(28), limb);
    }

    /* PEEKABOO LAST, over the eyes it is hiding. Posing the arms by angle cannot do this —
       the hands have to AIM at the eyes, which is the difference between hiding and squatting
       in a corner (`rig.ts` on the shipped dud this replaces). */
    if (st->rig.hands_up > 0.01f) {
        const float u = st->rig.hands_up > 1.0f ? 1.0f : st->rig.hands_up;
        const int rest_x = SX(66), rest_y = oy + SY(SHOULDER_Y) + SY(ARM_L);
        const int hx = (int)lrintf((float)rest_x + ((float)(ex) - (float)rest_x) * u);
        const int hyy = (int)lrintf((float)rest_y + ((float)ey - (float)rest_y) * u);
        /* The hand GROWS into a paw as it rises. A 17 px knob against a 41x48 eye is what
           made the shipped `hide` read as "arms up beside the head": measured, hands-up hid
           only 35% of the eye white. Covering it needs a radius past the eye's corner. */
        const int hr = (int)((18.0f + 14.0f * u) * s);
        fill_circle(fb, ox + tilt - hx, hyy, hr, limb);
        fill_circle(fb, ox + tilt + hx, hyy, hr, limb);
    }
    /* THE COOKIE, carried from beside the body to the corner of the mouth: the arm goes up
       behind the head, so the cookie is what the eye follows. Drawn last, in front of the face
       it is being eaten by. */
    if (st->fig.extra == EXTRA_FOOD) {
        const float r = st->fig.reach;
        const int fx0 = ox + SX(130), fy0 = oy - SY(31);
        const int fx1 = ox + tilt + SX(58), fy1 = hy + SY(34);
        float since;
        const int bites = rig_eat_bites(st->fig.extra_p, &since);
        draw_food(fb, st->food, LERPI(fx0, fx1, r), LERPI(fy0, fy1, r), s, bites, since,
                  (float)M_PI * 1.1f);
    }
    draw_overhead(fb, st, ox + tilt, hy - SY(HH), SX(HW), s);
    draw_tickle(fb, st, ox, oy + SY(40), SX(72), s);
}

static void draw_ostrich(uint16_t *fb, uint32_t hex, const face_state_t *st, int ox, int oy,
                         float sx, float sy, float s, int tilt)
{
    const uint16_t col = shade(hex, 1.00f);  /* crest and tail: the brightest accents */
    const uint16_t head = shade(hex, 0.94f);
    const uint16_t body = shade(hex, 0.86f);
    const uint16_t neck = shade(hex, 0.76f);
    const uint16_t leg = shade(hex, 0.70f);
    const uint16_t beak = shade(hex, 0.52f);
    const uint16_t wing = shade(hex, 0.64f);
    const uint16_t dark = shade(hex, 0.22f);

    /* The two channels that carry most of this form's motion. `neck` leans the neck and the
       head together — applied in full at the head and pro-rata down the neck, so the head
       leads and the neck follows instead of shearing off it. `bob` rides the head only. */
    const int lean = SX(st->rig.neck * 0.55f);
    const int bob = SY(st->rig.bob);
    const int hx = ox + tilt + lean + SX(OS_HEAD_DX);
    const int hy = oy + SY(OS_HEAD_Y) + bob;
    /* THE HEAD RIGHTING ITSELF (`face_state_t.upright`): `tilt` already carries it to the
       head as a swing about the neck's root, and the features turn about the head's centre so
       the face reads as turned, not merely moved. The head's own outline is not turned — it is
       nearly round, and resampling it is what `rig.h` refuses. */
    const float ur = st->upright * (float)M_PI / 180.0f;
    const float usin = sinf(ur), ucos = cosf(ur);
    /* A feature at (hx + dx, oy + y) upright, turned about the head's centre. Written so that
       at zero it is EXACTLY the old arithmetic, pixel for pixel. */
#define HC_Y (OS_HEAD_Y + OS_HEAD_H / 2)
    const int hcy = oy + SY(HC_Y) + bob;
#define RX(dx, y) (hx + (int)lrintf((float)SX(dx) * ucos - (float)SY((y) - HC_Y) * usin))
#define RY(dx, y)                                                                          \
    (oy + SY(y) + bob +                                                                    \
     (int)lrintf((float)SX(dx) * usin + (float)SY((y) - HC_Y) * (ucos - 1.0f)))
    /* Behind the bird and BELOW the plumes: the robot's anchor hung off the HEAD, so the
       offset that put its cloud by the hips put the ostrich's up at its midriff. */
    if (st->fig.extra == EXTRA_PUFF) draw_puff(fb, ox - SX(100), oy + SY(52), s);
    if (st->fig.extra == EXTRA_GAS) draw_gas(fb, ox - SX(100), oy + SY(52), s, st->fig.extra_p);

    /* Tail plumes first, behind the body. They FLAP with the arm pose — a bird has no arms,
       so the rig's arm angle drives the one thing on a bird that answers to it.
       BOTH arms, because several actions (wave, burp) move only the right one: driving the
       flap off `arm_l` alone made ACT_WAVE change exactly zero pixels on this form.

       AVERAGING the two was worse than the bug it fixed. Dance, bop, shimmy, wiggle and
       giggle swing both arms the SAME way, so the mean of the two deviations is a constant
       and the tail did not move for any of them — one action rescued, five broken. The
       larger deviation, signed, tracks whichever arm is actually doing something. */
    const float dl = st->rig.arm_l - 12.0f;
    const float dr = -st->rig.arm_r - 12.0f;
    const float dev = fabsf(dl) >= fabsf(dr) ? dl : dr;
    const float flap = dev * 0.35f + st->rig.tail;
    static const struct {
        float deg;
        int len;
    } TAIL[] = {{108.0f, 76}, {126.0f, 88}, {144.0f, 72}};
    for (unsigned i = 0; i < sizeof(TAIL) / sizeof(TAIL[0]); i++) {
        draw_limb(fb, ox - SX(56), oy + SY(6), TAIL[i].deg + flap, SY(TAIL[i].len), SX(16), col);
    }

    /* Legs and three-toed feet. The toes are what make it a bird rather than a stand.
       A bird drawn head-on cannot step fore-and-aft — there is no depth to step into — so
       `step` reads as a LIFT: the raised leg shortens and swings out, alternating. */
    const float lift = st->rig.step;
    for (int side = -1; side <= 1; side += 2) {
        const int lx = ox + SX(side * 30);
        const float up = side < 0 ? fmaxf(0.0f, lift) : fmaxf(0.0f, -lift);
        const float deg =
            (side < 0 ? st->rig.leg_l : st->rig.leg_r) + (side < 0 ? up : -up) * 0.5f;
        const int len = SY(OS_LEG_L) - (int)((float)SY(OS_LEG_L) * fminf(0.45f, up / 60.0f));
        draw_limb(fb, lx, oy + SY(OS_LEG_Y), deg, len, SX(20), leg);
        const float a = deg * (float)M_PI / 180.0f;
        const int ex = lx + (int)lrintf(-sinf(a) * (float)len);
        const int ey = oy + SY(OS_LEG_Y) + (int)lrintf(cosf(a) * (float)len);
        for (int t = -1; t <= 1; t++) {
            draw_limb(fb, ex, ey, deg + (float)t * 62.0f, SY(19), SX(11), leg);
        }
    }

    /* Body: an egg, wider than tall. */
    fill_round_rect(fb, ox - SX(OS_BODY_W / 2), oy + SY(OS_BODY_Y), SX(OS_BODY_W),
                    SY(OS_BODY_H), (int)(54 * s), body);

    /* PEEKABOO RIDES THE WING: a bird tucks its head under a wing, which is a better answer
       than the robot's hands over the eyes. Two things the first cut got wrong, both found by
       counting eye-white pixels rather than by looking:
         - a 74-wide wing parked at the eye line covers ONE eye and half the other, so it
           GROWS as it rises;
         - it was drawn before the head, which then painted straight over it — so once it is
           actually hiding, it is drawn LAST instead. */
    const float u = st->rig.hands_up > 1.0f ? 1.0f : st->rig.hands_up;
    const int wx = LERPI(ox + SX(2), hx - SX(72), u);
    const int wy = LERPI(oy, oy + SY(OS_EYE_Y - 34) + bob, u);
    const int ww = LERPI(SX(74), SX(144), u);
    const int wh = LERPI(SY(66), SY(88), u);
    if (u <= 0.01f) draw_wing(fb, wx, wy, ww, wh, s, wing, col);
    /* THE COOKIE, on the wing, where the beak comes down to at the bottom of a peck: before
       the neck and head, so the beak goes over it. Bitten from the top, where the beak is. */
    if (st->fig.extra == EXTRA_FOOD) {
        float since;
        const int bites = rig_eat_bites(st->fig.extra_p, &since);
        draw_food(fb, st->food, ox + SX(26), oy + SY(30), s, bites, since, -(float)M_PI * 0.5f);
    }

    /* Neck: segments narrowing toward the head, leaning forward for life. Each segment
       carries its share of the head's total displacement — the segments used to be drawn at
       a bare `ox`, so any tilt or lean tore the head clean off the top of the neck. */
    for (int i = 0; i < 7; i++) {
        const float t = (float)i / 6.0f;
        const int seg_w = (int)(50.0f - 16.0f * t);
        const int nx = ox + (int)lrintf((float)(tilt + lean) * t) + SX((int)(7.0f * t));
        fill_round_rect(fb, nx - SX(seg_w / 2),
                        oy + SY(-26 - i * 14) + (int)lrintf((float)bob * t), SX(seg_w),
                        SY(11), (int)(5 * s), neck);
    }

    /* Crest: three thin plumes, the signature of the silhouette. 180 turns them upward. */
    static const struct {
        float deg;
        int len;
    } CREST[] = {{-18.0f, 42}, {0.0f, 50}, {18.0f, 42}};
    for (unsigned i = 0; i < sizeof(CREST) / sizeof(CREST[0]); i++) {
        draw_limb(fb, RX(0, -182), RY(0, -182),
                  CREST[i].deg + 180.0f + st->rig.crest + st->upright, SY(CREST[i].len), SX(9),
                  col);
    }

    fill_round_rect(fb, hx - SX(OS_HEAD_W / 2), hy, SX(OS_HEAD_W), SY(OS_HEAD_H),
                    (int)(44 * s), head);

    /* Beak: a wedge that PROTRUDES below the head, or it reads as a chin. It comes off with
       the eyes when the figure is turned away — a beak is the most front-facing thing on a
       bird, and leaving it on a back view is what would give the trick away. */
    const bool front = st->fig.facing >= 0.0f;
    if (front) {
        static const struct {
            int w, h, y;
        } BEAK[] = {{42, 13, -112}, {33, 12, -101}, {23, 11, -91}, {13, 10, -82}};
        /* THE LOWER MANDIBLE DROPS, the upper one does not — which is how a beak opens and
           the reason this is not just "make the whole beak bigger".
         *
           MUCH BIGGER THAN THE FIRST ATTEMPT, and the owner's verdict is the measurement:
           *"the mouth movement is definitely not big enough or obvious enough that his mouth
           is moving for talking."* 11 px of drop, split so only the narrow tip moved, was a
           third of the beak twitching on a 29 mm screen — legible in a host render, invisible
           across a bedroom. So the hinge moves UP (only the widest segment is the upper
           mandible, the other three swing as one jaw) and the gape goes to 30, which is most
           of the beak's own height. A talking mouth has to read from the far side of a room
           or it is not doing the job the animation exists for. */
        const int gape = (int)lrintf(fmaxf(st->talk, st->fig.chew) * 30.0f);
        for (unsigned i = 0; i < sizeof(BEAK) / sizeof(BEAK[0]); i++) {
            /* Progressive, so the jaw pivots at the hinge instead of sliding down in one
               piece: the further from the joint, the further it travels. */
            const int drop = i >= 1 ? SY(gape * (int)i / 3) : 0;
            /* Laid out upright, then turned whole about the head's centre — the jaw's drop
               included, so it opens along the turned head's down and not the glass's. */
            fill_turned_round_rect(fb, hx - SX(BEAK[i].w / 2), oy + SY(BEAK[i].y) + bob + drop,
                                   SX(BEAK[i].w), SY(BEAK[i].h), (int)(5 * s), hx, hcy,
                                   st->upright, beak);
        }
        fill_turned_round_rect(fb, hx - SX(16), oy + SY(-97) + bob, SX(32), SY(2), 0, hx, hcy,
                               st->upright, shade(hex, 0.28f));
    }

    /* Cheeks, after the head and before the eyes. Outboard of the eyes and inboard of the
       head's edge, which on a 128-wide head leaves exactly this much room. */
    if (st->fig.extra == EXTRA_BLUSH) {
        draw_blush(fb, RX(0, -110), RY(0, -110), (int)lrintf((float)SX(50) * ucos),
                   (int)lrintf((float)SX(50) * usin), s);
    }

    /* THE ROBOT'S EYES, UNCHANGED. Six emotions already work as lid geometry; a form that
       redrew them would have to re-implement all six.
       1.10, not the mock's 0.98: that number is a multiplier on a DIFFERENT base. The mock
       draws 46x54 and `draw_eye` draws 52*0.78 x 62*0.78 = 40.6x48.4, so transcribing the
       scalar literally shrank the approved eyes by a ninth. 1.10 reproduces 45.1x52.9 to
       within half a pixel. */
    if (front) {
        draw_eye(fb, RX(-OS_EYE_X, OS_EYE_Y), RY(-OS_EYE_X, OS_EYE_Y), dark, &st->eyes.l,
                 st->open, st->startle, s * 1.10f, st->upright);
        draw_eye(fb, RX(OS_EYE_X, OS_EYE_Y), RY(OS_EYE_X, OS_EYE_Y), dark, &st->eyes.r,
                 st->open, st->startle, s * 1.10f, st->upright);
    }

    if (u > 0.01f) draw_wing(fb, wx, wy, ww, wh, s, wing, col);
    draw_overhead(fb, st, hx, hy, SX(OS_HEAD_W / 2), s);
    draw_tickle(fb, st, ox, oy + SY(OS_BODY_Y + OS_BODY_H / 2), SX(OS_BODY_W / 2), s);
#undef RX
#undef RY
#undef HC_Y
}

void face_rest(face_state_t *st)
{
    if (st == NULL) return;
    st->form = FORM_OSTRICH;
    st->bob = 0;
    st->lean = 0;
    st->upright = 0.0f;
    st->poop_x = st->poop_size = st->poop_t = 0.0f;
    st->game = NULL;
    st->game_ms = 0;
    st->food = FOOD_COOKIE;
    st->talk = 0.0f;
    st->open = 1.0f;
    st->startle = 0.0f;
    emotion_resolve(FACE_HAPPY, &st->eyes);
    rig_for(ACT_NONE, 0.0f, 1.0f, 0, &st->rig);
    rig_figure(ACT_NONE, 0.0f, 1.0f, 0, 0.0f, &st->fig);
}

/* The neck's root to the head's centre: oy-26 to oy-137 for the ostrich. The robot has no
   neck, so its head NODS over rather than swinging — a short arm, or a 216 px head slides off
   its own shoulders. */
#define OS_UPRIGHT_ARM 111
#define ROBOT_UPRIGHT_ARM 40

static int upright_dx(face_form_t form, float deg, float s)
{
    const float arm = form == FORM_ROBOT ? ROBOT_UPRIGHT_ARM : OS_UPRIGHT_ARM;
    return (int)lrintf(sinf(deg * (float)M_PI / 180.0f) * arm * s);
}

/* THE GAME'S FIELD: the whole frame upright, the square on its side — the part the viewer
   sees. Its centre, and how far it reaches either way. */
static void field(float *cy, float *hw, float *hh)
{
    const bool side = s_fit_oy >= 0;
    *cy = side ? (float)ORIENT_SQ_Y0 + (float)ORIENT_SQ / 2.0f : (float)FACE_H / 2.0f;
    *hw = (float)FACE_W / 2.0f;
    *hh = side ? (float)ORIENT_SQ / 2.0f : (float)FACE_H / 2.0f;
}

void face_game_field(float *hw, float *hh)
{
    float cy;
    field(&cy, hw, hh);
}

void face_game_home(int lean, float *x, float *y)
{
    float cy, hw, hh;
    field(&cy, &hw, &hh);
    *x = (float)lean * s_fit;
    *y = (float)(s_fit_oy >= 0 ? s_fit_oy : OY) - cy;
}

/* THE TWELVE THINGS TO DODGE, each in its own unit frame, about a unit across, turned by the
   thing's own spin. Chunky shapes and loud colours: they are 30 px across on a panel a child
   holds at arm's length, so each has to read from its silhouette. */
static void draw_thing(uint16_t *fb, int kind, const xf_t *t)
{
    const uint16_t white = rgb(0xF4, 0xF6, 0xF8), black = rgb(0x20, 0x24, 0x2C);
    const uint16_t yellow = rgb(0xFF, 0xD2, 0x3F), orange = rgb(0xFF, 0x8A, 0x1F);
    const uint16_t brown = rgb(0x8A, 0x5A, 0x2B), green = rgb(0x3F, 0xBF, 0x5A);
    const uint16_t red = rgb(0xE8, 0x47, 0x3C), grey = rgb(0x8C, 0x95, 0xA4);
    const uint16_t pink = rgb(0xFF, 0x7E, 0xB6), blue = rgb(0x4D, 0xD2, 0xFF);
    switch (kind) {
    case 0: /* a football */
        t_circ(fb, t, 0, 0, 1.0f, white);
        t_circ(fb, t, 0, 0, 0.36f, black);
        for (int i = 0; i < 5; i++) {
            const float a = (float)i * 1.2566f - 1.5708f;
            t_circ(fb, t, cosf(a) * 0.8f, sinf(a) * 0.8f, 0.2f, black);
        }
        break;
    case 1: /* a rubber duck */
        t_circ(fb, t, -0.15f, 0.25f, 0.62f, yellow);
        t_circ(fb, t, 0.35f, 0.3f, 0.48f, yellow);
        t_circ(fb, t, 0.45f, -0.35f, 0.42f, yellow);
        t_tri(fb, t, 0.82f, -0.45f, 1.25f, -0.3f, 0.82f, -0.15f, orange);
        t_circ(fb, t, 0.55f, -0.45f, 0.09f, black);
        break;
    case 2: /* a banana */
        for (int i = 0; i <= 9; i++) {
            const float a = 0.45f + 2.25f * (float)i / 9.0f;
            t_circ(fb, t, cosf(a) * 1.0f, -0.55f + sinf(a) * 1.0f, 0.22f, yellow);
        }
        t_circ(fb, t, cosf(0.45f), -0.55f + sinf(0.45f), 0.12f, brown);
        break;
    case 3: /* a boot */
        t_quad(fb, t, -0.55f, -1.0f, 0.7f, 1.5f, brown);
        t_quad(fb, t, -0.55f, 0.2f, 1.45f, 0.55f, brown);
        t_quad(fb, t, -0.6f, 0.68f, 1.55f, 0.2f, rgb(0x5A, 0x3A, 0x1A));
        break;
    case 4: /* a cactus in a pot */
        t_quad(fb, t, -0.25f, -1.0f, 0.5f, 1.6f, green);
        t_quad(fb, t, -0.75f, -0.4f, 0.28f, 0.7f, green);
        t_quad(fb, t, 0.47f, -0.65f, 0.28f, 0.7f, green);
        t_quad(fb, t, -0.75f, 0.1f, 0.6f, 0.22f, green);
        t_quad(fb, t, 0.2f, -0.15f, 0.6f, 0.22f, green);
        t_quad(fb, t, -0.5f, 0.55f, 1.0f, 0.45f, rgb(0xD0, 0x70, 0x3A));
        break;
    case 5: /* an anvil */
        t_quad(fb, t, -1.0f, -0.5f, 1.8f, 0.35f, grey);
        t_tri(fb, t, 0.8f, -0.5f, 1.2f, -0.5f, 0.8f, -0.15f, grey);
        t_quad(fb, t, -0.35f, -0.15f, 0.7f, 0.5f, grey);
        t_quad(fb, t, -0.7f, 0.35f, 1.4f, 0.35f, grey);
        break;
    case 6: /* a donut */
        t_circ(fb, t, 0, 0, 1.0f, rgb(0xD9, 0x9A, 0x52));
        t_circ(fb, t, 0, -0.05f, 0.86f, pink);
        for (int i = 0; i < 8; i++) {
            const float a = (float)i * 0.785f;
            t_circ(fb, t, cosf(a) * 0.6f, sinf(a) * 0.6f, 0.09f,
                   (i % 3) == 0 ? yellow : (i % 3) == 1 ? blue : white);
        }
        t_circ(fb, t, 0, 0, 0.32f, 0);
        break;
    case 7: /* a rocket */
        t_tri(fb, t, -0.22f, 0.7f, 0.22f, 0.7f, 0, 1.2f, orange);
        t_quad(fb, t, -0.3f, -0.6f, 0.6f, 1.3f, white);
        t_tri(fb, t, -0.3f, -0.6f, 0.3f, -0.6f, 0, -1.15f, red);
        t_tri(fb, t, -0.3f, 0.3f, -0.62f, 0.8f, -0.3f, 0.7f, red);
        t_tri(fb, t, 0.3f, 0.3f, 0.62f, 0.8f, 0.3f, 0.7f, red);
        t_circ(fb, t, 0, -0.15f, 0.16f, blue);
        break;
    case 8: /* a stripy sock */
        for (int i = 0; i < 4; i++) t_quad(fb, t, -0.4f, -1.0f + (float)i * 0.35f, 0.8f, 0.36f, i % 2 ? white : red);
        t_quad(fb, t, -0.4f, 0.36f, 1.2f, 0.5f, red);
        t_circ(fb, t, 0.8f, 0.61f, 0.25f, red);
        break;
    case 9: /* a flying saucer */
        t_circ(fb, t, 0, -0.25f, 0.45f, rgb(0x8E, 0xF0, 0xA0));
        t_quad(fb, t, -0.75f, -0.15f, 1.5f, 0.38f, grey);
        t_circ(fb, t, -0.75f, 0.04f, 0.19f, grey);
        t_circ(fb, t, 0.75f, 0.04f, 0.19f, grey);
        for (int i = -1; i <= 1; i++) t_circ(fb, t, 0.55f * (float)i, 0.05f, 0.09f, yellow);
        break;
    case 10: /* a fish */
        t_circ(fb, t, -0.35f, 0, 0.45f, orange);
        t_circ(fb, t, 0.15f, 0, 0.42f, orange);
        t_tri(fb, t, 0.45f, 0, 1.1f, -0.5f, 1.1f, 0.5f, orange);
        t_circ(fb, t, -0.45f, -0.1f, 0.14f, white);
        t_circ(fb, t, -0.48f, -0.1f, 0.07f, black);
        break;
    default: /* a slice of pizza */
        t_tri(fb, t, -0.95f, -0.75f, 0.95f, -0.75f, 0, 1.05f, rgb(0xE0, 0xA5, 0x4A));
        t_tri(fb, t, -0.75f, -0.55f, 0.75f, -0.55f, 0, 0.82f, rgb(0xFF, 0xD5, 0x6B));
        t_circ(fb, t, -0.3f, -0.3f, 0.14f, rgb(0xC8, 0x40, 0x2F));
        t_circ(fb, t, 0.3f, -0.25f, 0.14f, rgb(0xC8, 0x40, 0x2F));
        t_circ(fb, t, 0, 0.2f, 0.14f, rgb(0xC8, 0x40, 0x2F));
        break;
    }
}

static void draw_text_c(uint16_t *fb, int cx, int y, int scale, const char *s, uint16_t c)
{
    font_draw(fb, FACE_W, FACE_H, cx - font_text_w(s, scale) / 2, y, scale, s, c);
}

/* JETPACK'S PICTURE (`jet.h`), the mock's drawing (artifact 1iQJmXLfdpUsjvC7kaXFTN) in the
 * panel's own primitives. Everything is in field pixels from its top-left, which is the frame's
 * left edge and `jy0` down; on its side the field is the square, and `s_clip0`..`s_clip1` keep
 * the scrolling world inside it. */
static int jy0;

static int jwrap(float v, int m)
{
    const int i = (int)lrintf(v) % m;
    return i < 0 ? i + m : i;
}

static void j_rect(uint16_t *fb, float x, float y, float w, float h, uint16_t c)
{
    const int x0 = (int)lrintf(x), x1 = (int)lrintf(x + w) - 1;
    const int ya = (int)lrintf(y) + jy0, yb = (int)lrintf(y + h) + jy0;
    for (int r = ya; r < yb; r++) hspan(fb, r, x0, x1, c);
}

static void j_circ(uint16_t *fb, float x, float y, float r, uint16_t c)
{
    fill_circle(fb, (int)lrintf(x), (int)lrintf(y) + jy0, (int)lrintf(r), c);
}

static void j_ell(uint16_t *fb, float x, float y, float a, float b, uint16_t c)
{
    const int bb = (int)lrintf(b);
    for (int dy = -bb; dy <= bb; dy++) {
        const float k = 1.0f - ((float)dy / b) * ((float)dy / b);
        if (k < 0.0f) continue;
        const float half = a * sqrtf(k);
        hspan(fb, (int)lrintf(y) + jy0 + dy, (int)lrintf(x - half), (int)lrintf(x + half), c);
    }
}

static void j_tri(uint16_t *fb, float x0, float y0, float x1, float y1, float x2, float y2,
                  uint16_t c)
{
    const float o = (float)jy0;
    fill_tri(fb, x0, y0 + o, x1, y1 + o, x2, y2 + o, c);
}

/* A bar turned by `a` about (x, y): the spinners. */
static void j_bar(uint16_t *fb, float x, float y, float a, float u0, float u1, float hw, uint16_t c)
{
    const xf_t t = xf(x, y + (float)jy0, a, 1.0f);
    t_quad(fb, &t, u0, -hw, u1 - u0, hw * 2.0f, c);
}

static void j_star(uint16_t *fb, float x, float y, float r, float a, uint16_t c)
{
    for (int i = 0; i < 10; i++) {
        const float k0 = a + (float)i * 0.6283185f - 1.5707963f, k1 = k0 + 0.6283185f;
        const float r0 = i % 2 ? r * 0.45f : r, r1 = i % 2 ? r : r * 0.45f;
        j_tri(fb, x, y, x + cosf(k0) * r0, y + sinf(k0) * r0, x + cosf(k1) * r1, y + sinf(k1) * r1, c);
    }
}

/* Half as bright, for the banner's band: a 565 channel halved is the value shifted, masked. */
static void j_dim(uint16_t *fb, int y0, int y1)
{
    if (y0 < s_clip0) y0 = s_clip0;
    if (y1 > s_clip1) y1 = s_clip1;
    for (int y = y0; y < y1; y++) {
        uint16_t *row = &fb[y * FACE_W];
        for (int x = 0; x < FACE_W; x++) {
            const uint16_t v = (uint16_t)(row[x] >> 8 | row[x] << 8);
            const uint16_t h = (uint16_t)((v >> 1) & 0x7BEF);
            row[x] = (uint16_t)(h >> 8 | h << 8);
        }
    }
}

typedef struct {
    uint8_t r0, g0, b0, r1, g1, b1;
} sky_t;

/* Each world's sky, top to bottom; the last is the mixes'. */
static const sky_t SKIES[JET_WORLDS + 1] = {
    {0x3B, 0x8F, 0xD9, 0x9F, 0xD6, 0xFF}, {0x6F, 0xB7, 0xE8, 0xD6, 0xF0, 0xFF},
    {0x1B, 0x16, 0x26, 0x3A, 0x2F, 0x4A}, {0xFF, 0x9C, 0xCF, 0xFF, 0xD9, 0xEC},
    {0x0F, 0x3A, 0x24, 0x2F, 0x6B, 0x3A}, {0xFF, 0x8A, 0x5C, 0xFF, 0xD2, 0x7A},
    {0x2B, 0x2F, 0x36, 0x4A, 0x50, 0x5A}, {0x0B, 0x3D, 0x6B, 0x1C, 0x7C, 0xB0},
    {0x05, 0x06, 0x0F, 0x14, 0x1A, 0x3A}, {0x0A, 0x0D, 0x18, 0x1D, 0x24, 0x40},
    {0x2A, 0x1B, 0x4A, 0x5A, 0x3B, 0x8A},
};

/* THE SKY, faded in by `k` on the walk in and out, so the world arrives as he shrinks. */
static void jet_sky(uint16_t *fb, int bd, float fh, float k)
{
    const sky_t *s = &SKIES[bd];
    const int n = (int)fh;
    for (int y = 0; y < n; y++) {
        const float u = (float)y / fh;
        hspan(fb, y + jy0, 0, FACE_W - 1,
              rgb((uint8_t)((s->r0 + (s->r1 - s->r0) * u) * k), (uint8_t)((s->g0 + (s->g1 - s->g0) * u) * k),
                  (uint8_t)((s->b0 + (s->b1 - s->b0) * u) * k)));
    }
}

/* WHICH WORLD THIS IS, at a glance: a far layer that scrolls slower than the obstacles. */
static void jet_scenery(uint16_t *fb, int bd, float scroll, float t, float fw, float fh)
{
    const float p = scroll * 0.3f;
    const uint16_t cloud = rgb(0xF0, 0xF6, 0xFF);
    switch (bd) {
    case 0:
        for (int i = 0; i < 4; i++) {
            const float x = (float)jwrap((float)i * 130.0f - p * 0.5f, 520) - 60.0f;
            j_ell(fb, x, 60.0f + (float)i * 25.0f, 34, 12, cloud);
            j_ell(fb, x + 22.0f, 52.0f + (float)i * 25.0f, 22, 12, cloud);
        }
        for (int i = 0; i < 5; i++) j_ell(fb, (float)jwrap((float)i * 120.0f - p, 600) - 80.0f, fh + 20.0f, 110, 70, rgb(0x5C, 0xB8, 0x5C));
        break;
    case 1:
        for (int i = 0; i < 4; i++) {
            const float x = (float)jwrap((float)i * 160.0f - p * 0.4f, 640) - 100.0f;
            j_tri(fb, x, fh, x + 200.0f, fh, x + 100.0f, fh - 220.0f, rgb(0x9D, 0xB8, 0xCC));
        }
        for (int i = 0; i < 5; i++) j_ell(fb, (float)jwrap((float)i * 140.0f - p, 700) - 80.0f, fh + 30.0f, 130, 90, rgb(0x78, 0xB8, 0x6A));
        break;
    case 2:
        for (float x = -(float)jwrap(p, 40) - 40.0f; x < fw + 40.0f; x += 40.0f) {
            j_tri(fb, x, 0, x + 40.0f, 0, x + 20.0f, 26, rgb(0x2A, 0x22, 0x38));
            j_tri(fb, x, fh, x + 40.0f, fh, x + 20.0f, fh - 22.0f, rgb(0x2A, 0x22, 0x38));
        }
        break;
    case 3:
        for (int i = 0; i < 4; i++) {
            const float x = (float)jwrap((float)i * 140.0f - p * 0.6f, 560) - 70.0f;
            j_ell(fb, x, 80.0f + (float)(i % 2) * 220.0f, 40, 16, cloud);
            j_ell(fb, x + 26.0f, 72.0f + (float)(i % 2) * 220.0f, 26, 14, cloud);
        }
        for (int i = 0; i < 6; i++) {
            static const uint32_t C[3] = {0xFF6FB5, 0xFFE14D, 0x7CF08B};
            j_circ(fb, (float)jwrap((float)i * 90.0f - p, 540) - 60.0f, fh - 10.0f, 26, shade(C[i % 3], 1.0f));
        }
        break;
    case 4:
        for (int i = 0; i < 8; i++) {
            const float x = (float)jwrap((float)i * 60.0f - p * 0.6f, 480) - 50.0f;
            const int n = 4 + i % 3;
            for (int k = 0; k < n; k++) j_ell(fb, x + sinf((float)k * 0.9f) * 6.0f, (float)k * 26.0f, 5, 9, rgb(0x1D, 0x5A, 0x2E));
        }
        break;
    case 5:
        j_circ(fb, fw - 70.0f, 90, 36, rgb(0xFF, 0xF3, 0xB0));
        for (int i = 0; i < 5; i++) j_ell(fb, (float)jwrap((float)i * 110.0f - p * 0.7f, 550) - 60.0f, 140.0f + (float)(i % 3) * 90.0f, 46, 14, cloud);
        break;
    case 6:
        for (int i = 0; i < 4; i++) j_rect(fb, (float)jwrap((float)i * 130.0f - p, 520) - 40.0f, 0, 10, fh, rgb(0x2B, 0x2F, 0x36));
        for (int i = 0; i < 3; i++) {
            const float x = (float)jwrap((float)i * 160.0f - p * 0.5f, 480) - 60.0f, y = 120.0f + (float)(i % 2) * 200.0f;
            const float a = t * 0.4f * (i % 2 ? 1.0f : -1.0f);
            for (int k = 0; k < 4; k++) j_bar(fb, x, y, a + (float)k * 0.785f, -40, 40, 6, rgb(0x3A, 0x3F, 0x48));
            j_circ(fb, x, y, 30, rgb(0x3A, 0x3F, 0x48));
        }
        break;
    case 7:
        for (int i = 0; i < 18; i++) {
            j_circ(fb, (float)jwrap((float)i * 47.0f - p * 0.4f, (int)fw + 20) - 10.0f,
                   (float)jwrap((float)i * 83.0f - t * 30.0f, (int)fh + 20) - 10.0f, (float)(3 + (i % 3) * 2), rgb(0x3D, 0x8A, 0xC4));
        }
        for (int i = 0; i < 6; i++) j_ell(fb, (float)jwrap((float)i * 110.0f - p, 660) - 80.0f, fh + 6.0f, 90, 22, rgb(0xD9, 0xC3, 0x8A));
        break;
    default:
        for (int i = 0; i < 60; i++) {
            const float x = (float)jwrap((float)i * 97.0f - p * (0.3f + (float)(i % 3) * 0.2f), (int)fw + 20) - 10.0f;
            j_rect(fb, x, (float)((i * 53) % (int)fh), i % 5 ? 2 : 3, i % 5 ? 2 : 3, i % 5 ? rgb(0x88, 0x88, 0x99) : rgb(0xEE, 0xEE, 0xFF));
        }
        if (bd == 8) {
            j_circ(fb, fw - 50.0f, 70, 26, rgb(0xC9, 0x6A, 0x3A));
            j_circ(fb, fw - 58.0f, 63, 8, rgb(0xE8, 0x94, 0x5A));
        }
        if (bd == 9) {
            for (float x = -(float)jwrap(p, 46); x < fw; x += 46.0f) j_rect(fb, x, 0, 2, fh, rgb(0x2B, 0x35, 0x56));
        }
        break;
    }
}

static void jet_balloon(uint16_t *fb, float x, float y, uint16_t c)
{
    j_rect(fb, x - 1.0f, y + 20.0f, 2, 14, rgb(0xDD, 0xDD, 0xDD));
    j_ell(fb, x, y, 17, 20, c);
    j_ell(fb, x - 6.0f, y - 7.0f, 4, 6, rgb(0xFF, 0xFF, 0xFF));
}

static void jet_hazard(uint16_t *fb, float x, float y, float w, float h)
{
    j_rect(fb, x, y, w, h, rgb(0xFF, 0xD2, 0x3F));
    for (float i = 0; i < w; i += 16.0f) {
        j_tri(fb, x + i, y, x + fminf(w, i + 8.0f), y, x + fmaxf(0, i - 4.0f), y + h, rgb(0x1A, 0x1A, 0x1A));
    }
}

/* ONE OBSTACLE, AT WORLD TIME `t`. */
static void jet_obj(uint16_t *fb, const jet_obj_t *o, float t, float fh)
{
    const uint16_t green = rgb(0x3F, 0xAE, 0x49), light = rgb(0x7A, 0xD6, 0x6B), dark = rgb(0x2E, 0x8A, 0x37);
    const uint16_t red = rgb(0xE8, 0x47, 0x3C), white = rgb(0xFF, 0xFF, 0xFF);
    switch (o->kind) {
    case JET_PIPE: {
        const float top = o->y0 - o->gap / 2.0f, bot = o->y0 + o->gap / 2.0f;
        j_rect(fb, o->x + 4, 0, o->w - 8, top, green);
        j_rect(fb, o->x + 4, bot, o->w - 8, fh - bot, green);
        j_rect(fb, o->x + 10, 0, 8, top, light);
        j_rect(fb, o->x + 10, bot, 8, fh - bot, light);
        j_rect(fb, o->x - 2, top - 24, o->w + 4, 24, dark);
        j_rect(fb, o->x - 2, bot, o->w + 4, 24, dark);
        break;
    }
    case JET_PILLAR: {
        const float y = o->from_top ? 0.0f : fh - o->y0;
        j_rect(fb, o->x, y, o->w, o->y0, rgb(0x8A, 0x7F, 0x6A));
        for (float k = y + 14.0f; k < y + o->y0 - 10.0f; k += 30.0f) {
            j_rect(fb, o->x + 6, k, 18, 10, rgb(0xA3, 0x97, 0x7F));
            j_rect(fb, o->x + 28, k + 15.0f, 18, 10, rgb(0xA3, 0x97, 0x7F));
        }
        if (o->from_top) {
            for (int i = 0; i < 4; i++) j_rect(fb, o->x + 4.0f + (float)i * 12.0f, o->y0 - 8.0f, 8, 18.0f + (float)(i % 2) * 14.0f, rgb(0x5C, 0xB8, 0x5C));
        } else {
            j_ell(fb, o->x + o->w / 2.0f, fh - o->y0, o->w / 2.0f + 10.0f, 14, rgb(0x5C, 0xB8, 0x5C));
            j_circ(fb, o->x + 14.0f, fh - o->y0 - 6.0f, 4, red);
            j_circ(fb, o->x + 38.0f, fh - o->y0 - 2.0f, 4, red);
        }
        break;
    }
    case JET_ROCK: {
        j_tri(fb, o->x, fh, o->x + 50.0f, fh, o->x + 25.0f, fh - o->y0, rgb(0x6A, 0x5A, 0x7E));
        j_tri(fb, o->x + 6.0f, fh, o->x + 18.0f, fh, o->x + 25.0f, fh - o->y0 + 6.0f, rgb(0x8C, 0x7A, 0xA3));
        j_tri(fb, o->x + 8.0f, 0, o->x + 42.0f, 0, o->x + 25.0f, 18, rgb(0x4A, 0x3F, 0x5A));
        /* About to go, it trembles. */
        const float shake = o->fy <= 22.0f && o->x < o->trig + 60.0f ? sinf(t * 30.0f) * 2.0f : 0.0f;
        const xf_t r = xf(o->x + 25.0f + shake, o->fy + (float)jy0, 0, 20.0f);
        for (int i = 0; i < 9; i++) {
            const float a0 = (float)i / 9.0f * 6.2831853f, a1 = (float)(i + 1) / 9.0f * 6.2831853f;
            const float q0 = 0.85f + (float)((i * 7) % 3) * 0.08f, q1 = 0.85f + (float)(((i + 1) * 7) % 3) * 0.08f;
            t_tri(fb, &r, 0, 0, cosf(a0) * q0, sinf(a0) * q0, cosf(a1) * q1, sinf(a1) * q1, rgb(0x7B, 0x6E, 0x66));
        }
        j_circ(fb, o->x + 19.0f + shake, o->fy - 4.0f, 5, rgb(0x5A, 0x50, 0x4A));
        break;
    }
    case JET_SLIDE: {
        const float y = jet_slide_y(o, t), top = y - o->gap / 2.0f, bot = y + o->gap / 2.0f;
        j_rect(fb, o->x + 6, 0, o->w - 12, top, white);
        j_rect(fb, o->x + 6, bot, o->w - 12, fh - bot, white);
        /* The stripes, as bands down the cane that crawl. */
        const float off = fmodf(t * 30.0f, 36.0f);
        for (float k = -36.0f + off; k < fh; k += 36.0f) {
            const float a = fmaxf(k, 0.0f), b = k + 16.0f;
            if (b > 0.0f && a < top) j_rect(fb, o->x + 6, a, o->w - 12, fminf(b, top) - a, red);
            if (b > bot && a < fh) j_rect(fb, o->x + 6, fmaxf(a, bot), o->w - 12, b - fmaxf(a, bot), red);
        }
        j_circ(fb, o->x + o->w / 2.0f, top, (o->w - 12.0f) / 2.0f, white);
        j_circ(fb, o->x + o->w / 2.0f, bot, (o->w - 12.0f) / 2.0f, white);
        break;
    }
    case JET_TUNNEL: {
        const float x0 = fmaxf(o->x, -10.0f), x1 = fminf(o->x + o->w, (float)FACE_W + 10.0f);
        for (float x = x0; x < x1; x += 3.0f) {
            const float y = jet_tunnel_y(o, x);
            j_rect(fb, x, 0, 3, y - o->gap / 2.0f, rgb(0x5A, 0x3D, 0x1E));
            j_rect(fb, x, y + o->gap / 2.0f, 3, fh - y - o->gap / 2.0f, rgb(0x5A, 0x3D, 0x1E));
        }
        for (float x = ceilf(x0 / 22.0f) * 22.0f; x <= x1; x += 22.0f) {
            const float y = jet_tunnel_y(o, x);
            j_ell(fb, x, y - o->gap / 2.0f - 4.0f, 9, 6, rgb(0x5F, 0xBF, 0x4A));
            j_ell(fb, x, y + o->gap / 2.0f + 4.0f, 9, 6, rgb(0x5F, 0xBF, 0x4A));
        }
        break;
    }
    case JET_BIRDS:
        for (int i = 0; i < 3; i++) {
            float x, y;
            jet_bird(o, t, i, &x, &y);
            const float flap = sinf(t * 14.0f + x);
            j_ell(fb, x, y, 15, 9, rgb(0x2D, 0x3A, 0x5A));
            j_tri(fb, x - 13.0f, y - 2.0f, x - 24.0f, y + 2.0f, x - 13.0f, y + 5.0f, rgb(0xFF, 0x9F, 0x1C));
            j_circ(fb, x - 7.0f, y - 3.0f, 3, white);
            j_tri(fb, x - 2.0f, y - 2.0f, x + 10.0f, y - 2.0f, x + 4.0f, y - 4.0f - flap * 15.0f, rgb(0x3E, 0x4F, 0x78));
            j_tri(fb, x + 11.0f, y, x + 22.0f, y - 6.0f, x + 20.0f, y + 4.0f, rgb(0x3E, 0x4F, 0x78));
        }
        break;
    case JET_RISERS: {
        static const uint32_t C[5] = {0xE8473C, 0xFFE14D, 0x4DD2FF, 0x7CF08B, 0xB98CFF};
        for (int k = 0; k < 4; k++) jet_balloon(fb, o->x + 22.0f, jet_riser_y(o, t, k, fh), shade(C[k], 1.0f));
        break;
    }
    case JET_CRUSHER: {
        const float open = jet_crush_open(o, t), h = open * JET_CRUSH_HALF;
        const uint16_t steel = rgb(0x7D, 0x85, 0x90);
        j_rect(fb, o->x + 22, 0, 22, fh, rgb(0x5F, 0x66, 0x70));
        j_rect(fb, o->x, 0, o->w, o->y0 - h, steel);
        j_rect(fb, o->x, o->y0 + h, o->w, fh - o->y0 - h, steel);
        jet_hazard(fb, o->x, o->y0 - h - 12.0f, o->w, 12);
        jet_hazard(fb, o->x, o->y0 + h, o->w, 12);
        /* The light flashes before it slams. */
        const float p = fmodf(t + o->t0, 3.2f) / 3.2f;
        const bool warn = p > 0.42f && p < 0.55f && sinf(t * 30.0f) > 0.0f;
        j_circ(fb, o->x + o->w / 2.0f, o->y0 - h - 30.0f, 7, warn ? rgb(0xFF, 0x3B, 0x30) : rgb(0x66, 0x18, 0x14));
        break;
    }
    case JET_MINES:
        for (int col = 0; col < 2; col++) {
            for (int k = 0;; k++) {
                float x, y;
                const int m = jet_mine(o, t, fh, col, k, &x, &y);
                if (m < 0) break;
                if (m == 0) continue;
                for (int i = 0; i < 8; i++) {
                    const float a = (float)i * 0.785f + t * 0.3f;
                    j_circ(fb, x + cosf(a) * 15.0f, y + sinf(a) * 15.0f, 3.5f, rgb(0x2B, 0x2F, 0x36));
                }
                j_circ(fb, x, y, 13, rgb(0x2B, 0x2F, 0x36));
                j_circ(fb, x, y, 4, sinf(t * 5.0f + y) > 0.0f ? rgb(0xFF, 0x3B, 0x30) : rgb(0x7A, 0x1C, 0x18));
            }
        }
        break;
    case JET_SPINNER:
    case JET_SPINNER2:
        for (int i = 0;; i++) {
            float px, py, a;
            const int n = jet_spin(o, t, fh, i, &px, &py, &a);
            j_bar(fb, px, py, a, -o->len, o->len, 7, rgb(0xC7, 0xCF, 0xDD));
            j_bar(fb, px, py, a, o->len - 30.0f, o->len, 12, rgb(0x4D, 0xD2, 0xFF));
            j_bar(fb, px, py, a, -o->len, -o->len + 30.0f, 12, rgb(0x4D, 0xD2, 0xFF));
            j_circ(fb, px, py, 12, red);
            j_circ(fb, px, py, 5, rgb(0xFF, 0xE1, 0x4D));
            if (i + 1 >= n) break;
        }
        break;
    case JET_LASER: {
        float a, b;
        jet_laser_span(o, fh, &a, &b);
        const int st = jet_laser_state(o, t);
        const float x = o->x + 7.0f;
        if (st == JET_LASER_ON) {
            j_rect(fb, x - 7, a, 14, b - a, rgb(0x8A, 0x20, 0x1C));
            j_rect(fb, x - 4, a, 8, b - a, rgb(0xFF, 0x5A, 0x4F));
            j_rect(fb, x - 1, a, 2, b - a, white);
        } else if (st == JET_LASER_WARN && sinf(t * 40.0f) > 0.0f) {
            for (float y = a; y < b; y += 14.0f) j_rect(fb, x - 1, y, 2, 7, rgb(0xFF, 0x5A, 0x4F));
        }
        const uint16_t box = rgb(0x8E, 0x97, 0xA8);
        j_rect(fb, x - 14, a <= 0.0f ? -6.0f : a - 12.0f, 28, 22, box);
        j_rect(fb, x - 14, b >= fh ? fh - 16.0f : b - 10.0f, 28, 22, box);
        break;
    }
    default:
        break;
    }
    float sx, sy;
    if (o->star && !o->got && jet_star_at(o, t, fh, &sx, &sy)) j_star(fb, sx, sy, 13, t * 1.5f, rgb(0xFF, 0xE1, 0x4D));
}

/* BEHIND HIM: the sky, the far layer, the obstacles, and the jetpack's tanks and flame, drawn
   before the figure so he is in front of all of it. */
static void draw_jet_back(uint16_t *fb, const game_t *g, uint32_t now, int ox, int oy, float s)
{
    float cy, hw, hh;
    field(&cy, &hw, &hh);
    jy0 = (int)lrintf(cy - hh);
    s_clip0 = jy0;
    s_clip1 = jy0 + (int)lrintf(hh * 2.0f);
    const jet_t *j = &g->jet;
    const int bd = jet_backdrop(j->world);
    const float fw = hw * 2.0f, fh = hh * 2.0f;
    const bool walking = g->phase == GAME_WALK_IN || g->phase == GAME_WALK_OUT;
    float k = 1.0f;
    if (walking) {
        float e = (float)(now - g->since) / (float)GAME_WALK_MS;
        e = e < 0.0f ? 0.0f : e > 1.0f ? 1.0f : e;
        e = e * e * (3.0f - 2.0f * e);
        k = g->phase == GAME_WALK_IN ? e : 1.0f - e;
    }
    jet_sky(fb, bd, fh, k);
    if (!walking) {
        jet_scenery(fb, bd, j->scroll, j->t, fw, fh);
        for (int i = 0; i < JET_MAX_OBJ; i++) {
            if (j->obj[i].live) jet_obj(fb, &j->obj[i], j->t, fh);
        }
    }
    /* THE JETPACK: two tanks either side of the torso (the robot's and the bird's are the same
       width there), the flame growing with the burn. Strapped on halfway through the walk in. */
    if (k > 0.5f || !walking) {
        const float thrust = g->phase == GAME_PLAY ? j->burn : 0.0f;
        const float flick = sinf((float)now * 0.04f) * 10.0f;
        for (int side = -1; side <= 1; side += 2) {
            const float x = (float)ox + (float)side * 84.0f * s;
            const float f = (40.0f + thrust * 100.0f + flick) * s;
            const float base = (float)oy + 60.0f * s;
            fill_tri(fb, x - 16.0f * s, base, x + 16.0f * s, base, x, base + f, rgb(0xFF, 0x9F, 0x1C));
            fill_tri(fb, x - 8.0f * s, base, x + 8.0f * s, base, x, base + f * 0.6f, rgb(0xFF, 0xE1, 0x4D));
            fill_round_rect(fb, (int)lrintf(x - 22.0f * s), (int)lrintf((float)oy - 40.0f * s), (int)lrintf(44.0f * s),
                            (int)lrintf(104.0f * s), (int)lrintf(20.0f * s), rgb(0x9A, 0xA3, 0xB2));
            fill_round_rect(fb, (int)lrintf(x - 22.0f * s), (int)lrintf((float)oy - 40.0f * s), (int)lrintf(44.0f * s),
                            (int)lrintf(20.0f * s), (int)lrintf(10.0f * s), rgb(0xE8, 0x47, 0x3C));
        }
    }
    s_clip0 = 0;
    s_clip1 = FACE_H;
}

/* IN FRONT: the score, the world, the gates gone by, and the banners. */
static void draw_jet_front(uint16_t *fb, const game_t *g)
{
    if (g->phase != GAME_PLAY && g->phase != GAME_OVER) return;
    float cy, hw, hh;
    field(&cy, &hw, &hh);
    const jet_t *j = &g->jet;
    const uint16_t gold = rgb(0xFF, 0xE1, 0x4D), white = rgb(0xF4, 0xF6, 0xF8);
    const int top = (int)lrintf(cy - hh) + 10;
    char buf[32];
    snprintf(buf, sizeof(buf), "%d", g->score);
    font_draw(fb, FACE_W, FACE_H, 14, top, 4, buf, gold);
    snprintf(buf, sizeof(buf), "%d %s", j->world + 1, jet_world_name(j->world));
    font_draw(fb, FACE_W, FACE_H, FACE_W - 14 - font_text_w(buf, 2), top, 2, buf, white);
    snprintf(buf, sizeof(buf), "BEST %d", g->best);
    font_draw(fb, FACE_W, FACE_H, FACE_W - 14 - font_text_w(buf, 2), top + 20, 2, buf, white);
    for (int i = 0; i < JET_GATES; i++) {
        fill_circle(fb, 18 + i * 13, top + 42, 4, i < j->gates ? gold : rgb(0x80, 0x88, 0x90));
    }
    /* The banner goes in the half he is not in, so it never hides him. */
    const int by = (int)lrintf(j->jy < hh ? cy + hh * 0.45f : cy - hh * 0.5f);
    if (g->phase == GAME_PLAY && j->t < JET_READY_S + 0.6f) {
        j_dim(fb, by - 50, by + 50);
        if (j->t < JET_READY_S) {
            draw_text_c(fb, OX, by - 30, 5, jet_world_name(j->world), gold);
            snprintf(buf, sizeof(buf), "WORLD %d", j->world + 1);
            draw_text_c(fb, OX, by + 18, 2, buf, white);
        } else {
            draw_text_c(fb, OX, by - 18, 5, "GO!", gold);
        }
    } else if (g->phase == GAME_OVER) {
        j_dim(fb, by - 50, by + 50);
        snprintf(buf, sizeof(buf), "SCORE %d", g->score);
        draw_text_c(fb, OX, by - 30, 4, buf, gold);
        draw_text_c(fb, OX, by + 14, 2,
                    g->score > 0 && g->score >= g->best ? "NEW BEST! TAP TO PLAY" : "TAP TO TRY AGAIN", white);
    }
}

/* The things in flight, the score and level, and the banner: over the figure, so a hit is
   seen to be a hit. */
static void draw_game(uint16_t *fb, const game_t *g, uint32_t now)
{
    if (g->kind == GAME_JET) {
        draw_jet_front(fb, g);
        return;
    }
    float cy, hw, hh;
    field(&cy, &hw, &hh);
    for (int i = 0; i < GAME_MAX_OBJ; i++) {
        const game_obj_t *o = &g->obj[i];
        if (!o->live) continue;
        const xf_t t = xf((float)OX + o->x, cy + o->y, o->a, 17.0f * o->size);
        draw_thing(fb, o->kind, &t);
    }
    if (g->phase != GAME_PLAY && g->phase != GAME_OVER) return;
    const uint16_t gold = rgb(0xFF, 0xE1, 0x4D), dim = rgb(0x9A, 0xA5, 0xB8);
    const uint16_t white = rgb(0xE9, 0xED, 0xF3);
    const int top = (int)lrintf(cy - hh) + 10;
    char buf[24];
    snprintf(buf, sizeof(buf), "%d", g->score);
    font_draw(fb, FACE_W, FACE_H, 14, top, 4, buf, gold);
    snprintf(buf, sizeof(buf), "BEST %d", g->best);
    font_draw(fb, FACE_W, FACE_H, FACE_W - 14 - font_text_w(buf, 2), top, 2, buf, dim);
    snprintf(buf, sizeof(buf), "LEVEL %d", game_level(g, now));
    font_draw(fb, FACE_W, FACE_H, FACE_W - 14 - font_text_w(buf, 2), top + 20, 2, buf, dim);
    const int by = (int)lrintf(cy - hh * 0.5f);
    if (g->phase == GAME_PLAY && now - g->since < GAME_READY_MS) {
        draw_text_c(fb, OX, by - 14, 5, now - g->since < GAME_READY_MS / 2 ? "READY" : "GO!", gold);
    } else if (g->phase == GAME_OVER) {
        snprintf(buf, sizeof(buf), "SCORE %d", g->score);
        draw_text_c(fb, OX, by - 24, 4, buf, gold);
        draw_text_c(fb, OX, by + 14, 2,
                    g->score > 0 && g->score >= g->best ? "NEW BEST! TAP TO PLAY" : "TAP TO PLAY AGAIN",
                    white);
    }
}

void face_draw(uint16_t *fb, int colour, const face_state_t *st)
{
    /* Everything hangs off these, so one pair of offsets moves the whole figure. See
       ROOM_ENDPOINT_PLAN.md §10.4s: consecutive frames have to DIFFER, not merely arrive. */
    int ox = OX + (int)lrintf(((float)st->lean + st->fig.ox) * s_fit);
    float sx = st->fig.sx * s_fit, sy = st->fig.sy * s_fit;
    /* PLAYING: the game says where he is and how big (`game_figure`), in field pixels. */
    const bool playing = st->game != NULL && game_on(st->game);
    float gy = 0.0f;
    if (playing) {
        float gx, gs, cy, hw, hh;
        game_figure(st->game, st->game_ms, &gx, &gy, &gs);
        field(&cy, &hw, &hh);
        ox = OX + (int)lrintf(gx);
        gy += cy;
        sx *= gs;
        sy *= gs;
    }
    /* SITTING: the body comes down by what the legs' splay took off their height, so the feet
       stay on the floor. This form's leg, which is why it is here and not in the rig. */
    const float leg = st->form == FORM_ROBOT ? (float)LEG_L : (float)OS_LEG_L;
    const float sit = st->fig.sit * leg * (1.0f - cosf(st->rig.leg_l * (float)M_PI / 180.0f)) * sy;
    const int oy = playing ? (int)lrintf(gy + ((float)st->bob + st->fig.oy) * sy + sit)
                           : (s_fit_oy >= 0 ? s_fit_oy : OY) +
                                 (int)lrintf(((float)st->bob + st->fig.oy) * s_fit + sit);
    /* One scalar for radii and stroke widths, which cannot take independent x and y. */
    const float s = (sx + sy) * 0.5f;
    /* The head tilt stands in for the web rig's whole-figure rotation — see rig.h. Offsetting
       the head against the torso is the cue curious and silly actually need, and it costs two
       adds instead of resampling 165 000 pixels. The righting rides the same channel: a head
       turned by `upright` about the neck's root has moved sideways by the arm's length times
       its sine, so the neck shears to it and every feature drawn off the head follows. */
    const int tilt = (int)lrintf(st->fig.tilt * 1.6f) + upright_dx(st->form, st->upright, s);
    const uint32_t hex = PALETTE[colour % face_colour_count()];

    /* Black, not dark grey: on an AMOLED an unlit pixel is OFF, which is why the mock's
       field is #000 and why the figure reads as emitting rather than as a picture. */
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

    /* THE POOP, behind him on the floor his feet are on: where the feet are at rest, so a jump
       or a squat does not lift the floor with him. */
    if (st->poop_size > 0.0f) {
        const int base = s_fit_oy >= 0 ? s_fit_oy : OY;
        const int floor = base + (int)lrintf((st->form == FORM_ROBOT ? 166.0f : 184.0f) * s_fit);
        draw_poop(fb, st->poop_kind, OX + (int)lrintf(st->poop_x * s_fit), floor, s_fit * 1.3f,
                  st->poop_size, st->poop_t);
    }

    if (playing && st->game->kind == GAME_JET) draw_jet_back(fb, st->game, st->game_ms, ox, oy, s);
    if (st->form == FORM_ROBOT) {
        draw_robot(fb, hex, st, ox, oy, sx, sy, s, tilt);
    } else {
        draw_ostrich(fb, hex, st, ox, oy, sx, sy, s, tilt);
    }
    if (playing) draw_game(fb, st->game, st->game_ms);
    if (st->fig.extra == EXTRA_PARTY) draw_confetti(fb, st->fig.extra_p, s_fit);
}
#undef SX
#undef SY
#undef LERPI
