/* svc_debug's pure half (plan §4): the describer registry, the RAM log ring, the command
 * dispatcher and the stripe screenshot codec. No ESP-IDF and no LVGL, so all of it runs in the
 * host suite; the device glue (esp_log hook, the poll that fetches a request, the upload) is
 * thin and lives beside the transport. */
#pragma once

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "dbg_json.h"
#include "svc_debug.h"

/* ---- registry ---- */
#define DBG_PROVIDERS_MAX 24
/* {"v":1,"<name>":{…},…} — every provider, in registration order. */
void dbg_snapshot(dbg_json_t *w);
/* The same members, into an object the caller has open. */
void dbg_snapshot_fields(dbg_json_t *w);
void dbg_registry_reset(void);

/* ---- system: heap, reset reason, crash breadcrumb, coredump, task stacks (dbg_sys.c) ---- */
void dbg_sys_init(void);
/* A FreeRTOS TaskHandle_t whose stack high-water mark the snapshot reports. */
void dbg_sys_watch_task(void *handle);
/* The UI task's stage, kept across a panic's reset and reported by the next boot. */
void dbg_sys_crumb(uint32_t stage);

/* ---- log ring ---- */
#define DBG_LOG_BYTES 3072
#define DBG_LOG_LINE_MAX 120
void dbg_log_line(const char *line);
/* An array of lines, oldest first, under `key`. Secrets are scrubbed by the writer. */
void dbg_log_write(dbg_json_t *w, const char *key);
uint32_t dbg_log_evicted(void);
void dbg_log_reset(void);
void dbg_log_set_clock(uint32_t (*now_ms)(void));
/* The esp_log hook: on the device, dbg_log_chain(esp_log_set_vprintf(dbg_log_vprintf)) puts
 * every ESP_LOGx line in the ring and still prints it. */
typedef int (*dbg_vprintf_t)(const char *fmt, va_list ap);
int dbg_log_vprintf(const char *fmt, va_list ap);
void dbg_log_chain(dbg_vprintf_t prev);

/* ---- commands ---- */
typedef struct {
    char id[40];
    char cmd[16];
    char app[16];
    char tag[16];
    char event[12];
    char name[20];
    int32_t level, x, y, value;
    bool confirm;
} dbg_req_t;

typedef enum { DBG_PARSE_OK, DBG_PARSE_BAD, DBG_PARSE_NESTED, DBG_PARSE_TOO_LONG } dbg_parse_t;
/* A flat JSON object of strings, integers and booleans. Anything nested is refused: commands
 * are deliberately too simple to need it, and a parser that cannot recurse cannot be made to. */
dbg_parse_t dbg_req_parse(const char *json, dbg_req_t *out);

typedef struct {
    const char *name;
    bool destructive; /* the box must send "confirm": true, or it is refused */
    bool idempotent;  /* safe to retry after a lost result */
} dbg_cmd_info_t;

const dbg_cmd_info_t *dbg_cmd_find(const char *name);
/* Writes the handler's answer into `data` (an open object). False with `err` set to refuse. */
typedef bool (*dbg_cmd_fn)(const dbg_req_t *req, dbg_json_t *data, char *err, size_t err_len);
/* Only names in the whitelist bind; anything else is refused at bind time. */
bool dbg_cmd_bind(const char *name, dbg_cmd_fn fn);
void dbg_cmd_reset(void);
/* {"id":…,"cmd":…,"ok":…,"data":{…}|"err":…}. On the UI task only: handlers open apps,
 * inject presses and read widget counts, and the UI task is the one owner of LVGL. */
void dbg_cmd_run(const char *req_json, dbg_json_t *out);

/* ---- stripe screenshot ----
 * No PSRAM means no frame to copy, so a capture rides the renderer: the screen is invalidated
 * and each strip LVGL renders into its draw buffer is run-length encoded as it is flushed.
 * Stream: "JPS1" u16 w, u16 h; per stripe u16 y0, u16 rows, u32 nbytes, then runs of
 * (u8 len-1, u16 rgb565 LE); end u16 0xFFFF. Runs never cross a stripe. */
typedef void (*dbg_sink_fn)(void *ctx, const uint8_t *bytes, size_t n);
#define DBG_SHOT_OUT 512
typedef struct {
    dbg_sink_fn sink;
    void *ctx;
    uint16_t w, h, next_row;
    uint32_t bytes, stripes;
    bool failed;
    uint8_t out[DBG_SHOT_OUT];
    size_t n;
} dbg_shot_t;

void dbg_shot_begin(dbg_shot_t *s, uint16_t w, uint16_t h, dbg_sink_fn sink, void *ctx);
/* Stripes must arrive top to bottom, full width; anything else fails the capture. */
bool dbg_shot_stripe(dbg_shot_t *s, uint16_t y0, uint16_t rows, const uint16_t *px);
bool dbg_shot_end(dbg_shot_t *s);
/* Decodes into a w*h RGB565 frame (sim and tests; the box does this in Python). */
bool dbg_shot_decode(const uint8_t *buf, size_t n, uint16_t *frame, uint16_t w, uint16_t h);
