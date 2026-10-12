/* The alarm sounds, synthesised a chunk at a time from a sine table: no audio files, no libm, no
 * float (the C6 has no FPU). Pure C, host-tested: the audio task fills its 20 ms chunk from here
 * and writes it to the ES8311 as it writes a reply's speech. */
#pragma once

#include <stdint.h>

/* Each sound is a pattern that repeats: */
/* chime — two bell strikes (E6, then B5) with a decaying ring, then quiet; 2 s. */
/* beeps — four short A6 beeps, then a pause; 1.4 s. */
/* rise  — a sweep from A4 up to E6, then a pause; 2 s. */
#define ALARM_SOUND_RATE 16000
/* The volume climbs from ALARM_RAMP_FLOOR to full over this, so the first seconds wake rather than
 * startle and a ring left going still gets loud. */
#define ALARM_RAMP_MS 30000u
#define ALARM_RAMP_FLOOR_Q15 4915 /* 15 % */
/* Full scale for the sounds: -6 dBFS, the headroom the speaker path wants. */
#define ALARM_PEAK 16384

typedef struct {
    uint8_t sound;
    uint32_t frame; /* frames since the start of the ring */
    uint32_t phase; /* the oscillator, a 32-bit turn */
} alarm_synth_t;

void alarm_synth_init(alarm_synth_t *s, uint8_t sound);
/* The next `frames` samples at `gain_q15` (32768 = full). Chunk boundaries do not show: filling
 * 320 then 320 is the same as 640 at once. */
void alarm_synth_fill(alarm_synth_t *s, int16_t *out, uint32_t frames, uint16_t gain_q15);
/* The pattern's length in frames. */
uint32_t alarm_synth_period(uint8_t sound);
/* The ramp: ALARM_RAMP_FLOOR_Q15 at 0 ms, 32768 from ALARM_RAMP_MS. */
uint16_t alarm_ramp_q15(uint32_t ms);
