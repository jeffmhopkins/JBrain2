#include "confirm.h"

#include <stdbool.h>
#include <math.h>

int confirm_cy(int over_h)
{
    return over_h - CONFIRM_MARGIN - CONFIRM_R;
}

static bool within(int fx, int fy, int cx, int cy)
{
    const int dx = fx - cx, dy = fy - cy;
    return dx * dx + dy * dy <= CONFIRM_HIT_R * CONFIRM_HIT_R;
}

/* Which half of the band a touch is in, and which side. The whole geometry of both hit tests is
   here so the two cannot drift: the icons are DRAWN as discs, and HIT as quadrants. */
static bool in_band(int fx, int fy, int over_h)
{
    return fx >= 0 && fx < FACE_W && fy >= 0 && fy < over_h;
}

confirm_hit_t confirm_hit(int fx, int fy, int over_h)
{
    /* QUADRANTS, NOT DISCS, AND THE DISCS WERE THE BUG. The targets were 82 px of reach around
       a 56 px circle, tuned against a fingertip, with a deliberate 20 px dead band between them
       so a finger landing in the middle did nothing. The owner, watching the twins actually use
       it: *"the presses on the buttons ... don't need to be exactly on the icon. Just split the
       [screen] into quarters and use that. The children's press accuracy is not great."*
       A dead band is only a safety feature when the alternative is picking the WRONG target;
       here the alternative was picking NEITHER, which for a four-year-old reads as the panel
       ignoring them.

       THE TOP HALF STAYS INERT, and that part of the old design was right and is kept: the pet
       is up there, and a stray palm during a recording must not be able to send or destroy it.
       So the reach grows from a circle to a quarter of the glass, and no further. */
    if (!in_band(fx, fy, over_h) || fy < over_h / 2) return CONFIRM_NONE;
    return fx < FACE_W / 2 ? CONFIRM_CANCEL : CONFIRM_SEND;
}

/* A filled disc, clipped to the buffer. */
static void disc(uint16_t *fb, int w, int h, int cx, int cy, int r, uint16_t colour)
{
    for (int dy = -r; dy <= r; dy++) {
        const int py = cy + dy;
        if (py < 0 || py >= h) continue;
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx;
            if (px >= 0 && px < w) fb[py * w + px] = colour;
        }
    }
}

/* A thick stroke between two points: a disc walked along the line rather than a thin line
   widened afterwards, which is what keeps the elbow of the tick and the crossing of the cross
   solid instead of showing a notch where two strokes meet. */
static void stroke_r(uint16_t *fb, int w, int h, int x0, int y0, int x1, int y1, int r,
                     uint16_t colour)
{
    const int dx = x1 - x0, dy = y1 - y0;
    const int ax = dx < 0 ? -dx : dx, ay = dy < 0 ? -dy : dy;
    int steps = ax > ay ? ax : ay;
    if (steps < 1) steps = 1;
    if (r < 1) r = 1;
    for (int i = 0; i <= steps; i++) {
        disc(fb, w, h, x0 + dx * i / steps, y0 + dy * i / steps, r, colour);
    }
}

static void stroke(uint16_t *fb, int w, int h, int x0, int y0, int x1, int y1, uint16_t colour)
{
    stroke_r(fb, w, h, x0, y0, x1, y1, CONFIRM_STROKE / 2, colour);
}

