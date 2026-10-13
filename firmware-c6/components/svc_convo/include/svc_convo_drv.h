/* The conversation service's device side, beside the app-facing svc_convo.h: the main task takes
 * the one waiting request and hands back the box's answer; the UI task applies it. The same file
 * runs on the device and in the sim; only the lock differs (esp/convo_port_esp.c, or
 * convo_port_fake.c), and in the sim the sim itself answers as the box. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "convo_wire.h"

/* One request: what to ask, of which agent, and (a switch) which conversation. `seq` tells a
 * stale answer from the one the glass is waiting for. */
typedef struct {
    uint8_t kind; /* convo_rq_t */
    char agent[8];
    char id[CONVO_ID_BYTES];
    uint32_t seq;
    /* CONVO_RQ_HISTORY (0.0.21): the thread it is for, the path built on the UI task (where the
       panels' ids live), which page (0 counts, 1 earlier, 2 newer) and, for the counts, the
       local id of the oldest message they are counted from. */
    char thread[16];
    char path[HIST_PATH_BYTES];
    uint8_t side;
    uint32_t counted;
} convo_req_t;

/* Its section of the debug snapshot ("convo"): what is waiting, and each agent's title. */
void svc_convo_init(void);
/* Called when the glass asks: notifies the main task (it must only notify). */
void svc_convo_set_wake(void (*wake)(void));

/* ---- main task ---- */
/* The waiting request, now in flight. False when there is none. */
bool svc_convo_take(convo_req_t *out);
/* Its answer: `status` the HTTP status (-1 none), `body` a heap buffer of `len` bytes holding
 * the 200's JSON, NUL-terminated, whose ownership passes here (NULL for anything but a 200). */
void svc_convo_done(const convo_req_t *req, int status, char *body, size_t len);

/* ---- UI task, once per loop pass ---- */
void svc_convo_service(void);

/* `convo=rq:N,ok:N,fail:N,stale:N,last:<status>` for telemetry. */
void svc_convo_note(char *out, size_t len);

/* ---- what each build supplies: a critical section on the device ---- */
void convo_port_lock(void);
void convo_port_unlock(void);
