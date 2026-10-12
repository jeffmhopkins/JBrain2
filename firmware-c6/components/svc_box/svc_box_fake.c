#include "box_logic.h"
#include "svc_box_fake.h"
#include "svc_debug.h"
#include "svc_net.h"

static fw_info_t s_fw = {"0.0.8", "", FW_IDLE, 0, ""};
static unsigned s_reports;

static const char *const PHASE[] = {"idle", "checking", "downloading", "restarting", "done", "uptodate", "failed"};

void svc_box_fw(fw_info_t *out)
{
    *out = s_fw;
}

void svc_box_fake_set(const fw_info_t *fw)
{
    s_fw = *fw;
}

bool svc_box_update_now(void)
{
    net_info_t n;
    svc_net_info(&n);
    if (!box_may_update(n.state, s_fw.phase)) return false;
    s_fw.phase = FW_CHECKING;
    svc_debug_log("box", "update now: manifest check");
    return true;
}

void svc_box_report_now(void)
{
    s_reports++;
}

unsigned svc_box_fake_reports(void)
{
    return s_reports;
}

uint32_t svc_box_quiet_s(void)
{
    return 0;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "version", s_fw.version);
    dbg_json_str(w, "ota_phase", PHASE[s_fw.phase]);
    dbg_json_int(w, "ota_pct", s_fw.pct);
    dbg_json_int(w, "box_quiet_s", svc_box_quiet_s());
    dbg_json_int(w, "reports", s_reports);
}

void svc_box_init(void)
{
    svc_debug_register("box", describe, NULL);
}
