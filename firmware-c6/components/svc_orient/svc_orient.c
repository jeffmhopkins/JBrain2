/* Auto-rotate (0.0.18, plan §2b): the QMI8658 read on the UI task's own passes, the decision in
 * orient_logic.c, the content turned by svc_display. Touch follows the turn the display actually
 * took (svc_display_on_rotated, wired by the device's UI task), never this service's request.
 * Runs the same in the sim. */
#include <stdio.h>
#include <string.h>

#include "orient_logic.h"
#include "svc_debug.h"
#include "svc_display.h"
#include "svc_orient.h"
#include "svc_orient_drv.h"
#include "svc_settings.h"

/* "off" until started; written once on the starting task before `s_up`, then only read. */
static char s_state[20] = "off";
static volatile bool s_up;
/* The UI task's own from here down. */
static orient_t s_o;
static bool s_powered, s_hold;
static uint32_t s_next_ms, s_reads, s_errs;
static int16_t s_raw[3];
static bool s_have_raw;
/* The last turn asked of the display; what the glass shows is svc_display_rotation(). */
static uint8_t s_asked;

static void describe(void *ctx, dbg_json_t *w);

bool svc_orient_start(void)
{
    static bool registered;
    if (!registered) registered = svc_debug_register("orient", describe, NULL);
    s_up = false;
    const char *step = NULL;
    if (!orient_port_start(&step)) {
        snprintf(s_state, sizeof s_state, "failed:%s", step ? step : "?");
        return false;
    }
    snprintf(s_state, sizeof s_state, "ok");
    s_up = true;
    return true;
}

bool svc_orient_available(void)
{
    return s_up;
}

uint8_t svc_orient_rotation(void)
{
    return svc_display_rotation();
}

void svc_orient_hold(bool hold)
{
    s_hold = hold;
    orient_forget(&s_o);
}

static void apply(uint8_t rot)
{
    if (rot == s_asked) return;
    s_asked = rot;
    svc_display_set_rotation(rot);
}

void svc_orient_service(uint32_t now_ms)
{
    const bool on = s_up && svc_settings_get(SET_AUTO_ROTATE) != 0;
    /* Read only while someone could be looking: a dark panel holds the turn it had. */
    const bool want = on && !svc_display_dark();
    const bool due = (int32_t)(now_ms - s_next_ms) >= 0;
    if (want != s_powered && due) {
        if (orient_port_power(want)) {
            s_powered = want;
            orient_forget(&s_o);
        } else {
            /* A chip that stopped answering costs a bounded bus wait a second, not every pass. */
            s_errs++;
            s_next_ms = now_ms + ORIENT_RETRY_MS;
        }
    }
    if (!on) {
        /* Switched off, or no sensor: the panel's own upright, and a fresh start when it is on. */
        s_o.rot = 0;
        s_o.seen = ORIENT_SEEN_NONE;
        orient_forget(&s_o);
    } else if (s_powered && !s_hold && (int32_t)(now_ms - s_next_ms) >= 0) {
        s_next_ms = now_ms + ORIENT_PERIOD_MS;
        int16_t a[3];
        if (orient_port_read(a)) {
            s_reads++;
            memcpy(s_raw, a, sizeof s_raw);
            s_have_raw = true;
            orient_feed(&s_o, (uint8_t)svc_settings_get(SET_IMU_AXES), a, now_ms);
        } else {
            s_errs++;
            s_next_ms = now_ms + ORIENT_RETRY_MS;
        }
    }
    apply(s_o.rot);
}

/* With the panel upright, the `imu_axes` that would read this reading as "bottom": the board's
   answer to HARDWARE.md §6's open question, one snapshot away. Keeps the current x flip when both
   do, since one reading cannot tell them apart. */
static int axes_if_upright(uint8_t axes)
{
    if (!s_have_raw) return -1;
    const uint8_t order[9] = {axes, 0, 1, 2, 3, 4, 5, 6, 7};
    for (int i = 0; i < 9; i++) {
        orient_t t;
        orient_init(&t);
        t.rot = 1; /* anything but bottom, so a bottom reading is a candidate */
        orient_feed(&t, order[i], s_raw, 0);
        if (t.seen == ORIENT_SEEN_EDGE && t.edge == 0 && (order[i] & 2) == (axes & 2)) return order[i];
    }
    return -1;
}

/* With the panel on its right edge, and `axes` already right for upright (axes_if_upright): the
   value with the x flip that reads this reading as "right". One upright reading cannot settle that
   bit; this one does, and without it 90° and 270° come out swapped. */
static int axes_if_right_edge(uint8_t axes)
{
    if (!s_have_raw) return -1;
    const uint8_t order[2] = {axes, (uint8_t)(axes ^ 2)};
    for (int i = 0; i < 2; i++) {
        orient_t t;
        orient_init(&t);
        orient_feed(&t, order[i], s_raw, 0);
        if (t.seen == ORIENT_SEEN_EDGE && t.edge == 1) return order[i];
    }
    return -1;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    const uint8_t axes = (uint8_t)svc_settings_get(SET_IMU_AXES);
    dbg_json_str(w, "imu", s_state);
    dbg_json_bool(w, "auto", svc_settings_get(SET_AUTO_ROTATE) != 0);
    const uint8_t shown = svc_display_rotation();
    dbg_json_int(w, "rotation", shown * 90);
    if (shown != s_asked) dbg_json_int(w, "asked", s_asked * 90);
    dbg_json_str(w, "down", orient_edge_name(shown));
    dbg_json_str(w, "seen", orient_seen_name(s_o.seen));
    if (s_o.seen == ORIENT_SEEN_EDGE) dbg_json_str(w, "seen_edge", orient_edge_name(s_o.edge));
    if (s_have_raw) {
        char raw[40], mg[40];
        snprintf(raw, sizeof raw, "%d,%d,%d", s_raw[0], s_raw[1], s_raw[2]);
        snprintf(mg, sizeof mg, "%ld,%ld,%ld", (long)s_raw[0] * 1000 / ORIENT_1G,
                 (long)s_raw[1] * 1000 / ORIENT_1G, (long)s_raw[2] * 1000 / ORIENT_1G);
        dbg_json_str(w, "raw", raw);
        dbg_json_str(w, "raw_mg", mg);
    }
    dbg_json_int(w, "axes", axes);
    dbg_json_int(w, "axes_if_upright", axes_if_upright(axes));
    dbg_json_int(w, "axes_if_right_edge", axes_if_right_edge(axes));
    dbg_json_bool(w, "powered", s_powered);
    dbg_json_bool(w, "held", s_hold);
    dbg_json_int(w, "reads", s_reads);
    dbg_json_int(w, "read_errors", s_errs);
    dbg_json_int(w, "changes", s_o.changes);
    orient_port_describe(w);
}

void svc_orient_note(char *out, size_t len)
{
    snprintf(out, len, "orient=%s,rot:%u,chg:%lu,rd:%lu,err:%lu,axes:%ld", s_state,
             (unsigned)svc_display_rotation() * 90, (unsigned long)s_o.changes, (unsigned long)s_reads,
             (unsigned long)s_errs, (long)svc_settings_get(SET_IMU_AXES));
}
