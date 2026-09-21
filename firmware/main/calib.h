#pragma once

#include <stdbool.h>
#include <stdint.h>

/* TOUCH CALIBRATION: a grid of targets, and a piecewise-linear correction built from them.
 *
 * THE SYMPTOM THIS EXISTS FOR, in the owner's words: the middle of the panel is "pretty dead
 * on" and the outer 20% is skewed. That is the ordinary edge behaviour of these controllers —
 * the sensed position compresses toward the centre as you approach the bezel — and it is
 * exactly what a fixed scale factor cannot fix, because the error is zero in the middle and
 * grows outward. A correction with KNOTS, fitted to measurements, can.
 *
 * WHAT THIS MODEL DOES AND DOES NOT CORRECT. It is separable: an independent piecewise-linear
 * curve per axis, so it handles offset, scale and edge compression on x and y. It does NOT
 * correct a rotation or a corner that pulls diagonally, because those couple the axes. That is
 * a deliberate first cut — separable is monotone by construction, cannot fold the panel onto
 * itself from a noisy tap, and needs no cell search at runtime. The routine records the
 * residual at every target so the question "was separable enough?" is answered by measurement
 * rather than by this comment: if the corners still miss after calibrating, the next model is
 * a full bilinear grid and we will know why we paid for it.
 *
 * Pure C, no ESP headers, like the rest of the island `firmware/host` exercises.
 */

/* Knots per axis; the tap count is the square of it, so this is bought with the owner's
   patience. FOUR IS A MEASUREMENT, not a guess. Against a simulated edge compression whose
   worst error is 29 px, a piecewise-linear fit gives:

       knots   taps   worst residual
         3       9       12.3 px
         4      16        7.1 px
         5      25        4.6 px

   Three leaves an error you can still feel; five costs nine more taps to save 2.5 px. Four
   also holds its ratio across distortion strengths — 4.1 / 7.1 / 12.2 px against baselines of
   16 / 29 / 45 — which matters because the real panel's curve is unknown and over-fitting a
   model this file invented would be its own mistake. */
#define CAL_KNOTS 4

/* Where the targets sit, as a fraction of the panel. Deliberately not 0 and 1: a target in the
   literal corner cannot be hit with a fingertip, and the outermost band is extrapolated from
   the outer segment instead. Pushing the outer pair further toward the bezel helps only while
   the distortion is mild — at the strongest simulated curve `.04` is worse than `.06`, because
   the outer segment then has to bend too far. */
extern const float CAL_FRAC[CAL_KNOTS];

/* REPEATED SAMPLES PER TARGET, because one tap is not a measurement.
 *
 * The owner's observation: where a tap lands moves with how much fingertip goes down. On a
 * 1.8" panel a millimetre of contact-patch drift is about eleven pixels, so a single reading
 * carries more noise than the edge error the whole routine exists to remove. Taking several
 * and requiring them to AGREE is what turns a tap into a measurement.
 *
 * The median, not the mean: one slip with the side of a finger would drag a mean several
 * pixels and cannot move a median at all.
 *
 * Adaptive rather than fixed: a target that settles in three taps costs three, and only a
 * wobbly one costs more. That is the owner's "keep on pressing it until we have conformance"
 * — with a cap, because a target that will not settle must not trap them on it forever. */
#define CAL_SAMPLES_MIN 3
#define CAL_SAMPLES_MAX 6
/* Raw units of disagreement tolerated across the samples for a target. Roughly a millimetre
   on this panel: tight enough that it is really the same spot, loose enough that an adult
   tapping normally reaches it. */
#define CAL_SPREAD_MAX 14

typedef struct {
    int16_t x[CAL_SAMPLES_MAX];
    int16_t y[CAL_SAMPLES_MAX];
    int n;
} cal_samples_t;

void calib_sample_reset(cal_samples_t *s);
/* Keeps the most recent CAL_SAMPLES_MAX — a run that starts badly and settles should be
   judged on where it settled, not on where it began. */
void calib_sample_add(cal_samples_t *s, int x, int y);
/* Enough samples, and they agree. */
bool calib_sample_settled(const cal_samples_t *s);
/* The widest disagreement on either axis, for showing the owner how the target is going. */
int calib_sample_spread(const cal_samples_t *s);
/* The median of what has been collected. Defined for any n > 0, so a capped-out target still
   yields its best estimate rather than nothing. */
void calib_sample_result(const cal_samples_t *s, int16_t *x, int16_t *y);

typedef struct {
    bool valid;
    /* The raw controller reading observed at each knot, ascending. */
    int16_t raw_x[CAL_KNOTS];
    int16_t raw_y[CAL_KNOTS];
} calib_t;

/* The screen coordinate a knot targets, in panel pixels. */
int calib_target_x(int i);
int calib_target_y(int j);

/* Map a raw controller point to a screen point. The identity when `c` is NULL or not valid,
   so an uncalibrated panel behaves exactly as it did before this file existed. */
void calib_apply(const calib_t *c, int raw_x, int raw_y, int *sx, int *sy);

/* Build a calibration from a completed grid: `mx[j][i]`/`my[j][i]` are the raw readings taken
   while the target for column `i`, row `j` was displayed.

   Each knot averages across the perpendicular axis — the three readings in a column all aimed
   at the same x — which both cuts noise and is what makes the separable model well defined.

   False when the result is not strictly monotone on either axis. That is the one failure mode
   that matters: a non-monotone map folds part of the panel on top of another part, and a
   calibration that makes touch WORSE is far worse than none, because the way out of it is the
   touchscreen you just broke. */
bool calib_build(const int16_t mx[CAL_KNOTS][CAL_KNOTS], const int16_t my[CAL_KNOTS][CAL_KNOTS],
                 calib_t *out);

/* Serialise to a fixed little-endian blob for NVS, and back. Returns bytes written/read. */
#define CAL_BLOB_BYTES (2 + CAL_KNOTS * 4)
int calib_save(const calib_t *c, uint8_t *buf, int cap);
bool calib_load(const uint8_t *buf, int len, calib_t *out);
