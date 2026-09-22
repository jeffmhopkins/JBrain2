#include "cue.h"

#include <math.h>
#include <stdbool.h>
#include <stddef.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#define TWO_PI 6.28318530717958647692f

/* WHERE THE PARTIALS STOP, and why it is a fade rather than a wall.
 *
 * Nothing above Nyquist is ever generated — that is the whole point of summing sines instead
 * of squaring a phase. But a harmonic that switches from full amplitude to zero the instant a
 * sweep carries it past the limit is a step change in the waveform, which is a click. So the
 * top octave-and-a-bit is a raised-cosine taper: a partial fades out as it approaches the
 * ceiling and is gone before it reaches it. On a falling sweep the same taper runs backwards
 * and harmonics fade IN as the pitch drops, which is why the `oops` and `stop` cues get
 * richer as they descend rather than thinner. */
#define FADE_LO 5800.0f
#define FADE_HI 7600.0f

#define MAX_HARM 24

static float harm_gain(float hz)
{
    if (hz >= FADE_HI) return 0.0f;
    if (hz <= FADE_LO) return 1.0f;
    return 0.5f * (1.0f + cosf((float)M_PI * (hz - FADE_LO) / (FADE_HI - FADE_LO)));
}

/* A PULSE WAVE, BUILT FROM COSINES. The Fourier series of a duty-d pulse is
   a_n = (4/(n*pi)) * sin(n*pi*d), and the n=0 term — the DC offset, which is 2d-1 and reaches
   -0.75 on a narrow pulse — is simply never summed. That matters for more than tidiness: DC
   costs headroom, pushes the speaker cone off centre, and turns a duty sweep into an audible
   thump that has nothing to do with the note.

   COSINE and not sine, which is not cosmetic. The cosine basis reproduces the real pulse
   shape and peaks at about 1.18 for a 50% duty (the Gibbs overshoot, which is correct). The
   same series on sines peaks near 2.7 for the same wave — all the partials line up at the
   origin — and throws away 7 dB of headroom for nothing. The cost is that a cosine pulse
   STARTS at its peak, so the attack ramp below is mandatory rather than polite. */
static float pulse(float phase, float duty, float f0)
{
    float s = 0.0f;
    for (int n = 1; n <= MAX_HARM; n++) {
        const float g = harm_gain((float)n * f0);
        if (g == 0.0f) break;
        s += g * (4.0f / ((float)n * (float)M_PI)) * sinf((float)n * (float)M_PI * duty) *
             cosf((float)n * phase);
    }
    return s;
}

/* A triangle: odd harmonics only, falling as 1/n^2 with alternating sign, on SINES — which is
   the natural basis here because a triangle starts at zero. Almost no high partials, so it is
   the quiet, round thing to put underneath a pulse without adding harshness. */
static float triangle(float phase, float f0)
{
    float s = 0.0f;
    for (int n = 1; n <= MAX_HARM; n += 2) {
        const float g = harm_gain((float)n * f0);
        if (g == 0.0f) break;
        const float sign = (((n - 1) / 2) & 1) ? -1.0f : 1.0f;
        s += g * sign * (8.0f / ((float)M_PI * (float)M_PI)) / (float)(n * n) *
             sinf((float)n * phase);
    }
    return s;
}

/* THE ENVELOPE, AND THE TWO CLICKS IT EXISTS TO PREVENT.
 *
 * A linear attack over a millisecond or two, because a cosine-basis pulse starts at full
 * amplitude and writing that as sample zero is a full-scale step — a broadband click.
 *
 * Then an exponential decay NORMALISED TO REACH EXACTLY ZERO. A plain expf(-k*u) never gets
 * there, so the buffer ends on a non-zero sample and the speaker steps back to silence: the
 * same click at the other end, and the one most often missed because it is quiet on a desk
 * and obvious on a small hard-cased speaker.
 *
 * `punch` lifts the first moments above unity and falls back to 1.0 across the hold. It is
 * what makes a sound read as struck rather than faded up — the auditory system treats a fast
 * attack and an exponential tail as evidence of a physical impact, which is the whole reason
 * the chip idiom sounds percussive. */
typedef struct {
    float attack_ms;
    float hold_ms;
    float decay_ms;
    float punch;
} env_t;

static float env_at(const env_t *e, float t_ms)
{
    if (t_ms < e->attack_ms) return t_ms / e->attack_ms;
    const float t = t_ms - e->attack_ms;
    if (t < e->hold_ms) return 1.0f + e->punch * (1.0f - t / e->hold_ms);
    const float u = (t - e->hold_ms) / e->decay_ms;
    if (u >= 1.0f) return 0.0f;
    const float k = 5.0f;              /* about 43 dB of travel */
    const float floor_ = 0.006737947f; /* expf(-5) */
    return (expf(-k * u) - floor_) / (1.0f - floor_);
}

