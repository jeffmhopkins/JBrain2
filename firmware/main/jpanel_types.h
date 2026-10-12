#pragma once

/* Voice post's shared types: the commands render posts to the engine, and the one snapshot the
 * engine publishes back (plan §7.1). Plain C, no ESP-IDF, so the engine core, the panel state
 * machine and their host tests all build from this.
 *
 * TWO ARROWS AND NOTHING ELSE. Render → engine is a command queue; engine → render is one
 * level-triggered snapshot (`jp_view_t`), copied once per frame under a seqlock. Neither side
 * reads the other's private state. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The same number as `JPANEL_QUEUE_MAX` in `backend/src/jbrain/api/jpanel.py`: a contract written
   down twice, pinned by `test_the_queue_cap_is_the_same_number_on_both_sides`. */
#define JPANEL_QUEUE_MAX 8
#define JP_ID_MAX 48   /* a uuid and its NUL, with room */
#define JP_NAME_MAX 32 /* the panel's name field; the box cuts names to fit (plan §6) */
#define JP_REASON_MAX 24

/* Wrap-safe compares for u32 clocks and sequence numbers (I7). */
static inline bool jp_seq_ge(uint32_t a, uint32_t b) { return (int32_t)(a - b) >= 0; }
static inline bool jp_due(uint32_t now, uint32_t at) { return (int32_t)(now - at) >= 0; }

/* Seq 0 is "none": no command ever carries it. */
#define JP_SEQ_NONE 0u

/* The UI backstop margin (plan §7.5c): >= the largest snapshot delay the machine tolerates (2 s)
   + LINK_STEP_MS + one frame. The engine never claims a command within this of its expiry. */
#define JP_UI_MARGIN_MS 3000u
#define JP_PLAY_EXPIRE_MS 35000u /* PLAY/REPLAY: claimable for 32 s */
#define JP_SEND_EXPIRE_MS 60000u

typedef enum { JP_TO_PANEL = 0, JP_TO_DAD } jp_to_t;

/* Who a message is from. `id` is empty when the box sent none (an old box). */
typedef struct {
    char id[JP_ID_MAX];
    char from[JP_NAME_MAX];
    bool from_dad;
} jp_who_t;

typedef enum { JP_CMD_PLAY = 1, JP_CMD_REPLAY, JP_CMD_SEND } jp_cmd_kind_t;

/* Written only by render (the machine forms it; the executor stamps `gen` and, for SEND, takes
   lease 1 and fills `pcm`). A post that does not fit the queue comes back as `post_failed`. */
typedef struct {
    jp_cmd_kind_t kind;
    uint32_t seq;        /* never JP_SEQ_NONE */
    uint32_t gen;        /* the stop gen at posting; zero and ignored for SEND */
    uint32_t expires_at; /* posted_at + JP_*_EXPIRE_MS */
    jp_who_t target;     /* PLAY: the selected message (id, or `at` on an old box); REPLAY: what
                            to replay, with its sender, since `/message/{id}/pcm` sends none */
    int at;              /* PLAY on a box without ids: the position */
    jp_to_t to;          /* SEND */
    const int16_t *pcm;  /* SEND: the leased capture; untouched until lease 1 is released */
    size_t bytes;
} jp_cmd_t;

typedef enum {
    JP_IDLE = 0,
    JP_POLLING,
    JP_FETCHING,
    JP_SOUNDING,
    JP_ACKING,
    JP_SENDING,
} jp_phase_t;

typedef enum {
    JP_PLAY_NONE = 0,
    JP_PLAY_HEARD,         /* played whole and acknowledged (or its ack is owed) */
    JP_PLAY_HEARD_UNACKED, /* played whole, but the head carried no X-Jpanel-Id */
    JP_PLAY_STOPPED_HEARD, /* stopped at or past the heard threshold, or by REPLY: acked */
    JP_PLAY_RETURNED,      /* stopped below it with a delivery header: handed back */
    JP_PLAY_SHORT,         /* the stream ended before its declared length: never acked */
    JP_PLAY_EMPTY,         /* 204 for the pressed message: nothing to play */
    JP_PLAY_CANCELLED,     /* expired before claim, stale gen, or a STOP before a byte */
    JP_PLAY_FAILED,        /* `reason` says why */
} jp_play_kind_t;

typedef enum {
    JP_SEND_NONE = 0,
    JP_SEND_SENT,
    JP_SEND_NOBODY, /* 409: no single other panel */
    JP_SEND_EMPTY,  /* 400: the box trimmed it to nothing */
    JP_SEND_FAILED,
} jp_send_kind_t;

typedef struct {
    uint32_t seq;  /* the command it ends */
    jp_who_t who;  /* what played: the offer is set from this, never from a later snapshot */
    jp_play_kind_t kind;
    bool replay;
    char reason[JP_REASON_MAX];
} jp_play_outcome_t;

typedef struct {
    uint32_t seq;
    jp_send_kind_t kind;
    char reason[JP_REASON_MAX];
} jp_send_outcome_t;

/* What the box says is waiting. `q` describes the first `n` of `count`, in the box's order. */
typedef struct {
    int count;
    int n;
    bool has_ids; /* the box sent ids (a new box); otherwise only q[0] and `count` are real */
    jp_who_t q[JPANEL_QUEUE_MAX];
    char sibling[JP_NAME_MAX];
} jp_waiting_t;

typedef struct {
    uint32_t seq;      /* the command this belongs to */
    uint8_t stream_id; /* 0 until the stream has begun */
    uint32_t gen;
    jp_who_t who;      /* seeded before the request: from the waiting entry, or the REPLAY cmd */
    bool replay;
    bool run_continues;
    int8_t pos;      /* index in `waiting` when it became cur; -1 for a REPLAY */
    bool in_waiting; /* the published `waiting` still lists it */
} jp_cur_t;

/* The snapshot. Single writer: the engine task. Readers copy it whole (jpanel_core.h). */
typedef struct {
    jp_phase_t phase;
    uint32_t claimed_seq; /* the command it is doing now */
    uint32_t deadline_ms; /* when that must have ended; 0 in IDLE */
    uint32_t done_seq;    /* every command ends here exactly once */
    jp_cur_t cur;
    jp_who_t last_begun;  /* set at a non-replay stream's first byte: what AGAIN replays */
    jp_play_outcome_t play;
    jp_send_outcome_t send;
    jp_waiting_t waiting; /* a RETURNED id is re-inserted here in the same write as its outcome */
    bool ack_owed;
    uint32_t hb;          /* bumped every loop pass and every link progress tick */
} jp_view_t;

/* Telemetry: flat integers only — they become the report's `jp` object, and `TelemetryIn` takes
   `dict[str, int]` (plan §7.7). The existing `msg_*` report fields are filled from these too. */
typedef struct {
    uint32_t msg_ok, msg_bad, msg_bytes, msg_ms, msg_waited_ms;
    uint32_t ack_owed, ack_retries, no_refund, returned, lease_refusals, snapshot_retries,
        post_failed, late_outcome, cue_expired, jpanel_stack_free, sm_violations;
    uint32_t wd_engine, wd_arbiter, wd_disarmed;
} jp_stats_t;
