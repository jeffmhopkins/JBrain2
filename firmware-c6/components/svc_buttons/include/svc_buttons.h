/* The three top buttons, seen from the front left to right: BOOT (GPIO9), PWR, KEY (GPIO10)
 * (HARDWARE.md "Measured on the board"). This service samples and debounces them on its own
 * task and hands each press to a sink; it never calls the shell or LVGL. */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

typedef enum { BTN_KEY_DOWN, BTN_KEY_UP, BTN_BOOT, BTN_PWR } btn_event_t;
typedef void (*btn_sink_fn)(btn_event_t ev);

/* `sink` is called from this service's task. `notify` is woken (a task notification) when PWR
 * is pressed by either route, so the box hears which route works within seconds. */
void svc_buttons_start(btn_sink_fn sink, TaskHandle_t notify);

/* The AXP2101 latched a PWR press (svc_power's callback). Any task. */
void svc_buttons_pek(void);

/* `pwr=… g18idle=… edges=…` — telemetry-safe tokens. */
void svc_buttons_note(char *out, size_t len);

/* True once since the last call if PWR was pressed: the caller sends a report. */
bool svc_buttons_take_dirty(void);
