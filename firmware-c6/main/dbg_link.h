/* The panel's half of the debug relay (plan §4, wave C2d): collect a request from the box, run
 * it through svc_debug's dispatcher, and answer.
 *
 * Two tasks, one rule each. The MAIN task owns the box connection: it collects the request and
 * posts the answer, one HTTPS request at a time, like every other box call. The UI task owns
 * LVGL: every command runs there (open, inject and screenshot touch widgets; snapshot reads the
 * shell), handed over through a one-slot job the UI loop checks each pass. The main task waits
 * for it with a timeout and answers "ui busy" rather than wait on a stuck UI. On a boot with no
 * UI (after a fault, or a UI that stopped) nothing else touches LVGL, so the main task runs the
 * command itself — which is when a snapshot and the logs matter most. */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "dbg_core.h"

/* Main task, after the rollback gate and before the UI starts: binds the device's commands. */
void dbg_link_start(void);

/* Main task: one pickup, and when a request came, run and answer it. FAIL only when the box did
 * not answer the pickup; anything after that is the request's own result. Bounded: the pickup,
 * the UI's turn and the answer each have their own timeout. */
dbg_poll_t dbg_link_poll(void);

/* UI task, once per loop pass, never inside a render: runs a request the main task handed over. */
void dbg_link_ui_service(void);

/* `dbg=…` (dbg_stats_note). */
void dbg_link_note(char *out, size_t len);
