#include "overlay.h"

#include <stdio.h>

#include "confirm.h"
#include "face.h"
#include "font.h"

/* A filled rounded box. `display.c` has no drawing library and does not need one: the bubble
   is one rectangle and four corners, and the corners are the difference between a speech
   bubble and a dialog from 1994. */
static void bubble(uint16_t *fb, int x0, int y0, int w, int h, int r, uint16_t c)
{
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            const int dx = x < r ? r - x : (x >= w - r ? x - (w - r - 1) : 0);
            const int dy = y < r ? r - y : (y >= h - r ? y - (h - r - 1) : 0);
            if (dx * dx + dy * dy > r * r) continue;
            const int px = x0 + x, py = y0 + y;
            if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) fb[py * FACE_W + px] = c;
        }
    }
}

/* THE THINKING BOX, AND IT IS NOT DECORATION — IT IS THE LATENCY BUDGET.
 *
 * The round trip is capture, upload, transcribe, a language model and speech synthesis, and
 * the transcription alone is the slowest part (see `../proposed/PANEL_CONVERSATION_PLAN.md`).
 * A pet that is visibly thinking the instant the finger lifts buys a second or more of that
 * for nothing, because a wait you can see someone working through is not the same wait.
 *
 * Three dots filling in turn, which is the one idiom a four-year-old already reads. */
static void draw_thinking(uint16_t *fb, int y0, int h, uint32_t now, bool failed)
{
    const int bw = 118, bh = 54;
    const int bx = FACE_W - bw - 12;
    const int by = y0 + 14;
    bubble(fb, bx, by, bw, bh, 16, failed ? SWAP16(0x4208) : SWAP16(0x2965));
    (void)h;
    const int cy = by + bh / 2;
    if (failed) {
        /* A single flat dash: it tried and has nothing. Deliberately not a third dot — the
           shape has to differ from thinking at a glance, not merely in colour. */
        for (int y = cy - 2; y <= cy + 2; y++) {
            for (int x = bx + 34; x < bx + bw - 34; x++) fb[y * FACE_W + x] = SWAP16(0xF800);
        }
        return;
    }
    const int lit = (int)((now / 320) % 4); /* 0..3, so all three are briefly up */
    for (int i = 0; i < 3; i++) {
        const int cx = bx + 30 + i * 29;
        const int r = (i < lit) ? 9 : 5;
        const uint16_t c = (i < lit) ? SWAP16(0xFFFF) : SWAP16(0x6B4D);
        for (int dy = -r; dy <= r; dy++) {
            for (int dx = -r; dx <= r; dx++) {
                if (dx * dx + dy * dy > r * r) continue;
                const int px = cx + dx, py = cy + dy;
                if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) {
                    fb[py * FACE_W + px] = c;
                }
            }
        }
    }
}

/* LISTENING: a red dot, pulsing so it cannot be mistaken for a dead pixel or a bit of the pet.
 *
 * AND A WORD, because the dot did need explaining. This comment used to claim it was "the one
 * symbol for recording that needs no explaining", and then the owner — who specified the
 * feature — photographed it and asked what it indicated. A symbol only reads as recording
 * next to a camera; on a pet's face it reads as part of the pet. The dot stays for the twins,
 * who cannot read it, and the word is for whoever has to work out why the panel is doing
 * something. It is also the fastest way to notice a listen nobody started, which is the
 * failure this release is otherwise chasing. */
#define LISTEN_SCALE 2
/* ONE SHAPE, TWO MEANINGS, AND THE COLOUR IS THE DIFFERENCE. Factored out when voice post
   arrived rather than copied: a second pulsing dot drawn by a second function is two things
   to keep in step, and the pulse rate IS the affordance — a recording indicator that breathed
   at a different speed would read as a different kind of thing entirely. */
static void draw_indicator(uint16_t *fb, int y0, uint32_t now, uint16_t colour, const char *word)
{
    const int r = 13 + (int)((now / 140) % 4);
    const int cx = FACE_W - 34, cy = y0 + 34;
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx, py = cy + dy;
            if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) {
                fb[py * FACE_W + px] = colour;
            }
        }
    }
    const int w = font_text_w(word, LISTEN_SCALE);
    font_draw(fb, FACE_W, FACE_H, FACE_W - 8 - w, cy + 22, LISTEN_SCALE, word, colour);
}

