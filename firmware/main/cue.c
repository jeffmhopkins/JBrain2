#include "cue.h"

#include <math.h>
#include <stddef.h>

#include "rig.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#define TWO_PI 6.28318530717958647692f

/* WHERE THE PARTIALS STOP, and why it is a fade rather than a wall.
 *
 * Nothing above Nyquist is ever generated — that is the point of summing sines instead of
 * squaring a phase. But a harmonic that snaps from full amplitude to zero as a sweep carries
 * it past the limit is a step in the waveform, which is a click. So the top octave is a
 * raised-cosine taper: a partial fades out before it reaches the ceiling. On a falling sweep
 * the same taper runs backwards and harmonics fade IN as the pitch drops, which is why the
 * falling cues get richer as they go rather than thinner. */
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
   a_n = (4/(n*pi)) * sin(n*pi*d), and the n=0 term — the DC offset, 2d-1, which reaches -0.75
   on a narrow pulse — is simply never summed. That is not tidiness: DC costs headroom, pushes
   the cone off centre, and turns a duty change into a thump unrelated to the note.

   COSINE and not sine, which is not cosmetic. The cosine basis reproduces the real pulse and
   peaks near 1.18 for a 50% duty (Gibbs overshoot, which is correct). The same series on
   sines peaks near 2.7 — every partial aligned at the origin — and throws away 7 dB of
   headroom. The cost is that a cosine pulse STARTS at its peak, which makes the attack ramp
   below mandatory rather than polite. */
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

/* Odd harmonics falling as 1/n^2, on sines. Almost no high partials, so this is the round,
   quiet thing to build a coo from or slide under a pulse without adding harshness. */
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

/* A 15-bit LFSR, the NES noise channel's own shape — feedback from bit0 XOR bit1, shifted
   into bit 14. Aliasing is the one thing that does NOT matter here: a flat spectrum folded
   onto itself is still flat, so this can clock at any rate and simply fold. */
typedef struct {
    uint16_t reg;
    float acc;
    float x1, y1;
} noise_t;

/* Cutoff for the DC blocker below. Well under the kilohertz these are clocked at, well over
   the fraction of a hertz that the drift lives at. */
#define NOISE_HP_HZ 200.0f

static float noise_next(noise_t *n, float rate_hz, int sr)
{
    n->acc += rate_hz / (float)sr;
    while (n->acc >= 1.0f) {
        n->acc -= 1.0f;
        const uint16_t fb = (uint16_t)((n->reg ^ (n->reg >> 1)) & 1u);
        n->reg = (uint16_t)((n->reg >> 1) | (uint16_t)(fb << 14));
    }
    const float x = (n->reg & 1u) ? -1.0f : 1.0f;
    /* A slowly-clocked LFSR is a sample-and-hold of a coin flip, and over the few hundred
       flips a cue lasts they do NOT average out: the register's period is 32767 but a 300 ms
       crunch only gets ~500 clocks of it, so the noise rides a random DC offset worth a few
       percent of full scale — measured at 13% on the eat. That parks the cone off-centre for
       the length of the sound and snaps it back at the end, which is the same click the
       envelope exists to prevent, arriving by another route. Which seeds land badly is luck,
       so the fix has to be structural rather than a better starting register. One pole takes
       it out and costs nothing audible. */
    const float r = 1.0f - TWO_PI * NOISE_HP_HZ / (float)sr;
    const float y = x - n->x1 + r * n->y1;
    n->x1 = x;
    n->y1 = y;
    return y;
}

/* THE ENVELOPE, AND THE TWO CLICKS IT EXISTS TO PREVENT. A linear attack over a millisecond
   or two, because a cosine-basis pulse starts at full amplitude and writing that as sample
   zero is a full-scale step. Then an exponential decay NORMALISED TO REACH EXACTLY ZERO: a
   plain expf(-k*u) never gets there, so the buffer ends on a live sample and the speaker
   steps back to silence — the same click at the other end, and the one most often missed
   because it is quiet on a desk and obvious on a small hard-cased speaker.

   `punch` lifts the opening above unity and falls back across the hold. It is what makes a
   sound read as struck rather than faded up. */
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
    const float floor_ = 0.006737947f; /* expf(-5) */
    return (expf(-5.0f * u) - floor_) / (1.0f - floor_);
}

