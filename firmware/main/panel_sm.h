#pragma once

/* The panel's one state machine (plan §8). Replaces `ui.c` and every piece of interaction state
 * `display.c` used to keep. `sm_step` runs once per frame on the render task, on the device and in
 * the host tests alike; `display.c` only samples inputs, executes the effects in order, and draws
 * `sm_view_t`. `make check_no_logic_in_display` holds it to that.
 *
 * TWO RULES ABOUT THE FRAME BOUNDARY (§8.2), because `in` is sampled before the step and the
 * effects run after it:
 *   1. nothing the machine entered this frame is judged by this frame's inputs;
 *   2. a press that changes the mode graces the new mode's EXIT, FACE, NOTICE, TICK, CROSS, the
 *      pair's AGAIN/REPLY and (near the hold point) grid cells for EXIT_GRACE_MS.
 *
 * Plain C, no ESP-IDF. */

#include <stdbool.h>
#include <stdint.h>

#include "cue.h"
#include "jpanel_types.h"
#include "stream.h"

/* ── TIMING, moved from `ui.h` with the reasons each number is what it is ─────────────────── */

/* PRESS AND HOLD. `gesture.h` records that 4-5 year olds produce ordinary presses lasting up to
 * 4.2 s, so the maintenance gestures stopped being a bare hold. Opening the "who?" menu cannot
 * wait that long, so it fires at 700 ms, comfortably past the 600 ms that still counts as a tap,
 * and accepts that ordinary play will sometimes open a menu. A hold that begins while a tap run
 * is live belongs to the maintenance gestures. */
#define HOLD_TALK_MS 700
/* AND IT HAS TO LAND ON THE PET. A hand carrying a 32 mm panel touches its RIM; a press meant for
 * the pet lands in the middle. So the hold has to begin inside a rectangle inset ~6 mm on every
 * side (224x304 that a four-year-old cannot miss), in PANEL coordinates on purpose: the rim is
 * the rim whichever way up the thing is mounted. The owner found the red dot already lit after
 * picking a unit up to photograph it, which is what this margin stops. */
#define TALK_MARGIN_PX 72
/* A safety net, not a target. The first warm turn ever measured took 12,777 ms and a 12 s timeout
 * threw a working answer away 777 ms before it landed; whisper alone is a flat ~10.7 s. Beyond it
 * the panel says it failed rather than returning to idle, because "it didn't hear you" and "it
 * broke" must not look the same. */
#define TALK_TIMEOUT_MS 60000
#define TALK_FAILED_MS 2500

/* HANDS-FREE, AND THE WHOLE PROBLEM IS KNOWING WHEN THEY STOPPED. A name has no release, so the
 * end of the sentence is read off the front end's VAD (`speech_hearing`).
 *   HUSH  they finished -> send it. 1.8 s, up from 900 ms: *"the babies keep getting cut off
 *         because they're a little bit slow."* A four-year-old assembling a sentence pauses for
 *         longer than an adult, and the box trims the silence before whisper sees it.
 *   LEAD  they said the name and nothing else -> drop it, silently. An accidental "hey fish"
 *         from the television must not become an upload.
 *   the cap (`CAPTURE_MAX_MS`) -> send what there is rather than truncating to nothing. */
#define LISTEN_HUSH_MS 1800
#define LISTEN_LEAD_MS 3000

/* AND THEN IT LISTENS AGAIN, WITHOUT BEING ASKED: *"after the text-to-speech comes back and
 * finishes talking, we should just turn the microphone on ... That way I can have fluid
 * conversations."* No beep on this one — a tone after every reply is the toy interrupting the
 * conversation it started. Capped, because this is a loop with a loudspeaker in it: a television
 * could otherwise hold a conversation with the panel all night. A deliberate start resets it. */
#define FOLLOW_LEAD_MS 2000
#define FOLLOW_MAX_TURNS 6

/* A message to a sister is a sentence being composed out loud, not one already formed, so it gets
 * a longer lead than a question to the pet. */
#define RECORD_LEAD_MS 4000

/* THE AGAIN/REPLY PAIR NEVER EXPIRES (0.3.25: *"no more waiting for it to time out"*); it leaves
 * on the exit, on REPLY, on a child-chosen stop or on standby, and hides while a message waits.
 * This is when it STANDS: after ten seconds (*"the replay button probably needs to stay on there
 * for about 10 seconds"*) AGAIN and REPLY are hit only by their drawn discs, so a hold on the rest
 * of the pet opens the menu again (owner, 2026-10-11). */
