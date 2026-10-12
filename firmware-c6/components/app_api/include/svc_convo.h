/* An agent's conversations on the box (0.0.15, owner 2026-10-12): the Recent list, switching the
 * panel's thread to another conversation, and the current one's title. The box keeps every
 * conversation (backend `api/panel_convo.py`); the panel keeps only what it is showing.
 *
 * Every call here is the UI task's and returns at once: a request is handed to the main task's
 * network loop (one HTTPS request in flight, after a talk), and its answer is applied on the UI
 * task's next svc_convo_service. A view reads the state and redraws when svc_convo_rev moves. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* What a Recent row keeps, terminators included: the box cuts a title to 40 bytes and a last
 * line to 80 (backend `panel_convo.TITLE_BYTES`, `SNIPPET_BYTES`); longer is cut here too. */
#define CONVO_ROWS 10
#define CONVO_ID_BYTES 37
#define CONVO_TITLE_BYTES 41
#define CONVO_SNIPPET_BYTES 81
/* A switched thread: its last exchanges, at most the box's PAIRS_MAX (4) and the eight messages
 * an agent's thread keeps. */
#define CONVO_PAIRS 3

typedef enum { CONVO_FROM_PANEL, CONVO_FROM_PWA } convo_origin_t;

typedef struct {
    char id[CONVO_ID_BYTES];
    char title[CONVO_TITLE_BYTES];
    char snippet[CONVO_SNIPPET_BYTES]; /* "You: " before the owner's own line */
    uint8_t origin;                    /* convo_origin_t: the row's microphone or phone */
    bool current;                      /* the box's current thread for this panel */
    int64_t at;                        /* last active, UTC seconds */
} convo_row_t;

typedef struct {
    uint8_t n;
    convo_row_t row[CONVO_ROWS];
} convo_list_t;

typedef enum {
    CONVO_IDLE,    /* nothing asked */
    CONVO_WAITING, /* asked; the box has not answered */
    CONVO_READY,   /* answered */
    CONVO_FAILED,  /* no answer, or one the panel could not read */
} convo_state_t;

/* Recent: asks the box for `agent`'s list ("jerv" or "dave"). The list is on the heap from here
 * until svc_convo_recent_close — about 1.8 KB, only while Recent is open. False with no memory. */
bool svc_convo_recent_open(const char *agent);
/* The list's state; `*out` is the list once READY (valid until the next open or close). */
convo_state_t svc_convo_recent(const convo_list_t **out);
void svc_convo_recent_close(void);

/* Switch: points the panel's thread with `agent` at conversation `id`, then its last exchanges
 * replace this panel's view of that thread, the title becomes its title, and the agent's next
 * talk continues it (a new conversation asked for before is forgotten). */
void svc_convo_switch(const char *agent, const char *id);
convo_state_t svc_convo_switching(void);

/* The current conversation's title, from the box: NULL until the box has said, "" when the panel
 * has no conversation with the agent there. Asked again with svc_convo_refresh_title. */
const char *svc_convo_title(const char *agent);
void svc_convo_refresh_title(const char *agent);
/* Older messages than the ones shown are in the app: set by a switch, cleared by a new
 * conversation. */
bool svc_convo_earlier(const char *agent);
/* New conversation: the title and the earlier-messages note go with the old one. */
void svc_convo_new(const char *agent);
/* Moves whenever anything above does: a view redraws when it is not what it drew. */
uint32_t svc_convo_rev(void);