/* WHO IS ON THE OTHER END, AS A FACE, AT A QUARTER OF THE GLASS.
 *
 * The owner: *"up where it says listening and it's either red or blue. We should have like a
 * 1/4 size face of who they're talking to. So it has a picture of the dad [or] the sister face
 * or the robot up on there."*
 *
 * THE WORD WAS NEVER READABLE BY THE READERS. "LISTENING" and "TO DAD" are shapes a
 * pre-literate four-year-old memorises, and the panel already has a picture of each of these
 * three people that the child picked out of a menu themselves. The same face, in the same
 * colour, in the place that says who is listening: no reading required, and it matches what
 * they just pressed.
 *
 * TOP LEFT, opposite the pulsing dot that still says whether it is a message or a turn — the
 * face says WHO, the dot and its colour say WHAT, and neither has to carry both. */
static void draw_who(uint16_t *fb, int y0, int over_h, sendto_hit_t who)
{
    const int r = (FACE_W / 2) / 2 - 8;
    sendto_draw_face(fb, FACE_W, FACE_H, r + 10, y0 + r + 10, r, who);
    (void)over_h;
}

/* THE MICROPHONE IS OPEN, AND THAT IS ONE FACT WITH ONE COLOUR.
 *
 * Listening to the pet was red and recording a message was blue, and the blue was carrying the
 * RECIPIENT — pet or person — because for a while it was the only thing that could. It is not any
 * more: the sender's face is drawn above the word, so who this is for is a picture now. The owner:
 * *"now that we have icons the colour differential is not important, and blue kind of doesn't have
 * the same effect as red as knowing when we are recording."*
 *
 * So the colour is freed to mean the thing every other device in a child's life uses it for. Red
 * is recording, whoever is listening. */
#define REC_COLOUR SWAP16(0xF800)

static void draw_listening(uint16_t *fb, int y0, int over_h, uint32_t now)
{
    /* The pet's own face: a hands-free turn is a conversation with the pet, and the icon the
       child presses for it on the grid is this one. */
    draw_who(fb, y0, over_h, SENDTO_PET);
    draw_indicator(fb, y0, now, REC_COLOUR, "LISTENING");
}

/* RECORDING A MESSAGE. Red, like every other open microphone on this panel (`REC_COLOUR`), and
   the word names the RECIPIENT rather than the act — the act is the part a child already knows,
   they just asked for it, and who it is going to is the part they cannot see.
 *
 * THE SIBLING'S NAME USED TO BE UNKNOWABLE HERE, and the placeholder MESSAGE was the honest
 * way to say so: a panel is flashed with its OWN name and the box mints the other one's at the
 * other unit's flash, so nothing on this device could answer "who is my twin". `GET /waiting`
 * now carries it — it is a poll that was already happening — and the fallback stays for the
 * cases the box deliberately declines to answer: before the first poll, on a box with one
 * panel, and on a box with three, where "the other one" is a question rather than a name and a
 * guess would put the wrong child on the glass. */
static void draw_recording(uint16_t *fb, int y0, int over_h, uint32_t now, jp_to_t to,
                           const char *sibling)
{
    /* The face first, under the word: whoever cannot read the word can read the face, and
       whoever can read gets both. */
    draw_who(fb, y0, over_h, to == JP_TO_DAD ? SENDTO_DAD : SENDTO_SISTER);
    if (to == JP_TO_DAD) {
        draw_indicator(fb, y0, now, REC_COLOUR, "TO DAD");
        return;
    }
    /* "TO " plus the longest name the box will accept, uppercased: the font has no lowercase
       (`font.c`), so a name typed in the PWA has to be shouted here exactly as the pop-up
       shouts it. */
    char who[40];
    if (sibling == NULL || sibling[0] == '\0') {
        draw_indicator(fb, y0, now, REC_COLOUR, "MESSAGE");
        return;
    }
    snprintf(who, sizeof(who), "TO %.31s", sibling);
    for (char *q = who; *q != '\0'; q++) {
        if (*q >= 'a' && *q <= 'z') *q = (char)(*q - 'a' + 'A');
    }
    draw_indicator(fb, y0, now, REC_COLOUR, who);
}

/* THE POP-UP, AND IT IS THE ONLY THING ON THIS GLASS THAT COVERS THE PET.
 *
 * The owner: *"a little pop-up box would show up if a message is available to play."* A
 * badge in a corner would be the polite version and would be the wrong one — the reader is
 * four, cannot read, and is not auditing the screen for changes. It has to interrupt, and it
 * has to be tappable anywhere inside, because a four-year-old aiming at a small target with
 * an excited finger is a miss.
 *
 * IT DOES NOT AUTO-PLAY. A message that starts talking on its own would be the panel making
 * noise in a bedroom at a moment nobody chose; the pop-up survives a reboot (the state lives
 * on the box) and waits.
 *
 * Drawn centred in the SQUARE, not the frame, so a side-mounted panel keeps it — the same
 * rule the caption and the label were moved to obey. */
