/* Records requests the way the real service will; the sim has no controller to discard them,
 * so it reports them applied. On the device `applied` stays false until C2 has watched a
 * runtime brightness write reach this panel's glass. */
#pragma once

#include <stdbool.h>

void svc_display_init(void);
/* A requested turn held back (true) as the device holds it until its UI task's next pass between
   frames, or let through (false) at the next svc_display_fake_service (the sim's step calls it). */
void svc_display_fake_hold(bool hold);
void svc_display_fake_service(void);
