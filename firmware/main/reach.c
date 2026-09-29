#include "reach.h"

#include <stddef.h>

typedef struct {
    int fails;       /* cumulative since boot */
    int streak;      /* consecutive, 0 after any success */
    const char *err; /* last reason, never cleared by a success — see reach.h */
    uint32_t at_ms;  /* when that last failure was */
    bool ever;       /* has this path ever failed at all */
} row_t;

static row_t s_row[REACH_PATHS];
/* When any path last succeeded. `s_seen` is meaningless until `s_ever_ok`, and the two are
   separate rather than a sentinel value because 0 is a real millisecond: a panel whose first
   success lands in its first millisecond of uptime must not read as "never". */
static uint32_t s_seen_ms;
static bool s_ever_ok;

static bool bad(reach_path_t path)
{
    return path < 0 || path >= REACH_PATHS;
}

void reach_ok(reach_path_t path, uint32_t now_ms)
{
    if (bad(path)) return;
    s_row[path].streak = 0;
    s_seen_ms = now_ms;
    s_ever_ok = true;
}

void reach_fail(reach_path_t path, const char *why, uint32_t now_ms)
{
    if (bad(path)) return;
    row_t *r = &s_row[path];
    r->fails++;
    r->streak++;
    /* A caller with nothing to say must not blank a reason an earlier failure gave. */
    if (why != NULL && why[0] != '\0') r->err = why;
    r->at_ms = now_ms;
    r->ever = true;
}

void reach_faults(reach_path_t path, int *fails, const char **err, uint32_t *ago_ms,
                  uint32_t now_ms)
{
    if (bad(path)) return;
    const row_t *r = &s_row[path];
    if (fails != NULL) *fails = r->fails;
    if (err != NULL) *err = r->err != NULL ? r->err : "";
    /* Unsigned throughout, so this stays right across the ~49-day rollover of a millisecond
       clock — the same argument as `cadence_retry_due`, and it matters more here: a panel that
       has been up seven weeks is exactly the one whose faults nobody has looked at. */
    if (ago_ms != NULL) *ago_ms = r->ever ? (uint32_t)(now_ms - r->at_ms) : 0;
}

int reach_streak(reach_path_t path)
{
    return bad(path) ? 0 : s_row[path].streak;
}

uint32_t reach_quiet_ms(uint32_t now_ms)
{
    return s_ever_ok ? (uint32_t)(now_ms - s_seen_ms) : REACH_NEVER;
}

void reach_reset_for_test(void)
{
    for (int i = 0; i < REACH_PATHS; i++) s_row[i] = (row_t){0};
    s_seen_ms = 0;
    s_ever_ok = false;
}