/* One voice. NEVER reset the phase on a pitch change: a discontinuity mid-waveform clicks on
   every note, and it is the commonest way a two-note motif ends up sounding broken. */
typedef struct {
    float phase;
} osc_t;

static float step_osc(osc_t *o, float hz, int sr)
{
    o->phase += TWO_PI * hz / (float)sr;
    if (o->phase >= TWO_PI) o->phase -= TWO_PI;
    return o->phase;
}

static float semi(float hz, float semitones)
{
    return hz * exp2f(semitones / 12.0f);
}

/* --- the table ------------------------------------------------------------------------
 *
 * Eight shapes, and every cue is a row of numbers against one of them. The numbers live here
 * rather than in the renderer so that "are these actually all different" is a question you
 * answer by READING, not by listening — and so a tuning change is a number, not a branch.
 */
typedef enum {
    SH_TONE = 0, /* one flat note */
    SH_STEPS,    /* discrete notes, no glide between them */
    SH_SWEEP,    /* a glide; `a` octaves over the run, `hold` flat first */
    SH_WARBLE,   /* a note with vibrato */
    SH_COO,      /* warm, rises then settles — the happy one */
    SH_NOISE,    /* filtered noise, optionally over a falling tone */
    SH_RUDE,     /* the body noises */
    SH_PAIR,     /* two detuned voices, falling together */
} shape_t;

#define STEPS_MAX 4

typedef struct {
    uint8_t shape;
    uint16_t ms;
    float hz;      /* the fundamental, or the start of a sweep */
    float duty;    /* pulse duty; 0 means use a triangle instead */
    float a;       /* SWEEP/COO: octaves travelled. WARBLE: vibrato Hz. NOISE: noise clock kHz */
    float b;       /* SWEEP: ms held flat first. WARBLE: depth. NOISE: tone under it, Hz */
    int8_t steps[STEPS_MAX]; /* SH_STEPS: semitone offsets from `hz`; 127 ends the list */
    uint8_t attack_ms;
    uint8_t punch_pct;
} cue_def_t;

#define END 127

/* Ordered exactly as `cue_t` declares them. */
/* --- THE FIVE FARTS -------------------------------------------------------------------
 *
 * The owner: *"fart should have five different kinds of farts, different tones, length,
 * squeakiness, etc. The kids really love the farts."*
 *
 * `variant` already transposed every cue by up to a tone and stretched it by a tenth, and for
 * a fart that is not enough: a fart transposed a semitone is the SAME fart. What makes two of
 * them different is what makes them different in life — how long the pressure lasts, whether
 * it is tight enough to squeak, and how much is liquid. So the fart gets five rows of its own
 * rather than one row and a pitch knob, and they differ on every axis at once.
 *
 * Four things actually distinguish one from another, and every row moves all four:
 *
 *   - **Register.** 92 Hz is a rumble you feel; 340 Hz is a squeak. That is nearly two
 *     octaves of spread, where the variant knob offers two semitones.
 *   - **Contour.** Pressure normally runs out, so the pitch FALLS. The squeaker is the
 *     exception and rises, because a tight one pinches higher as it closes — which is why
 *     `fall` is allowed to be negative.
 *   - **Flutter.** The amplitude modulation is the texture. Slow and shallow is blubbery;
 *     past `depth` ~0.5 the tremolo reaches zero and the thing stops being one sound and
 *     becomes a SPUTTER — separate bursts, which no amount of pitch change imitates.
 *   - **Wetness.** Noise against sawtooth. Dry is brassy; wet is the one the twins will
 *     request by name.
 *
 * Lengths run 170 ms to 700 ms, a factor of four, and that is deliberate: length is the first
 * thing a listener notices and the cheapest axis to be lazy about. */
