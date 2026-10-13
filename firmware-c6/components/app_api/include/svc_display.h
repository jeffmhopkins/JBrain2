/* The panel. Calls RECORD a request; the display task, the only owner of the CO5300's io
 * handle, applies it on its next pass (firmware/README.md "One task owns the codec, one owns
 * the panel"). Nothing here may be assumed to have reached the glass: on the S3's CO5300 a
 * runtime brightness write was silently discarded, so `applied` reports only what the owner
 * task confirmed, and C2 proves it on this board before anything relies on it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

void svc_display_set_brightness(uint8_t pct);
uint8_t svc_display_brightness(void);
/* Dark is brightness 0 with the controller left initialised — never display-off, whose wake
 * path is the transition this controller family fails to come back from. */
void svc_display_set_dark(bool dark);
bool svc_display_dark(void);
bool svc_display_applied(void);

/* Auto-rotate (0.0.18): the panel turned `quarters` quarter turns clockwise from upright
 * (disp_logic.h). svc_orient records it; the UI task applies it between frames and redraws the
 * whole screen. `rotation` is what the glass shows now, which lags a request until the UI task's
 * next pass between frames. Everything that must agree with the glass (touch, calibration)
 * follows `rotation`, never the request: `on_rotated` is called on the UI task with each turn the
 * glass actually took. Since 0.0.22 a turn needs no memory of its own, so it is never refused. */
void svc_display_set_rotation(uint8_t quarters);
uint8_t svc_display_rotation(void);
void svc_display_on_rotated(void (*fn)(uint8_t quarters));
