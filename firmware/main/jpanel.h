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

/* HOW MANY OF THE WAITING MESSAGES THIS PANEL KNOWS THE SENDER OF, one by one, as opposed to
   merely counting. `JPANEL_QUEUE_MAX` in `backend/src/jbrain/api/jpanel.py` is the same number and
   a test pins that the two agree — a contract written down twice, like the nudge port.

   EIGHT, AND THE COST IS THE POINT: eight names at 32 bytes is 256 bytes of static RAM on a
   board whose internal heap is the scarce thing, and `jpanel_waiting()` still reports the true
   total. A child swiping past the eighth unheard message is not a case worth more. */
#define JPANEL_QUEUE_MAX 8

/* Fetch a waiting message and play it. `at` is a POSITION IN THE QUEUE — 0 is the oldest, which
   is what this route always served and what every automatic path still asks for. False when
   nothing is in a flight-able state.

   A POSITION, NOT AN ID, because the panel does not learn a message's id until it plays one:
   `X-Jpanel-Id` arrives with the audio. The queue is ordered identically on both sides by the same
   predicate, so the index the poll handed back is the index the box resolves. It is only as fresh
   as that poll — a message acknowledged in between shifts the queue under the finger — and the
   box answers an index past the end with "nothing here" rather than an error, so the worst case is
   a neighbouring message rather than a dead press.

   The box is told it was played only once the speaker has actually finished, which is what makes a
   message survive a reboot mid-playback rather than being lost by being handed over. */
bool jpanel_play_at(int at);

/* The oldest one — `jpanel_play_at(0)`, kept because most callers mean exactly that. */
bool jpanel_play_next(void);

/* How many messages are waiting, and who the oldest is from. `from` may be NULL. The name is
   copied out under a lock-free snapshot rule: the poll writes it before it raises the count,
   so a reader that sees a count sees a name that goes with it. */
int jpanel_waiting(char *from, size_t cap);

/* WHERE A REPLY TO THE LAST MESSAGE SHOULD GO. Not derived from the name the child hears —
   that is the owner's to change — but from `X-Jpanel-From-Kind`, which the box sends because
   it is the only side that knows. `JPANEL_TO_PANEL` on a box too old to say. */
jpanel_to_t jpanel_in_from(void);

/* Whether the OLDEST WAITING message is from the owner, so the badge can draw their face. From
   the poll's `from_owner`, for the same reason `jpanel_in_from` exists: the name is the owner's
   to change and the kind is not. False on a box too old to say. */
bool jpanel_waiting_from_dad(void);

/* ONE ENTRY OF THE QUEUE, so the glass can draw the message a finger has SELECTED rather than
   only the one it would play next. `at` is the same index `jpanel_play_at` takes. False when the
   queue is shorter than that, or when the box is too old to send a queue at all — in which case
   only index 0 answers, from the two fields above, because those are all such a box sends.

   `from` may be NULL. Same snapshot rule as `jpanel_waiting`: the poll writes the entries before
   it raises the count, so a reader that sees a count sees entries that go with it. */
bool jpanel_waiting_at(int at, char *from, size_t cap, bool *from_dad);

/* THE OTHER PANEL'S NAME, learned from the poll, "" when the box did not name one. Written
   into `out` (up to `cap`), returns its length — so a caller can fall back in one test.
   Empty is the normal answer on a box with one panel, or with three: it is only filled where
   there is EXACTLY ONE other unit, which is the same rule addressing already follows. */
int jpanel_sibling(char *out, size_t cap);

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

/* THE PUSH STREAM. `jpanel_push_live()` is what lets `main.c` relax the settings cadence: a box
   that can say when something changed does not need to be asked every three seconds, and the
   relaxation is what keeps the concurrent-TLS count the same as before rather than one higher.
   The counters ride in telemetry because a push channel that silently stopped working looks
   exactly like a quiet house. */
/* IS A MESSAGE ON ITS WAY TO THE SPEAKER. Distinct from `jpanel_state() == JPANEL_BUSY`, which
   also covers SENDING — using that to decide when to show the playback controls put a pause
   button and a sender's face over an outgoing message, where nothing was playing and nothing
   would answer a press. */
bool jpanel_fetching(void);

bool jpanel_push_live(void);
unsigned jpanel_push_events(void);
unsigned jpanel_push_drops(void);