#define POPUP_SCALE 3
#define POPUP_NAME_SCALE 4
static void draw_popup(uint16_t *fb, int y0, int h, const char *from, int count, int sel,
                       sendto_hit_t who)
{
    /* TALLER, TO MAKE ROOM FOR A FACE. The owner, twice: *"make sure that the notification pop
       up and icon on the top left show the icons of the person instead of being generic."*
       Both surfaces, and the big one matters most — it is what a child sees FIRST, before the
       badge it shrinks into, and it was four lines of words to a reader who cannot read. */
    /* THE SAME RECTANGLE THE MACHINE HIT-TESTS — `sm_notice_box`. It was two locals here and a
       quadrant over there, and they did not overlap. */
    int box[4];
    sm_notice_box(y0, h, true, box);
    const int bx = box[0], by = box[1];
    const int bw = SM_NOTICE_BIG_W, bh = SM_NOTICE_BIG_H;
    bubble(fb, bx, by, bw, bh, 22, SWAP16(0x001F));
    bubble(fb, bx + 5, by + 5, bw - 10, bh - 10, 18, SWAP16(0x0010));

    /* WHOSE MESSAGE IT IS, as the same picture they pressed to send one. Above the name rather
       than beside it: centred and large is what a four-year-old reads across a room, and the
       name underneath is for the adult and for the child who is learning to. */
    /* BIGGER THAN THE BADGE'S, because this is the notice that arrives first and the badge is
       what it shrinks into; a primary notification with a smaller picture than its own
       fallback had the hierarchy backwards. */
    sendto_draw_face(fb, FACE_W, FACE_H, bx + bw / 2, by + 62, 50, who);

    char line[40];
    /* Uppercase because that is the alphabet the font has, and the box's names arrive in
       whatever case the owner typed at flash time. */
    snprintf(line, sizeof(line), "%s", from != NULL && from[0] != '\0' ? from : "SOMEONE");
    for (char *q = line; *q != '\0'; q++) {
        if (*q >= 'a' && *q <= 'z') *q = (char)(*q - 'a' + 'A');
    }
    int w = font_text_w(line, POPUP_NAME_SCALE);
    /* A long name is shrunk rather than clipped: a name cut in half names nobody. */
    const int name_scale = w > bw - 32 ? POPUP_SCALE : POPUP_NAME_SCALE;
    w = font_text_w(line, name_scale);
    font_draw(fb, FACE_W, FACE_H, bx + (bw - w) / 2, by + 122, name_scale, line, SWAP16(0xFFFF));

    /* THE NUMBER, not "SOME". Five messages used to be five pop-ups and five taps, which is
       indistinguishable from the panel repeating itself — and it is the count that tells a
       child whether one press is about to cost them ten seconds or a minute.
     *
       WHICH OF HOW MANY, once a finger can point at one of them. The line used to read SENT YOU 2
       whoever the face above it belonged to, which is fine while the oldest is the only thing a
       press can reach and actively misleading now: a child looking at the sister's face under
       "SENT YOU 2" has no way to tell that the 2 is not about her. `2/2` says both facts and puts
       them in the order they are read — this one, of that many. */
    char sub[28];
    if (count > 1) {
        snprintf(sub, sizeof(sub), "%d/%d", sel + 1, count);
    } else {
        snprintf(sub, sizeof(sub), "SENT YOU ONE");
    }
    w = font_text_w(sub, count > 1 ? 3 : 2);
    font_draw(fb, FACE_W, FACE_H, bx + (bw - w) / 2, by + 156, count > 1 ? 3 : 2, sub,
              SWAP16(0xFFFF));
    /* SWIPE, NOT ALL, AND THIS LINE IS FOR THE ADULT. "TAP FOR ALL" was accurate and still is —
       one press plays the chosen message and the run walks on through the rest — but a gesture
       nobody can see is a gesture nobody uses, and the tap is the part a four-year-old already
       knows. Flanked by dashes rather than arrowheads because the 5x7 font has no `<` or `>`, and
       a word they cannot read beats a glyph that renders as a blank cell. */
    const char *act = count > 1 ? "- SWIPE -" : "TAP TO HEAR";
    w = font_text_w(act, POPUP_SCALE);
    font_draw(fb, FACE_W, FACE_H, bx + (bw - w) / 2, by + 186, POPUP_SCALE, act,
              SWAP16(0x07FF));
}

