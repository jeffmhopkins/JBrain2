#pragma once

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
 * updates ("a pushed update is not urgent"). */

/* How long to sleep before the next short-cadence pass, given how much of the long period is
 * left. Never longer than `remaining_ms`, so the long work keeps its own schedule instead of
 * drifting later by up to one slice every time round; never zero while anything remains, so a
 * caller looping on it always progresses. A `period_ms` of zero means "no short cadence" and
 * sleeps out the remainder in one go. */
uint32_t cadence_slice_ms(uint32_t remaining_ms, uint32_t period_ms);
