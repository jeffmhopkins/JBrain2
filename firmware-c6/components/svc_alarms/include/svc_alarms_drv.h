/* The alarms service's device side, beside the app-facing svc_alarms.h: the main task fetches the
 * box's list and carries the switches back, the UI task fires the alarms. The same file runs on
 * the device and in the sim; only the cache port differs (esp/alarm_port_nvs.c, or
 * alarm_port_fake.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "alarm_logic.h"

/* Before any app asks: the last list the box sent, from the cache, so an alarm rings after a
 * reboot with the box unreachable. */
void svc_alarms_init(void);
/* Called when a switch on the glass wants the box: notifies the main task (it must only notify). */
void svc_alarms_set_wake(void (*wake)(void));

/* ---- main task ---- */
/* The version the panel holds, for `GET /endpoint/alarms?etag=`; empty before the first list. */
void svc_alarms_etag(char *out, size_t cap);
/* A 200's body. False when it is not the expected shape (the copy stays as it is). */
bool svc_alarms_offer(const char *json, size_t len);
/* A 304: the copy is current. */
void svc_alarms_unchanged(void);
/* No answer (-1), or a status other than 200 / 304. */
void svc_alarms_failed(int status);
/* The oldest switch the box has not got yet. */
bool svc_alarms_take_put(alarm_put_t *out);
void svc_alarms_put_done(const alarm_put_t *sent, int status);

/* ---- UI task, once per loop pass ---- */
/* Applies a list the main task left, fires what is due, ends a ring at the cap, saves the cache
 * once things have been still for a moment. */
void svc_alarms_service(uint32_t now_ms);

/* `alarm=n:3,next:+545,fired:1,snz:0,stop:1,cap:0,miss:0,q:0,src:box,fetch:304` for telemetry. */
void svc_alarms_note(char *out, size_t len);

/* ---- what each build supplies ---- */
typedef struct {
    alarm_list_t list; /* with the waiting switches already applied */
    alarm_queue_t queue;
} alarm_cache_t;
bool alarm_port_load(alarm_cache_t *out);
void alarm_port_save(const alarm_cache_t *in);
/* The queue and the etag are shared with the main task: a critical section on the device. */
void alarm_port_lock(void);
void alarm_port_unlock(void);