/* THE BADGE THE POP-UP BECOMES. Top-left, small, and still the whole tap target it was —
 * shrinking the box must not shrink what a four-year-old has to hit, so the rectangle stays
 * generous around a small mark.
 *
 * A DOT AND A NAME, not a count. "3" is a number a four-year-old cannot act on; who it is from
 * is the thing they care about, and one glance at it is the whole content. */
/* A QUARTER OF THE GLASS, NOT A LOZENGE IN THE CORNER.
 *
 * This was a 44 px bubble at 14,14, sized so it would not cover the pet. The owner, watching
 * the twins: *"waiting messages are still showing up in the top left as a small blue
 * notification, they need to be like that 1/4 size."*
 *
 * TWO REASONS IT IS RIGHT. It is a TARGET, and the same aim that made the icon discs too small
 * makes a 44 px lozenge worse — this is the control a child presses to hear their sister, and
 * it was the smallest thing on the panel. And the badge is what is left AFTER the big pop-up
 * has given up (`POPUP_BIG_MS`), which is precisely when nobody has noticed yet, so shrinking
 * to a whisper at the moment of least attention had it backwards.
 *
 * The top-left QUADRANT, so it lines up with the grid the button opens and the halves the tick
 * and cross take: every target on this panel is now a quarter or a half, and none of them need
 * aiming at. Three quarters of the pet still show, which was the original objection and is
 * still respected. */
/* THE SENDER, TOP LEFT, AT THE ONE SIZE AND THE ONE PLACE — drawn by the notification badge,
 * by a message while it plays, and by the again-and-reply pair after it ends. Three callers and
 * one drawing, because this quadrant means exactly one thing on this screen ("who is this
 * about?") and a picture that moved or resized between those three states would read as three
 * different notices rather than one conversation.
 *
 * Sized FROM the quadrant rather than to a constant: the quadrant is also the hit area, and a
 * picture that did not fill what the finger may press is how a control and its target come to
 * disagree — which is the fault this file spent 0.3.24 removing. */
static void draw_sender_face(uint16_t *fb, int y0, int over_h, sendto_hit_t who)
{
    const int qh = (over_h - y0) / 2;
    const int qw = FACE_W / 2;
    sendto_draw_face(fb, FACE_W, FACE_H, qw / 2, y0 + qh / 2, (qw < qh ? qw : qh) / 3, who);
}

static void draw_popup_badge(uint16_t *fb, int y0, int over_h, const char *from,
                             sendto_hit_t who)
{
    /* THE FACE ALONE, ON THE BLACK. It was a blue rounded box carrying a pulsing dot and the
       sender's name, and the owner cut all three: *"when the notification is in the top left it
       shouldn't have the blue background. It should just have a dad face."*
       The box was solving a problem the face does not have. A name needed a panel to sit on to
       be legible; a face is already a shape against an AMOLED black, and the box only made it
       smaller — the one thing the child actually reads had been shrunk to make room for a label
       they cannot read. */
    (void)from;
    draw_sender_face(fb, y0, over_h, who);
}

/* PLAYING, AND THE SAME PAIR THE ENDED STATE CARRIES — see `confirm.h`. A lone centred STOP
 * disc used to sit here, and it answered the wrong question: a child listening to Dad wants to
 * hold it for a second, or answer him, far more often than they want it gone. Stopping is what
 * pausing already does to the sound; the queue simply waits rather than being thrown away.
 *
 * AND WHO IS TALKING, TOP LEFT, because the voice alone does not say it for these readers: the
 * owner asked for the badge's quadrant to keep carrying the sender *"when we're playing back a
 * message"* too. It is the same face in the same corner the notice used, so the picture that
 * said "Dad sent you one" is the picture that says "this is Dad" while it plays. */
