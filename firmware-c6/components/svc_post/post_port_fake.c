/* The voice post service's port on the host: one thread needs no lock, and RAM stands in for the
 * stamps' NVS blob, so the sim and the host suite can show a reboot keeps them. */
#include <string.h>

#include "post_port_fake.h"
#include "svc_post_drv.h"

static post_stamps_t s_flash;
static bool s_written;
static unsigned s_saves;

void post_port_lock(void) {}

void post_port_unlock(void) {}

bool post_port_load(post_stamps_t *out)
{
    if (!s_written) return false;
    *out = s_flash;
    return true;
}

void post_port_save(const post_stamps_t *in)
{
    s_flash = *in;
    s_written = true;
    s_saves++;
}

void post_port_fake_wipe(void)
{
    memset(&s_flash, 0, sizeof s_flash);
    s_written = false;
}

unsigned post_port_fake_saves(void)
{
    return s_saves;
}
