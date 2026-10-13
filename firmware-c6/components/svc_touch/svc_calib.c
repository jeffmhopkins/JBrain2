/* Touch calibration (0.0.18): the fit's bookkeeping, the same on the device and in the sim. The
 * maths is tp_logic.c's; keeping it and handing it to the touch task is the port's
 * (tp_cal_port.h). Everything here runs on the UI task. */
#include <stdio.h>

#include "svc_calib.h"
#include "svc_debug.h"
#include "svc_orient.h"
#include "svc_rtc.h"
#include "tp_cal_port.h"

static tp_cal_t s_cal;
static bool s_custom, s_capturing, s_loaded;
static uint32_t s_date, s_fits, s_refused;
static calib_result_t s_last = CALIB_SAVED;
static bool s_any_result;

static void describe(void *ctx, dbg_json_t *w);

/* Loaded on first use rather than at a start of its own: the sim, the host and the device each
   reach it first from a different place, and NVS is up long before the UI task asks. */
static void load(void)
{
    if (s_loaded) return;
    s_loaded = true;
    svc_debug_register("calib", describe, NULL);
    s_custom = tp_cal_port_load(&s_cal, &s_date);
    if (!s_custom) {
        tp_cal_identity(&s_cal);
        s_date = 0;
    }
    tp_cal_port_apply(&s_cal, s_custom);
}

bool svc_calib_custom(void)
{
    load();
    return s_custom;
}

uint32_t svc_calib_date(void)
{
    load();
    return s_custom ? s_date : 0;
}

bool svc_calib_capturing(void)
{
    return s_capturing;
}

void svc_calib_begin(void)
{
    load();
    s_capturing = true;
    tp_cal_port_apply(&s_cal, false);
    svc_orient_hold(true);
}

void svc_calib_cancel(void)
{
    load();
    if (!s_capturing) return;
    s_capturing = false;
    tp_cal_port_apply(&s_cal, s_custom);
    svc_orient_hold(false);
}

static uint32_t today(void)
{
    rtc_time_t t;
    svc_rtc_now(&t);
    return t.valid ? (uint32_t)t.year * 10000u + t.mon * 100u + t.mday : 0;
}

calib_result_t svc_calib_finish(const calib_pair_t *pairs, uint8_t n)
{
    load();
    /* Into the glass's own frame, the one the fit lives in: the screen is held, so the turn the
       taps were made under is still the one showing. */
    tp_pt_t want[16], got[16];
    if (n > 16) n = 16;
    const uint8_t rot = svc_orient_rotation();
    for (uint8_t i = 0; i < n; i++) {
        tp_rotate(rot, pairs[i].want_x, pairs[i].want_y, &want[i].x, &want[i].y);
        tp_rotate(rot, pairs[i].got_x, pairs[i].got_y, &got[i].x, &got[i].y);
    }
    /* The taps were measured with no calibration in force, so the fit replaces the old one. */
    tp_cal_t c;
    const tp_fit_t f = tp_cal_fit(want, got, n, &c);
    s_capturing = false;
    svc_orient_hold(false);
    s_any_result = true;
    if (f != TP_FIT_OK) {
        s_refused++;
        tp_cal_port_apply(&s_cal, s_custom);
        s_last = f == TP_FIT_WILD ? CALIB_WILD : f == TP_FIT_SCATTERED ? CALIB_SCATTERED : CALIB_FEW;
        return s_last;
    }
    s_fits++;
    s_cal = c;
    s_custom = true;
    s_date = today();
    tp_cal_port_apply(&s_cal, true);
    s_last = tp_cal_port_save(&s_cal, s_date) ? CALIB_SAVED : CALIB_NOT_SAVED;
    return s_last;
}

void svc_calib_reset(void)
{
    load();
    s_capturing = false;
    s_custom = false;
    s_date = 0;
    tp_cal_identity(&s_cal);
    tp_cal_port_apply(&s_cal, false);
    tp_cal_port_save(NULL, 0);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    static const char *const RESULT[] = {"saved", "few", "wild", "scattered", "not_saved"};
    dbg_json_bool(w, "custom", s_custom);
    dbg_json_bool(w, "capturing", s_capturing);
    dbg_json_int(w, "date", s_custom ? s_date : 0);
    /* a,b,c,d,e,f: x' = a·x + b·y + c, y' = d·x + e·y + f in the glass's frame, the scales and
       shears in thousandths and the offsets in pixels, so 1000,0,0,0,1000,0 is no correction. */
    char k[96];
    snprintf(k, sizeof k, "%ld,%ld,%ld,%ld,%ld,%ld", (long)s_cal.a * 1000 / TP_Q16,
             (long)s_cal.b * 1000 / TP_Q16, (long)(s_cal.c / TP_Q16), (long)s_cal.d * 1000 / TP_Q16,
             (long)s_cal.e * 1000 / TP_Q16, (long)(s_cal.f / TP_Q16));
    dbg_json_str(w, "fit", k);
    dbg_json_int(w, "fits", s_fits);
    dbg_json_int(w, "refused", s_refused);
    if (s_any_result) dbg_json_str(w, "last", RESULT[s_last]);
}