static void draw_run(uint16_t *fb, int y0, int over_h, int count, int sel, sendto_hit_t who,
                     bool playing, bool reply, int progress)
{
    draw_sender_face(fb, y0, over_h, who);
    confirm_draw_transport(fb, FACE_W, FACE_H, over_h, playing, reply, progress);

    if (count > 1) {
        /* WHICH OF HOW MANY, and it keeps the slot the count already had rather than the one the
           old numeral used: that sat beside the left disc, which is now a button a finger goes
           to. Between the two discs it is on black, clear of both targets.

           IT USED TO BE "HOW MANY MORE", which answered the only question a child sitting through
           four messages had while the panel chose for them. A finger can point at one of them now,
           so a bare number would say nothing about WHICH — and the swipe would have no visible
           answer on the screen it is most likely to be used on.

           ONLY WITH SOMETHING TO CHOOSE BETWEEN. One message has no position to report, and `1/1`
           over a message that is playing is noise.

           Sized for the FORMAT, not the expected value: both are ints, so eleven digits and a sign
           must fit either side or the compiler is right to refuse it. */
        char n[26];
        snprintf(n, sizeof(n), "%d/%d", sel + 1, count);
        const int tw = font_text_w(n, 3);
        font_draw(fb, FACE_W, FACE_H, FACE_W / 2 - tw / 2, confirm_cy(over_h) - (FONT_H * 3) / 2,
                  3, n, SWAP16(0xFFFF));
    }
}

static sendto_hit_t who_face(const jp_who_t *w)
{
    return w->from_dad ? SENDTO_DAD : SENDTO_SISTER;
}

void overlay_draw(uint16_t *fb, const sm_view_t *view, const overlay_ctx_t *c)
{
    /* EVERYTHING BELOW IS `sm_view_t`, AND ONLY THAT: what is drawn is what the machine's
       targets are (I3), and the face top left is `sm_face` (I2). */
    const sendto_hit_t who = view->face_none ? SENDTO_SISTER : who_face(&view->face);
    if (view->mode == SM_LISTEN) draw_listening(fb, c->over_y0, c->over_h, c->now);
    if (view->mode == SM_RECORD) {
        draw_recording(fb, c->over_y0, c->over_h, c->now, view->rec_to, c->sibling);
    }
    if (view->confirm) {
        confirm_draw(fb, FACE_W, FACE_H, c->over_h);
    } else if (view->think) {
        draw_thinking(fb, c->over_y0, c->over_h - c->over_y0, c->now, view->dash);
    }
    if (view->notice) {
        /* Big for the first fifteen seconds of a wait, then the badge, which is the same
           quadrant meaning the same thing: who is waiting. */
        if (view->notice_big) {
            draw_popup(fb, c->over_y0, c->over_h, view->face.from, view->sel_m, view->sel_n - 1,
                       who);
        } else {
            draw_popup_badge(fb, c->over_y0, c->over_h, view->face.from, who);
        }
    }
    if (view->run_bar) {
        draw_run(fb, c->over_y0, c->over_h, view->sel_m, view->sel_n - 1, who,
                 view->playing_glyph, view->reply, view->progress);
    } else if (view->pair) {
        /* THE FACE STAYS FOR THE WHOLE EXCHANGE: AGAIN and REPLY are about the person who
           just spoke, so their face is the answer to "reply to whom?". */
        draw_sender_face(fb, c->over_y0, c->over_h, who);
        confirm_draw_transport(fb, FACE_W, FACE_H, c->over_h,
                               view->transport && view->playing_glyph, view->reply,
                               view->transport ? view->progress : -1);
    }
    if (view->exit_corner) confirm_draw_exit(fb, FACE_W, FACE_H, c->over_y0, c->over_h);
    /* PLACEHOLDERS (F4): a send in flight, then its outcome, top right. */
    if (view->sending && !view->confirm && !view->think) {
        draw_indicator(fb, c->over_y0, c->now, SWAP16(0x001F), "SENDING");
    } else if (c->result_word != NULL && !view->confirm && !view->think) {
        draw_indicator(fb, c->over_y0, c->now, c->result_colour, c->result_word);
    }
    /* THE GRID LAST OF ALL: the one overlay a child asked for by pressing. */
    if (view->grid) sendto_draw(fb, FACE_W, FACE_H, c->over_h);
}

void overlay_result(cue_t c, bool nobody, const char **word, uint16_t *colour)
{
    if (c == CUE_SENT) {
        *word = "SENT";
        *colour = SWAP16(0x07E0);
    } else if (nobody) {
        *word = "NOBODY";
        *colour = SWAP16(0xFD20);
    } else if (c == CUE_STOP) {
        *word = "DROPPED";
        *colour = SWAP16(0x8410);
    } else {
        *word = "NOT SENT";
        *colour = SWAP16(0xF800);
    }
}
