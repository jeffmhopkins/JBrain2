#pragma once

#include <stdbool.h>
#include <stdint.h>

/* WHY THE PANEL COULD NOT REACH ITS BOX, KEPT UNTIL SOMEBODY CAN ASK.
 *
 * On 2026-09-29 a panel stopped talking to the box entirely. The owner pressed the pet, got the
 * red dash immediately, and asked why. The box could answer almost nothing: its access log showed
 * the last contact of any kind at 04:24:51 and then half an hour of silence, because A REQUEST
 * THAT NEVER LEFT THE PANEL LEAVES NO TRACE ON THE SERVER. The reason existed — `talk.c` logs
 * `"connect failed"` — on a serial console that does not exist in a four-year-old's bedroom
 * (CLAUDE.md #10).
 *
 * THE CATCH-22 IS THE WHOLE DESIGN PROBLEM. Everything the box learns about a panel arrives by
 * telemetry, and telemetry is a POST to the box — so the one moment worth reporting is the one
 * moment the panel cannot report. That makes this module's job RETROSPECTIVE: hold what happened
 * during the outage so the first report after it says what went wrong, rather than describing a
 * panel that is, by then, visibly fine.
 *
 * Which is why nothing here is ever cleared by a success. `s_set_err`, the field this replaces,
 * was: *"Cleared on success, so what is reported is the CURRENT state rather than the worst thing
 * that ever happened."* Reasonable, and it made the field useless — reports only happen at the end
 * of a cycle, right after a fetch that succeeded, so the string was empty in EVERY report a panel
 * ever sent. The 03:27 report says `set_fails: 3, set_err: ""`: three failures, no reason, and the
 * count was the only evidence the failures had happened at all. A count says something is wrong; a
 * name says what. So the name survives, and `ago_ms` says whether it is current — which is the
 * question clearing was trying to answer, asked in the one way that does not destroy the answer.
 *
 * PURE, AND ON THE HOST SUITE, for `cadence.c`'s reason: the loop this serves cannot be tested on
 * a desk, and bookkeeping that reports the wrong thing is worse than no bookkeeping. `now_ms` is a
 * parameter rather than a call into `esp_timer` so the tests can move time. */

typedef enum {
    /* `GET /endpoint/settings`, the three-second poll. Every knob rides it — volume, brightness,
       the appearance, the report-now counter, and since 0.3.33 the waiting count. */
    REACH_SETTINGS = 0,
    /* `GET /jpanel/waiting`, the thirty-second poll on the jpanel task. A SEPARATE TASK, which is
       why it is a separate row: on 2026-09-29 this one kept working for an hour after settings
       stopped, and that difference was the first real clue about where the fault was. */
    REACH_POLL,
    /* `POST /endpoint/converse`, the conversation. The one the child is waiting on. */
    REACH_TALK,
    /* `POST /jpanel/send`, a child's recorded message to her father or her sister.

       THE ONE WHERE FAILING SILENTLY COSTS THE MOST. The other three fail and something is
       merely late or unanswered; this one fails and a message a four-year-old recorded for
       somebody is gone, with no row on the box and nothing to look at. The owner: *"sometimes
       when we're in the menu for playback and they hit the green reply button and record a
       message, it doesn't actually get sent and doesn't show up in my inbox on the pwa."*
       Every send that REACHED the box in that window succeeded — so the failures never arrived,
       which means the only possible record of them is this one. */
    REACH_SEND,
    REACH_PATHS,
} reach_path_t;

/* A path succeeded. Resets that path's consecutive-failure streak and stamps the last-contact
   clock that `reach_quiet_ms` reports. */
void reach_ok(reach_path_t path, uint32_t now_ms);

/* A path failed, with a SHORT STABLE reason — `esp_err_to_name`, an HTTP status, or one of the
   fixed strings the callers use. Stored by pointer, never copied, so it must outlive the call:
   every caller passes a literal or `esp_err_to_name`'s static table. */
void reach_fail(reach_path_t path, const char *why, uint32_t now_ms);

/* What to report for one path. Every out-parameter is optional.
     `fails`   — cumulative since boot, never reset.
     `err`     — the LAST reason, kept until the next failure replaces it. "" if never.
     `ago_ms`  — how long since that last failure; 0 when there has never been one, which is
                 unambiguous because `fails` is 0 in exactly that case. */
void reach_faults(reach_path_t path, int *fails, const char **err, uint32_t *ago_ms,
                  uint32_t now_ms);

/* Consecutive failures on this path, back to 0 on any success. This is the BACKOFF's input, not a
   diagnostic: `fails` above is what gets reported. */
int reach_streak(reach_path_t path);

/* HOW LONG THE BOX HAS BEEN QUIET, across every path — the single number that would have answered
   the whole of 2026-09-29 at a glance. `REACH_NEVER` when no path has ever succeeded, which is a
   different fault (a panel that has never been able to talk to its box) from one that has gone
   silent, and must not read as "zero seconds ago". */
uint32_t reach_quiet_ms(uint32_t now_ms);

#define REACH_NEVER UINT32_MAX

/* Drop everything. The firmware never calls this — a panel's counters are cumulative since boot
   by design, and that is the whole point of them. It exists because the host suite runs every
   case against the same module-level state, and a test that inherited the previous test's streak
   would pass or fail on its neighbours. */
void reach_reset_for_test(void);
