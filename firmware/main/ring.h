#pragma once

#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

/* A single-producer, single-consumer sample ring — the arithmetic half of streamed playback.
 *
 * IT IS ITS OWN FILE FOR THE REASON `orient.c` AND `screen.c` ARE. What can be wrong here is
 * wrap-around and the full-versus-empty test, and both fail quietly: a wrap off by one plays a
 * fragment of the previous second in the middle of a child's message, and a full ring mistaken
 * for an empty one ends the message early. Neither shows up as a crash, and neither can be
 * seen from a panel across the house. Pure arithmetic over a caller's buffer, so the host
 * suite can hold it to the properties its comments claim.
 *
 * THE CURSORS ARE MONOTONIC SAMPLE COUNTS, NOT INDICES, and the modulo happens where they are
 * used. Two indices chasing each other cannot tell a full ring from an empty one when they
 * meet — the classic answer is to waste a slot; counting forever is exact, and the subtraction
 * is what every question below actually asks. They are `uint32_t`, so the difference stays
 * correct across the wrap of the counters themselves: at 16 kHz that happens after about three
 * days of continuous audio, and unsigned subtraction gives the right answer through it.
 *
 * One writer, one reader, no lock, and on two cores. The writer publishes `w` with release
 * only after its bytes are copied, and the reader advances `r` with release only after its
 * samples are played; each loads the other's cursor with acquire. So neither can see a cursor
 * that covers memory the other has not finished with — `volatile` alone promised that on one
 * core and nothing on two.
 *
 * NEVER RESET ON A LIVE RING. The device's ring (`stream.c`) only ever moves forward: the reader
 * discards by catching up to `w`, so a writer mid-copy can never have its cursor pulled out from
 * under it (A9). For a ring that size the cap must be a power of two, or `w % cap` jumps when the
 * counters wrap. */
typedef struct {
    int16_t *buf;
    int cap; /* samples */
    _Atomic uint32_t w;
    _Atomic uint32_t r;
} ring_t;

void ring_init(ring_t *ring, int16_t *buf, int cap);

/* Back to empty, from a quiet ring only: both cursors move, so neither side may be running. */
void ring_reset(ring_t *ring);

/* The reader's discard: everything up to `w`, a value of `ring_written` it already read, counts
   as played. A cursor read earlier, never a fresh one: what the writer published since may
   belong to the next stream. */
void ring_discard(ring_t *ring, uint32_t w);

/* How far the writer has published. */
uint32_t ring_written(const ring_t *ring);

/* Samples written but not yet played. */
int ring_filled(const ring_t *ring);

/* Copy in what fits. Returns BYTES accepted, which is less than `bytes` when the ring is full
   and zero when it is completely full — not an error, but the speaker saying "not yet". */
int ring_write(ring_t *ring, const void *pcm, int bytes);

/* The next contiguous run to play, up to `want` samples: writes its start index to `*at` and
   returns how many samples are readable from there without wrapping. Contiguous because the
   codec is handed a pointer, not a callback — a run that wrapped would have to be copied. */
int ring_read_run(const ring_t *ring, int want, int *at);

/* Say `n` samples have been played. */
void ring_advance(ring_t *ring, int n);
