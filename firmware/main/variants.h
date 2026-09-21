#pragma once

#include <stddef.h>
#include <stdint.h>

/* The anti-boredom engine: weighted-random variant pools, per-variant cooldowns, and a
 * repetition penalty. The C port of `frontend/src/pet/variants.ts`, whose tables were written
 * as "constant data the firmware can put in flash".
 *
 * WHY IT EXISTS. Loona ships 700-1000 expressions and reviewers still describe it going on a
 * shelf, so variety is not a content problem — it is an engine problem. Vector layers five
 * independent anti-repetition systems; the three cheap ones are here.
 *
 * THE AGE-SPECIFIC TENSION, DELIBERATELY RESOLVED. Preschoolers love repetition; Elora will
 * ask for the same gag a hundred times. So recency is suppressed WITHIN a pool — you get a
 * different wiggle — and nothing here can ever refuse to react.
 *
 * Pure C: no ESP headers, no clock of its own. The caller passes `now_ms`.
 */

typedef enum {
    POOL_POKE = 0,
    POOL_COUNT,
} pool_t;

/* Selection memory, kept out of the const tables. One instance lives in the render task. */
typedef struct {
    uint32_t last_played[8]; /* per variant within the pool, ms */
    uint32_t last_fired;     /* per pool, ms, for the repetition penalty */
    int streak;
    /* "Never played" needs its own bit, because 0 ms is a REAL time here: it is boot. The web
       version gets this free from `lastPlayed[key] ?? -Infinity`; zero-initialised C does not,
       and without these a variant that had never been chosen looked like one chosen at boot —
       so early on the whole pool read as still cooling and the picker fell through to its
       "everything is cooling" branch, which is allowed to repeat. The first poke of the day
       would then repeat itself, which is precisely the boredom this file exists to prevent. */
    uint32_t played_mask; /* bit i set once variant i has actually played */
    int fired;            /* last_fired holds a real timestamp */
} pool_memory_t;

/* How long a burst has to be quiet before the penalty resets. */
#define PENALTY_RESET_MS 12000
/* The floor: a heavily-repeated action still moves, it just moves less. NEVER zero — a
   motionless response is indistinguishable from a broken one. */
#define PENALTY_FLOOR 0.35f

void variants_reset(pool_memory_t *mem);

/* Choose an action for this pool, honouring cooldowns. Returns an `action_t` (see rig.h),
   as an int so this file need not depend on the rig.

   IT CAN NEVER FAIL. If every variant is cooling down the full pool is used rather than
   returning nothing: a child who pokes the pet must always get a reaction, and a silent
   no-op is the one outcome that reads as "broken". */
int variants_pick(pool_t pool, pool_memory_t *mem, uint32_t now_ms, uint32_t rnd);

/* Magnitude multiplier for this firing (Vector's `repetitionPenalty`). The tenth identical
   poke in a burst lands softer than the first; leave it alone for twelve seconds and it is
   fresh again. Mutates `mem`, so call it exactly once per firing. */
float variants_penalty(pool_t pool, pool_memory_t *mem, uint32_t now_ms);
