/* The alarms cache on the host: RAM standing in for the device's NVS blob, so the sim and the host
 * suite can show that a reboot keeps the alarms and the switches still on their way. */
#include <string.h>

#include "alarm_port_fake.h"
#include "svc_alarms_drv.h"

static alarm_cache_t s_flash;
static bool s_written;
static uint32_t s_saves;

bool alarm_port_load(alarm_cache_t *out)
{
    if (!s_written) return false;
    *out = s_flash;
    return true;
}

void alarm_port_save(const alarm_cache_t *in)
{
    s_flash = *in;
    s_written = true;
    s_saves++;
}

void alarm_port_lock(void) {}

void alarm_port_unlock(void) {}

void alarm_port_fake_wipe(void)
{
    memset(&s_flash, 0, sizeof s_flash);
    s_written = false;
    s_saves = 0;
}

uint32_t alarm_port_fake_saves(void)
{
    return s_saves;
}
