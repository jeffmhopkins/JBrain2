/* Touch calibration as Settings drives it (0.0.18, mock Calib.dc.html). The glass's taps are
 * corrected by an affine fit in the glass's own frame (svc_touch's tp_logic.h), kept in NVS and
 * applied to every touch from then on. Settings shows the targets, judges each tap, and hands
 * this the result; it fits, checks, saves and applies. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* A target and where its taps landed, both as the upright screen has them (LVGL's coordinates,
 * whichever way the panel is turned). */
typedef struct {
    int16_t want_x, want_y, got_x, got_y;
} calib_pair_t;

typedef enum {
    CALIB_SAVED,
    CALIB_FEW,       /* too few targets, or taps in a line: no correction can be fitted */
    CALIB_WILD,      /* a correction no real panel needs: a tap went far astray */
    CALIB_SCATTERED, /* the taps disagree with every straight correction */
    CALIB_NOT_SAVED, /* fitted, but flash refused it: still in force until the next boot */
} calib_result_t;

/* From here until finish or cancel, taps arrive with no calibration in force, and the panel
 * holds whichever way up it is (svc_orient_hold). */
void svc_calib_begin(void);
/* BOOT, or the app closed: the old calibration (or none) back in force. */
void svc_calib_cancel(void);
calib_result_t svc_calib_finish(const calib_pair_t *pairs, uint8_t n);
/* Settings' "Reset calibration": the 0.0.7 measured mapping alone, and the saved fit erased. */
void svc_calib_reset(void);
bool svc_calib_custom(void);
bool svc_calib_capturing(void);
/* The local day the calibration was saved, as yyyymmdd; 0 when there is none or no clock then. */
uint32_t svc_calib_date(void);