void confirm_draw(uint16_t *fb, int w, int h, int over_h)
{
    const int cy = confirm_cy(over_h);
    const int a = CONFIRM_R / 2;

    /* CANCEL, left. */
    disc(fb, w, h, CONFIRM_CX_CANCEL, cy, CONFIRM_R, CONFIRM_RED);
    stroke(fb, w, h, CONFIRM_CX_CANCEL - a, cy - a, CONFIRM_CX_CANCEL + a, cy + a,
           CONFIRM_GLYPH);
    stroke(fb, w, h, CONFIRM_CX_CANCEL + a, cy - a, CONFIRM_CX_CANCEL - a, cy + a,
           CONFIRM_GLYPH);

    /* SEND, right. Both arms are drawn OUTWARDS FROM THE ELBOW so they meet exactly there;
       drawing the long arm as one stroke through the joint would thin the corner. */
    disc(fb, w, h, CONFIRM_CX_SEND, cy, CONFIRM_R, CONFIRM_GREEN);
    const int ex = CONFIRM_CX_SEND - a / 3, ey = cy + a * 2 / 3;
    stroke(fb, w, h, ex, ey, CONFIRM_CX_SEND - a, cy - a / 4, CONFIRM_GLYPH);
    stroke(fb, w, h, ex, ey, CONFIRM_CX_SEND + a, cy - a, CONFIRM_GLYPH);
}

bool confirm_hit_centre(int fx, int fy, int over_h)
{
    return within(fx, fy, CONFIRM_CX_CENTRE, confirm_cy(over_h));
}

/* A filled axis-aligned rectangle, clipped. */
static void rect(uint16_t *fb, int w, int h, int x0, int y0, int x1, int y1, uint16_t colour)
{
    for (int y = y0; y <= y1; y++) {
        if (y < 0 || y >= h) continue;
        for (int x = x0; x <= x1; x++) {
            if (x >= 0 && x < w) fb[y * w + x] = colour;
        }
    }
}

void confirm_draw_stop(uint16_t *fb, int w, int h, int over_h)
{
    const int cy = confirm_cy(over_h);
    disc(fb, w, h, CONFIRM_CX_CENTRE, cy, CONFIRM_R, CONFIRM_RED);
    /* A SQUARE, not an X. The cross already means "throw this away" on the recording screen,
       and a control that stops a message playing must not read as one that destroys it — the
       message stays in the queue either way. Square is what every transport control on earth
       uses for stop, including the ones these children have already seen on a television. */
    const int a = CONFIRM_R / 2;
    rect(fb, w, h, CONFIRM_CX_CENTRE - a, cy - a, CONFIRM_CX_CENTRE + a, cy + a, CONFIRM_GLYPH);
}

/* An arc of the circle centred on (cx,cy), swept between two angles in degrees, drawn with the
   same walked disc the strokes use so its ends and its thickness match them. */
static void arc(uint16_t *fb, int w, int h, int cx, int cy, int r, int deg0, int deg1,
                uint16_t colour)
{
    const int steps = (deg1 - deg0 > 0 ? deg1 - deg0 : deg0 - deg1) * 2;
    for (int i = 0; i <= steps; i++) {
        const double t = (double)deg0 + (double)(deg1 - deg0) * (double)i / (double)steps;
        const double rad = t * M_PI / 180.0;
        disc(fb, w, h, cx + (int)lround(cos(rad) * r), cy + (int)lround(sin(rad) * r),
             CONFIRM_STROKE / 2, colour);
    }
}

/* A filled triangle, by half-plane test. Three points, no winding assumption. */
static void tri(uint16_t *fb, int w, int h, const int x[3], const int y[3], uint16_t colour)
{
    int xmin = x[0], xmax = x[0], ymin = y[0], ymax = y[0];
    for (int i = 1; i < 3; i++) {
        if (x[i] < xmin) xmin = x[i];
        if (x[i] > xmax) xmax = x[i];
        if (y[i] < ymin) ymin = y[i];
        if (y[i] > ymax) ymax = y[i];
    }
    for (int py = ymin; py <= ymax; py++) {
        if (py < 0 || py >= h) continue;
        for (int px = xmin; px <= xmax; px++) {
            if (px < 0 || px >= w) continue;
            const int d0 = (x[1] - x[0]) * (py - y[0]) - (y[1] - y[0]) * (px - x[0]);
            const int d1 = (x[2] - x[1]) * (py - y[1]) - (y[2] - y[1]) * (px - x[1]);
            const int d2 = (x[0] - x[2]) * (py - y[2]) - (y[0] - y[2]) * (px - x[2]);
            const bool neg = (d0 < 0) || (d1 < 0) || (d2 < 0);
            const bool pos = (d0 > 0) || (d1 > 0) || (d2 > 0);
            if (!(neg && pos)) fb[py * w + px] = colour;
        }
    }
}

