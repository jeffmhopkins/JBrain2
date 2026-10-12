#include "ring.h"

#include <string.h>

#define LOAD(p, o) atomic_load_explicit(p, memory_order_##o)
#define STORE(p, v, o) atomic_store_explicit(p, v, memory_order_##o)

void ring_init(ring_t *ring, int16_t *buf, int cap)
{
    ring->buf = buf;
    ring->cap = cap;
    STORE(&ring->w, 0, relaxed);
    STORE(&ring->r, 0, relaxed);
}

void ring_reset(ring_t *ring)
{
    STORE(&ring->w, 0, release);
    STORE(&ring->r, 0, release);
}

int ring_filled(const ring_t *ring)
{
    return (int)(LOAD(&ring->w, acquire) - LOAD(&ring->r, acquire));
}

uint32_t ring_written(const ring_t *ring)
{
    return LOAD(&ring->w, acquire);
}

void ring_discard(ring_t *ring, uint32_t w)
{
    STORE(&ring->r, w, release);
}

int ring_write(ring_t *ring, const void *pcm, int bytes)
{
    if (ring->buf == NULL || pcm == NULL || bytes < (int)sizeof(int16_t)) return 0;
    const uint32_t w = LOAD(&ring->w, relaxed);
    const int room = ring->cap - (int)(w - LOAD(&ring->r, acquire));
    int want = bytes / (int)sizeof(int16_t);
    if (want > room) want = room;
    if (want <= 0) return 0;
    /* Two copies at most: the wrap is the only place this is not a straight memcpy. */
    const int at = (int)(w % (uint32_t)ring->cap);
    const int first = ring->cap - at < want ? ring->cap - at : want;
    memcpy(&ring->buf[at], pcm, (size_t)first * sizeof(int16_t));
    if (want > first) {
        memcpy(ring->buf, (const int16_t *)pcm + first,
               (size_t)(want - first) * sizeof(int16_t));
    }
    /* PUBLISHED LAST. The reader must never see a count that covers bytes not yet copied. */
    STORE(&ring->w, w + (uint32_t)want, release);
    return want * (int)sizeof(int16_t);
}

int ring_read_run(const ring_t *ring, int want, int *at)
{
    const int have = ring_filled(ring);
    int take = want < have ? want : have;
    if (take <= 0) {
        *at = 0;
        return 0;
    }
    const int from = (int)(LOAD(&ring->r, relaxed) % (uint32_t)ring->cap);
    const int run = ring->cap - from;
    *at = from;
    return take < run ? take : run;
}

void ring_advance(ring_t *ring, int n)
{
    if (n > 0) STORE(&ring->r, LOAD(&ring->r, relaxed) + (uint32_t)n, release);
}
