#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "face.h"

/* THE TWO WAYS OUT OF A RECORDING, AS SOMETHING A FOUR-YEAR-OLD CAN AIM AT.
 *
 * Until now a recording had three silent exits and one gesture: going quiet sent it, saying
 * nothing dropped it, the cap sent what there was — and a touch ANYWHERE cancelled. That
 * gesture was right when it was the only one available ("a finger is the one input that is
 * always available and never ambiguous"), and wrong the moment there is somewhere deliberate
 * to press: a stray palm, or a finger aimed at the pet during a message, should not be able
 * to throw away what a child just said.
 *
 * The owner: *"I want icons that are green check and red x that are kind of large in the
 * bottom third ... Green check will finish when pressed and send, red x cancel."*
 *
 * GOING QUIET STILL SENDS. The check is "I am done, do not wait it out", not the only way
 * through — a voice assistant that needs a button press every time is not hands-free, and the
 * reader here is four and did not ask to press anything.
 *
 * ANCHORED TO THE OVERLAY BAND, NOT THE FRAME, which is the same rule the caption and the
 * label were moved to obey. `over_h` is the frame on a portrait panel and the SQUARE on a
 * side-mounted one, so measuring up from its bottom puts these in the bottom third either way
 * rather than off the edge of a panel that has been turned a quarter. */

/* The drawn disc, and the target around it. The target is deliberately the larger of the two:
   the pop-up's comment already paid for this lesson — "a four-year-old aiming at a small
   target with an excited finger is a miss". */
#define CONFIRM_R 56
#define CONFIRM_HIT_R 82
/* Up from the bottom of the band. */
#define CONFIRM_MARGIN 24
/* Far enough apart that the two targets cannot overlap: 184 px between centres against a
   82 px reach leaves a 20 px dead band, so a finger that lands between them does NOTHING
   rather than picking whichever circle happened to win. Cancel left, send right — the
   convention every phone call in the house already uses. */
#define CONFIRM_CX_CANCEL 92
#define CONFIRM_CX_SEND (FACE_W - CONFIRM_CX_CANCEL)

typedef enum {
    CONFIRM_NONE = 0, /* not on either target: consumed, and nothing happens */
    CONFIRM_SEND,
    CONFIRM_CANCEL,
} confirm_hit_t;

/* The centre line of both icons, measured up from the bottom of the overlay band. */
int confirm_cy(int over_h);

/* Which target a finger landed on, in FACE coordinates. `CONFIRM_NONE` for everything else,
   including the gap between them and the whole of the rest of the glass. */
confirm_hit_t confirm_hit(int fx, int fy, int over_h);

/* RGB565 BYTE-SWAPPED FOR THE PANEL'S BUS, the same convention `display.c` writes in — these
   go straight into the framebuffer, so they have to already be in its order. */
#define CONFIRM_SWAP(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))
#define CONFIRM_RED CONFIRM_SWAP(0xF800)
#define CONFIRM_GREEN CONFIRM_SWAP(0x07E0)
#define CONFIRM_GLYPH 0xFFFF /* white, and a palindrome either way round */
/* Thick enough to read across a room at a glance, thin enough that the glyph is a symbol
   rather than a blob filling its disc. */
#define CONFIRM_STROKE 11

/* Draw both icons into a framebuffer. Pure: it takes the buffer and its size and touches
   nothing else, which is what lets the host suite RENDER this rather than only measure it —
   the drawing is the half of this feature a hit test cannot check. */
void confirm_draw(uint16_t *fb, int w, int h, int over_h);

/* --- THE SINGLE-CONTROL ICONS, same size and the same place ---------------------------------
 *
 * The owner, after the tick and the cross were working: *"when panel is playing back a message
 * from my pwa, show a big stop icon similar to the x when recording. And when we have the again
 * have it the same size icon but with like a repeat and big like that."*
 *
 * Both replace text controls that were sized for an adult reading them: a
 * `"N MORE  TAP TO STOP"` bar and an `"AGAIN"` word box. `draw_repeat`'s comment explained why
 * it was a WORD — *"a hand-plotted circular arrow at this size reads as a smudge"* — and that
 * objection was entirely about SIZE. At the tick's size an arrow is a symbol again, and the
 * readers are four and pre-literate, so a glyph beats a word they cannot read.
 *
 * CENTRED, because unlike the tick and the cross these are ALONE. Two targets need a dead band
 * between them; one target should be where the thumb already is. */
#define CONFIRM_CX_CENTRE (FACE_W / 2)
/* Blue, the colour the AGAIN box already used, so the one control that survives from the old
   design keeps the colour a child had learned. */
