#pragma once

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
 * One writer, one reader, no lock. The writer publishes `w` only after its bytes are copied
 * and the reader advances `r` only after its samples are played, so neither can see a cursor
 * that covers memory the other has not finished with. */
typedef struct {
    int16_t *buf;
    int cap; /* samples */
    volatile uint32_t w;
    volatile uint32_t r;
} ring_t;

void ring_init(ring_t *ring, int16_t *buf, int cap);

/* Back to empty. The caller must know nothing is reading — `audio.c` claims the speaker
   first, which is what makes that true. */
void ring_reset(ring_t *ring);

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
