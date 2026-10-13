#include "orient_port_fake.h"
#include "svc_orient_drv.h"

/* Upright on a desk stand, leaning back a little: most of a g toward the glass's bottom, which
 * with the default axes (orient_down) is the chip's -Y. */
static int16_t s_a[3] = {0, -15400, 5600};
static const char *s_fail;
static bool s_on;
static unsigned s_reads;

void orient_port_fake_set(int16_t ax, int16_t ay, int16_t az)
{
    s_a[0] = ax;
    s_a[1] = ay;
    s_a[2] = az;
}

void orient_port_fake_fail(const char *step)
{
    s_fail = step;
}

bool orient_port_fake_powered(void)
{
    return s_on;
}

unsigned orient_port_fake_reads(void)
{
    return s_reads;
}

bool orient_port_start(const char **step)
{
    *step = s_fail;
    return s_fail == NULL;
}

bool orient_port_power(bool on)
{
    s_on = on;
    return true;
}

bool orient_port_read(int16_t a[3])
{
    if (!s_on) return false;
    s_reads++;
    a[0] = s_a[0];
    a[1] = s_a[1];
    a[2] = s_a[2];
    return true;
}

void orient_port_describe(dbg_json_t *w)
{
    dbg_json_str(w, "chip", "fake");
}
