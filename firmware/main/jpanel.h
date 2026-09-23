#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cfg.h"

/* VOICE POST — the panel's half of `docs/plans/JPANEL_PLAN.md`.
 *
 * A message is recorded, filed on the box, and waits until the recipient chooses to play it.
 * Nothing rings and nothing is missed by not being there, which is the only shape that works
 * for a four-year-old: an intercom needing both ends present would be a toy that mostly
 * fails.
 *
 * ITS OWN TASK, for the same reason `talk.c` has one: every call here is an HTTPS round trip
 * measured in seconds (a send is transcribed by whisper on the way in), and the render task
 * must never block. The renderer asks questions that return immediately — `jpanel_state()`,
 * `jpanel_waiting()` — and acts on the answers a frame later.
 *
 * THE POLL IS PART OF THIS FILE, NOT OF THE RENDERER. The manifest poll is ~15 minutes, far
 * too slow for *"my sister just sent me something"*, so this task asks `GET /jpanel/waiting`
 * on its own ~30 s cadence whenever it is otherwise idle. A poll that lived in the render
 * loop would be a network call on the frame clock.
 */

typedef enum {
    JPANEL_TO_PANEL = 0, /* the other panel — the twin's unit */
    JPANEL_TO_DAD,       /* the owner's PWA */
} jpanel_to_t;

typedef enum {
    JPANEL_IDLE = 0, /* nothing in flight */
    JPANEL_BUSY,     /* sending or fetching */
    JPANEL_SENT,     /* the box filed it */
    JPANEL_PLAYING,  /* a fetched message was handed to the speaker */
    JPANEL_NOBODY,   /* 409: `to=panel` with no single other panel — say so out loud */
    JPANEL_FAILED,   /* it did not go; never fail silently */
} jpanel_state_t;

/* Starts the task. False means no task, and every call below then refuses. */
bool jpanel_start(const cfg_t *cfg);

/* Hand over a finished recording for `to`. The buffer must stay valid until the state leaves
   BUSY, which `audio.c` guarantees: it is not reused until the next `audio_capture_open()`.
   False when something is already in flight. */
bool jpanel_send(const int16_t *pcm, size_t bytes, jpanel_to_t to);

/* Fetch the oldest waiting message and play it. False when nothing is in flight-able state.
   The box is told it was played only once the speaker has actually finished, which is what
   makes a message survive a reboot mid-playback rather than being lost by being handed over. */
bool jpanel_play_next(void);

/* How many messages are waiting, and who the oldest is from. `from` may be NULL. The name is
   copied out under a lock-free snapshot rule: the poll writes it before it raises the count,
   so a reader that sees a count sees a name that goes with it. */
int jpanel_waiting(char *from, size_t cap);

/* Where the last request got to, and back to idle once the renderer has shown the outcome. */
jpanel_state_t jpanel_state(void);
void jpanel_clear(void);

/* A RUN IS PLAYING — one tap, every waiting message, oldest first. */
bool jpanel_running(void);

/* End a run on a finger: cut the message sounding and fetch no more. What was not played is
   still unplayed on the box, so the pop-up comes back for it. */
void jpanel_stop(void);

/* Who the message in the buffer came from; "" when nothing has been played. */
const char *jpanel_last_from(void);

/* Replay what was last played, without asking the box again. Backs the repeat icon: the
   audio is still in this module's buffer, so a replay costs nothing and works when the box
   is unreachable. False when there is nothing to replay. */
bool jpanel_replay(void);

/* Make the next poll happen now rather than up to 30 s from now. Called after a send and
   after a play, so the pop-up and the badge reflect what just happened. */
void jpanel_poll_soon(void);