typedef struct {
    uint16_t ms;
    float hz;      /* where it starts */
    float fall;    /* Hz shed across the run; NEGATIVE rises, which is what squeaking is */
    float wet;     /* how much noise rides the sawtooth; 0 is dry */
    float flutter; /* Hz of the amplitude flutter — the texture */
    float depth;   /* how deep it cuts, 0..1; past ~0.5 the sound breaks into bursts */
    float buzz;    /* third-harmonic lift: brassy rather than breathy */
} rude_t;

static const rude_t FARTS[] = {
    /* 1. THE RUMBLER. Long, low, slow flutter, barely wet — the one that goes on too long,
          which at four is the entire joke. */
    {700, 92.0f, 38.0f, 0.10f, 19.0f, 0.34f, 0.30f},
    /* 2. THE SQUEAKER. High and tight, and the only one that RISES: a small opening pinches
          higher as it closes. Short, dry, buzzy. */
    {220, 340.0f, -95.0f, 0.00f, 41.0f, 0.30f, 0.55f},
    /* 3. THE SPUTTERER. Mid, and `depth` 0.92 is the whole row — the tremolo goes through
          zero, so this arrives as four or five separate bursts rather than one noise. */
    {420, 150.0f, 55.0f, 0.15f, 13.0f, 0.92f, 0.40f},
    /* 4. THE WET ONE. Noise at 0.75 against the sawtooth, low and unhurried. */
    {560, 118.0f, 46.0f, 0.75f, 31.0f, 0.38f, 0.22f},
    /* 5. THE PFFT. Over before it starts: 170 ms, mostly air, falling hard. The one that
          punctuates rather than performs. Shallow flutter on purpose — at 48 Hz a deep one
          reads as buzz rather than rhythm anyway, and nothing escaping this fast has time to
          flutter. */
    {170, 210.0f, 120.0f, 0.55f, 48.0f, 0.25f, 0.20f},
};

#define FART_COUNT ((int)(sizeof(FARTS) / sizeof(FARTS[0])))

/* The burp stays one character — it was not what was asked for, and the same struct describes
   it exactly: these are the numbers the single RUDE row used to carry inline. */
static const rude_t BURP = {560, 140.0f, 65.0f, 0.0f, 27.0f, 0.38f, 0.30f};

static const rude_t *rude_def(cue_t c, unsigned variant)
{
    return c == CUE_FART ? &FARTS[variant % (unsigned)FART_COUNT] : &BURP;
}

