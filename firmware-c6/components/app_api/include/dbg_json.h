/* The one writer every debug snapshot, log dump and command result goes through. Redaction is
 * here, not in the callers, so a provider cannot leak a secret by forgetting to: a key on the
 * deny-list never has its value written, and a registered secret VALUE is scrubbed from every
 * string whatever key carries it (a token logged by mistake included). Pure C; host-tested. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DBG_JSON_DEPTH 8
#define DBG_REDACTED "[redacted]"

typedef struct {
    char *buf;
    size_t cap, len;
    bool overflow;
    uint8_t depth;
    bool first[DBG_JSON_DEPTH];
} dbg_json_t;

void dbg_json_init(dbg_json_t *w, char *buf, size_t cap);
/* `key` is NULL at the root and inside arrays. */
void dbg_json_obj_begin(dbg_json_t *w, const char *key);
void dbg_json_obj_end(dbg_json_t *w);
void dbg_json_arr_begin(dbg_json_t *w, const char *key);
void dbg_json_arr_end(dbg_json_t *w);
void dbg_json_str(dbg_json_t *w, const char *key, const char *val);
void dbg_json_int(dbg_json_t *w, const char *key, int64_t val);
void dbg_json_bool(dbg_json_t *w, const char *key, bool val);
/* NUL-terminated text, or "" after an overflow so a truncated document is never sent as JSON. */
const char *dbg_json_text(const dbg_json_t *w);

bool dbg_json_key_is_secret(const char *key);
/* Registers a value to scrub (Wi-Fi password, device key, CA). Values shorter than 4 bytes
 * are ignored: they would scrub ordinary words. The pointer is kept, not copied. */
void dbg_secret_add(const char *value);
void dbg_secret_clear(void);
bool dbg_secret_in(const char *text);
/* Whether exactly this value is registered: the boot's check that provisioning did it. */
bool dbg_secret_registered(const char *value);
/* In place, within `cap` bytes: every registered secret in `s` becomes [redacted]; when the
 * text was `clipped`, a tail of 4+ characters that starts a secret does too. True if anything
 * changed. The log ring's store, so a line keeps its context and loses only the secret. */
bool dbg_secret_scrub(char *s, size_t cap, bool clipped);
