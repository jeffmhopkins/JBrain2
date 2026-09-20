/* The robot, drawn at the geometry the mock settled on.
 *
 * Every number here is lifted from `docs/mocks/room-endpoint/pet-face.html`, whose canvas is
 * 368x448 — the panel exactly — so its coordinates need no mapping. That is the point of
 * having built the mock at true geometry: the design was measured against this screen before
 * the screen existed, and copying the numbers is more faithful than re-deriving them.
 *
 * WHAT THIS IS NOT. The mock's robot is a rig: ~17 tweened floats, six emotions in lid
 * geometry, limbs that exist for the gags. This is the REST POSE only — head, eyes, antenna,
 * torso, chest plate, smile — with no tweening, no emotion and no limbs. It is the thing that
 * proves the geometry and the colour pipeline are right, so that W4's rig has something true
 * to animate rather than a drawing to argue with.
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

static void draw_eye(uint16_t *fb, int cx, int cy, uint16_t dark)
{
    /* 52x62 in the mock, scaled 0.78 because the eyes shrink with the head on a body. */
    const int w = (int)(52 * 0.78f), h = (int)(62 * 0.78f);
    const int r = (int)((w < h ? w : h) * 0.38f);
    fill_round_rect(fb, cx - w / 2, cy - h / 2, w, h, r, rgb(0xF6, 0xF9, 0xFC));
    fill_circle(fb, cx, cy, (int)(17 * 0.78f), dark);
    /* The catchlight is what stops the eye reading as a hole. */
    fill_circle(fb, cx - (int)(6 * 0.78f), cy - (int)(7 * 0.78f), (int)(5 * 0.78f),
                rgb(0xFF, 0xFF, 0xFF));
}

void face_draw(uint16_t *fb, int colour)
{
    const uint32_t hex = PALETTE[colour % face_colour_count()];
    const uint16_t col = shade(hex, 1.0f);
    const uint16_t dark = shade(hex, 0.22f);
    const uint16_t torso = shade(hex, 0.88f);
    const uint16_t plate = shade(hex, 0.55f);

    /* Black, not dark grey: on an AMOLED an unlit pixel is OFF, which is why the mock's
       field is #000 and why the robot reads as emitting rather than as a picture. */
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));

    /* Rest pose from the mock's `rig()`: arms +/-12 degrees, legs +/-4. Drawing order is the
       mock's too — legs behind everything, arms behind the torso while they hang. */
    const uint16_t limb = shade(hex, 0.78f);
    draw_limb(fb, OX - 34, OY + HIP_Y, 4.0f, LEG_L, 30, limb);
    draw_limb(fb, OX + 34, OY + HIP_Y, -4.0f, LEG_L, 30, limb);
    draw_limb(fb, OX - 66, OY + SHOULDER_Y, 12.0f, ARM_L, 28, limb);
    draw_limb(fb, OX + 66, OY + SHOULDER_Y, -12.0f, ARM_L, 28, limb);

    fill_round_rect(fb, OX - 72, OY - 26, 144, 132, 40, torso);
    fill_round_rect(fb, OX - 26, OY + 10, 52, 40, 12, plate);

    const int hy = OY + HEAD_Y;
    /* Antenna first: it sits behind the head, as the mock's silhouette pass does. */
    fill_rect(fb, OX - 4, hy - HH - 30, 8, 30, col);
    fill_circle(fb, OX, hy - HH - 36, 11, col);

    fill_round_rect(fb, OX - HW, hy - HH, HW * 2, HH * 2, 40, col);

    const int ex = (int)(HW * 0.43f), ey = hy - (int)(HH * 0.17f);
    draw_eye(fb, OX - ex, ey, dark);
    draw_eye(fb, OX + ex, ey, dark);

    /* Smile: arc(cx, cy-16, 30) from 0.15pi to 0.85pi, stroked 9 wide. */
    arc_stroke(fb, OX, hy + (int)(HH * 0.52f) - 16, 30, (float)M_PI * 0.15f,
               (float)M_PI * 0.85f, 9, dark);
}