void confirm_draw_repeat(uint16_t *fb, int w, int h, int over_h)
{
    const int cy = confirm_cy(over_h);
    disc(fb, w, h, CONFIRM_CX_CENTRE, cy, CONFIRM_R, CONFIRM_BLUE);

    /* Three quarters of a circle with an arrowhead on the open end — the refresh symbol, which
       is the one circular arrow an adult reads instantly and a child has seen on every remote
       and tablet in the house. The gap is at the top right so the head sits where the eye
       finishes the sweep. */
    const int r = (CONFIRM_R * 55) / 100;
    arc(fb, w, h, CONFIRM_CX_CENTRE, cy, r, 300, 580, CONFIRM_GLYPH);

    /* The head, tangent to the arc at its end. Pointing anticlockwise-onward, which is the
       direction the sweep was travelling. */
    const double end = 300.0 * M_PI / 180.0;
    const int hx = CONFIRM_CX_CENTRE + (int)lround(cos(end) * r);
    const int hy = cy + (int)lround(sin(end) * r);
    const int a = CONFIRM_STROKE + 5;
    const int tx[3] = {hx - a, hx + a, hx + (a / 2)};
    const int ty[3] = {hy - (a / 2), hy - (a / 2), hy + a};
    tri(fb, w, h, tx, ty, CONFIRM_GLYPH);
}

/* --- the "who?" grid ------------------------------------------------------------------------ */

int sendto_cy_bottom(int over_h)
{
    return confirm_cy(over_h);
}

int sendto_cy_top(int over_h)
{
    return confirm_cy(over_h) - SENDTO_ROW_GAP;
}

sendto_hit_t sendto_hit(int fx, int fy, int over_h)
{
    /* FOUR QUADRANTS, FOR THE REASON IN `confirm_hit`: the children's aim is the constraint, and
       four icons drawn in four corners already say which quarter means what. Every part of the
       band belongs to exactly one target, so there is nowhere left to miss — and the menu is
       modal, so the pet is not underneath it to be poked by mistake. */
    if (!in_band(fx, fy, over_h)) return SENDTO_NONE;
    const bool lower = fy >= over_h / 2, right = fx >= FACE_W / 2;
    if (!lower) return right ? SENDTO_DAD : SENDTO_SISTER;
    return right ? SENDTO_PET : SENDTO_CANCEL;
}

/* A disc, but only the part of it at or below `ymin`. A beard is the bottom of a circle and a
   fringe is the top of one, so both fall out of this rather than needing a polygon filler this
   file does not have and does not need. */
static void disc_below(uint16_t *fb, int w, int h, int cx, int cy, int r, int ymin,
                       uint16_t colour)
{
    for (int dy = -r; dy <= r; dy++) {
        const int py = cy + dy;
        if (py < ymin || py < 0 || py >= h) continue;
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx;
            if (px >= 0 && px < w) fb[py * w + px] = colour;
        }
    }
}

/* And the part at or above `ymax`. Hair on a head is the TOP of a circle; the first pass used
   the `below` helper for it, which filled the whole disc from its own top edge downwards and
   rendered both faces wearing a hood. */
static void disc_above(uint16_t *fb, int w, int h, int cx, int cy, int r, int ymax,
                       uint16_t colour)
{
    for (int dy = -r; dy <= r; dy++) {
        const int py = cy + dy;
        if (py > ymax || py < 0 || py >= h) continue;
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx;
            if (px >= 0 && px < w) fb[py * w + px] = colour;
        }
    }
}

/* A SMILE, as an arc rather than a line. Three chords of a shallow curve is all it takes at
   this size, and a straight mouth on a picture of your sister reads as cross. */