#define REPEAT_MS 10000

/* THE NOTICE SHRINKS RATHER THAN NAGS: *"if it's not acknowledged within say 15 seconds, it
 * should kind of be a smaller one up on the top left."* Counted from the first frame the notice
 * is actually drawn, so a wait that began behind a conversation still gets its fifteen seconds. */
#define POPUP_BIG_MS 15000

/* A DESTRUCTIVE CONTROL MUST NOT ARM ITSELF UNDER A FINGER THAT IS ALREADY THERE. The notice and
 * the exit overlap over a quarter of the notice, and `touch.c` reports every down edge with no
 * debounce — a fingertip that lightens for one 15 ms sample is two edges: *"it just shows the
 * screen with the pause icon for half a second and then goes back."* 700 ms is HOLD_TALK_MS on
 * purpose: this panel's measure of "long enough to be meant rather than spilled". The same grace
 * covers every target a mode-changing press puts under the finger (§8.2). PLAY's PAUSE and REPLY
 * are exempt: they are ignored until the message is audible, and a clock on them was tried and
 * rejected (0.3.36: 700 ms of dead pause button is a real cost). */
#define EXIT_GRACE_MS 700
/* Grid cells are graced only within this of the hold's own point — the four-cell junction where a
 * lightening finger lands — so a deliberate tap on a face elsewhere acts at once. */
#define SM_HOLD_GRACE_PX 40

/* THE BIG NOTICE'S BOX, 296x214 and CENTRED, and the one answer to where the notice is: its
 * picture and its target used to be different rectangles, so most of what a child could SEE was
 * not pressable. The badge's target is the whole top-left quadrant: *"capture everything in that
 * top left quadrant as far as clicks to play it."* */
#define SM_NOTICE_BIG_W 296
#define SM_NOTICE_BIG_H 214

/* 60 px is 4.7 mm on this glass — far enough that no jab crosses it, close enough to be one motion
 * of a small hand — and more horizontal than vertical. A swipe CLAMPS: three messages and a wrap
 * means three swipes right land back where you started, which reads as the panel ignoring you. */
#define SWIPE_MIN_PX 60

/* A menu a child walked away from must not sit on the pet's face forever, and must not be so brief
 * that opening it and then deciding is a race. */
#define SENDTO_MS 10000

/* HOW LONG A PRESS WAITS FOR THE SPEAKER. The play is posted once nothing sounds, so the press's
 * own cue is never cut by the message it announced. Derived from the worst case of what can hold
 * the speaker ahead of it — a pet reply, then one cue in every slot — plus a second; past it the
 * press ends audibly (E2.10), never silently. */
#define PENDING_MS (PLAY_BUF_MS + STREAM_SLOT_N * CUE_MAX_MS + 1000)

#define SM_SEEN_MAX 16
#define SM_FX_MAX 24

typedef enum { SM_PET = 0, SM_GRID, SM_LISTEN, SM_RECORD, SM_PLAY } sm_mode_t; /* THINK is derived */
typedef enum { SM_TURN_NONE = 0, SM_TURN_THINKING, SM_TURN_FAILED } sm_turn_t;
typedef enum { SM_NET_IDLE = 0, SM_NET_BUSY, SM_NET_SPOKE, SM_NET_FAILED } sm_net_t; /* talk.h's */
/* The voice commands the machine owns; actions, forms and colours stay pass-through in display.c. */
typedef enum { SM_VOICE_NONE = 0, SM_VOICE_WAKE, SM_VOICE_STOP, SM_VOICE_SEND_PANEL, SM_VOICE_SEND_DAD } sm_voice_t;
/* Why a red dash was drawn: the request failed (talk.c has the reason), or the panel gave up
   waiting and nothing else recorded it. */
typedef enum { SM_DASH_NONE = 0, SM_DASH_NET, SM_DASH_TIMEOUT } sm_dash_t;

typedef enum {
    SM_T_NONE = 0,
    SM_T_IGNORED, /* claimed with no effect: a grace, the transport before ON, FACE in a replay */
    SM_T_PET,
    SM_T_NOTICE,
    SM_T_EXIT,
    SM_T_AGAIN,
    SM_T_REPLY,
    SM_T_FACE,
    SM_T_PAUSE,
    SM_T_TICK,
    SM_T_CROSS,
    SM_T_CELL_SISTER,
    SM_T_CELL_DAD,
    SM_T_CELL_CANCEL,
    SM_T_CELL_PET,
    SM_T_SWALLOW,
} sm_target_t;

