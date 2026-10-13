/* The panel's settings from the PWA (0.0.21): the main task fetches what the box holds for it
 * when the pickup says something waits, and the UI task applies it (settings_sync.h has the
 * rules). A request that arrives while the panel is offline waits on the box; one fetched and not
 * yet applied when the panel reboots is fetched again, because the panel still reports the old
 * value and the box keeps it until a report settles it. */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* The pickup's `X-Panel-Settings` (main task; "" when it carried none). */
void set_link_signal(const char *header);
/* Main task, in the loop's pass: fetches the queue when one is due. True when it made a request. */
bool set_link_run(void);
/* UI task, once per loop pass: applies a fetched queue, brightness on the glass at once. */
void set_link_ui_service(void);
/* `sync=v:N,fetch:N,applied:N,kept:N,last:<status>` for telemetry. */
void set_link_note(char *out, size_t len);
