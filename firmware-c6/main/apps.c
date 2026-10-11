/* The panel's app set: which apps exist, who the Thread instances are, and where each tile
 * sits. The only file that knows every app — the shell knows them through app_t alone. */
#include "apps.h"

#include "app_clock.h"
#include "app_jerv.h"
#include "app_minecraft.h"
#include "app_settings.h"
#include "app_thread.h"
#include "shell.h"

static const app_thread_config_t QUORRA = {"quorra", "Quorra", {UI_GLYPH_INITIAL, 'Q'}};
static const app_thread_config_t TAMSIN = {"tamsin", "Tamsin", {UI_GLYPH_INITIAL, 'T'}};
static app_t s_quorra, s_tamsin;

bool apps_register(const char *only)
{
    app_thread_make(&s_quorra, &QUORRA, UI_ACCENT_QUORRA, 0, 2);
    app_thread_make(&s_tamsin, &TAMSIN, UI_ACCENT_TAMSIN, 0, 3);
    app_t *all[] = {&app_jerv, &app_minecraft, &s_quorra, &s_tamsin, &app_clock, &app_settings};
    bool ok = true;
    for (unsigned i = 0; i < sizeof all / sizeof *all; i++)
        if (!only || !strcmp(only, all[i]->id)) ok &= shell_register(all[i]);
    return ok;
}