static void smile(uint16_t *fb, int w, int h, int cx, int cy, int fr, uint16_t colour)
{
    const int half = fr * 9 / 32, drop = fr * 4 / 32, t = fr * 3 / 32;
    stroke_r(fb, w, h, cx - half, cy, cx - half / 3, cy + drop, t, colour);
    stroke_r(fb, w, h, cx - half / 3, cy + drop, cx + half / 3, cy + drop, t, colour);
    stroke_r(fb, w, h, cx + half / 3, cy + drop, cx + half, cy, t, colour);
}

/* HER EYES ARE BLUE, and they are drawn as an iris with a pupil rather than a dot: at this size
   a solid blue disc reads as a blank stare, and the dark centre is what makes it a face. */
static void eye_blue(uint16_t *fb, int w, int h, int cx, int cy, int r)
{
    disc(fb, w, h, cx, cy, r, CONFIRM_GLYPH);
    disc(fb, w, h, cx, cy, r * 3 / 4, SENDTO_BLUE_EYE);
    disc(fb, w, h, cx, cy, r * 3 / 8, SENDTO_HAIR);
}

/* A LITTLE GIRL WITH LONG BLONDE HAIR, BLUE EYES AND A SMILE — the owner's daughters, not a
   generic child: *"the girls have blonde hair, blue eyes ... and the girls should be smiling."*
   The hair falls at the SIDES and her CHIN IS LEFT BARE, which is what stops her reading as
   bearded; an earlier pass drew it as a mass below the face, exactly where a beard goes, and
   the owner said so. The two faces are told apart by WHERE the dark is, not how much there is. */
static void face_girl(uint16_t *fb, int w, int h, int cx, int cy, int fr)
{
    /* The crown and the back of the head. */
    disc(fb, w, h, cx, cy - fr / 10, fr * 6 / 5, SENDTO_BLONDE);
    /* THE TWO FALLS, down each side and past the jaw, with nothing between them. */
    const int fall_x = fr * 19 / 20, fall_r = fr * 2 / 5;
    stroke_r(fb, w, h, cx - fall_x, cy - fr / 5, cx - fall_x - fr / 6, cy + fr * 3 / 2, fall_r,
             SENDTO_BLONDE);
    stroke_r(fb, w, h, cx + fall_x, cy - fr / 5, cx + fall_x + fr / 6, cy + fr * 3 / 2, fall_r,
             SENDTO_BLONDE);
    /* The face on top, so the falls sit behind it. */
    disc(fb, w, h, cx, cy, fr * 23 / 25, SENDTO_SKIN);
    /* A fringe: a band across the brow, well clear of the eyes. */
    disc_above(fb, w, h, cx, cy - fr * 11 / 20, fr * 19 / 20, cy - fr * 7 / 20, SENDTO_BLONDE);
    const int fr2 = fr * 23 / 25;
    const int eye_r = fr2 * 6 / 32, eye_dx = fr2 * 13 / 32, eye_dy = fr2 * 5 / 32;
    eye_blue(fb, w, h, cx - eye_dx, cy - eye_dy, eye_r);
    eye_blue(fb, w, h, cx + eye_dx, cy - eye_dy, eye_r);
    smile(fb, w, h, cx, cy + fr2 * 10 / 32, fr2, SENDTO_HAIR);
}

/* A RECTANGULAR LENS, as four thin strokes. Dark frames on a light face are the second thing
   anyone would name about him, and at this size an outline reads as glasses where a filled
   block would read as a blindfold. */
static void lens(uint16_t *fb, int w, int h, int x0, int y0, int x1, int y1, int t)
{
    stroke_r(fb, w, h, x0, y0, x1, y0, t, SENDTO_HAIR);
    stroke_r(fb, w, h, x0, y1, x1, y1, t, SENDTO_HAIR);
    stroke_r(fb, w, h, x0, y0, x0, y1, t, SENDTO_HAIR);
    stroke_r(fb, w, h, x1, y0, x1, y1, t, SENDTO_HAIR);
}

