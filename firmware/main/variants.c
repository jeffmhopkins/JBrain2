#include "variants.h"

#include "rig.h"

typedef struct {
    int action;
    int weight; /* tenths, so the table stays integer: 0.4 in the web pool is 4 here */
    uint32_t cooldown_ms;
} variant_t;

/* One entry per thing the child can trigger repeatedly. Transcribed from
   `frontend/src/pet/variants.ts:POOLS`. `hiccup` is rare ON PURPOSE: a child who sees
   something once in three weeks talks about it for a month, which is why Vector ships
   calendar-gated animations at very low weight. */
static const variant_t POKE[] = {
    {ACT_WIGGLE, 30, 2500},
    {ACT_GIGGLE, 30, 2500},
    {ACT_BOING, 20, 4000},
    {ACT_BLUSH, 10, 9000},
    {ACT_SNEEZE, 10, 20000},
    {ACT_HICCUP, 4, 45000},
};

typedef struct {
    const variant_t *v;
    int n;
} pool_def_t;

static const pool_def_t POOLS[POOL_COUNT] = {
    [POOL_POKE] = {POKE, (int)(sizeof(POKE) / sizeof(POKE[0]))},
};

/* A variant that has never played is never cooling, however early it is. */
static int cooling(const pool_def_t *def, const pool_memory_t *mem, int i, uint32_t now_ms)
{
    if ((mem->played_mask & ((uint32_t)1 << i)) == 0) return 0;
    return (uint32_t)(now_ms - mem->last_played[i]) <= def->v[i].cooldown_ms;
}

void variants_reset(pool_memory_t *mem)
{
    if (mem == NULL) return;
    for (unsigned i = 0; i < sizeof(mem->last_played) / sizeof(mem->last_played[0]); i++) {
        mem->last_played[i] = 0;
    }
    mem->last_fired = 0;
    mem->streak = 0;
    mem->played_mask = 0;
    mem->fired = 0;
}

int variants_pick(pool_t pool, pool_memory_t *mem, uint32_t now_ms, uint32_t rnd)
{
    if ((unsigned)pool >= (unsigned)POOL_COUNT) return ACT_WIGGLE;
    const pool_def_t *def = &POOLS[pool];
    if (def->n <= 0 || mem == NULL) return ACT_WIGGLE;

    /* Two passes over the same table: first only the variants off cooldown, and if that is
       empty, all of them. IT CAN NEVER FAIL — a poke that produces nothing is the one outcome
       a child reads as "broken", so a fully-cooling pool still answers. */
    for (int pass = 0; pass < 2; pass++) {
        int total = 0;
        for (int i = 0; i < def->n; i++) {
            if (pass == 0 && cooling(def, mem, i, now_ms)) continue;
            total += def->v[i].weight;
        }
        if (total <= 0) continue;
        int r = (int)(rnd % (uint32_t)total);
        for (int i = 0; i < def->n; i++) {
            if (pass == 0 && cooling(def, mem, i, now_ms)) continue;
            r -= def->v[i].weight;
            if (r < 0) {
                mem->last_played[i] = now_ms;
                mem->played_mask |= (uint32_t)1 << i;
                return def->v[i].action;
            }
        }
    }
    return def->v[0].action;
}

float variants_penalty(pool_t pool, pool_memory_t *mem, uint32_t now_ms)
{
    (void)pool;
    if (mem == NULL) return 1.0f;
    const uint32_t gap = now_ms - mem->last_fired;
    const int first = !mem->fired;
    mem->last_fired = now_ms;
    mem->fired = 1;
    if (first || gap > PENALTY_RESET_MS) {
        mem->streak = 0;
        return 1.0f;
    }
    mem->streak++;
    float mag = 1.0f;
    for (int i = 0; i < mem->streak && i < 32; i++) mag *= 0.82f;
    return mag < PENALTY_FLOOR ? PENALTY_FLOOR : mag;
}
