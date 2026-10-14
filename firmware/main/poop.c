#include "poop.h"

#include <math.h>
#include <string.h>

/* Full tilt is a gravity's worth of push along the floor, in figure units a second squared;
   friction bleeds the speed off as it slides; a wall takes half of it back. */
#define POOP_ACCEL 900.0f
#define POOP_FRICTION 2.2f
#define POOP_BOUNCE 0.45f
/* Below this tilt it does not start moving: a panel on a table reads a little off level, and a
   poop that creeps across a still screen looks broken. Once moving, friction decides. */
#define POOP_STICK 0.08f
#define POOP_REST 6.0f

/* Every kind differs from every other in at least two of shape, face, stink and flies, and
   the colours are a child's: brown, gold, a sickly green, a unicorn's pink. */
static const poop_look_t POOP_LOOKS[POOP_KINDS] = {
    {POOP_SWIRL, POOP_SMILE, 2, 0, 0x8A, 0x55, 0x2B},   /* the classic */
    {POOP_SWIRL, POOP_COOL, 1, 0, 0xD9, 0xA4, 0x2E},    /* golden, and knows it */
    {POOP_BLOB, POOP_SLEEPY, 2, 2, 0x6E, 0x44, 0x22},   /* a sleepy puddle, flies already */
    {POOP_PELLETS, POOP_SHOCKED, 1, 0, 0x7A, 0x4C, 0x26}, /* bunny droppings, surprised */
    {POOP_TOWER, POOP_TONGUE, 3, 1, 0x93, 0x5C, 0x2E},  /* a tower, blowing a raspberry */
    {POOP_LOG, POOP_GRUMPY, 3, 3, 0x5A, 0x3A, 0x1E},    /* a grumpy log, swarming */
    {POOP_SWIRL, POOP_QUEASY, 3, 2, 0x7C, 0x8A, 0x2E},  /* green: something he ate */
    {POOP_TOWER, POOP_WINK, 0, 0, 0xF2, 0x8C, 0xC8},    /* unicorn: pink, and no smell at all */
};

const poop_look_t *poop_look(int kind)
{
    return &POOP_LOOKS[(unsigned)kind % POOP_KINDS];
}

void poop_reset(poop_t *p)
{
    memset(p, 0, sizeof(*p));
    p->kind = POOP_KINDS; /* none yet: the first may be any of the eight */
}

bool poop_ate(poop_t *p, uint32_t now)
{
    if (p->n == POOP_MEALS) {
        for (int i = 1; i < POOP_MEALS; i++) p->meals[i - 1] = p->meals[i];
        p->n--;
    }
    p->meals[p->n++] = now;
    if (p->n == POOP_MEALS && now - p->meals[0] <= POOP_WINDOW_MS) {
        p->owed = true;
        p->n = 0;
        return true;
    }
    return false;
}

void poop_drop(poop_t *p, uint32_t now, float x, float half, uint32_t rnd)
{
    const unsigned prev = (unsigned)p->kind;
    p->kind = prev < POOP_KINDS ? (int)((prev + 1u + rnd % (POOP_KINDS - 1u)) % POOP_KINDS)
                                : (int)(rnd % POOP_KINDS);
    /* Clamped here and not left to the slide: on a level panel it never starts sliding, so
       whatever it lands on is where it stays. */
    if (x > half) x = half;
    if (x < -half) x = -half;
    p->owed = false;
    p->on = true;
    p->born = now;
    p->x = x;
    p->v = 0.0f;
}

void poop_step(poop_t *p, uint32_t now, float g, uint32_t dt_ms, float half)
{
    if (!p->on) return;
    if (now - p->born >= POOP_SHOW_MS) {
        p->on = false;
        return;
    }
    if (g > 1.0f) g = 1.0f;
    if (g < -1.0f) g = -1.0f;
    const float dt = (float)dt_ms / 1000.0f;
    if (fabsf(p->v) < POOP_REST && fabsf(g) < POOP_STICK) {
        p->v = 0.0f;
        return;
    }
    p->v += g * POOP_ACCEL * dt;
    p->v *= expf(-POOP_FRICTION * dt);
    p->x += p->v * dt;
    if (p->x > half) {
        p->x = half;
        p->v = -p->v * POOP_BOUNCE;
    } else if (p->x < -half) {
        p->x = -half;
        p->v = -p->v * POOP_BOUNCE;
    }
}

float poop_scale(const poop_t *p, uint32_t now)
{
    if (!p->on) return 0.0f;
    const uint32_t age = now - p->born;
    if (age >= POOP_SHOW_MS) return 0.0f;
    if (age < POOP_POP_MS) return (float)age / (float)POOP_POP_MS;
    const uint32_t left = POOP_SHOW_MS - age;
    if (left < POOP_SHRINK_MS) return (float)left / (float)POOP_SHRINK_MS;
    return 1.0f;
}