/* Every cue's length in milliseconds. Kept in one table so `cue_samples` and the renderer
   cannot disagree about how much buffer a cue needs — a mismatch there is a write past the
   end, which on this part is a reboot rather than a wrong noise. */
static const float CUE_MS[CUE_COUNT] = {
    [CUE_BLIP] = 55.0f,   [CUE_TOGGLE] = 55.0f, [CUE_HEARD] = 260.0f, [CUE_LISTEN] = 190.0f,
    [CUE_STOP] = 210.0f,  [CUE_OOPS] = 300.0f,  [CUE_TICK] = 30.0f,
};

int cue_samples(cue_t c, int rate)
{
    if (c < 0 || c >= CUE_COUNT || rate <= 0) return 0;
    return (int)(CUE_MS[c] * (float)rate / 1000.0f + 0.5f);
}

/* One voice's state, so a two-note cue keeps its phase across the step.
   NEVER reset the phase on a pitch change: a discontinuity mid-waveform is a click on every
   note, and it is the single most common way a two-note motif ends up sounding broken. */
typedef struct {
    float phase;
} osc_t;

static float step(osc_t *o, float hz, int rate)
{
    o->phase += TWO_PI * hz / (float)rate;
    if (o->phase >= TWO_PI) o->phase -= TWO_PI;
    return o->phase;
}

/* ONE PASS OVER THE GENERATOR, TWICE — and not a buffer, which is the whole point.
 *
 * The first cut held the rendered floats in `float buf[CUE_MAX_SAMPLES]` so it could find the
 * peak before scaling. That is 22 KB on the stack of whichever task asks for a sound, and the
 * task that asks is the renderer, whose stack is 8192 bytes — a guaranteed overflow on the
 * first tap, and `display.c` already carries a comment about raising that stack once while
 * chasing a field panic. A static buffer would have cost the same 22 KB permanently, out of
 * the INTERNAL RAM that `speech.c` refuses to start the recogniser without.
 *
 * So nothing is stored. The generator is pure — same inputs, same samples, no randomness —
 * so it can simply be run twice: once to measure the peak, once to write. Rendering cost is
 * irrelevant here (a cue is built once, then played from `s_play`), and the memory cost is
 * now a handful of floats.
 *
 * `out == NULL` means the measuring pass and returns the peak; otherwise it writes and the
 * return is unused. */
