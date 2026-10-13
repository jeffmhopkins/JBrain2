/* The push notices' box requests (0.0.19, svc_notice): each notice collected as a JN1 stream
 * straight into its file on `storage`, and the states the glass owes the box. Run on the main
 * task's loop beside the conversations' and the voice post's, one request at a time over the kept
 * session; a notice's sound plays from its file through talk_link.c. */
#pragma once

#include <stdbool.h>

/* Collects what the pickup said waits (or a torn stream again), sends the states owed, and
 * deletes finished notices' files. True when a request went to the box. */
bool notice_link_run(void);
