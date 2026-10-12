/* The owner's voice post with the twins' panels (0.0.16, owner 2026-10-12: "Am I able to receive
 * voice messages now from the kids' other jpanel?"). The person threads hold the box's last
 * messages with each twin (backend `api/panel_messages.py`); a play streams one through the
 * speaker and marks it played on the box, so the PWA shows it played too; KEY in a thread sends
 * her a voice message through svc_audio.
 *
 * Every call here is the UI task's and returns at once: requests go to the main task's network
 * loop, answers are applied on the UI task's next svc_post_service and announced as the thread's
 * MSG_ARRIVED. The badges are svc_messages' unplayed counts, as before. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* The box keeps 30 s of a message: the talk ring's cap in a person's thread. */
#define POST_MAX_MS 30000u

/* A thread view opened: ask the box for that thread now, rather than at the next signal. */
void svc_post_open(const char *thread);
/* The box has answered for this thread at least once since it was bound. */
bool svc_post_loaded(const char *thread);
