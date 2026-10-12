#include <ctype.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#include "dbg_core.h"

/* THE WHITELIST. A command not named here cannot be bound, so nothing reaches the panel
 * through the debug route that was not decided in review. */
static const dbg_cmd_info_t CMDS[] = {
    {"help", false, true},       {"snapshot", false, true},  {"logs", false, true},
    {"log_level", false, true},  {"open", false, true},      {"home", false, true},
    {"set", false, true},        {"inject", false, false},   {"ota_check", false, true},
    {"report_now", false, true}, {"screenshot", false, true}, {"reboot", true, false},
    {"play_tone", false, true},
};
#define NCMDS (sizeof CMDS / sizeof *CMDS)

static dbg_cmd_fn s_fn[NCMDS];

const dbg_cmd_info_t *dbg_cmd_find(const char *name)
{
    for (size_t i = 0; i < NCMDS; i++)
        if (!strcmp(CMDS[i].name, name)) return &CMDS[i];
    return NULL;
}

bool dbg_cmd_bind(const char *name, dbg_cmd_fn fn)
{
    const dbg_cmd_info_t *c = dbg_cmd_find(name);
    if (!c) return false;
    s_fn[c - CMDS] = fn;
    return true;
}

void dbg_cmd_reset(void)
{
    memset(s_fn, 0, sizeof s_fn);
}

/* ---- the flat parser ---- */

static const char *ws(const char *p)
{
    while (*p && isspace((unsigned char)*p)) p++;
    return p;
}

/* Copies a JSON string body into dst; returns the char after the closing quote, or NULL. */
static const char *str(const char *p, char *dst, size_t cap, bool *too_long)
{
    size_t n = 0;
    for (p++; *p && *p != '"'; p++) {
        char c = *p;
        if (c == '\\') {
            p++;
            if (!*p) return NULL;
            c = *p == 'n' ? '\n' : *p == 't' ? '\t' : *p;
            if (*p == 'u') return NULL; /* nothing a command needs */
        }
        if (n + 1 < cap)
            dst[n++] = c;
        else
            *too_long = true;
    }
    if (*p != '"') return NULL;
    if (cap) dst[n] = '\0';
    return p + 1;
}

typedef struct {
    const char *key;
    size_t off, cap;
} sfield_t;

#define SF(k, f) {k, offsetof(dbg_req_t, f), sizeof(((dbg_req_t *)0)->f)}
static const sfield_t SFIELDS[] = {SF("id", id),       SF("cmd", cmd),   SF("app", app),
                                   SF("tag", tag),     SF("event", event), SF("name", name)};

typedef struct {
    const char *key;
    size_t off;
} ifield_t;

#define IF(k, f) {k, offsetof(dbg_req_t, f)}
static const ifield_t IFIELDS[] = {IF("level", level), IF("x", x), IF("y", y), IF("value", value)};

dbg_parse_t dbg_req_parse(const char *json, dbg_req_t *out)
{
    memset(out, 0, sizeof *out);
    const char *p = ws(json);
    if (*p++ != '{') return DBG_PARSE_BAD;
    p = ws(p);
    if (*p == '}') return ws(p + 1)[0] ? DBG_PARSE_BAD : DBG_PARSE_OK;
    for (;;) {
        char key[16];
        bool too_long = false;
        if (*p != '"' || !(p = str(p, key, sizeof key, &too_long))) return DBG_PARSE_BAD;
        p = ws(p);
        if (*p++ != ':') return DBG_PARSE_BAD;
        p = ws(p);
        if (*p == '{' || *p == '[') return DBG_PARSE_NESTED;
        bool known = false;
        for (size_t i = 0; i < sizeof SFIELDS / sizeof *SFIELDS && !known; i++) {
            if (strcmp(key, SFIELDS[i].key)) continue;
            known = true;
            if (*p != '"') return DBG_PARSE_BAD;
            bool tl = false;
            p = str(p, (char *)out + SFIELDS[i].off, SFIELDS[i].cap, &tl);
            if (!p) return DBG_PARSE_BAD;
            if (tl) return DBG_PARSE_TOO_LONG;
        }
        for (size_t i = 0; i < sizeof IFIELDS / sizeof *IFIELDS && !known; i++) {
            if (strcmp(key, IFIELDS[i].key)) continue;
            known = true;
            char *end;
            long v = strtol(p, &end, 10);
            if (end == p || v < INT32_MIN || v > INT32_MAX) return DBG_PARSE_BAD;
            *(int32_t *)((char *)out + IFIELDS[i].off) = (int32_t)v;
            p = end;
        }
        if (!known && !strcmp(key, "confirm")) {
            known = true;
            if (!strncmp(p, "true", 4)) {
                out->confirm = true;
                p += 4;
            } else if (!strncmp(p, "false", 5)) {
                p += 5;
            } else {
                return DBG_PARSE_BAD;
            }
        }
        if (!known) {
            /* Unknown keys are skipped so a newer box can send fields an older panel ignores. */
            if (*p == '"') {
                char sink[1];
                bool tl = false;
                if (!(p = str(p, sink, 0, &tl))) return DBG_PARSE_BAD;
            } else {
                while (*p && *p != ',' && *p != '}') p++;
            }
        }
        p = ws(p);
        if (*p == ',') {
            p = ws(p + 1);
            continue;
        }
        if (*p == '}') return ws(p + 1)[0] ? DBG_PARSE_BAD : DBG_PARSE_OK;
        return DBG_PARSE_BAD;
    }
}

