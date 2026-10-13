#include "svc_display.h"

#include "svc_debug.h"
#include "svc_display_fake.h"

static uint8_t s_pct = 80;
static bool s_dark;
static bool s_applied;
/* What svc_orient asked and what the glass took: the device's rule (svc_display_co5300.c),
   including a quarter turn that finds no memory for its piece buffer and waits. */
static uint8_t s_rot, s_rot_req;
static bool s_no_piece;
static uint32_t s_rot_fail;
static void (*s_rotated)(uint8_t quarters);

void svc_display_set_brightness(uint8_t pct)
{
    s_pct = pct;
    s_applied = true;
}

uint8_t svc_display_brightness(void)
{
    return s_pct;
}

void svc_display_set_dark(bool dark)
{
    s_dark = dark;
}

bool svc_display_dark(void)
{
    return s_dark;
}

bool svc_display_applied(void)
{
    return s_applied;
}

void svc_display_fake_service(void)
{
    if (s_rot_req == s_rot) return;
    if ((s_rot_req & 1) && s_no_piece) {
        s_rot_fail++;
        return;
    }
    s_rot = s_rot_req;
    if (s_rotated) s_rotated(s_rot);
}

void svc_display_set_rotation(uint8_t quarters)
{
    s_rot_req = quarters & 3;
    /* No frames to wait for in the sim and the host: applied at once, unless refused. */
    svc_display_fake_service();
}

uint8_t svc_display_rotation(void)
{
    return s_rot;
}

void svc_display_on_rotated(void (*fn)(uint8_t quarters))
{
    s_rotated = fn;
}

void svc_display_fake_no_piece(bool refuse)
{
    s_no_piece = refuse;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_int(w, "brightness", s_pct);
    dbg_json_bool(w, "dark", s_dark);
    dbg_json_bool(w, "applied", s_applied);
    dbg_json_int(w, "rotation", s_rot * 90);
    dbg_json_int(w, "rot_no_memory", s_rot_fail);
    dbg_json_str(w, "driver", "fake");
}

void svc_display_init(void)
{
    svc_debug_register("display", describe, NULL);
}
