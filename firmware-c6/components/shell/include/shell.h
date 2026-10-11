/* The shell: home pages, the one open app, button routing and the overlays above every app.
 * Everything it decides is shell_core's; this is the LVGL half that carries it out, and it
 * runs only on the UI task. Other tasks reach it through shell_post / shell_button, which
 * queue. */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "app_api.h"
#include "shell_core.h"

/* Before shell_start. The app must outlive the shell (they are static descriptors). */
bool shell_register(app_t *app);
/* The app that ALARM_DUE opens. */
void shell_set_alarm_app(const char *id);
/* Validates the registry (sh_registry_check) and draws home. False, with `err`, on a clash. */
bool shell_start(char *err, size_t err_len);

/* Thread-safe: queue for the UI task. */
void shell_post(const app_event_t *ev);
void shell_button(sh_button_t b);
/* A synthetic press through a virtual pointer, so a debug command reaches widgets exactly as a
 * finger would. dx != 0 drags that far to the right (negative: left) before lifting. */
void shell_touch(int x, int y, int dx);

/* UI task: drain the queue and act. Call before lv_timer_handler each pass. */
void shell_pump(void);

bool shell_open(const char *id);
void shell_home(void);
const app_t *shell_open_app(void);

/* Counts every widget and timer that exists now — the proof that closing an app freed it. */
unsigned shell_obj_count(void);
unsigned shell_timer_count(void);
