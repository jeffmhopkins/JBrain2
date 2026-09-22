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

#include <math.h>
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

static void fill_circle(uint16_t *fb, int cx, int cy, int r, uint16_t c)
{
    for (int j = -r; j <= r; j++)
        for (int i = -r; i <= r; i++)
            if (i * i + j * j <= r * r) px(fb, cx + i, cy + j, c);
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
static void draw_limb(uint16_t *fb, int x, int y, float deg, int len, int w, uint16_t c)
{
    const float a = deg * (float)M_PI / 180.0f;
    const float ex = -sinf(a), ey = cosf(a);
    const int steps = len;
    for (int i = 0; i <= steps; i++) {
        const float t = (float)i;
        fill_circle(fb, x + (int)lrintf(ex * t), y + (int)lrintf(ey * t), w / 2, c);
    }
    fill_circle(fb, x + (int)lrintf(ex * (float)len), y + (int)lrintf(ey * (float)len),
                (int)(w * 0.62f), c);
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
                     float open, float startle, float scale)
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
        fill_round_rect(fb, cx - (int)(w / 2), cy - 2, (int)w, 5, 2, dark);
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

    const int x0 = (int)(-hw) - 1, x1 = (int)(hw) + 1;
    const int y0 = (int)(-hh) - 1, y1 = (int)(hh) + 1;
    for (int j = y0; j <= y1; j++) {
        const float fy = (float)j;
        for (int i = x0; i <= x1; i++) {
            const float fx = (float)i;

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

static void draw_blush(uint16_t *fb, int cx, int cy, int dx, float scale)
{
    const uint16_t pink = rgb(0xFF, 0x7A, 0x9C);
    const int r = (int)(16.0f * scale);
    fill_circle(fb, cx - dx, cy, r, pink);
    fill_circle(fb, cx + dx, cy, r, pink);
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
    if (st->fig.extra == EXTRA_BLUSH) draw_blush(fb, ox + tilt, hy + SY(26), SX(78), s);

    /* NO FACE ON THE BACK OF A HEAD. `facing` goes negative for the half of a spin the
       figure is turned away (rig.h), and the squash alone does not read as a turn — a face
       that stays put while the body narrows reads as the body being crushed. Dropping the
       eyes and the smile for that half is what makes it a spin. */
    const bool front = st->fig.facing >= 0.0f;
    const int ex = SX((int)(HW * 0.43f)), ey = hy - SY((int)(HH * 0.17f));
    if (front) {
        draw_eye(fb, ox + tilt - ex, ey, dark, &st->eyes.l, st->open, st->startle, s);
        draw_eye(fb, ox + tilt + ex, ey, dark, &st->eyes.r, st->open, st->startle, s);
    }

    /* Smile: arc(cx, cy-16, 30) from 0.15pi to 0.85pi, stroked 9 wide. */
    if (front) {
        const int mx = ox + tilt, my = hy + SY((int)(HH * 0.52f)) - SY(16);
        /* The open mouth goes UNDER the arc, so the smile stays the lip of it rather than
           being replaced by a hole. At talk 0 nothing is drawn and the face is byte-for-byte
           what it was before this channel existed. */
        /* The robot's mouth gets the same treatment for the same reason — it was sized
           against a host render rather than against a room. */
        const int gape = (int)lrintf(st->talk * 26.0f);
        if (gape > 0) {
            fill_round_rect(fb, mx - SX(21), my + SY(2), SX(42), SY(gape), (int)(8 * s), dark);
        }
        arc_stroke(fb, mx, my, (int)(30 * s), (float)M_PI * 0.15f, (float)M_PI * 0.85f,
                   (int)(9 * s), dark);
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
    /* Behind the bird and BELOW the plumes: the robot's anchor hung off the HEAD, so the
       offset that put its cloud by the hips put the ostrich's up at its midriff. */
    if (st->fig.extra == EXTRA_PUFF) draw_puff(fb, ox - SX(100), oy + SY(52), s);

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
        draw_limb(fb, hx, oy + SY(-182) + bob, CREST[i].deg + 180.0f + st->rig.crest,
                  SY(CREST[i].len), SX(9), col);
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
        const int gape = (int)lrintf(st->talk * 30.0f);
        for (unsigned i = 0; i < sizeof(BEAK) / sizeof(BEAK[0]); i++) {
            /* Progressive, so the jaw pivots at the hinge instead of sliding down in one
               piece: the further from the joint, the further it travels. */
            const int drop = i >= 1 ? SY(gape * (int)i / 3) : 0;
            fill_round_rect(fb, hx - SX(BEAK[i].w / 2), oy + SY(BEAK[i].y) + bob + drop,
                            SX(BEAK[i].w), SY(BEAK[i].h), (int)(5 * s), beak);
        }
        fill_rect(fb, hx - SX(16), oy + SY(-97) + bob, SX(32), SY(2), shade(hex, 0.28f));
    }

    /* Cheeks, after the head and before the eyes. Outboard of the eyes and inboard of the
       head's edge, which on a 128-wide head leaves exactly this much room. */
    if (st->fig.extra == EXTRA_BLUSH) draw_blush(fb, hx, oy + SY(-110) + bob, SX(50), s);

    /* THE ROBOT'S EYES, UNCHANGED. Six emotions already work as lid geometry; a form that
       redrew them would have to re-implement all six.
       1.10, not the mock's 0.98: that number is a multiplier on a DIFFERENT base. The mock
       draws 46x54 and `draw_eye` draws 52*0.78 x 62*0.78 = 40.6x48.4, so transcribing the
       scalar literally shrank the approved eyes by a ninth. 1.10 reproduces 45.1x52.9 to
       within half a pixel. */
    const int ey = oy + SY(OS_EYE_Y) + bob;
    if (front) {
        draw_eye(fb, hx - SX(OS_EYE_X), ey, dark, &st->eyes.l, st->open, st->startle,
                 s * 1.10f);
        draw_eye(fb, hx + SX(OS_EYE_X), ey, dark, &st->eyes.r, st->open, st->startle,
                 s * 1.10f);
    }

    if (u > 0.01f) draw_wing(fb, wx, wy, ww, wh, s, wing, col);
}

void face_rest(face_state_t *st)
{
    if (st == NULL) return;
    st->form = FORM_OSTRICH;
    st->bob = 0;
    st->lean = 0;
    st->talk = 0.0f;
    st->open = 1.0f;
    st->startle = 0.0f;
    emotion_resolve(FACE_HAPPY, &st->eyes);
    rig_for(ACT_NONE, 0.0f, 1.0f, 0, &st->rig);
    rig_figure(ACT_NONE, 0.0f, 1.0f, 0, 0.0f, &st->fig);
}

void face_draw(uint16_t *fb, int colour, const face_state_t *st)
{
    /* Everything hangs off these, so one pair of offsets moves the whole figure. See
       ROOM_ENDPOINT_PLAN.md §10.4s: consecutive frames have to DIFFER, not merely arrive. */
    const int ox = OX + (int)lrintf(((float)st->lean + st->fig.ox) * s_fit);
    const int oy = (s_fit_oy >= 0 ? s_fit_oy : OY) +
                   (int)lrintf(((float)st->bob + st->fig.oy) * s_fit);
    const float sx = st->fig.sx * s_fit, sy = st->fig.sy * s_fit;
    /* The head tilt stands in for the web rig's whole-figure rotation — see rig.h. Offsetting
       the head against the torso is the cue curious and silly actually need, and it costs two
       adds instead of resampling 165 000 pixels. */
    const int tilt = (int)lrintf(st->fig.tilt * 1.6f);
    /* One scalar for radii and stroke widths, which cannot take independent x and y. */
    const float s = (sx + sy) * 0.5f;
    const uint32_t hex = PALETTE[colour % face_colour_count()];

    /* Black, not dark grey: on an AMOLED an unlit pixel is OFF, which is why the mock's
       field is #000 and why the figure reads as emitting rather than as a picture. */
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

    if (st->form == FORM_ROBOT) {
        draw_robot(fb, hex, st, ox, oy, sx, sy, s, tilt);
    } else {
        draw_ostrich(fb, hex, st, ox, oy, sx, sy, s, tilt);
    }
}
#undef SX
#undef SY
#undef LERPI
