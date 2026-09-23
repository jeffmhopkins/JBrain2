#include "ring.h"

#include <string.h>

void ring_init(ring_t *ring, int16_t *buf, int cap)
{
    ring->buf = buf;
    ring->cap = cap;
    ring->w = 0;
    ring->r = 0;
}

void ring_reset(ring_t *ring)
{
    ring->w = 0;
    ring->r = 0;
}

int ring_filled(const ring_t *ring)
{
    return (int)(ring->w - ring->r);
}

int ring_write(ring_t *ring, const void *pcm, int bytes)
{
    if (ring->buf == NULL || pcm == NULL || bytes < (int)sizeof(int16_t)) return 0;
    const int room = ring->cap - ring_filled(ring);
    int want = bytes / (int)sizeof(int16_t);
    if (want > room) want = room;
    if (want <= 0) return 0;
    /* Two copies at most: the wrap is the only place this is not a straight memcpy. */
    const int at = (int)(ring->w % (uint32_t)ring->cap);
    const int first = ring->cap - at < want ? ring->cap - at : want;
    memcpy(&ring->buf[at], pcm, (size_t)first * sizeof(int16_t));
    if (want > first) {
        memcpy(ring->buf, (const int16_t *)pcm + first,
               (size_t)(want - first) * sizeof(int16_t));
    }
    /* PUBLISHED LAST. The reader must never see a count that covers bytes not yet copied. */
    ring->w += (uint32_t)want;
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
    const int from = (int)(ring->r % (uint32_t)ring->cap);
    const int run = ring->cap - from;
    *at = from;
    return take < run ? take : run;
}

void ring_advance(ring_t *ring, int n)
{
    if (n > 0) ring->r += (uint32_t)n;
}
