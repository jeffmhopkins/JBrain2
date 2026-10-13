#include "tp_cal_port.h"

static tp_cal_t s_kept, s_applied;
static uint32_t s_date;
static bool s_have, s_on, s_refuse;

bool tp_cal_port_load(tp_cal_t *cal, uint32_t *date)
{
    if (!s_have) return false;
    *cal = s_kept;
    *date = s_date;
    return true;
}

bool tp_cal_port_save(const tp_cal_t *cal, uint32_t date)
{
    if (s_refuse) return false;
    s_have = cal != 0;
    if (cal) s_kept = *cal;
    s_date = date;
    return true;
}

void tp_cal_port_apply(const tp_cal_t *cal, bool on)
{
    s_applied = *cal;
    s_on = on;
}

void tp_cal_port_fake_applied(tp_cal_t *cal, bool *on)
{
    *cal = s_applied;
    *on = s_on;
}

void tp_cal_port_fake_wipe(void)
{
    s_have = false;
    s_date = 0;
}

void tp_cal_port_fake_refuse_save(bool refuse)
{
    s_refuse = refuse;
}