typedef struct {
    uint32_t now;
    jp_view_t snap;        /* this frame's copy (render keeps the last good one) */
    uint32_t post_failed;  /* the seq the executor could not post; JP_SEQ_NONE otherwise */
    bool capture_refused;  /* the executor's capture_open was refused last frame */
    stream_facts_t audio;
    /* Touch, as touch.c reports it (no debounce). `down`/`up` are edges; `x`,`y` are OVERLAY
       coordinates of the finger, valid on the down edge and on every frame it stays down (the
       hold's point is where it is when the hold fires). `on_pet` is sampled at the down edge, in
       panel coordinates, against TALK_MARGIN_PX. */
    bool down, up;
    int x, y;
    bool on_pet;
    int gest_taps;
    int swipe; /* -1, 0, +1 on the frame a swipe is recognised */
    int over_y0, over_h; /* the overlay band for this orientation */
    sm_voice_t voice;
    bool btn_short, btn_hold;
    bool wake;      /* display's sleep stage woke the screen from standby (a touch, an arrival) */
    bool hearing;   /* speech_hearing(): the only evidence she spoke */
    int capture_ms; /* valid only for a capture opened in an earlier frame */
    bool leased;
    sm_net_t net;
    bool speech_ok; /* the recogniser started; a deaf panel cannot send (§6a) */
    /* Calibration owns the frame. The machine handles entry on the first frame this is true
       (and is a no-op on any later one), so display.c may stop stepping it until it ends. */
    bool calibrating;
    bool dark_wake; /* the screen woke from dark this frame */
    bool sound;     /* audio_start succeeded */
} sm_in_t;

typedef enum {
    SM_FX_SILENCE = 1,   /* stream_silence */
    SM_FX_CUE,           /* arg = cue_t, slot = stream_slot_t */
    SM_FX_STOP,          /* stream_stop; arg = heard */
    SM_FX_PAUSE,         /* arg = on, slot = the stream_id it is for */
    SM_FX_CAPTURE_OPEN,
    SM_FX_CAPTURE_DROP,
    SM_FX_POST,          /* post `out.post`; for SEND the executor takes lease 1 and fills pcm */
    SM_FX_SEND_TALK,     /* lease 2, then talk_send */
    SM_FX_TALK_CLEAR,
    SM_FX_POLL_SOON,
    SM_FX_STANDBY,       /* arg = on */
    SM_FX_BLANK,
    SM_FX_DEEP_SLEEP,
    SM_FX_NOD,
    SM_FX_FLINCH,
    SM_FX_MUTE_COMMANDS, /* arg = muted; every frame, derived, so no exit can leave it stuck */
} sm_fx_kind_t;

typedef struct {
    sm_fx_kind_t kind;
    int arg;
    int slot;
} sm_fx_t;

/* What display.c draws. Nothing here is decided by display.c. */
typedef struct {
    sm_mode_t mode;
    bool think, dash;
    bool notice, notice_big;
    bool offer, offer_standing; /* the again/reply pair in PET */
    bool pair;                  /* the pair layout: the offer, or a replay once it is audible */
    bool exit_corner;
    bool face_none;
    jp_who_t face;        /* sm_face: the top-left face, and who REPLY addresses (I2) */
    int sel_n, sel_m;     /* the n/m numeral */
    bool run_bar, transport, paused, playing_glyph;
    /* How much of the message playing has been heard, 0..1000, for the ring round the play
       button; -1 draws no ring (nothing playing, or a box that does not say how long it is). */
    int progress;
    /* REPLY is drawn and pressable: always on the again/reply pair, and in PLAY only while the
       message is paused (0.4.2, owner: a reply starts from a message that has stopped talking,
       paused or finished, never from one still talking). */
    bool reply;
    bool sending;
    bool grid;
    bool confirm;         /* the tick and the cross */
    bool listen_dot;      /* the dot: the microphone is open */
    jp_to_t rec_to;
} sm_view_t;

typedef struct {
    sm_fx_t fx[SM_FX_MAX]; /* in order; the executor runs them in this order */
    int n;
    jp_cmd_t post;         /* valid when an SM_FX_POST is in `fx` */
    sm_view_t view;
    /* for display.c's pass-through and its sleep stages */
    bool pet_press;        /* a press reached the pet: display's poke (a flinch while it speaks) */
    bool arrived;          /* an unseen message id: the sleep `arrived` wake */
    const char *say;       /* a caption line, NULL when there is nothing to say */
    /* telemetry */
    bool tap_missed;
    bool rim_hold;         /* a hold that began on the rim, once per press */
    bool backstop;         /* a UI backstop fired (§7.5c) */
    bool late_outcome;     /* an outcome arrived after its backstop: logged, never sounded */
    sm_dash_t dash_why;
    uint32_t violations;   /* invariants broken this step (counted; only I1/I5/I6 recover) */
} sm_out_t;