/* ---- dispatch ---- */

static bool cmd_help(const dbg_req_t *req, dbg_json_t *data, char *err, size_t n)
{
    (void)req, (void)err, (void)n;
    dbg_json_arr_begin(data, "commands");
    for (size_t i = 0; i < NCMDS; i++) {
        dbg_json_obj_begin(data, NULL);
        dbg_json_str(data, "name", CMDS[i].name);
        dbg_json_bool(data, "destructive", CMDS[i].destructive);
        dbg_json_bool(data, "idempotent", CMDS[i].idempotent);
        dbg_json_bool(data, "available", i < 3 || s_fn[i] != NULL);
        dbg_json_obj_end(data);
    }
    dbg_json_arr_end(data);
    return true;
}

static bool cmd_snapshot(const dbg_req_t *req, dbg_json_t *data, char *err, size_t n)
{
    (void)req, (void)err, (void)n;
    dbg_snapshot_fields(data);
    return true;
}

static bool cmd_logs(const dbg_req_t *req, dbg_json_t *data, char *err, size_t n)
{
    (void)req, (void)err, (void)n;
    dbg_json_int(data, "evicted", dbg_log_evicted());
    dbg_log_write(data, "lines");
    return true;
}

static const char *parse_err(dbg_parse_t r)
{
    return r == DBG_PARSE_NESTED ? "nested_not_allowed" : r == DBG_PARSE_TOO_LONG ? "too_long" : "parse";
}

void dbg_cmd_run(const char *req_json, dbg_json_t *out)
{
    dbg_req_t req;
    dbg_parse_t pr = dbg_req_parse(req_json, &req);
    dbg_json_obj_begin(out, NULL);
    dbg_json_str(out, "id", req.id);
    dbg_json_str(out, "cmd", req.cmd);
    if (pr != DBG_PARSE_OK) {
        dbg_json_bool(out, "ok", false);
        dbg_json_str(out, "err", parse_err(pr));
        dbg_json_obj_end(out);
        return;
    }
    const dbg_cmd_info_t *c = dbg_cmd_find(req.cmd);
    const char *e = NULL;
    dbg_cmd_fn fn = NULL;
    if (!c)
        e = "unknown_command";
    else if (c->destructive && !req.confirm)
        e = "needs_confirm";
    else if (!strcmp(c->name, "help"))
        fn = cmd_help;
    else if (!strcmp(c->name, "snapshot"))
        fn = cmd_snapshot;
    else if (!strcmp(c->name, "logs"))
        fn = cmd_logs;
    else if (!(fn = s_fn[c - CMDS]))
        e = "unavailable";
    if (c) dbg_json_bool(out, "destructive", c->destructive);
    if (fn) {
        char err[48] = "";
        dbg_json_obj_begin(out, "data");
        bool ok = fn(&req, out, err, sizeof err);
        dbg_json_obj_end(out);
        dbg_json_bool(out, "ok", ok);
        if (!ok) dbg_json_str(out, "err", err[0] ? err : "failed");
    } else {
        dbg_json_bool(out, "ok", false);
        dbg_json_str(out, "err", e);
    }
    dbg_json_obj_end(out);
}
