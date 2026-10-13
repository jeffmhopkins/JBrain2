/* The owner's voice post with the twins' panels, as the panel reads and sends it (backend
 * `api/panel_messages.py`). Pure C, no allocation, host-tested: what a bubble may hold, which
 * messages are new, whether a new one chimes, and how a recording is framed on its way up are
 * decided here. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_messages.h"
#include "svc_post.h"

/* A box message id (a uuid's text) with its terminator. */
#define POST_ID_BYTES 37
/* What a person's thread keeps (svc_messages' PERSON_KEEP) and what the panel asks the box for. */
#define POST_KEEP 4
/* The reply a thread is read into: four bubbles of 255 bytes with JSON's quoting, ids and
 * fields. A reply that fills it is taken as cut off. Allocated per request. */
#define POST_BODY_MAX 3072
/* The send's answer, `{"id","text","ms"}`: one bubble's text and an id. */
#define POST_SENT_MAX 640
/* The longest path any request here takes: `/endpoint/messages/<36>/<36>/played`. */
#define POST_PATH_BYTES 112
/* The box keeps 30 s of a message (backend `jpanel.MAX_MESSAGE_MS`) and refuses more: the
 * upload stops at exactly that many bytes, whatever the hold ran to. */
#define POST_MAX_BYTES (16000u * 2u * (POST_MAX_MS / 1000u))
/* The recording streams up as it is made, so its digest cannot ride a header: the panel says
 * `X-Jpanel-Sha256: trailer` and sends the 32 raw bytes of sha256(audio) after the audio. */
#define POST_SHA_HEADER "X-Jpanel-Sha256"
#define POST_SHA_TRAILER "trailer"
#define POST_DIGEST_BYTES 32
/* The pickup's new-message signal (`X-Jpanel-Post`), at most this long with its terminator. */
#define POST_TAG_BYTES 12

typedef struct {
    char id[POST_ID_BYTES];
    bool mine;   /* the owner's own (right), or hers (left, with play) */
    bool played; /* hers: he has heard it, here or in the PWA. his: she has */
    bool voice;
    uint32_t ms;
    char text[256]; /* msg_t.text: cut at a character with an ellipsis */
} post_line_t;

typedef struct {
    uint8_t n;
    char tag[POST_TAG_BYTES];
    post_line_t line[POST_KEEP]; /* oldest first */
} post_thread_t;

/* `[0-9a-f-]{36}`: what the box's ids are, and the only thing that goes into a path. */
bool post_id_ok(const char *id);
/* `/endpoint/messages/<device>?limit=4`, `.../<device>/send`, `.../<device>/<id>/pcm`,
 * `.../<device>/<id>/played`. False (and "") for an id that is not one. */
bool post_thread_path(const char *device, char *out, size_t cap);
bool post_send_path(const char *device, char *out, size_t cap);
bool post_pcm_path(const char *device, const char *id, char *out, size_t cap);
bool post_played_path(const char *device, const char *id, char *out, size_t cap);

/* `{"device_id","name","tag","messages":[{id,mine,text,ms,played,voice},…]}`. Unknown fields are
 * skipped; a message with no id is dropped; past POST_KEEP only the newest are kept; text too
 * long for a bubble is cut on a character. False on anything that is not that shape. */
bool post_parse_thread(const char *json, size_t len, post_thread_t *out);
/* `{"id","text","ms"}`, the send's 201. */
bool post_parse_sent(const char *json, size_t len, char id[POST_ID_BYTES], char *text, size_t cap,
                     uint32_t *ms);

/* A box message as the thread's bubble: hers with play and her duration, his with "Played" once
 * she has heard it, else "Sent". A message with no transcript says "Voice message". */
#define POST_NO_TEXT "Voice message"
void post_to_msg(const post_line_t *l, uint32_t local_id, msg_t *out);

/* How much of `n` more bytes may go up after `sent`: the upload ends at POST_MAX_BYTES. */
uint32_t post_upload_room(uint32_t sent, uint32_t n);

/* ---- what is new, and whether it chimes ---- */
/* The ids a thread last listed. The first list of a boot (or of a newly bound panel) is the
 * backlog: badged, never chimed. */
typedef struct {
    bool loaded;
    uint8_t n;
    char id[POST_KEEP][POST_ID_BYTES];
} post_seen_t;
/* How many of `t`'s messages are hers, unplayed and not in `seen` — arrived since the last list.
 * Then `seen` becomes `t`'s ids. */
uint8_t post_arrivals(post_seen_t *seen, const post_thread_t *t);
/* A new message chimes when the chime is on, it is not the night window (the notice rule) and
 * nothing else has the speaker — a hold, a reply, a message playing, an alarm. Never a pop-up:
 * the badge is the signal, the chime only says to look. */
bool post_chime(uint8_t arrivals, bool chime_on, bool night_now, bool busy);

/* The pickup's tag: true when it is new (the first one a boot sees included), so the threads are
 * fetched exactly when the owner's post has moved. An empty tag is no signal. */
typedef struct {
    char last[POST_TAG_BYTES];
} post_tag_t;
bool post_tag_feed(post_tag_t *t, const char *tag);

/* ---- "played" stamps the box has not confirmed (review of 0.0.16) ----
 * A message heard here is played on the panel at once; its stamp to the box waits in this list
 * until the box answers 204 (or 404: the message is gone), retried with a bounded backoff, and
 * the list is kept in NVS so neither a reboot nor an outage longer than a few plays loses one.
 * While a stamp waits, every list from the box shows that message played. */
#define POST_STAMPS 8
typedef struct {
    char device[POST_ID_BYTES];
    char id[POST_ID_BYTES];
} post_stamp_t;
typedef struct {
    uint8_t n;
    post_stamp_t s[POST_STAMPS]; /* oldest first */
} post_stamps_t;
/* Adds one (a repeat is not added twice); full, the oldest gives way. False for a repeat. */
bool post_stamps_add(post_stamps_t *l, const char *device, const char *id);
bool post_stamps_has(const post_stamps_t *l, const char *device, const char *id);
/* True when it was there. */
bool post_stamps_remove(post_stamps_t *l, const char *device, const char *id);
/* After a list read from flash: entries that are not two box ids are dropped, strings ended. */
void post_stamps_clean(post_stamps_t *l);
/* What a stamp's answer means: done (204, or 404 — the message is gone, never retried), or try
 * again later (no answer, a 5xx, a 403 while the owner's switch is off, anything else). */
bool post_stamp_done(int status);
/* The wait before the next try after `fails` failures: 5 s, 15 s, 60 s, then every 5 min. */
uint32_t post_backoff_ms(uint8_t fails);
