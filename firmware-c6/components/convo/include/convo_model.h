/* A conversation's rules, apart from drawing them. Pure C, host-tested. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_audio.h"
#include "svc_messages.h"

/* Listen (0.0.21, owner-approved 2026-10-13; answer 1: on every reply, read-aloud on or off).
 * The speaker button beside a reply: on every finished reply the box has named, never on the
 * owner's own words or a reply still on its way; dimmed while the agent works on the next answer
 * (a tap says LISTEN_WAITS); the playing state on the one being spoken, Listen's or read-aloud's.
 * Its values are ui_listen_t's. `speech` may be NULL (nothing is being said). */
uint8_t convo_listen_state(const msg_t *m, bool working, const svc_speech_t *speech);
/* What the caption under it says: "Starting… · tap to stop", "0:14 · tap to stop", or "". */
void convo_listen_caption(uint8_t state, uint32_t ms, char *out, size_t cap);

/* The steps under a running turn (owner, 2026-10-13: "When using more than two tools, the first
 * line should just be a running record of how many tools called and the second line down the
 * current one that's being used"). Up to two steps: each its own line, as since 0.0.14. Three or
 * more: two lines — "✓ N tools used" (every call started, the running one too), then the running
 * step with the spinner, or the phase line ("Thinking… 0:37") between tools. */
typedef struct {
    bool collapsed;
    uint16_t count;  /* collapsed: tools started */
    int8_t running;  /* collapsed: the step[] index of the running one, or -1 for the phase line */
} convo_work_lines_t;
convo_work_lines_t convo_work_lines(const svc_work_t *w);
/* "4 tools used". */
void convo_tools_used(uint16_t count, char *out, size_t cap);
/* What ending a hold tells the person, or NULL when the send itself is the answer. */
const char *convo_talk_toast(talk_outcome_t o);

/* What a conversation's drawing depends on. The messages and how they are drawn rebuild the
 * list; the working view under the question (a step, the phase, its clock) changes in place —
 * so a status line moving on never recreates the widgets above it, nor the spinner (owner,
 * 2026-10-12: the thinking animation "kind of like resetting"). */
typedef struct {
    uint32_t rev;      /* svc_messages_rev: the messages, and Load earlier's rows (0.0.21) */
    uint32_t speaking; /* the bubble being spoken (svc_audio_speech); its progress moves in place */
    bool working;      /* the working view is up */
    uint32_t work_seq; /* svc_work_t.seq */
    uint32_t meta;     /* svc_convo_rev: the title, the earlier-messages chip, a switch */
    bool fresh;        /* svc_audio_fresh_pending: a new conversation, not yet begun */
} convo_sig_t;
typedef enum {
    CONVO_SAME,    /* nothing to draw */
    CONVO_WORK,    /* only the working view: update it where it stands */
    CONVO_REBUILD, /* the messages, or whether there is a working view at all */
} convo_redraw_t;
convo_redraw_t convo_redraw(const convo_sig_t *drawn, const convo_sig_t *now);

/* ---- 0.0.15: New conversation, Recent, and switching (owner, 2026-10-12) ---- */

#define CONVO_TOAST_NEW "New conversation — the last one is in Recent"
#define CONVO_TOAST_ALREADY "Already a new conversation"
#define CONVO_TOAST_OPENING "One moment: opening that conversation"
#define CONVO_TOAST_NOT_OPENED "Couldn't open that conversation. Try again."

/* New conversation is one tap with no confirm (nothing is lost: the old one stays in Recent and
 * the app) — and does nothing when the thread already is a new, empty one, rather than pile up
 * blank conversations. */
typedef enum { CONVO_NEW_START, CONVO_NEW_ALREADY } convo_new_t;
convo_new_t convo_new_decide(bool fresh_pending, uint16_t shown);

/* What the thread's header says under the agent's name: "New conversation" for one not yet
 * begun (or when the box has none), the box's title, or "Desk panel" until the box has said. */
const char *convo_subtitle(bool fresh, const char *title);

/* Whether a Recent row is the conversation the panel is in: the box's current one, unless a new
 * conversation has been asked for since (it is not on the box until something is said). */
bool convo_row_is_current(bool box_current, bool fresh);

/* A row's time, in the panel's 24-hour clock: "Just now", "12 min ago", "Today 08:12",
 * "Yesterday 19:40", "Sat 10:05" within the week, else "5 Oct". `at` and `now` are UTC seconds;
 * `offset_s` is the panel's zone now (local minus UTC). */
void convo_when(int64_t at, int64_t now, int32_t offset_s, char *out, size_t cap);

/* Recent, as a state machine: the view only draws what this decides. */
typedef enum {
    RC_CLOSED,
    RC_LOADING, /* asked; skeleton rows */
    RC_LIST,
    RC_EMPTY,   /* the box has none */
    RC_FAILED,  /* no answer: "Try again" */
    RC_OPENING, /* a row tapped: "Opening…" until the switch lands */
} rc_mode_t;
typedef struct {
    rc_mode_t mode;
    int8_t opening; /* the row being opened */
} rc_t;
typedef enum {
    RC_IN_OPEN,           /* Recent conversations, or Try again */
    RC_IN_LISTED,         /* the list is in: `arg` rows */
    RC_IN_FAILED,         /* no list */
    RC_IN_TAP,            /* a row: `arg` its index, `current` whether it is the one the panel is in */
    RC_IN_SWITCHED,       /* the switch landed */
    RC_IN_SWITCH_FAILED,  /* it did not */
    RC_IN_BACK,           /* the back arrow, or BOOT */
    RC_IN_KEY,            /* KEY pressed */
    RC_IN_NEW,            /* the "+ New conversation" row */
} rc_in_t;
typedef enum {
    RC_DO_NOTHING,
    RC_DO_FETCH,      /* ask the box for the list, draw "Loading…" */
    RC_DO_DRAW,       /* draw the mode */
    RC_DO_SWITCH,     /* ask the box to switch to row `opening`, draw "Opening…" on it */
    RC_DO_CLOSE,      /* back to the thread */
    RC_DO_CLOSE_TALK, /* back to the thread, and KEY talks in it */
    RC_DO_CLOSE_NEW,  /* back to the thread, as a new conversation */
    RC_DO_WAIT,       /* say a switch is under way; nothing else */
    RC_DO_NOT_OPENED, /* back to the list, saying the switch failed */
} rc_do_t;
rc_do_t rc_step(rc_t *r, rc_in_t in, int arg, bool current);
