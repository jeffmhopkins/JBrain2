/* An agent's conversations with the box (0.0.15): `GET /endpoint/convo/{agent}/recent`, `POST
 * …/select` then `GET …/messages`, and `GET …/messages?limit=0` for the title. MAIN TASK ONLY,
 * like every box request: one at a time on the kept session, after a talk. The answers go to
 * svc_convo, which the UI task applies; the glass never waits on any of it. */
#pragma once

#include <stdbool.h>

/* Runs the request the glass is waiting on, if there is one. True when one ran. */
bool convo_link_run(void);
