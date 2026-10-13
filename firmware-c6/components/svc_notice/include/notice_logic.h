/* The notice path's small decisions, apart from the files and the radio (plan §2 "The panel
 * side"). Pure C, host-tested: what the pickup reports, the box paths, the state POSTs that must
 * reach the box across an outage and a reboot, and when a notice hides or lapses. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* How many notices the panel holds at once: the box's MAX_OPEN, which refuses a fifth. */
#define NOTICE_OPEN_MAX 4
/* A notice's id as text (`8-4-4-4-12`) with its terminator. */
#define NOTICE_ID_TEXT 37

/* ---- the report on the debug pickup (backend `notice.parse_prefs`) ---- */
typedef struct {
    bool accept, quiet, chime;
    uint16_t night_from, night_to; /* minutes after local midnight */
    uint8_t tz;                    /* svc_rtc's zone index (backend PANEL_ZONES) */
} notice_prefs_t;
/* `&na=1&nq=1&nc=1&nf=1290&nt=390&tz=5`: all six or the box reads none. */
void notice_pickup_query(const notice_prefs_t *p, char *out, size_t cap);
/* The pickup's `X-Notice: <n>`: how many wait, 0 for none or anything that is not a count. */
uint8_t notice_waiting_count(const char *header);

/* ---- the box's paths ---- */
/* `/endpoint/notice/next` */
#define NOTICE_NEXT_PATH "/endpoint/notice/next"
/* `/endpoint/notice/<id>/body`, `/endpoint/notice/<id>/state`. */
#define NOTICE_PATH_BYTES 72
void notice_body_path(const char *id, char *out, size_t cap);
void notice_state_path(const char *id, char *out, size_t cap);

/* ---- what the panel tells the box ---- */
typedef enum {
    NOTICE_SHOWN,
    NOTICE_ANSWERED,
    NOTICE_DISMISSED,
    NOTICE_EXPIRED,
} notice_state_t;
const char *notice_state_name(notice_state_t s);
/* `{"state":"answered","choice":1}`, or without the choice for every other state. */
void notice_state_json(notice_state_t s, uint8_t choice, char *out, size_t cap);

/* The state POSTs the box has not confirmed, oldest first: kept in NVS so neither a dropped
 * radio nor a reboot loses an answer, and sent one at a time, in order — `shown` before the
 * answer it precedes. Full, the oldest gives way. */
#define NOTICE_POSTS 8
typedef struct {
    char id[NOTICE_ID_TEXT];
    uint8_t state; /* notice_state_t */
    uint8_t choice;
} notice_post_t;
typedef struct {
    uint8_t n;
    notice_post_t p[NOTICE_POSTS];
} notice_posts_t;
/* False when it was full and the oldest was dropped to make room. */
bool notice_posts_add(notice_posts_t *l, const char *id, notice_state_t s, uint8_t choice);
void notice_posts_pop(notice_posts_t *l);
/* Whether `id` has a final state (answered, dismissed, expired) waiting: a notice that ended
 * before a reboot and is not shown again. */
bool notice_posts_final(const notice_posts_t *l, const char *id);
/* After a list read from flash: entries that are not an id and a known state are dropped. */
void notice_posts_clean(notice_posts_t *l);
/* Done with it: 2xx; or a 404 (not this panel's live notice any more — its answer window
 * closed), 409 (a move the box refuses) or 422 (a choice it refuses), which no retry changes.
 * Anything else — no answer, a 5xx, a 401 while the key is being rotated — is tried again. */
bool notice_post_done(int status);
/* The wait before the next try after `fails` failures: 5 s, 15 s, 60 s, then every 5 min. */
uint32_t notice_backoff_ms(uint8_t fails);

/* ---- a torn or refused stream, fetched again from `…/{id}/body` ---- */
#define NOTICE_REFETCH_TRIES 3
/* The wait before try `tries` (1-based): 2 s, 10 s, 30 s. */
uint32_t notice_refetch_ms(uint8_t tries);

/* ---- the clock of a notice on the glass ---- */
/* `hide_s` > 0 hides it that long after it is shown. */
bool notice_hide_due(uint32_t shown_ms, uint16_t hide_s, uint32_t now_ms);
/* The box takes an answer for `ttl_s` from delivery; past it, it 404s anything the panel says,
 * so the notice is gone from here too. */
bool notice_lapsed(uint32_t arrived_ms, uint32_t ttl_s, uint32_t now_ms);

/* ---- the files: `<seq>-<id hex>.jn1`, the order they came in and which notice ---- */
#define NOTICE_FILE_NAME 48
void notice_file_name(uint32_t seq, const uint8_t id[16], char *out, size_t cap);
/* False for any name that is not one this panel wrote. */
bool notice_file_parse(const char *name, uint32_t *seq, uint8_t id[16]);
