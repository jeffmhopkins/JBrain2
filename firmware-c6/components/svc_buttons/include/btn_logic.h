/* The button service's decisions, pure so the host suite holds them: debouncing a sampled line,
 * and turning two possible PWR sources into one press. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* A level counts once it has read the same for this many consecutive samples (10 ms apart):
 * the 0.0.3 probe saw clean ~0.4 s taps, and 30 ms is well under any deliberate press. */
#define BTN_DB_SAMPLES 3

typedef struct {
    bool pressed; /* the debounced state */
    uint8_t run;  /* consecutive samples disagreeing with it */
} btn_db_t;

/* Feeds one sample (true = reads pressed). Returns +1 on a debounced press, -1 on a release,
 * 0 otherwise. */
int btn_db_step(btn_db_t *b, bool pressed_now);

/* BOOT's two meanings (owner, 2026-10-12). Held this long it opens the Clock — once, the moment
 * the threshold passes, while still held, so the owner sees it happen without letting go. A
 * shorter press is home/back, sent on the RELEASE: only then is it known not to be a long one.
 * 800 ms is well clear of the 0.0.3 probe's ~0.4 s deliberate taps and short of feeling stuck. */
#define BTN_LONG_MS 800

typedef enum { BTN_HOLD_NONE, BTN_HOLD_SHORT, BTN_HOLD_LONG } btn_hold_ev_t;
typedef struct {
    bool down, fired;
    uint32_t since;
} btn_hold_t;

/* Fed every poll with btn_db_step's result for the button (so a glitch the debounce swallowed
 * is nothing here either) and the time. */
btn_hold_ev_t btn_hold_step(btn_hold_t *h, int edge, uint32_t now_ms);

/* PWR may reach the chip two ways and which one works is what 0.0.4 measures (HARDWARE.md
 * "Measured on the board"): GPIO18 through the BSS138, and the AXP2101's latched key press.
 * When both work, one press arrives twice, a poll apart; the second inside this window is the
 * same press, not a toggle back. */
#define PWR_MERGE_MS 400

typedef enum { PWR_SRC_GPIO18 = 1, PWR_SRC_PEK = 2 } pwr_src_t;

typedef struct {
    uint32_t last_ms;
    bool any;
    uint8_t seen; /* every source that has ever reported a press: the measurement */
} pwr_merge_t;

/* True when this report is a new press. */
bool pwr_merge_accept(pwr_merge_t *m, pwr_src_t src, uint32_t now_ms);
/* "none", "gpio18", "pek" or "pek+gpio18". */
const char *pwr_seen_name(uint8_t seen);
