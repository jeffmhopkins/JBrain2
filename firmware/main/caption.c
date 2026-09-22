#include "caption.h"

#include <string.h>

#include "font.h"

/* Big enough to read across a room on a 322 ppi panel, small enough that the figure above it
   is still the thing you look at. At scale 2 a glyph advances 12 px, so ~30 characters are on
   the glass at once. */
#define SCALE 2
#define ADVANCE ((FONT_W + 1) * SCALE)
#define ROW_H (FONT_H * SCALE)
#define MARGIN 8
#define PIP_R 5
/* 32, NOT 10, AND THE DIFFERENCE IS WHETHER ANYONE CAN SEE IT. At 10 the dot's outer edge sat
   about 50 px from the bottom-left corner's centre of curvature, against the case's 48 px
   radius (`FACE_CASE_CORNER_R`) — so the indicator the ICO Children's Code requires while the
   microphone is open was drawn perfectly and hidden by the enclosure. 32 puts the whole dot,
   at its largest pulse, inside the opening with room to spare. */
#define PIP_X 32

/* Slow enough to read, and it SPEEDS UP WITH THE BACKLOG. A fixed rate is what makes a ticker
   feel broken: say four things quickly and the fourth arrives half a minute later, by which
   time the child has stopped looking. */
#define SPEED_MIN 46.0f
#define SPEED_MAX 150.0f

/* One blank between phrases, so two heard things do not run together as one word. */
#define GAP "   "

void caption_reset(caption_t *c)
{
    if (c == NULL) return;
    memset(c, 0, sizeof(*c));
}

static int pending_px(const caption_t *c)
{
    return c->len * ADVANCE - (int)c->scrolled;
}

float caption_speed(const caption_t *c, int panel_w)
{
    if (c == NULL || panel_w <= 0) return SPEED_MIN;
    /* One panel-width of backlog is "keeping up"; beyond that, drain faster in proportion. */
    const int over = pending_px(c) - panel_w;
    if (over <= 0) return SPEED_MIN;
    const float s = SPEED_MIN + (float)over * (SPEED_MIN / (float)panel_w);
    return s > SPEED_MAX ? SPEED_MAX : s;
}

bool caption_say(caption_t *c, const char *text)
{
    if (c == NULL || text == NULL || *text == '\0') return false;
    const int add = (int)strlen(text) + (c->len > 0 ? (int)sizeof(GAP) - 1 : 0);
    if (c->len + add > CAPTION_MAX) return false;
    if (c->len > 0) {
        memcpy(c->buf + c->len, GAP, sizeof(GAP) - 1);
        c->len += (int)sizeof(GAP) - 1;
    }
    memcpy(c->buf + c->len, text, strlen(text));
    c->len += (int)strlen(text);
    c->buf[c->len] = '\0';
    return true;
}

void caption_tick(caption_t *c, uint32_t now_ms, int panel_w, bool live, bool speech)
{
    if (c == NULL) return;
    const uint32_t dt = c->last_ms == 0 ? 0 : now_ms - c->last_ms;
    c->last_ms = now_ms;
    c->live = live;

    /* Eased both ways, because a VAD flag toggling per frame would strobe a light that exists
       to tell a room the microphone is open. */
    const float want = (live && speech) ? 1.0f : (live ? 0.25f : 0.0f);
    c->lit += (want - c->lit) * 0.25f;

    if (panel_w <= 0) panel_w = 1;
    if (c->len == 0 || dt == 0 || dt > 2000) return; /* a long gap is a stall, not travel */
    c->scrolled += caption_speed(c, panel_w) * (float)dt / 1000.0f;

    /* Retire characters that have left the panel, and take the same distance back off the
       scroll so nothing on screen moves when they go. */
    while (c->len > 0 && c->scrolled >= (float)(panel_w + ADVANCE)) {
        memmove(c->buf, c->buf + 1, (size_t)c->len);
        c->len--;
        c->scrolled -= (float)ADVANCE;
    }
    if (c->len == 0) c->scrolled = 0.0f;
}

bool caption_idle(const caption_t *c)
{
    return c == NULL || c->len == 0;
}

void caption_draw(const caption_t *c, uint16_t *fb, int fbw, int fbh, uint16_t text,
                  uint16_t pip)
{
    if (c == NULL || fb == NULL) return;
    const int y = fbh - MARGIN - ROW_H;

    if (c->len > 0) {
        /* OUTLINED, NOT BOXED. This used to clear a full-width black strip before drawing,
           and the reason was real: the figure's feet reach y=435 on a 448 px panel while the
           ticker runs at 432, and "PLAY PEEKABOO" measurably ran straight through the
           ostrich's toes. The bar made it legible over anything.
           
           But the owner watches this thing: "the text scrolling on the bottom ... the black
           in. It should be transparent." A band of dead black across a pet's feet is a
           subtitle bar on a toy, and on an AMOLED it is not even subtle — those pixels are
           OFF, so it is a hard-edged hole in the picture rather than a tint.
           
           So the legibility comes from the glyphs instead, the way subtitles have always
           done it: the text is drawn four times in unlit black, offset by two pixels each
           way, and then once in its own colour on top. That carves a dark halo around each
           letter and leaves every pixel between the letters untouched, so the pet shows
           through and the words stay readable over legs, wings or nothing at all.
           
           Five draws rather than one, on a string this short, at the five frames a second
           the face actually redraws. */
        const int x = fbw - (int)c->scrolled;
        static const int HALO[4][2] = {{-2, 0}, {2, 0}, {0, -2}, {0, 2}};
        for (unsigned i = 0; i < sizeof(HALO) / sizeof(HALO[0]); i++) {
            font_draw(fb, fbw, fbh, x + HALO[i][0], y + HALO[i][1], SCALE, c->buf, 0);
        }
        /* Enters from the right edge and travels left, so the newest word is the one
           arriving. `font_draw` clips, which is the whole reason the ticker can be drawn as
           one string however long the backlog is. */
        font_draw(fb, fbw, fbh, x, y, SCALE, c->buf, text);
    }

    /* THE INDICATOR IS DRAWN WHENEVER THE MICROPHONE IS OPEN, with or without a caption, and
       LAST so a caption sweeping past can never paint over it. It is what tells the room it
       is being listened to. */
    if (c->live) {
        const int r = (int)(PIP_R * (0.6f + 0.6f * c->lit));
        const int cy = y + ROW_H / 2;
        for (int dy = -r; dy <= r; dy++) {
            for (int dx = -r; dx <= r; dx++) {
                if (dx * dx + dy * dy > r * r) continue;
                const int px = PIP_X + dx, py = cy + dy;
                if (px < 0 || py < 0 || px >= fbw || py >= fbh) continue;
                fb[py * fbw + px] = pip;
            }
        }
    }
}
