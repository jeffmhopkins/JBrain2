/* A talk to Jerv or Dave on the box connection (README "What 0.0.10 adds"): the recording goes up as
 * a chunked POST while KEY is held, and the reply — what was heard, what Jerv said, then its
 * speech — is read as it arrives, the speech straight into the speaker's ring. Since 0.0.16 the
 * same exchange carries a voice message to a twin (the recording up the same way, its digest
 * after it, the box's id and transcript back) and a play of one of hers (a GET whose audio goes
 * through the same ring and speaker start as a reply).
 *
 * MAIN TASK ONLY, like every box request: it is the one owner of the HTTPS session, one request
 * at a time. A talk outranks everything else on it — the manifest, telemetry and the debug
 * pickup wait for it — and is bounded: a minute of recording, 130 s for the box to answer,
 * then the reply's speech at the speaker's pace. After a talk the update loop always makes its
 * regular pass, so a check or a heartbeat that fell due during it runs next. The UI never
 * waits on any of it: it reads the talk's state and nothing else. */
#pragma once

#include <stdbool.h>

/* Runs the exchange a hold started, if one did. True when it ran (its outcome is the talk's
 * state, not this answer); false at once when no hold is waiting. */
bool talk_link_run(void);