static float cue_pass(cue_t c, int n, int rate, int16_t *out, float scale)
{
    osc_t a = {0.0f}, b = {0.0f}, sub = {0.0f};
    float peak = 1e-6f;

    for (int i = 0; i < n; i++) {
        const float t = (float)i * 1000.0f / (float)rate;
        float v = 0.0f;

        switch (c) {
        case CUE_BLIP:
        case CUE_TICK:
        case CUE_TOGGLE: {
            /* NEUTRAL BY CONSTRUCTION. A pitch that moves carries a mood — rising reads as a
               question, falling as a refusal — so an acknowledgement that should mean nothing
               beyond "registered" must not move at all. Mid register, where the ear is
               sensitive without it being piercing, and short enough (under ~60 ms) that no
               melodic contour can establish itself even if one were there. The toggle sits two
               semitones up so the panel's two touch targets are distinguishable without
               either of them acquiring an opinion. */
            /* The tick sits a fourth ABOVE the tap rather than a semitone below it, which is
               where it started. Sixteen of these fire back to back during a calibration and
               the panel is otherwise silent through it, so it has to be the lightest thing in
               the set — short, high and thin — and it has to be obviously not the sound a tap
               makes, or the calibration run sounds like sixteen accidental taps. */
            const float hz = (c == CUE_TOGGLE) ? 987.77f : (c == CUE_TICK) ? 1244.51f : 880.0f;
            const env_t e = {1.2f, 10.0f, CUE_MS[c] - 11.2f, 0.0f};
            v = pulse(step(&a, hz, rate), 0.5f, hz) * env_at(&e, t);
            break;
        }
        case CUE_HEARD: {
            /* THE COIN'S SHAPE, because it is the right one and it was arrived at honestly.
               Super Mario Bros. plays B5 then E6 — an ascending perfect fourth — as a SHORT
               note into a LONG one, and the duration asymmetry does as much work as the
               interval: the ear parses short-into-long as an upbeat into a downbeat, which is
               why it feels like landing on something rather than asking a question. A fourth
               is consonant but open, which is what lets it be heard two hundred times without
               turning into a cadence. And it is a STEP, not a glide: a quantised jump reads as
               a discrete event, where the same interval slid reads as a boing. */
            const float f1 = 987.77f, f2 = 1318.51f, split = 70.0f;
            const float hz = (t < split) ? f1 : f2;
            const env_t e = {1.5f, split + 40.0f, CUE_MS[c] - split - 41.5f, 0.05f};
            v = pulse(step(&a, hz, rate), 0.5f, hz) * env_at(&e, t);
            break;
        }
        case CUE_LISTEN:
        case CUE_STOP: {
            /* A SWEEP, AND THE RATE IS WHAT IT MEANS. Both of these glide; the difference
               between a glide that reads as a toy opening up and one that reads as a laser is
               not the direction but the SPEED — past roughly ten octaves a second the ear
               stops following it as a gesture and hears a single zip. These run at about four,
               which stays legible as movement.
               Up for the microphone opening, because rising is the prosody of a question and
               an open microphone is one. Down for stop, and the same taper that keeps the
               rising cue from aliasing lets the falling one gain harmonics as it goes, so
               stopping sounds like it settles rather than thins out.
               Amplitude stays near flat across the sweep: the gesture is ongoing, and a decay
               underneath it would say the opposite. */
            const bool up = (c == CUE_LISTEN);
            const float f0 = up ? 520.0f : 780.0f;
            const float octaves = up ? 0.80f : -0.85f;
            const float dur = CUE_MS[c];
            const float hz = f0 * exp2f(octaves * t / dur);
            const env_t e = {2.0f, dur * 0.7f, dur * 0.3f - 2.0f, 0.0f};
            v = pulse(step(&a, hz, rate), 0.25f, hz) * env_at(&e, t);
            break;
        }
        case CUE_OOPS: {
            /* DESCENDING, LOW, AND ROUGH — in that order of importance.
               Descending because contour is the first thing heard and a rising apology reads
               as sarcasm. Low because roughness is not a property of an interval but of where
               it sits: two partials buzz when they land inside the same critical band, which
               near 300 Hz means a separation of roughly thirty Hz, and the SAME interval two
               octaves up just beats gently. So the dissonance has to be put down here to be
               felt at all. The second voice is about a semitone and a half off the first,
               which puts the pair right in that window.
               Slowly, though — 2.5 octaves a second. A fast fall is a laser; this has to read
               as a shrug. And a triangle underneath rather than a third pulse, because the
               point is a soft bottom end, not more harshness.
               It must not tell a four-year-old off. It says try again. */
            const float base = 311.13f;
            const float dur = CUE_MS[c], flat = 90.0f;
            const float fall = (t < flat) ? 1.0f : exp2f(-0.75f * (t - flat) / (dur - flat));
            const float fa = base * fall, fb = base * 1.0905f * fall;
            const env_t e = {2.0f, flat + 50.0f, dur - flat - 52.0f, 0.2f};
            /* Each voice stepped exactly ONCE per sample. Calling `step` twice on the same
               oscillator in one sample advances its phase at double the frequency it was
               asked for, which detunes it against the others and drifts further the longer
               the cue runs — silent in a spectrum plot, obvious as a sour note. */
            const float pa = step(&a, fa, rate);
            const float pb = step(&b, fb, rate);
            const float ps = step(&sub, fa * 0.5f, rate);
            v = (0.42f * pulse(pa, 0.125f, fa) + 0.42f * pulse(pb, 0.125f, fb) +
                 0.30f * triangle(ps, fa * 0.5f)) *
                env_at(&e, t);
            break;
        }
        default:
            return 0.0f;
        }

        if (out == NULL) {
            const float m = fabsf(v);
            if (m > peak) peak = m;
        } else {
            /* int32 and lrintf, not a cast to int16: the cast truncates toward zero, which
               biases every sample and adds correlated distortion, and
               (int16_t)(1.0f * 32768.0f) wraps to -32768 — full positive becomes full
               negative, which is the loudest possible click. */
            int32_t s = (int32_t)lrintf(v * scale);
            if (s > 32767) s = 32767;
            if (s < -32768) s = -32768;
            out[i] = (int16_t)s;
        }
    }
    return peak;
}

int cue_render(cue_t c, int16_t *out, int rate, int gain)
{
    const int n = cue_samples(c, rate);
    if (n <= 0 || out == NULL || n > CUE_MAX_SAMPLES) return 0;

    /* PEAK-NORMALISE, MEASURED RATHER THAN DERIVED. Reasoning about the peak analytically is
       a trap — the naive bound (the sum of the coefficients) is 2.7 for a square whose real
       peak is 1.18, and it moves with the harmonic count as a sweep crosses the taper. The
       side effect is that every cue in the set lands at the same level, which is what stops
       one of them being the loud one nobody wants to hear. */
    const float peak = cue_pass(c, n, rate, NULL, 0.0f);
    if (peak <= 0.0f) return 0;

    if (gain < 0) gain = 0;
    if (gain > 100) gain = 100;
    /* 26000 rather than 32767: a cue is an acknowledgement next to a speaking voice, and
       leaving it a couple of dB below full scale is what keeps it from being the loudest
       thing the panel ever does. */
    cue_pass(c, n, rate, out, 26000.0f * (float)gain / 100.0f / peak);
    return n;
}