/* The invariants (§8.2), as `sm_check` and `out.violations` report them. */
#define SM_I1 (1u << 0) /* one mode; the committed capture is open iff LISTEN or RECORD */
#define SM_I2 (1u << 1) /* the drawn face is sm_face, and RECORD addresses it */
#define SM_I3 (1u << 2) /* the drawn controls are exactly the mode's targets */
#define SM_I4 (1u << 3) /* every press is answered, or ignored and logged */
#define SM_I5 (1u << 4) /* a pause belongs to PLAY and to the stream that is current */
#define SM_I6 (1u << 5) /* every mode but PET is inside its deadline */
#define SM_I7 (1u << 6) /* times and seqs compare wrap-safely (by construction; never set) */
#define SM_RECOVERS (SM_I1 | SM_I5 | SM_I6)

/* The machine's state (plan §8.2). In the header for static allocation and for the host tests'
   assertions; only panel_sm.c writes it. */
typedef struct {
    sm_mode_t mode;
    uint32_t since;
    uint32_t mode_press_at; /* the press (or hold) that last changed the mode; 0 when none */
    int hold_x, hold_y;
    bool standby;
    bool cal;               /* calibration was entered and has not yet been left */
    bool capture;           /* the capture the machine committed to (I1) */
    uint32_t next_seq;
    struct {
        uint32_t seq; /* JP_SEQ_NONE until posted */
        jp_who_t target;
        bool replay;
        uint32_t since; /* the press */
        uint32_t expires_at;
        int run_pos;
        char run_id[JP_ID_MAX]; /* the run's current message, for advancing run_pos */
        uint8_t run_sid;
        bool run_began;
        uint8_t pause_stream;
        bool pause_on;
        uint32_t pause_since;
    } play;
    uint32_t stopped_seq;   /* a stopped, posted PLAY whose outcome is pending */
    uint32_t stopped_expires;
    jp_who_t stopped_who;   /* what the notice shows while that outcome is pending */
    uint32_t late_play, late_send; /* seqs whose backstop fired */
    struct {
        bool up;
        jp_who_t who;
        uint32_t since;
    } offer;
    struct {
        jp_to_t to;
        jp_who_t who;
        bool heard, deliberate;
        uint32_t hush, opened_at;
    } rec;
    struct {
        bool voice, heard, follow_armed, follow;
        int follow_turns;
        uint32_t hush, lead, opened_at;
    } talk;
    struct {
        sm_turn_t state;
        uint32_t since;
    } turn;
    uint32_t dashes, dash_at;
    bool reply_was;
    struct {
        uint32_t seq, posted_at, expires_at;
    } send;
    struct {
        uint32_t first_visible; /* 0 until the notice is first drawn in this wait */
        bool big;
    } notice;
    uint32_t grid_until;
    struct {
        char id[JP_ID_MAX];
        int index;
    } sel;
    char chime_ids[JPANEL_QUEUE_MAX][JP_ID_MAX];
    bool chime_owed; /* the no-ids fallback */
    char seen_ids[SM_SEEN_MAX][JP_ID_MAX];
    int seen_n;
    struct {
        bool active; /* the finger is on the glass */
        bool on_pet, swiped, held;
        uint32_t at;
        int x, y;
        sm_target_t target;
    } down;
    jp_view_t prev; /* the previous snapshot, for edges */
} sm_t;

void sm_init(sm_t *sm, uint32_t now);
void sm_step(sm_t *sm, const sm_in_t *in, sm_out_t *out);

/* Pure queries, for drawing and the tests. */
sm_target_t sm_target(const sm_t *sm, const sm_in_t *in, int x, int y);
bool sm_face(const sm_t *sm, const jp_view_t *snap, jp_who_t *out); /* false = none */
bool sm_busy(const sm_t *sm, const jp_view_t *snap);                /* the sleep `used` term */
/* I1–I7 against the committed state; returns a bitmask of violated invariants (host asserts 0). */
uint32_t sm_check(const sm_t *sm, const sm_in_t *in);
/* Where the notice is, big or badge: its picture and its target are this one rectangle. */
void sm_notice_box(int y0, int over_h, bool big, int box[4]);
const char *sm_dash_name(sm_dash_t why);
