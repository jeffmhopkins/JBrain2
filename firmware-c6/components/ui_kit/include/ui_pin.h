/* When a conversation jumps to its newest message: on opening, and when one arrives — never
 * on a redraw, which would yank the view down while someone is reading up the history. Pure,
 * so Jerv and Thread share one rule and the host suite holds it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool seen;
    uint32_t last_id;
} ui_pin_t;

/* True when the view should pin now; remembers `last_id` either way. */
bool ui_pin_should(ui_pin_t *p, uint32_t last_id);
