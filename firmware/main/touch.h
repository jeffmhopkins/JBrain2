#pragma once

#include <stdbool.h>

/* Open the touch controller. False means no panel answered; the caller carries on without it. */
bool touch_start(void);

/* TAKE THE OLDEST UNREAD PRESS, if there is one. False when nothing is waiting.
 *
 * THIS IS A QUEUE NOW, AND THAT IS THE WHOLE FIX FOR "the touch response is awful".
 *
 * It used to be a sample: `touch_tapped()` read the controller once per render-loop pass and
 * reported whether a finger had just arrived. That made touch sampling as fast as the FRAME
 * RATE, and the render loop is a 40 ms delay PLUS composing a face PLUS pushing 322 KB over
 * QSPI — so the real interval was 80-100 ms and it stretched further whenever there was more
 * to draw. A press that began and ended between two passes was not merely late: it had never
 * happened, because an edge is only visible to the poll that straddles it.
 *
 * Children jab. That is the tap that was being lost, and losing it more often when the screen
 * was busy is exactly why it felt worst when a notification was up.
 *
 * So the controller is sampled on its own task at `TOUCH_SAMPLE_MS` regardless of what the
 * renderer is doing, and every edge is LATCHED with the point it landed on. The renderer then
 * drains presses rather than catching them. A frame that takes 200 ms now costs a press its
 * latency, never its existence.
 *
 * `x` and `y` are that press's own point, not "wherever the finger was last seen" — two
 * presses queued behind a slow frame must not both report the second one's coordinates. */
bool touch_take(int *x, int *y);

/* How often the sampling task reads the controller. Fast enough that the shortest press a
   child can make spans several reads, and cheap: one 5-byte I2C transaction, on a bus whose
   driver serialises per-bus so this is safe beside the render task's IMU and PMU reads. */
#define TOUCH_SAMPLE_MS 15

/* Whether a finger is down as of the last `touch_tapped()` — the LEVEL, which the edge
   deliberately hides. Cached rather than re-read so a caller wanting both does not cost two
   I2C transactions per poll. Only the maintenance hold uses it; the product gesture is still
   the edge (§10.4p). */
bool touch_is_down(void);

/* Where the last press landed, in panel pixels. Captured on the edge and held, so a caller
   that asks after the finger has lifted still gets the point that was pressed. Prefer the
   point `touch_take` hands back; this remains for the diagnostic marker, which wants the most
   recent point rather than the oldest unread one.

   THE ORIENTATION IS NOT ASSUMED. The CST820 reports in its own frame, and whether that
   matches the display's is exactly the sort of thing §10.4af spent three releases getting
   wrong about the accelerometer by reasoning instead of measuring. So the raw values go out
   in telemetry and a marker is drawn where the firmware THINKS the finger was — if the dot
   is not under the finger, the mapping is wrong and the numbers say how. */
void touch_point(int *x, int *y);