#define CONFIRM_BLUE CONFIRM_SWAP(0x001F)

/* A red disc with a white square: stop what is playing. */
void confirm_draw_stop(uint16_t *fb, int w, int h, int over_h);

/* A blue disc with a circular arrow: play it again. */
void confirm_draw_repeat(uint16_t *fb, int w, int h, int over_h);

/* Whether a finger landed on a centred single control. Same generous reach as the pair. */
bool confirm_hit_centre(int fx, int fy, int over_h);

/* --- REPLAY AND REPLY, THE PAIR THAT REPLACES A LONE REPEAT ICON ----------------------------
 *
 * The owner, watching the twins use it: *"on listening to a sister or a dad message that comes
 * in where it has the replay button. I think maybe we need a replay and a reply button. Replay
 * can keep the same but move it to the bottom left and then make the bottom right a [mail]
 * envelope icon to send a reply. This way when sisters are sending messages back and forth they
 * can just hit the reply button."*
 *
 * THE POINT IS THE ROUND TRIP. Answering a message meant opening the menu and choosing the
 * person who had just spoken — a recipient the panel already knew. The reply button removes a
 * choice nobody needed to make, and it is the difference between a message and a conversation.
 *
 * Bottom left and bottom right, hit as the same halves `confirm_hit` uses, because the aim
 * that made the discs too small has not changed. The left disc keeps the blue it already had.
 *
 * AND IT IS THE SAME CONTROL WHILE A MESSAGE IS STILL PLAYING, which is what `playing` picks.
 * The run used to carry one centred STOP disc, and the owner replaced it: *"when playing the
 * message all we have is a stop button. I think we need a play pause button... and on the
 * bottom right have the reply button... it looks very similar to the ending state where we have
 * the play and reply button, but instead of the play button we have a pause button."*
 *
 * SO THE PAIR NEVER MOVES BETWEEN THE TWO STATES — same discs, same places, same colours, and
 * only the left glyph changes. A child who has learned where "again" lives has learned where
 * "hold on" lives, and the icon is the single thing that has to be read. `playing` true draws
 * the pause bars (press to hold), false the play triangle (press to start or resume), so the
 * button always shows what the press will DO rather than what the panel is doing. */
#define CONFIRM_ENVELOPE CONFIRM_SWAP(0x07E0)

void confirm_draw_transport(uint16_t *fb, int w, int h, int over_h, bool playing);

/* THE WAY OUT, DRAWN. The corner worked before this existed and that was the problem: the
 * owner had to be told where to press, which means no child would ever have found it. *"Make
 * the top right an exit button to take up that top right corner so that there's a clear
 * indication of where we should click to make it go away."*
 *
 * Mirrors the sender's face across the top band — same size, same inset, opposite corner — so
 * the two read as a pair: who this is from, and how to be done with it. Red, because it is the
 * only destructive control on this screen and red is what the cross already means here. */
void confirm_draw_exit(uint16_t *fb, int w, int h, int y0, int over_h);

/* --- THE "WHO?" GRID, AND WHY IT EXISTS -----------------------------------------------------
 *
 * MEASURED 2026-09-27, on 0.3.14, with all 48 phrases registered with hand-checked phonemes
 * and none refused: `tell dad` fired nine times out of nine, and `tell sister` did not fire
 * ONCE across an afternoon of two children trying it. The phoneme conversion — the thing that
 * was supposed to fix it — did not. The transcripts caught them working around it: the message
 * to Dad that says *"Tell Dad. It's doing it. So now you say your message."*
 *
 * So the recipient stops being something a four-year-old has to pronounce correctly. The owner:
 * *"When we press the button it should bring up a menu for icons, a 2x2 grid similar to the
 * check and cancel buttons ... top left and top right will be an icon for a sister and an icon
 * for a dad and the bottom left will be a cancel to exit out of the menu."*
 *
 * SAME SIZE, SAME PLACE, SAME SPACE as the tick and the cross, and that is not laziness: those
 * two are the only targets on this panel a child has been observed hitting reliably, their
 * reach was tuned against a real fingertip, and they are drawn BEFORE `flip_frame` and hit
 * tested through `tap_to_overlay` — the pairing that took a release to get right (§10.4bw).
 * A second geometry would be a second chance to get that wrong.
 *
 * THE BOTTOM RIGHT IS THE PET. It was left empty when three targets were asked for, and the
 * owner filled it deliberately: *"that blank spot should have one icon that is also the robot
 * that would tie into the one to have a conversation with the llm."* So the grid now covers
 * every way this panel can be spoken into — her sister, her dad, and the pet — and the wake
 * phrase stops being the only door to the last of them.
 *
 * THE ROWS ARE A FULL DEAD BAND APART, the same 184 px that separates the columns, so a finger
 * landing between rows does NOTHING rather than picking whichever circle happened to win —
 * the rule the pair already follows, applied to the axis it just gained. */
