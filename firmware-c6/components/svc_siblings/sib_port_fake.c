/* The siblings cache on the host: RAM standing in for the device's NVS blob, so the sim and the
 * host suite can show that a reboot keeps the names (svc_siblings_init reads it back). */
#include <string.h>

#include "sib_port_fake.h"
#include "svc_siblings_drv.h"

static sib_slots_t s_flash;
static bool s_written;

bool sib_port_load(sib_slots_t *out)
{
    if (!s_written) return false;
    *out = s_flash;
    return true;
}

void sib_port_save(const sib_slots_t *in)
{
    s_flash = *in;
    s_written = true;
}

void sib_port_fake_wipe(void)
{
    memset(&s_flash, 0, sizeof s_flash);
    s_written = false;
}
