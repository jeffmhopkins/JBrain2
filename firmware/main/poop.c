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

void poop_reset(poop_t *p)
{
    memset(p, 0, sizeof(*p));
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

void poop_drop(poop_t *p, uint32_t now, float x)
{
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