typedef enum {
    SENDTO_NONE = 0, /* the gap between targets and the rest of the glass */
    SENDTO_SISTER,
    SENDTO_DAD,
    SENDTO_CANCEL,
    SENDTO_PET, /* talk to the pet itself — the same turn the wake phrase starts */
} sendto_hit_t;

/* 184 px between centres against an 82 px reach leaves a 20 px dead band — see the columns. */
#define SENDTO_ROW_GAP 184

/* The bottom row shares the tick and cross's centre line, so the control a child has already
   learned to aim at has not moved. The top row is one dead band above it. */
int sendto_cy_bottom(int over_h);
int sendto_cy_top(int over_h);

/* Which target a finger landed on, in the same coordinates `confirm_hit` takes. */
sendto_hit_t sendto_hit(int fx, int fy, int over_h);

/* Draw the grid. Pure, like `confirm_draw`, so the host suite can render it.
 *
 * FACES, NOT FIGURES. The first pass drew a small stick person and a big one, on the theory
 * that size is the one cue a four-year-old needs no teaching for. Rendered, it was two blobs
 * with bars through them, and the owner said what would actually work: *"an actual man face
 * with a beard and then a little girl with long hair."* A beard and long hair are the features
 * these particular children would name if you asked them who someone was, which is a better
 * test than any theory about silhouettes.
 *
 * Neither disc is red or green: those two already mean cancel and send on this glass, and a
 * recipient that looked like a verb would undo the only colour vocabulary these children have. */
void sendto_draw(uint16_t *fb, int w, int h, int over_h);

/* ONE OF THOSE FACES, ANYWHERE, AT ANY SIZE — so the picture of a person means the same thing
 * wherever it appears. The owner asked for the faces to follow the person around: on the
 * indicator while a child is recording (*"a 1/4 size face of who they're talking to"*), and on
 * the waiting badge (*"new message with the picture of the icon and who it's from"*).
 *
 * A NAME IS NOT ENOUGH FOR THE READERS HERE. They are four and pre-literate, so "TO DAD" is a
 * shape they have memorised rather than a word they can read — and the panel already has a
 * picture of Dad that they picked out of a menu themselves. Using it everywhere costs nothing
 * and makes every one of these surfaces legible to someone who cannot read at all.
 *
 * `who` picks the face; `SENDTO_NONE` and `SENDTO_CANCEL` draw nothing. `r` is the disc radius,
 * so the caller sizes it — a quarter of the glass on the indicator, smaller on the badge. */
void sendto_draw_face(uint16_t *fb, int w, int h, int cx, int cy, int r, sendto_hit_t who);

#define SENDTO_SISTER_COLOUR CONFIRM_SWAP(0xF81F) /* magenta */
#define SENDTO_DAD_COLOUR CONFIRM_SWAP(0x07FF)    /* cyan */
/* A light face against dark hair, because contrast is what survives being glanced at across a
   bedroom on a 112 px disc. */
#define SENDTO_SKIN CONFIRM_SWAP(0xF6D6)
#define SENDTO_HAIR CONFIRM_SWAP(0x2124)
/* THE GIRLS ARE BLONDE WITH BLUE EYES, which is not decoration: these two icons are pictures of
   the two people in the house a four-year-old sends messages to, and a picture that is not of
   them is a picture of somebody else. */
#define SENDTO_BLONDE CONFIRM_SWAP(0xFEC0)
#define SENDTO_BLUE_EYE CONFIRM_SWAP(0x039F)
/* The pet's own disc. Amber, because every other colour on this glass is spoken for: red
   cancels, green sends, blue repeats, and the two people are magenta and cyan. */
#define SENDTO_PET_COLOUR CONFIRM_SWAP(0xFD20)
/* A ROBOT FACE, NOT THE OSTRICH. The panel can wear either body, and the ostrich is its
   default — but the owner asked for the robot here, and it is the better icon for the job: this
   target means "talk to the thing that answers", and a robot says machine-that-listens in a way
   a bird does not. Silver head, lit eyes. */
#define SENDTO_ROBOT CONFIRM_SWAP(0xC618)
#define SENDTO_ROBOT_EYE CONFIRM_SWAP(0x07FF)
