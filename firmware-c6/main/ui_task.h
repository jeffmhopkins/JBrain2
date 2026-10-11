#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "svc_buttons.h"

/* Starts the UI task: LVGL, the CO5300, the shell and the apps over the fake services. Only
 * after the rollback gate. A failure anywhere leaves a headless panel that still updates.
 * `notify` is woken once the UI has settled (or stopped), for a report that shows it. */
void ui_task_start(TaskHandle_t notify);

/* True once since the last call if the UI asked for a report. */
bool ui_task_take_dirty(void);

/* The button service's sink: a press reaches the shell only once it is running. */
void ui_task_button(btn_event_t ev);

/* `ui=… stage=… crumb=… ui_heap=free/min ui_stack=… ui_age=…` — telemetry-safe. */
void ui_task_note(char *out, size_t len);
