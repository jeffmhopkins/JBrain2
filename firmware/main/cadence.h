#pragma once

#include <stdbool.h>
#include <stdint.h>

/* TWO CADENCES OUT OF ONE SLEEP, and the reason it needs its own file is that the loop it
 * serves cannot be tested at all. `app_main`'s update loop runs on a panel; this arithmetic
 * runs anywhere, and the property worth pinning — that slicing a period never makes the long
 * work late and never stalls — is exactly the kind the host suite exists for.
 *
 * The problem it solves: settings used to be a PASSENGER on the update cycle. `apply_settings`
 * ran only after the fifteen-minute manifest fetch, so a knob turned in the PWA took up to a
 * quarter of an hour to reach the glass, while a message took thirty seconds. Nobody chose
 * that number for settings — it was inherited from an interval whose own comment is about
 * updates ("a pushed update is not urgent"). The poll is three seconds now and answers the
 * update question too, which is what makes the backoff below necessary. */

/* How long to sleep before the next short-cadence pass, given how much of the long period is
 * left. Never longer than `remaining_ms`, so the long work keeps its own schedule instead of
 * drifting later by up to one slice every time round; never zero while anything remains, so a
 * caller looping on it always progresses. A `period_ms` of zero means "no short cadence" and
 * sleeps out the remainder in one go. */
uint32_t cadence_slice_ms(uint32_t remaining_ms, uint32_t period_ms);

/* WHETHER A RETRY OF SOMETHING THAT FAILED IS DUE, which the fast poll made necessary rather
 * than merely tidy. `ota_apply` has no backoff and no attempt cap of its own — `ota_tries` is a
 * reported counter, not a limiter — and the install loop fires whenever the served version
 * differs from the running one. At fifteen minutes a failing install retried four times an
 * hour; at a three-second poll it would retry twelve hundred times an hour, each pulling a
 * 3.25 MB image: about 65 MB a minute per panel, of a failure the plan itself calls silent and
 * "indistinguishable from a panel nobody offered an update to".
 *
 * So the NOTICING is fast and the RETRYING is not. A version that has genuinely changed is
 * installed within a poll; an install that failed waits out the backoff before trying again.
 *
 * `last_ms` of 0 means never attempted. Wrap-safe on purpose: the subtraction is unsigned, so a
 * millisecond clock that has rolled over (about 49 days) still yields a sane elapsed time
 * instead of blocking retries until it rolls again. */
bool cadence_retry_due(uint32_t now_ms, uint32_t last_ms, uint32_t backoff_ms);