static const cue_def_t DEF[CUE_COUNT] = {
    /* -- the actions ----------------------------------------------------------------- */
    /* A wobble that goes nowhere: vibrato is the whole content, which is what makes it read
       as a shiver rather than a note. */
    [CUE_WIGGLE] = {SH_WARBLE, 240, 520.0f, 0.5f, 11.0f, 0.055f, {END}, 3, 0},
    /* Four rising blips. Laughter is repetition with a rising contour; a single blip is not
       funny and a glide is not laughter. */
    [CUE_GIGGLE] = {SH_STEPS, 300, 880.0f, 0.35f, 0, 0, {0, 4, 7, 12}, 2, 10},
    /* Up fast and down slower, with the wobble on the way back — a spring, not a jump. */
    [CUE_BOING] = {SH_SWEEP, 260, 300.0f, 0.3f, 1.35f, 30.0f, {END}, 2, 25},
    /* THE COO, and the reason this file grew. Warm (a triangle, almost no high partials),
       soft attack, rises a little and settles — the shape of a contented small noise rather
       than an announcement. Poking the head lands here 30% of the time. */
    [CUE_BLUSH] = {SH_COO, 420, 392.0f, 0.0f, 0.35f, 0, {END}, 35, 0},
    /* A catch of breath and then the release. The pitch climbs, the noise arrives late. */
    [CUE_SNEEZE] = {SH_NOISE, 330, 0.0f, 0, 9.0f, 620.0f, {END}, 4, 45},
    /* Two tiny blips and nothing else — the second higher, because that is the involuntary
       shape. Short enough to be over before it registers as a note. */
    [CUE_HICCUP] = {SH_STEPS, 150, 700.0f, 0.25f, 0, 0, {0, 9, END}, 1, 30},
    /* Two low soft notes, the second the same or lower: agreement settles, it does not ask. */
    [CUE_NOD] = {SH_STEPS, 260, 330.0f, 0.0f, 0, 0, {0, -2, END}, 6, 0},
    /* The SMB jump: flat first, then a rise of about an octave and a half. The plateau is
       most of why it reads as a jump and not a laser. */
    [CUE_JUMP] = {SH_SWEEP, 250, 440.0f, 0.25f, 1.45f, 55.0f, {END}, 2, 5},
    /* Hello: up a fifth, open and friendly, no cadence. */
    [CUE_WAVE] = {SH_STEPS, 260, 587.33f, 0.5f, 0, 0, {0, 7, END}, 3, 10},
    /* A major arpeggio — the powerup shape, shortened. Dancing is a figure, not a note. */
    [CUE_DANCE] = {SH_STEPS, 360, 523.25f, 0.25f, 0, 0, {0, 4, 7, 12}, 2, 5},
    /* Low and rhythmic, under the dance rather than beside it. */
    [CUE_BOP] = {SH_STEPS, 320, 196.0f, 0.5f, 0, 0, {0, 0, 7, 0}, 3, 20},
    /* The wiggle's louder cousin: faster vibrato, deeper, and a narrow duty to buzz. */
    [CUE_SHIMMY] = {SH_WARBLE, 300, 440.0f, 0.15f, 19.0f, 0.09f, {END}, 3, 0},
    /* A long slow fall that fades out — a yawn. Slow enough that it cannot be a laser. */
    [CUE_SLEEP] = {SH_SWEEP, 560, 330.0f, 0.0f, -0.9f, 60.0f, {END}, 25, 0},
    /* Gone: a fast drop to nothing. Fast, because hiding is sudden. */
    [CUE_HIDE] = {SH_SWEEP, 180, 900.0f, 0.3f, -1.8f, 0.0f, {END}, 2, 20},
    /* The two RUDE rows carry a shape, an attack and nothing else: their numbers are in
       FARTS[] and BURP above, because five characters do not fit in one row. `ms` here is the
       LONGEST of the five, which is what a caller sizing a buffer is promised — the real
       per-variant length comes from `cue_ms`. */
    [CUE_FART] = {SH_RUDE, 700, 0.0f, 0.0f, 0.0f, 0.0f, {END}, 3, 0},
    [CUE_BURP] = {SH_RUDE, 560, 0.0f, 0.0f, 0.0f, 0.0f, {END}, 3, 0},
    /* Two soft bites. Noise, but low-clocked and short, so it chews rather than hisses. */
    [CUE_EAT] = {SH_NOISE, 300, 0.0f, 0, 2.6f, 0.0f, {0, 0, END}, 3, 20},
    /* A thud: a noise burst over a tone dropping fast. Contact, then nothing. */
    [CUE_KICK] = {SH_NOISE, 200, 0.0f, 0, 4.2f, 260.0f, {END}, 1, 60},
    /* Three rises, each starting higher than the last — a spiral, which one sweep cannot be. */
    [CUE_SPIN] = {SH_STEPS, 380, 523.25f, 0.2f, 0, 0, {0, 5, 12, 17}, 2, 10},

    /* -- the interface --------------------------------------------------------------- */
    /* Neutrality comes from REMOVING contour: anything that moves imports a mood, and a tap
       that resolved to nothing should mean only "registered". */
    [CUE_BLIP] = {SH_TONE, 55, 880.0f, 0.5f, 0, 0, {END}, 2, 0},
    [CUE_TOGGLE] = {SH_TONE, 55, 987.77f, 0.5f, 0, 0, {END}, 2, 0},
    /* Sixteen of these fire in a row during a calibration, so it is the lightest thing in the
       set — and a fourth above the tap, or the run sounds like sixteen accidental taps. */
    [CUE_TICK] = {SH_TONE, 30, 1244.51f, 0.5f, 0, 0, {END}, 1, 0},
    /* The coin: an ascending fourth, short note into long. The duration asymmetry does as
       much work as the interval — short-into-long is the shape the ear reads as arriving. */
    [CUE_HEARD] = {SH_STEPS, 260, 987.77f, 0.5f, 0, 0, {0, 5, END}, 2, 5},
    [CUE_LISTEN] = {SH_SWEEP, 190, 520.0f, 0.25f, 0.8f, 0.0f, {END}, 2, 0},
    [CUE_STOP] = {SH_SWEEP, 210, 780.0f, 0.25f, -0.85f, 0.0f, {END}, 2, 0},
    /* Low, falling, and rough on purpose — roughness is a property of REGISTER, not interval:
       two partials buzz inside one critical band, which near 300 Hz means a ~30 Hz gap and
       two octaves up is just a gentle beat. Gentle: try again, not told off. */
    [CUE_OOPS] = {SH_PAIR, 300, 311.13f, 0.125f, -0.75f, 90.0f, {END}, 2, 20},
    /* GOING AWAY: three notes climbing an octave and leaving. Rising and UNRESOLVED on the
       octave rather than settling on the tonic, because a message that has been sent is not
       finished — someone else has it now. Distinct from the coin (two notes, a fourth) and
       from the dance (four notes, a full major arpeggio) by note count and by interval. */
    [CUE_SENT] = {SH_STEPS, 300, 659.25f, 0.4f, 0, 0, {0, 7, 12, END}, 2, 5},
    /* ARRIVING, AND IT HAS TO BE GENTLE, because this is the one cue in the set that fires
       WITHOUT anyone having touched or said anything — it goes off in a bedroom, possibly
       while a four-year-old is doing something else. So: a triangle (no bright partials), a
       slow attack, and a FALLING major third, which is the two-note shape a doorbell uses
       and the ear reads as an announcement rather than a demand. A rising one would ask a
       question the panel cannot answer. Apart from the nod (the same shape two octaves
       down, and a smaller interval) by register. */
    [CUE_MESSAGE] = {SH_STEPS, 440, 880.0f, 0.0f, 0, 0, {0, -4, END}, 9, 0},
};

