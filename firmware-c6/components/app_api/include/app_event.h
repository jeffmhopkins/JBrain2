/* What an app may react to. Apps never poll hardware: a service raises one of these and the
 * shell routes it. Pure C, so the shell's routing and its queue are host-tested. */
#pragma once

#include <stdint.h>

typedef enum {
    APP_EV_KEY_DOWN,
    APP_EV_KEY_UP,
    APP_EV_BOOT,
    APP_EV_NET_UP,
    APP_EV_NET_DOWN,
    APP_EV_MSG_ARRIVED,
    APP_EV_ALARM_DUE,
    APP_EV_NOTICE_ARRIVED,
    APP_EV_TICK_MINUTE,
    APP_EV_WAKE,
    APP_EV_ALARM_ENDED, /* snoozed, stopped or capped: the ringing view goes */
    APP_EV__COUNT,
} app_event_type_t;

/* Events cross from service tasks to the UI task through a queue, so every field is carried
 * by value. A pointer into another task's memory is the torn-read class the twins' messaging
 * review found (JPANEL_MESSAGING_FIXES_PLAN.md A7). */
typedef struct {
    app_event_type_t type;
    uint32_t at_ms;
    union {
        char principal[16]; /* MSG_ARRIVED: whose thread */
        uint32_t alarm_id;  /* ALARM_DUE, ALARM_ENDED */
        uint32_t notice_id; /* NOTICE_ARRIVED */
    } u;
} app_event_t;

/* Services raise events through here without knowing the shell exists; the shell installs the
 * sink at start-up (its queue). Before that, events are counted and dropped. */
typedef void (*app_event_sink_t)(const app_event_t *ev);
void app_events_set_sink(app_event_sink_t sink);
void app_events_post(const app_event_t *ev);
void app_events_post_type(app_event_type_t type);
uint32_t app_events_unsunk(void);
