/* Records requests the way the real service will; the sim has no controller to discard them,
 * so it reports them applied. On the device `applied` stays false until C2 has watched a
 * runtime brightness write reach this panel's glass. */
#pragma once

void svc_display_init(void);
