/* The box's conversation answers as the panel reads them (backend `api/panel_convo.py`), and the
 * requests it sends. Pure C, no allocation, host-tested: what a row or a bubble can hold is
 * decided here, so an answer longer than the box promised is cut, never overrun. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_convo.h"

/* What the main task reads a reply into: the box's longest answer (ten rows, or the newest
 * answer's 1000 bytes and seven bubbles of 255) with room for JSON's escapes. A reply that fills
 * it is taken as cut off. Allocated per request, freed by the UI task once applied. */
#define CONVO_BODY_MAX 6144

#define CONVO_STR_(x) #x
#define CONVO_STR(x) CONVO_STR_(x)

typedef enum { CONVO_RQ_NONE, CONVO_RQ_RECENT, CONVO_RQ_SWITCH, CONVO_RQ_TITLE } convo_rq_t;

/* "jerv" or "dave": the box's `{agent}`. False for anything else. */
bool convo_agent_ok(const char *agent);
/* The request's path: `/endpoint/convo/<agent>/recent`, `.../select`, `.../messages?limit=N`
 * (a switch's second request), `.../messages?limit=0` (the title alone). */
void convo_path(convo_rq_t kind, const char *agent, bool messages, char *out, size_t cap);
/* `{"session_id":"<id>"}`; false when `id` is not a box session id (36 of [0-9a-f-]). */
bool convo_select_body(const char *id, char *out, size_t cap);

/* `{"items":[{id,title,at,origin,snippet,current},…]}`. Unknown fields are skipped; a row
 * without an id or with an unknown origin is dropped; past CONVO_ROWS rows are ignored; a string
 * too long for its field is cut at a character with "…". False on anything that is not that
 * shape (the list is then empty). */
bool convo_parse_recent(const char *json, size_t len, convo_list_t *out);

/* `{"id","title","messages":[{mine,text},…],"more"}`: each message is handed to `on_msg` in
 * order, its text cut to `cap` bytes in `scratch` (the caller's: the newest answer may be 1000
 * bytes). `title` gets the title (cut to fit). False when the shape is wrong — nothing after the
 * fault is handed on. */
typedef void (*convo_msg_fn)(void *user, bool mine, const char *text);
bool convo_parse_thread(const char *json, size_t len, char *title, size_t title_cap, bool *more,
                        char *scratch, size_t cap, convo_msg_fn on_msg, void *user);

/* Copies `src` into `dst` (`cap` bytes with the terminator), cut at a character boundary with
 * "…" when it does not fit. */
void convo_fit(char *dst, size_t cap, const char *src, size_t n);
