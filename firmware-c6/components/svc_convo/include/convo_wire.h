/* The box's conversation answers as the panel reads them (backend `api/panel_convo.py`), and the
 * requests it sends. Pure C, no allocation, host-tested: what a row or a bubble can hold is
 * decided here, so an answer longer than the box promised is cut, never overrun. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "hist_logic.h"
#include "svc_convo.h"

/* What the main task reads a reply into: the box's longest answer (ten rows, or the newest
 * answer's 1000 bytes and seven bubbles of 255) with room for JSON's escapes. A reply that fills
 * it is taken as cut off. Allocated per request, freed by the UI task once applied. */
#define CONVO_BODY_MAX 6144

#define CONVO_STR_(x) #x
#define CONVO_STR(x) CONVO_STR_(x)

typedef enum {
    CONVO_RQ_NONE,
    CONVO_RQ_RECENT,
    CONVO_RQ_SWITCH,
    CONVO_RQ_TITLE,
    CONVO_RQ_HISTORY, /* 0.0.21: a page of a thread, or its counts alone; the path is built here */
} convo_rq_t;

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

/* `{"id","title","messages":[{mine,text,id},…],"more"}`: each message is handed to `on_msg` in
 * order, its text cut to `cap` bytes in `scratch` (the caller's: the newest answer may be 1000
 * bytes) and its turn's `seq` (0.0.21; 0 from a box before it, or past 32 bits). `title` gets the
 * title (cut to fit). False when the shape is wrong — nothing after the fault is handed on. */
typedef void (*convo_msg_fn)(void *user, bool mine, const char *text, uint32_t id);
bool convo_parse_thread(const char *json, size_t len, char *title, size_t title_cap, bool *more,
                        char *scratch, size_t cap, convo_msg_fn on_msg, void *user);

/* Copies `src` into `dst` (`cap` bytes with the terminator), cut at a character boundary with
 * "…" when it does not fit. */
void convo_fit(char *dst, size_t cap, const char *src, size_t n);

/* ---- Load earlier (0.0.21) ----
 * A page is at most the box's PAGE_BYTES (backend `panel_convo` / `panel_messages`, 4000) of JSON:
 * this buffer, read into on the main task and freed once applied, holds it with room to spare. */
#define HIST_BODY_MAX 4096
#define HIST_PATH_BYTES 128
/* `/endpoint/convo/<agent>/history?…` for an agent's thread (`who` "jerv" or "dave"), or
 * `/endpoint/messages/<device>/history?…` for a person's (`who` her panel's device id). `side`:
 * 0 the counts alone (`limit=0`, before `box_id`/`uuid` when one is given), 1 the page before the
 * anchor, 2 the page after it. False for a `who` or an anchor that is not one. */
bool convo_hist_path(const char *who, uint8_t side, uint32_t box_id, const char *uuid, char *out,
                     size_t cap);

typedef struct {
    uint32_t older, newer;
    int64_t start_at; /* 0: not the true start, or not said */
    uint8_t n;
} convo_page_t;
/* `{"messages":[{id,mine,text,ms?,played?,voice?},…],"older","newer","start_at"}`. An agent's id
 * is a number (its `seq`, into `box_id`), a person's a string (into `uuid`); a message with
 * neither is dropped; past HIST_PAGE are ignored. Each is handed to `on_item` with its text in
 * `scratch` (`cap` bytes, cut on a character). False on anything that is not that shape. */
typedef void (*convo_item_fn)(void *user, const hist_in_t *item);
bool convo_parse_page(const char *json, size_t len, convo_page_t *out, char *scratch, size_t cap,
                      convo_item_fn on_item, void *user);