/* The longest any variant stretches a cue. Kept beside `variant_stretch` in spirit; declared
   here because `cue_samples` is above it and a caller's buffer depends on the two agreeing. */
#define STRETCH_MAX 1.07f

/* The nominal length of `c` at `variant`, before stretch. One number per cue for everything
   except the fart, whose five characters differ in LENGTH as much as in pitch — 170 ms to
   700 ms — and a single `ms` field cannot say that. */
static float cue_ms(cue_t c, unsigned variant)
{
    return c == CUE_FART ? (float)rude_def(c, variant)->ms : (float)DEF[c].ms;
}

/* The longest `c` can nominally be, across every variant. Computed rather than read off the
   table, so adding a longer fart cannot leave this behind — a caller sizes a buffer from it,
   and it has to be the worst case rather than the typical one. */
static float cue_ms_max(cue_t c)
{
    if (c != CUE_FART) return (float)DEF[c].ms;
    float ms = 0.0f;
    for (int i = 0; i < FART_COUNT; i++) {
        if ((float)FARTS[i].ms > ms) ms = (float)FARTS[i].ms;
    }
    return ms;
}

int cue_samples(cue_t c, int rate)
{
    if (c < 0 || c >= CUE_COUNT || rate <= 0) return 0;
    return (int)(cue_ms_max(c) * STRETCH_MAX * (float)rate / 1000.0f + 0.5f);
}