/* A MAN WITH A BEARD AND GLASSES, drawn from the photograph the owner sent rather than from
   the idea of a beard: it is LONG, it hangs well past the jaw and it widens on the way down,
   and the glasses are rectangular and dark. Those two features are what a four-year-old in
   this house would name first, and neither of them is anywhere near where her hair is. */
static void face_man(uint16_t *fb, int w, int h, int cx, int cy, int fr)
{
    /* THE HANG, first and behind everything: the long mass below the chin. */
    disc(fb, w, h, cx, cy + fr * 36 / 32, fr * 26 / 32, SENDTO_HAIR);
    disc(fb, w, h, cx, cy, fr, SENDTO_SKIN);
    /* NARROWER THAN THE FACE WHERE IT MEETS IT, which is the difference between a beard and a
       balaclava. Two passes drew the beard and the crown at full face width and left only a
       letterbox of skin between them; a real beard starts at the jaw and leaves the cheeks
       showing on either side of it. Drawn as the bottom of a circle set low and narrow, so its
       top edge is about three-quarters of the face's width. */
    disc_below(fb, w, h, cx, cy + fr * 30 / 32, fr * 28 / 32, cy + fr * 16 / 32, SENDTO_HAIR);
    /* NO HAIR ON TOP, and that is a legibility decision rather than a likeness one. He has
       short hair in the photograph, but every version of it drawn here read as the top half of
       a balaclava once the beard was in place — two dark masses with a slot of face between
       them. The beard and the glasses are what a four-year-old in this house would name, and
       they are unmistakable without it. */
    /* Eyes on the bare band between hair and beard... */
    const int eye_r = fr * 4 / 32, eye_dy = fr * 5 / 32, eye_dx = fr * 13 / 32;
    disc(fb, w, h, cx - eye_dx, cy - eye_dy, eye_r, SENDTO_HAIR);
    disc(fb, w, h, cx + eye_dx, cy - eye_dy, eye_r, SENDTO_HAIR);
    /* ...and the glasses around them. */
    const int t = fr < 28 ? 1 : 2;
    const int ly0 = cy - fr * 10 / 32, ly1 = cy + fr * 1 / 32;
    lens(fb, w, h, cx - fr * 21 / 32, ly0, cx - fr * 5 / 32, ly1, t);
    lens(fb, w, h, cx + fr * 5 / 32, ly0, cx + fr * 21 / 32, ly1, t);
    /* The bridge. */
    stroke_r(fb, w, h, cx - fr * 5 / 32, cy - fr * 5 / 32, cx + fr * 5 / 32, cy - fr * 5 / 32, t,
             SENDTO_HAIR);
}

/* A FILLED ROUNDED RECTANGLE. A robot's head is the one shape on this grid that is not a
   circle, and the corners are the difference between a robot and a brick. */
static void rrect(uint16_t *fb, int w, int h, int cx, int cy, int hw, int hh, int r,
                  uint16_t colour)
{
    for (int dy = -hh; dy <= hh; dy++) {
        const int py = cy + dy;
        if (py < 0 || py >= h) continue;
        for (int dx = -hw; dx <= hw; dx++) {
            const int ox = (dx < -(hw - r)) ? dx + (hw - r) : (dx > hw - r ? dx - (hw - r) : 0);
            const int oy = (dy < -(hh - r)) ? dy + (hh - r) : (dy > hh - r ? dy - (hh - r) : 0);
            if (ox * ox + oy * oy > r * r) continue;
            const int px = cx + dx;
            if (px >= 0 && px < w) fb[py * w + px] = colour;
        }
    }
}

/* THE PET AS A ROBOT, the fourth target: press it and the panel starts the same conversation
   turn the wake phrase does. Which matters most for the child who cannot get a phrase
   recognised at all — this is the door to the pet that does not require being understood.
   Drawn as an icon rather than as `face.c`, which needs a live state struct and a frame this
   function does not have; it has to be recognisable as the thing that answers, not accurate. */
