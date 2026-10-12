#include "alarm_sound.h"

#include "svc_alarms.h"

#define MS(x) ((uint32_t)(x) * (ALARM_SOUND_RATE / 1000))
#define Q15 32768

/* One turn of a sine in 64 steps, Q15; read with linear interpolation between steps. */
static const int16_t SINE[64] = {
    0,      3212,   6393,   9512,   12540,  15447,  18205,  20788,  23170,  25330,  27246,
    28899,  30274,  31357,  32138,  32610,  32767,  32610,  32138,  31357,  30274,  28899,
    27246,  25330,  23170,  20788,  18205,  15447,  12540,  9512,   6393,   3212,   0,
    -3212,  -6393,  -9512,  -12540, -15447, -18205, -20788, -23170, -25330, -27246, -28899,
    -30274, -31357, -32138, -32610, -32767, -32610, -32138, -31357, -30274, -28899, -27246,
    -25330, -23170, -20788, -18205, -15447, -12540, -9512,  -6393,  -3212};

static int32_t sine(uint32_t phase)
{
    const uint32_t i = phase >> 26, frac = (phase >> 10) & 0xFFFF;
    const int32_t a = SINE[i], b = SINE[(i + 1) & 63];
    return a + (int32_t)(((int64_t)(b - a) * frac) >> 16);
}

static uint32_t step(uint32_t hz)
{
    return (uint32_t)(((uint64_t)hz << 32) / ALARM_SOUND_RATE);
}

/* Up over `edge` frames at the start, down over `edge` at the end of a `len`-frame note. */
static int32_t gate(uint32_t t, uint32_t len, uint32_t edge)
{
    if (t >= len) return 0;
    if (t < edge) return (int32_t)(t * Q15 / edge);
    if (len - t < edge) return (int32_t)((len - t) * Q15 / edge);
    return Q15;
}

/* A struck bell: a 2 ms attack, then the square of a linear fall — loud, then a long tail. */
static int32_t strike(uint32_t t, uint32_t len)
{
    if (t >= len) return 0;
    if (t < MS(2)) return (int32_t)(t * Q15 / MS(2));
    const int64_t left = (int64_t)(len - t) * Q15 / len;
    return (int32_t)(left * left / Q15);
}

uint32_t alarm_synth_period(uint8_t sound)
{
    return sound == ALARM_SOUND_BEEPS ? MS(1400) : MS(2000);
}

void alarm_synth_init(alarm_synth_t *s, uint8_t sound)
{
    *s = (alarm_synth_t){.sound = sound < ALARM_SOUNDS ? sound : ALARM_SOUND_CHIME};
}

/* The envelope (Q15) and pitch (Hz) of frame `t` of the pattern. */
static int32_t voice(uint8_t sound, uint32_t t, uint32_t *hz)
{
    switch (sound) {
    case ALARM_SOUND_BEEPS: {
        *hz = 1760;
        if (t >= MS(800)) return 0;
        return gate(t % MS(200), MS(100), MS(5));
    }
    case ALARM_SOUND_RISE: {
        *hz = 440 + (1319 - 440) * (t < MS(1500) ? t : MS(1500)) / MS(1500);
        return gate(t, MS(1500), MS(10));
    }
    default:
        if (t < MS(400)) {
            *hz = 1319;
            return strike(t, MS(400));
        }
        *hz = 988;
        return strike(t - MS(400), MS(800));
    }
}

void alarm_synth_fill(alarm_synth_t *s, int16_t *out, uint32_t frames, uint16_t gain_q15)
{
    const uint32_t period = alarm_synth_period(s->sound);
    for (uint32_t i = 0; i < frames; i++, s->frame++) {
        uint32_t hz;
        const int32_t env = voice(s->sound, s->frame % period, &hz);
        s->phase += step(hz);
        const int64_t v = (int64_t)sine(s->phase) * env / Q15 * gain_q15 / Q15;
        out[i] = (int16_t)(v * ALARM_PEAK / Q15);
    }
}

uint16_t alarm_ramp_q15(uint32_t ms)
{
    if (ms >= ALARM_RAMP_MS) return (uint16_t)Q15;
    const uint64_t rise = (uint64_t)(Q15 - ALARM_RAMP_FLOOR_Q15) * ms / ALARM_RAMP_MS;
    return (uint16_t)(ALARM_RAMP_FLOOR_Q15 + rise);
}
