/* The debug surface (plan §4). The shell and every service register a describer; a snapshot
 * walks them all through dbg_json, which is where redaction lives. Apps do not register: the
 * shell describes the open app through app_t.describe, because only the open app has state. */
#pragma once

#include <stdbool.h>

#include "dbg_json.h"

typedef void (*dbg_describe_fn)(void *ctx, dbg_json_t *w);

bool svc_debug_register(const char *name, dbg_describe_fn fn, void *ctx);
void svc_debug_log(const char *tag, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
