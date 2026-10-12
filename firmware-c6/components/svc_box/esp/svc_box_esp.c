/* The Firmware card's window onto the update loop (main.c). The loop writes, the UI reads: the
 * phase and percentage are single bytes, and `next` and `err` are written before the phase
 * that shows them, so the card never shows a phase with the text of the previous one. */

#include <stdio.h>
#include <string.h>

#include "box_logic.h"
#include "esp_timer.h"
#include "svc_box.h"
#include "svc_box_drv.h"
#include "svc_debug.h"
#include "svc_net.h"

static const char *s_version = "";
static void (*s_wake)(void);
static volatile bool s_request, s_report;
static volatile fw_phase_t s_phase = FW_IDLE;
static volatile uint8_t s_pct;
static char s_next[16], s_err[24];
static volatile int64_t s_reached_us = -1;
static unsigned s_presses;

static const char *const PHASE[] = {"idle", "checking", "downloading", "restarting",
                                    "done", "uptodate", "failed"};

void svc_box_fw(fw_info_t *out)
{
    memset(out, 0, sizeof *out);
    snprintf(out->version, sizeof out->version, "%s", s_version);
    snprintf(out->next, sizeof out->next, "%s", s_next);
    snprintf(out->err, sizeof out->err, "%s", s_err);
    out->phase = s_phase;
    out->pct = s_pct;
}

bool svc_box_update_now(void)
{
    net_info_t n;
    svc_net_info(&n);
    if (!box_may_update(n.state, s_phase)) return false;
    s_presses++;
    s_phase = FW_CHECKING;
    s_request = true;
    if (s_wake) s_wake();
    return true;
}

void svc_box_report_now(void)
{
    s_report = true;
    if (s_wake) s_wake();
}

bool svc_box_take_report_request(void)
{
    const bool r = s_report;
    s_report = false;
    return r;
}

uint32_t svc_box_quiet_s(void)
{
    const int64_t at = s_reached_us;
    return at < 0 ? UINT32_MAX : (uint32_t)((esp_timer_get_time() - at) / 1000000);
}

bool svc_box_take_update_request(void)
{
    const bool r = s_request;
    s_request = false;
    return r;
}

void svc_box_set_phase(fw_phase_t phase, const char *next, uint8_t pct, const char *err)
{
    if (next) snprintf(s_next, sizeof s_next, "%s", next);
    if (err) snprintf(s_err, sizeof s_err, "%s", err);
    s_pct = pct;
    s_phase = phase;
}

void svc_box_reached(void)
{
    s_reached_us = esp_timer_get_time();
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "version", s_version);
    dbg_json_str(w, "ota_phase", PHASE[s_phase]);
    dbg_json_int(w, "ota_pct", s_pct);
    dbg_json_str(w, "next", s_next);
    dbg_json_str(w, "err", s_err);
    dbg_json_int(w, "update_presses", s_presses);
    dbg_json_int(w, "box_quiet_s", s_reached_us < 0 ? -1 : (int64_t)svc_box_quiet_s());
}

void svc_box_start(const char *version, void (*wake)(void))
{
    s_version = version;
    s_wake = wake;
    svc_debug_register("box", describe, NULL);
}
