#include "notice_link.h"

#include <stdio.h>
#include <string.h>

#include "box_http.h"
#include "dbg_core.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "svc_notice_drv.h"

static const char *TAG = "notice";

/* The pickup's bound for the headers; a stream's whole body may take longer (a picture is up to
   320 KB), but never this long without a byte. */
#define NOTICE_TIMEOUT_MS 8000
#define READ_SLICE_MS 250
#define IDLE_MS 15000
#define FETCH_LIMIT_MS 90000
/* What one pass may do before the loop gets its turn back: the box keeps at most four open. */
#define FETCHES_PER_PASS NOTICE_OPEN_MAX
#define POSTS_PER_PASS 4

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static uint32_t int_free(void)
{
    return (uint32_t)heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
}

/* One stream from the box into its file, record by record: the network's bytes are read
   straight into the reader's one record buffer and written out from there, so nothing bigger
   than a record (4 KB) is ever held. The status, or -3 with no memory or no file. */
static int fetch(const char *path)
{
    dbg_sys_doing("notice-fetch");
    const uint32_t before = int_free();
    box_reply_t rep = {.date = {0}};
    const int status = box_http_get_open(path, &rep, NOTICE_TIMEOUT_MS);
    if (status != 200) {
        if (status > 0) box_http_reply_done();
        if (status != 204) {
            ESP_LOGW(TAG, "%s: HTTP %d", path, status);
            svc_notice_fetch_failed(path, status);
        }
        return status;
    }
    /* After the headers, so the record buffer and a handshake never hold the heap at once. */
    nfetch_t *f = svc_notice_fetch_begin(path);
    if (!f) {
        box_http_stream_abort();
        svc_notice_fetch_failed(path, -3);
        return -3;
    }
    const uint32_t ready = int_free(), t0 = now_ms();
    uint32_t low = ready, heard = t0;
    bool whole = false;
    for (;;) {
        uint8_t *at, drain[8];
        size_t room = svc_notice_fetch_room(f, &at);
        const bool ended = svc_notice_fetch_ended(f);
        if (!room && !ended) break; /* refused, or the file would not take it */
        if (ended) {
            at = drain;
            room = sizeof drain;
        }
        const int n = box_http_reply_read(at, room, READ_SLICE_MS);
        if (n == BOX_HTTP_AGAIN) {
            if (now_ms() - heard > IDLE_MS || now_ms() - t0 > FETCH_LIMIT_MS) break;
            continue;
        }
        if (n <= 0) {
            whole = n == 0;
            break;
        }
        heard = now_ms();
        if (ended) {
            svc_notice_fetch_trailing(f);
            break;
        }
        const bool more = svc_notice_fetch_took(f, (size_t)n);
        const uint32_t free_now = int_free();
        if (free_now < low) low = free_now;
        if (!more) break;
    }
    if (whole) box_http_reply_done();
    else box_http_stream_abort();
    svc_notice_fetch_mem(before, ready, low);
    const bool queued = svc_notice_fetch_end(f, whole, status);
    ESP_LOGI(TAG, "%s: %s in %lu ms, heap %lu -> %lu (low %lu)", path,
             queued ? "queued" : "not kept", (unsigned long)(now_ms() - t0), (unsigned long)before,
             (unsigned long)ready, (unsigned long)low);
    return status;
}

bool notice_link_run(void)
{
    bool ran = false;
    char path[NOTICE_PATH_BYTES];
    for (int i = 0; i < FETCHES_PER_PASS && svc_notice_want_fetch(path, sizeof path); i++) {
        ran = true;
        const int status = fetch(path);
        if (status == 204 && !strcmp(path, NOTICE_NEXT_PATH)) svc_notice_none_waiting();
        if (status != 200) break;
    }
    char json[48];
    for (int i = 0; i < POSTS_PER_PASS && svc_notice_post_take(path, sizeof path, json, sizeof json);
         i++) {
        ran = true;
        dbg_sys_doing("notice-state");
        const int status = box_http_call(path, "application/json", json, strlen(json), NULL, 0,
                                         NULL, NULL, NOTICE_TIMEOUT_MS);
        if (status != 204) ESP_LOGW(TAG, "%s %s: HTTP %d", path, json, status);
        svc_notice_post_done(status);
        if (status < 0) break;
    }
    dbg_sys_doing("notice");
    svc_notice_housekeep();
    return ran;
}
