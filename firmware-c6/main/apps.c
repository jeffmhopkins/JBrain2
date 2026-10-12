/* The panel's app set: which apps exist, which sibling each Thread instance is bound to, and
 * where each tile sits. The only file that knows every app — the shell knows them through app_t
 * alone. Nobody's name is here: a thread is called what the box says its panel is called
 * (svc_siblings). */
#include "apps.h"

#include "app_clock.h"
#include "app_jerv.h"
#include "app_minecraft.h"
#include "app_settings.h"
#include "app_thread.h"
#include "shell.h"
#include "svc_messages.h"

static const app_thread_config_t FIRST = {SVC_MESSAGES_THREAD_1, 0};
static const app_thread_config_t SECOND = {SVC_MESSAGES_THREAD_2, 1};
static app_t s_first, s_second;

bool apps_register(const char *only)
{
    app_thread_make(&s_first, &FIRST, 0, 2);
    app_thread_make(&s_second, &SECOND, 0, 3);
    app_t *all[] = {&app_jerv, &app_minecraft, &s_first, &s_second, &app_clock, &app_settings};
    bool ok = true;
    for (unsigned i = 0; i < sizeof all / sizeof *all; i++)
        if (!only || !strcmp(only, all[i]->id)) ok &= shell_register(all[i]);
    return ok;
}