cue_t cue_for_action(int action)
{
    switch (action) {
    case ACT_WIGGLE: return CUE_WIGGLE;
    case ACT_GIGGLE: return CUE_GIGGLE;
    case ACT_BOING:  return CUE_BOING;
    case ACT_BLUSH:  return CUE_BLUSH;
    case ACT_SNEEZE: return CUE_SNEEZE;
    case ACT_HICCUP: return CUE_HICCUP;
    case ACT_NOD:    return CUE_NOD;
    case ACT_JUMP:   return CUE_JUMP;
    case ACT_WAVE:   return CUE_WAVE;
    case ACT_DANCE:  return CUE_DANCE;
    case ACT_BOP:    return CUE_BOP;
    case ACT_SHIMMY: return CUE_SHIMMY;
    case ACT_SLEEP:  return CUE_SLEEP;
    case ACT_HIDE:   return CUE_HIDE;
    case ACT_FART:   return CUE_FART;
    case ACT_BURP:   return CUE_BURP;
    case ACT_EAT:    return CUE_EAT;
    case ACT_KICK:   return CUE_KICK;
    case ACT_SPIN:   return CUE_SPIN;
    default:         return CUE_BLIP;
    }
}

/* --- variation ------------------------------------------------------------------------
 *
 * The owner: *"They should all be unique or kind of change variations."* Uniqueness is the
 * table above; this is the second half — the fourth giggle in a row must not be a recording
 * of the first.
 *
 * What varies is deliberately narrow. Pitch moves by up to a tone and a bit, length by a
 * tenth, and the multi-note shapes rotate which of their intervals they lean on. What does
 * NOT vary is contour direction, register or shape, because those are the meaning: a rising
 * cue that sometimes falls is not a variation, it is a different sound with the wrong name.
 *
 * Deterministic from `variant` alone. That matters beyond tidiness — `cue_render` finds its
 * peak by generating the whole cue twice and keeping nothing, which is only correct while the
 * two passes agree sample for sample. */
static float variant_semitones(unsigned v)
{
    static const float TABLE[8] = {0.0f, 1.0f, -1.0f, 2.0f, -2.0f, 0.5f, -1.5f, 1.5f};
    return TABLE[v % 8u];
}

static float variant_stretch(unsigned v)
{
    static const float TABLE[4] = {1.0f, 0.93f, 1.07f, 0.97f};
    return TABLE[(v / 8u) % 4u];
}

/* ONE PASS OVER THE GENERATOR, TWICE — and not a buffer, which is the point.
 *
 * Holding the rendered floats to find the peak would be 38 KB on the stack of whichever task
 * asks for a sound, and the task that asks is the renderer, whose stack is 8192 bytes. A
 * static buffer would cost the same permanently, out of the internal RAM `speech.c` refuses
 * to start the recogniser without. The generator is pure, so it simply runs twice.
 *
 * `out == NULL` is the measuring pass and returns the peak; otherwise it writes. */
