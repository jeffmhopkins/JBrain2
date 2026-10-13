/* Records requests the way the real service will; the sim has no controller to discard them,
 * so it reports them applied. On the device `applied` stays false until C2 has watched a
 * runtime brightness write reach this panel's glass. */
#pragma once

#include <stdbool.h>

void svc_display_init(void);
/* A quarter turn's piece buffer refused (true) or given (false), and the retry the device's UI
   task makes every pass (the sim's step makes it too). */
void svc_display_fake_no_piece(bool refuse);
void svc_display_fake_service(void);
