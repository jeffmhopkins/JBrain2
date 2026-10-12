/* The device's side of svc_box.h. The box connection belongs to the update loop on the main
 * task; the UI never makes a request. Update now only raises a flag and wakes that loop, which
 * reports what it is doing back through svc_box_set_phase for the Firmware card to show. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_box.h"

/* `version` must outlive the program (the app descriptor's). `wake` is called from the UI task
 * when Update now is pressed; it must only notify. */
void svc_box_start(const char *version, void (*wake)(void));
/* Main task: true once per press. */
bool svc_box_take_update_request(void);
/* Main task: true once per svc_box_report_now (the debug `report_now`). */
bool svc_box_take_report_request(void);
/* Main task. `next` and `err` may be NULL to leave them as they are. */
void svc_box_set_phase(fw_phase_t phase, const char *next, uint8_t pct, const char *err);
/* Main task: a request reached the box. */
void svc_box_reached(void);
