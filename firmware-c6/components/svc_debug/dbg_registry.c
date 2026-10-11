#include <string.h>

#include "dbg_core.h"

typedef struct {
    const char *name;
    dbg_describe_fn fn;
    void *ctx;
} provider_t;

static provider_t s_prov[DBG_PROVIDERS_MAX];
static unsigned s_n;

bool svc_debug_register(const char *name, dbg_describe_fn fn, void *ctx)
{
    if (!name || !fn) return false;
    for (unsigned i = 0; i < s_n; i++)
        if (!strcmp(s_prov[i].name, name)) {
            s_prov[i].fn = fn;
            s_prov[i].ctx = ctx;
            return true;
        }
    if (s_n >= DBG_PROVIDERS_MAX) return false;
    s_prov[s_n++] = (provider_t){name, fn, ctx};
    return true;
}

void dbg_registry_reset(void)
{
    s_n = 0;
}

void dbg_snapshot(dbg_json_t *w)
{
    dbg_json_obj_begin(w, NULL);
    dbg_snapshot_fields(w);
    dbg_json_obj_end(w);
}

void dbg_snapshot_fields(dbg_json_t *w)
{
    dbg_json_int(w, "v", 1);
    for (unsigned i = 0; i < s_n; i++) {
        dbg_json_obj_begin(w, s_prov[i].name);
        s_prov[i].fn(s_prov[i].ctx, w);
        dbg_json_obj_end(w);
    }
}