static float cue_pass(cue_t c, int n, int rate, unsigned variant, int16_t *out, float scale)
{
    const cue_def_t *d = &DEF[c];
    /* The variant's transposition as a RATIO, not just applied to `base`. A noise-only cue
       has no fundamental to transpose — the sneeze, the bite and the thud all carry hz 0 —
       so pitch variation silently did nothing for exactly the three cues whose character is
       noise, and they repeated identically within a stretch bucket. The noise clock is their
       pitch; it varies with the same number. */
    const float vary = exp2f(variant_semitones(variant) / 12.0f);
    const float base = d->hz * vary;
    const float dur = (float)n * 1000.0f / (float)rate;
    osc_t a = {0.0f}, b = {0.0f};
    noise_t nz = {1u, 0.0f, 0.0f, 0.0f};
    float peak = 1e-6f;

    /* How many notes a stepped cue has, and how long each gets. The LAST note keeps the
       remainder, which is what gives the coin (and the hiccup, and the wave) their
       short-into-long asymmetry rather than an even march. */
    int n_steps = 0;
    while (n_steps < STEPS_MAX && d->steps[n_steps] != END) n_steps++;
    const float lead_ms = n_steps > 1 ? dur * 0.26f / (float)(n_steps - 1) : dur;

    for (int i = 0; i < n; i++) {
        const float t = (float)i * 1000.0f / (float)rate;
        const float u = t / dur;
        float v = 0.0f;
        env_t e = {(float)d->attack_ms, dur * 0.35f, dur * 0.65f - (float)d->attack_ms,
                   (float)d->punch_pct / 100.0f};

        switch (d->shape) {
        case SH_TONE:
            e.hold_ms = dur * 0.2f;
            e.decay_ms = dur * 0.8f - (float)d->attack_ms;
            v = pulse(step_osc(&a, base, rate), d->duty, base);
            break;

        case SH_STEPS: {
            /* Notes in order, the last one long. Rotating the start index by the variant
               keeps the same intervals but changes which one the ear hears first. */
            int k = 0;
            while (k < n_steps - 1 && t >= lead_ms * (float)(k + 1)) k++;
            const int idx = n_steps > 1 ? (int)((unsigned)k + variant / 32u) % n_steps : 0;
            const float hz = semi(base, (float)d->steps[idx]);
            e.hold_ms = dur * 0.55f;
            e.decay_ms = dur * 0.45f - (float)d->attack_ms;
            v = d->duty > 0.0f ? pulse(step_osc(&a, hz, rate), d->duty, hz)
                               : triangle(step_osc(&a, hz, rate), hz);
            break;
        }

        case SH_SWEEP: {
            const float flat = d->b;
            const float hz = t < flat ? base
                                      : base * exp2f(d->a * (t - flat) / (dur - flat));
            e.hold_ms = dur * 0.7f;
            e.decay_ms = dur * 0.3f - (float)d->attack_ms;
            v = pulse(step_osc(&a, hz, rate), d->duty > 0.0f ? d->duty : 0.5f, hz);
            /* The boing's wobble on the way back down, and only on the way back. */
            if (d->punch_pct >= 25 && u > 0.5f) {
                v *= 1.0f + 0.25f * sinf(TWO_PI * 22.0f * t * 0.001f);
            }
            break;
        }

        case SH_WARBLE: {
            const float hz = base * (1.0f + d->b * sinf(TWO_PI * d->a * t * 0.001f));
            e.hold_ms = dur * 0.75f;
            e.decay_ms = dur * 0.25f - (float)d->attack_ms;
            v = pulse(step_osc(&a, hz, rate), d->duty, hz);
            break;
        }

        case SH_COO: {
            /* Rises, then settles back — an arc rather than a climb, which is the difference
               between contentment and a question. A triangle, so it is warm rather than
               bright, with a soft fifth above it for body. A long attack on purpose: a coo
               has no impact, and a fast one would read as a beep. */
            const float arc = sinf((float)M_PI * u);
            const float hz = base * exp2f(d->a * arc);
            e.hold_ms = dur * 0.45f;
            e.decay_ms = dur * 0.55f - (float)d->attack_ms;
            v = triangle(step_osc(&a, hz, rate), hz) +
                0.28f * triangle(step_osc(&b, hz * 1.5f, rate), hz * 1.5f);
            break;
        }

        case SH_NOISE: {
            /* `b` is a tone under the noise (0 for none) and `steps` a bite pattern. The
               sneeze holds its breath first: the noise arrives after the tone has climbed. */
            const float bite = n_steps > 1 ? (fmodf(u * (float)n_steps, 1.0f) < 0.6f ? 1.0f : 0.0f)
                                           : 1.0f;
            const float gate = d->b > 0.0f && u < 0.35f ? u / 0.35f : 1.0f;
            float s =
            noise_next(&nz, d->a * vary * 1000.0f * (1.0f - 0.55f * u), rate) * gate * bite;
            if (d->b > 0.0f) {
                const float hz = d->b * vary * exp2f(-1.4f * u);
                s += 0.75f * triangle(step_osc(&a, hz, rate), hz);
            }
            e.hold_ms = dur * 0.2f;
            e.decay_ms = dur * 0.8f - (float)d->attack_ms;
            v = s;
            break;
        }

        case SH_RUDE: {
            /* What makes a noise read as a BODY and not a horn: the pitch moves while it
               sounds, the amplitude flutters fast enough to be texture rather than tremolo,
               and it runs out rather than stopping. A sawtooth supplies the harmonics a sine
               has not got. Every number comes from the character in FARTS[] (or BURP), so
               this is one renderer and six sounds rather than six branches. */
            const rude_t *r = rude_def(c, variant);
            const float hz = (r->hz - r->fall * u) * vary;
            const float ph = step_osc(&a, hz, rate);
            float s = ph / (float)M_PI - 1.0f + r->buzz * sinf(ph * 3.0f);
            if (r->wet > 0.0f) s += r->wet * noise_next(&nz, 3800.0f * vary, rate);
            /* CLAMPED AT ZERO, and that clamp is the sputterer. Below `depth` 0.5 it never
               bites and this is an ordinary tremolo; above it the envelope would go NEGATIVE,
               which inverts the waveform instead of interrupting it — a phase flip, audible
               as harshness rather than as a gap. Clamping turns the same number into real
               silence between bursts, which is what sputtering is. A corner in an envelope
               does not click; a jump would. */
            float trem = (1.0f - r->depth) + r->depth * sinf(TWO_PI * r->flutter * t * 0.001f);
            if (trem < 0.0f) trem = 0.0f;
            s *= trem;
            e.hold_ms = dur * 0.12f;
            e.decay_ms = dur * 0.88f - (float)d->attack_ms;
            v = s;
            break;
        }

        case SH_PAIR: {
            const float flat = 90.0f;
            const float fall = t < flat ? 1.0f : exp2f(d->a * (t - flat) / (dur - flat));
            const float fa = base * fall, fb = base * 1.0905f * fall;
            e.hold_ms = dur * 0.45f;
            e.decay_ms = dur * 0.55f - (float)d->attack_ms;
            v = 0.5f * (pulse(step_osc(&a, fa, rate), d->duty, fa) +
                        pulse(step_osc(&b, fb, rate), d->duty, fb));
            break;
        }

        default:
            return 0.0f;
        }

        v *= env_at(&e, t);

        if (out == NULL) {
            const float m = fabsf(v);
            if (m > peak) peak = m;
        } else {
            /* int32 and lrintf, not a cast to int16: the cast truncates toward zero, biasing
               every sample, and (int16_t)(1.0f * 32768.0f) wraps to -32768 — full positive
               becoming full negative, the loudest possible click. */
            int32_t s = (int32_t)lrintf(v * scale);
            if (s > 32767) s = 32767;
            if (s < -32768) s = -32768;
            out[i] = (int16_t)s;
        }
    }
    return peak;
}

int cue_render(cue_t c, int16_t *out, int rate, int gain, unsigned variant)
{
    if (c < 0 || c >= CUE_COUNT || out == NULL || rate <= 0) return 0;
    const int n =
    (int)(cue_ms(c, variant) * variant_stretch(variant) * (float)rate / 1000.0f + 0.5f);
    if (n <= 0 || n > CUE_MAX_SAMPLES) return 0;

    /* PEAK-NORMALISE BY MEASUREMENT, not arithmetic. The naive bound (the sum of the
       coefficients) is 2.7 for a square whose real peak is 1.18, and it moves as a sweep
       carries partials through the taper. The side effect is that every cue in the set lands
       at the same level, which is what stops one of them being the loud one. */
    const float peak = cue_pass(c, n, rate, variant, NULL, 0.0f);
    if (peak <= 0.0f) return 0;

    if (gain < 0) gain = 0;
    if (gain > 100) gain = 100;
    /* 26000 rather than 32767: a cue is an acknowledgement beside a speaking voice, and a
       couple of dB below full scale is what keeps it from being the loudest thing here. */
    cue_pass(c, n, rate, variant, out, 26000.0f * (float)gain / 100.0f / peak);
    return n;
}
