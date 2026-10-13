/* Auto-rotate as an app sees it (0.0.18, plan §2b). The service reads the QMI8658 while the
 * screen is on and auto-rotate is switched on, and turns the content when the panel has been
 * turned. The switch itself is svc_settings' SET_AUTO_ROTATE. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* The tilt sensor answered at boot. False: the panel stays upright and Settings says so. */
bool svc_orient_available(void);
/* The panel's rotation now: quarter turns clockwise from upright (disp_logic.h). */
uint8_t svc_orient_rotation(void);
/* Touch calibration holds whatever is showing, so the glass does not turn under its targets. */
void svc_orient_hold(bool hold);
