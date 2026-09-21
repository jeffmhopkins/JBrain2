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

/* The cheek blush and the gag puff: two extras the whole-figure transform cannot express, and
   the only artwork in the rig that is not the robot itself. */
static void draw_extra(uint16_t *fb, int ox, int hy, extra_t extra, float scale)
{
    if (extra == EXTRA_BLUSH) {
        const uint16_t pink = rgb(0xFF, 0x7A, 0x9C);
        const int dx = (int)(78.0f * scale), r = (int)(16.0f * scale);
        fill_circle(fb, ox - dx, hy + (int)(26.0f * scale), r, pink);
        fill_circle(fb, ox + dx, hy + (int)(26.0f * scale), r, pink);
    } else if (extra == EXTRA_PUFF) {
        /* Deliberately a cloud and not a colour: the gag has to read on a dark panel from
           across a room, and a tinted robot reads as a new robot. */
        const uint16_t puff = rgb(0x8C, 0x9A, 0x8C);
        const int r = (int)(18.0f * scale);
        fill_circle(fb, ox - (int)(96.0f * scale), hy + (int)(200.0f * scale), r, puff);
        fill_circle(fb, ox - (int)(124.0f * scale), hy + (int)(186.0f * scale),
                    (int)(r * 0.7f), puff);
        fill_circle(fb, ox - (int)(120.0f * scale), hy + (int)(216.0f * scale),
                    (int)(r * 0.6f), puff);
    }
}

void face_rest(face_state_t *st)
{
    if (st == NULL) return;
    st->bob = 0;
    st->lean = 0;
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
    const int ox = OX + st->lean + (int)lrintf(st->fig.ox);
    const int oy = OY + st->bob + (int)lrintf(st->fig.oy);
    const float sx = st->fig.sx, sy = st->fig.sy;
    /* The head tilt stands in for the web rig's whole-figure rotation — see rig.h. Offsetting
       the head against the torso is the cue curious and silly actually need, and it costs two
       adds instead of resampling 165 000 pixels. */
    const int tilt = (int)lrintf(st->fig.tilt * 1.6f);
    /* One scalar for radii and stroke widths, which cannot take independent x and y. */
    const float s = (sx + sy) * 0.5f;

#define SX(v) ((int)lrintf((float)(v) * sx))
#define SY(v) ((int)lrintf((float)(v) * sy))

    const uint32_t hex = PALETTE[colour % face_colour_count()];
    const uint16_t col = shade(hex, 1.0f);
    const uint16_t dark = shade(hex, 0.22f);
    const uint16_t torso = shade(hex, 0.88f);
    const uint16_t plate = shade(hex, 0.55f);
    const uint16_t limb = shade(hex, 0.78f);

    /* Black, not dark grey: on an AMOLED an unlit pixel is OFF, which is why the mock's
       field is #000 and why the robot reads as emitting rather than as a picture. */
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

    const int hy = oy + SY(HEAD_Y);
    draw_extra(fb, ox, hy, st->fig.extra, s);

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

    const int ex = SX((int)(HW * 0.43f)), ey = hy - SY((int)(HH * 0.17f));
    draw_eye(fb, ox + tilt - ex, ey, dark, &st->eyes.l, st->open, st->startle, s);
    draw_eye(fb, ox + tilt + ex, ey, dark, &st->eyes.r, st->open, st->startle, s);

    /* Smile: arc(cx, cy-16, 30) from 0.15pi to 0.85pi, stroked 9 wide. */
    arc_stroke(fb, ox + tilt, hy + SY((int)(HH * 0.52f)) - SY(16), (int)(30 * s),
               (float)M_PI * 0.15f, (float)M_PI * 0.85f, (int)(9 * s), dark);

    /* PEEKABOO LAST, over the eyes it is hiding. Posing the arms by angle cannot do this —
       the hands have to AIM at the eyes, which is the difference between hiding and squatting
       in a corner (`rig.ts` on the shipped dud this replaces). */
    if (st->rig.hands_up > 0.01f) {
        const float u = st->rig.hands_up > 1.0f ? 1.0f : st->rig.hands_up;
        const int rest_x = SX(66), rest_y = oy + SY(SHOULDER_Y) + SY(ARM_L);
        const int hx = (int)lrintf((float)rest_x + ((float)(ex) - (float)rest_x) * u);
        const int hyy = (int)lrintf((float)rest_y + ((float)ey - (float)rest_y) * u);
        const int hr = (int)(28 * 0.62f * s);
        fill_circle(fb, ox + tilt - hx, hyy, hr, limb);
        fill_circle(fb, ox + tilt + hx, hyy, hr, limb);
    }
#undef SX
#undef SY
}
