/* The voice post's box requests (0.0.16, svc_post): a twin's thread, and a message heard. Run on
 * the main task's loop beside the conversations' (convo_link.h), one request at a time over the
 * kept session; the send and the play ride the talk's own connection (talk_link.c). */
#pragma once

#include <stdbool.h>

/* Runs the waiting request, if any. True when one ran. */
bool post_link_run(void);