static void face_pet(uint16_t *fb, int w, int h, int cx, int cy, int fr)
{
    /* The antenna, first, so the head covers its root. */
    const int ant_top = cy - fr * 40 / 32;
    stroke_r(fb, w, h, cx, cy - fr, cx, ant_top, fr * 2 / 32, SENDTO_HAIR);
    disc(fb, w, h, cx, ant_top, fr * 6 / 32, SENDTO_ROBOT_EYE);
    /* The head. */
    rrect(fb, w, h, cx, cy, fr * 30 / 32, fr * 26 / 32, fr * 8 / 32, SENDTO_ROBOT);
    /* THE EYES ARE THE FACE, as they are on the pet itself — big, wide apart, and lit. */
    const int eye_r = fr * 8 / 32, eye_dx = fr * 14 / 32, eye_dy = fr * 5 / 32;
    disc(fb, w, h, cx - eye_dx, cy - eye_dy, eye_r, SENDTO_HAIR);
    disc(fb, w, h, cx + eye_dx, cy - eye_dy, eye_r, SENDTO_HAIR);
    disc(fb, w, h, cx - eye_dx, cy - eye_dy, eye_r * 5 / 8, SENDTO_ROBOT_EYE);
    disc(fb, w, h, cx + eye_dx, cy - eye_dy, eye_r * 5 / 8, SENDTO_ROBOT_EYE);
    /* A grille for a mouth: three bars, which reads as a speaker rather than a smile. */
    const int my = cy + fr * 13 / 32, mw = fr * 13 / 32, t = fr * 3 / 32;
    for (int i = -1; i <= 1; i++) {
        const int mx = cx + i * mw * 2 / 3;
        stroke_r(fb, w, h, mx, my - fr * 4 / 32, mx, my + fr * 4 / 32, t, SENDTO_HAIR);
    }
}

void sendto_draw(uint16_t *fb, int w, int h, int over_h)
{
    const int top = sendto_cy_top(over_h), bottom = sendto_cy_bottom(over_h);
    const int a = CONFIRM_R / 2;

    /* SISTER, top left. The face is deliberately smaller than the man's: she is a little girl,
       and the two faces sit on discs of the same size so what differs is the person. */
    disc(fb, w, h, CONFIRM_CX_CANCEL, top, CONFIRM_R, SENDTO_SISTER_COLOUR);
    face_girl(fb, w, h, CONFIRM_CX_CANCEL, top, CONFIRM_R * 9 / 20);

    /* DAD, top right. */
    disc(fb, w, h, CONFIRM_CX_SEND, top, CONFIRM_R, SENDTO_DAD_COLOUR);
    face_man(fb, w, h, CONFIRM_CX_SEND, top, CONFIRM_R * 11 / 20);

    /* THE PET, bottom right — the fourth way this panel can be spoken into, and the one that
       was a blank corner until the owner decided what belonged in it. */
    disc(fb, w, h, CONFIRM_CX_SEND, bottom, CONFIRM_R, SENDTO_PET_COLOUR);
    face_pet(fb, w, h, CONFIRM_CX_SEND, bottom, CONFIRM_R * 3 / 5);

    /* CANCEL, bottom left — the cross a child has already learned, in the colour and at the
       size they learned it, so leaving a menu is the gesture they already know. */
    disc(fb, w, h, CONFIRM_CX_CANCEL, bottom, CONFIRM_R, CONFIRM_RED);
    stroke(fb, w, h, CONFIRM_CX_CANCEL - a, bottom - a, CONFIRM_CX_CANCEL + a, bottom + a,
           CONFIRM_GLYPH);
    stroke(fb, w, h, CONFIRM_CX_CANCEL + a, bottom - a, CONFIRM_CX_CANCEL - a, bottom + a,
           CONFIRM_GLYPH);
}
