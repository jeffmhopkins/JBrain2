/* The voice post service's device side, beside the app-facing svc_post.h: the main task takes the
 * waiting request and hands back the box's answer, and gives it the pickup's tag; the UI task
 * applies the answers; svc_audio asks for the paths of a send and a play and reports how each
 * ended. The same file runs on the device and in the sim; only the lock differs
 * (esp/post_port_esp.c, or post_port_fake.c), and in the sim the sim answers as the box. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "post_logic.h"

typedef enum { POST_RQ_NONE, POST_RQ_THREAD, POST_RQ_PLAYED } post_rq_t;

/* One request: a thread's list, or a message heard. `path` is built on the UI task, where the
 * panels' ids live, so the main task never reads the siblings. */
typedef struct {
    uint8_t kind; /* post_rq_t */
    uint8_t slot; /* which person thread */
    char path[POST_PATH_BYTES];
} post_req_t;

/* Its section of the debug snapshot ("post"), and the stamps a reboot left waiting. */
void svc_post_init(void);
/* Called when the glass asks: notifies the main task (it must only notify). */
void svc_post_set_wake(void (*wake)(void));

/* ---- main task ---- */
/* The pickup's `X-Jpanel-Post` (or "" when it carried none). A tag that moved is a fetch of every
 * bound thread on the UI task's next pass. */
void svc_post_tag(const char *tag);
bool svc_post_take(post_req_t *out);
/* `body`: a heap buffer of `len` bytes holding a 200's JSON, NUL-terminated, whose ownership
 * passes here (NULL for anything but a thread's 200). */
void svc_post_done(const post_req_t *req, int status, char *body, size_t len);

/* ---- UI task, once per loop pass ---- */
void svc_post_service(void);
/* True once for each list that brought her a new message and should chime: the chime switch and
 * the night window are read here, `busy` is whether anything else has the speaker. */
bool svc_post_take_chime(bool busy);

/* ---- svc_audio's (UI task) ---- */
/* The send path for `thread`'s panel; false while no panel is bound to it. */
bool svc_post_send_path(const char *thread, char *out, size_t cap);
/* The play path for the bubble `local_id` (in whichever person thread holds it), and that
 * thread; false for a bubble that is not one of the box's messages. */
bool svc_post_pcm_path(uint32_t local_id, char thread[16], char *out, size_t cap);
/* A send landed: the thread is fetched so its bubble carries the box's id. */
void svc_post_sent(const char *thread);
/* `local_id`'s audio began to play (0.0.17: heard once its audio starts, however it ends): played here
 * at once, and on the box — its stamp waits in the NVS-kept list until the box confirms it,
 * retried with a backoff, and goes behind the play on the main task's one connection. */
void svc_post_heard(const char *thread, uint32_t local_id);

/* `post=rq:N,ok:N,fail:N,new:N,chimes:N,tag:<tag>,last:<status>` for telemetry. */
void svc_post_note(char *out, size_t len);

/* ---- what each build supplies: a critical section and the stamps' NVS blob on the device ---- */
void post_port_lock(void);
void post_port_unlock(void);
/* The unconfirmed "played" stamps, kept across a reboot (namespace `jpanel`, key `post_stamps`).
 * False when there is nothing readable. */
bool post_port_load(post_stamps_t *out);
void post_port_save(const post_stamps_t *in);
