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
 * Plain C, no ESP-IDF. Timing constants (EXIT_GRACE_MS, REPEAT_MS, HOLD_TALK_MS, …) move here
 * from `ui.h`, with their comments, when `ui.h` is deleted. */

#include <stdbool.h>
#include <stdint.h>

#include "cue.h"
#include "jpanel_types.h"
#include "stream.h"

#define SM_SEEN_MAX 16
#define SM_FX_MAX 24

typedef enum { SM_PET = 0, SM_GRID, SM_LISTEN, SM_RECORD, SM_PLAY } sm_mode_t; /* THINK is derived */
typedef enum { SM_TURN_NONE = 0, SM_TURN_THINKING, SM_TURN_FAILED } sm_turn_t;
typedef enum { SM_NET_IDLE = 0, SM_NET_BUSY, SM_NET_SPOKE, SM_NET_FAILED } sm_net_t; /* talk.h's */
/* The voice commands the machine owns; actions, forms and colours stay pass-through in display.c. */
typedef enum { SM_VOICE_NONE = 0, SM_VOICE_WAKE, SM_VOICE_STOP, SM_VOICE_SEND_PANEL, SM_VOICE_SEND_DAD } sm_voice_t;

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
    /* touch: edges with points, as touch.c reports them (no debounce) */
    bool down, up;
    int x, y;
    bool on_pet;
    int gest_taps;
    int swipe; /* -1, 0, +1 on the frame a swipe is recognised */
    sm_voice_t voice;
    bool btn_short, btn_hold;
    bool hearing;   /* speech_hearing(): the only evidence she spoke */
    int capture_ms; /* valid only for a capture opened in an earlier frame */
    bool leased;
    sm_net_t net;
    bool speech_ok; /* the recogniser started; a deaf panel cannot send (§6a) */
    bool calibrating;
    bool dark_wake; /* the screen woke from dark this frame */
    bool sound;     /* audio_start succeeded */
} sm_in_t;

typedef enum {
    SM_FX_SILENCE = 1,   /* stream_silence */
    SM_FX_CUE,           /* arg = cue_t, slot = stream_slot_t */
    SM_FX_STOP,          /* stream_stop; arg = heard */
    SM_FX_PAUSE,         /* arg = on, for snap.cur.stream_id */
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
    SM_FX_MUTE_COMMANDS, /* arg = muted */
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
    bool offer, offer_standing;
    bool face_none;
    jp_who_t face;        /* sm_face: the top-left face, and who REPLY addresses (I2) */
    int sel_n, sel_m;     /* the n/m numeral */
    bool run_bar, transport, paused, playing_glyph;
    bool sending;
    bool grid;
    bool listen_dot;      /* the red dot: the microphone is open */
    jp_to_t rec_to;
} sm_view_t;

typedef struct {
    sm_fx_t fx[SM_FX_MAX]; /* in order; the executor runs them in this order */
    int n;
    jp_cmd_t post;         /* valid when an SM_FX_POST is in `fx` */
    sm_view_t view;
    /* telemetry */
    bool tap_missed;
    int dash_why;
    uint32_t violations;   /* invariants broken this step (counted; only I1/I5/I6 recover) */
} sm_out_t;

/* The machine's state (plan §8.2). In the header for static allocation and for the host tests'
   assertions; only panel_sm.c writes it. */
typedef struct {
    sm_mode_t mode;
    uint32_t since;
    uint32_t mode_press_at; /* the press (or hold) that last changed the mode; 0 when none */
    int hold_x, hold_y;
    struct {
        uint32_t seq; /* JP_SEQ_NONE until posted */
        jp_who_t target;
        bool replay;
        uint32_t since; /* the press */
        int run_pos;
        uint8_t pause_stream;
        bool pause_on;
        uint32_t pause_since;
    } play;
    uint32_t stopped_seq;   /* a stopped, posted PLAY whose outcome is pending */
    struct {
        bool up;
        jp_who_t who;
        uint32_t since;
    } offer;
    struct {
        jp_to_t to;
        bool heard, deliberate;
        uint32_t hush, opened_at;
    } rec;
    struct {
        bool voice, heard, follow_armed;
        int follow_turns;
        uint32_t hush, lead, opened_at;
    } talk;
    struct {
        sm_turn_t state;
        uint32_t since;
    } turn;
    struct {
        uint32_t seq, posted_at, expires_at;
    } send;
    struct {
        uint32_t first_visible;
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
    struct {
        uint32_t at;
        int x, y;
        bool on_pet, swiped;
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
