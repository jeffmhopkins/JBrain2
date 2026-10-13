#include "dbg_link.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "box_http.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "ota.h"
#include "sdkconfig.h"
#include "svc_audio_drv.h"
#include "svc_box.h"
#include "svc_display_drv.h"
#include "svc_notice_drv.h"
#include "svc_post_drv.h"
#include "set_link.h"
#include "settings_sync.h"
#include "svc_settings.h"
#include "ui_task.h"

static const char *TAG = "dbg";

/* Short next to the manifest's 15 s: the pickup runs every few seconds, and a box that is slow
   to answer one should cost the loop little. */
#define BOX_TIMEOUT_MS 8000
/* The box sends ≤ 512 B of arguments; anything near this is not a request it wrote. */
#define REQ_MAX 768
/* The largest answer: the log ring (3 KB, escaped) or a snapshot (~2.5 KB on the device). */
#define OUT_MAX 6144
/* The UI loop checks the job every ≤ 50 ms; a command is milliseconds. Three seconds is a UI that
   is not coming. */
#define UI_WAIT_MS 3000

/* The screenshot's handoff: two chunks, so the UI fills one while the network sends the other.
   A full stripe is 38 KB raw and up to 57 KB encoded; it passes through 8 KB, never held. */
#define CHUNK 4096
#define SHOT_W 480
#define SHOT_H 480
/* The capture's ceiling — the longest the screen can stand still for a screenshot, since the UI
   is inside one render from the first strip to the last — and the longest one wait for a free
   chunk may take inside it. The plan's 50 ms per wait is too tight for the radio: under Wi-Fi
   power save one TCP ACK can sit a beacon interval (~100 ms) at the access point, so the ceiling
   that matters is the total, and power save is off while the stream goes up. */
#define SHOT_HANDOFF_MS 250
#define SHOT_DEADLINE_MS 2000

typedef enum { JOB_IDLE, JOB_POSTED, JOB_RUNNING, JOB_DONE, JOB_ABANDONED } job_t;

typedef struct {
    uint16_t n;
    uint8_t b[CHUNK];
} chunk_t;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile job_t s_job;
static SemaphoreHandle_t s_done;
/* Both read by the UI while a job runs; the main task touches neither unless the job is IDLE. */
static char s_req[REQ_MAX];
static char s_out_buf[OUT_MAX];
static dbg_json_t s_out;

static dbg_stats_t s_stats = {.last_status = -1};
static volatile bool s_reboot;

static QueueHandle_t s_free_q, s_full_q;
static chunk_t *s_chunks;
/* UI-task state of one capture; `armed` and `abort` are the main task's word to it. */
static struct {
    volatile bool armed, abort;
    bool ok;
    const char *why;
    chunk_t *cur;
    uint32_t t0, ms;
    dbg_shot_t shot;
} s_cap;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ---- the job: main posts, UI runs ---- */

static bool job_post(void)
{
    bool ok = false;
    portENTER_CRITICAL(&s_mux);
    if (s_job == JOB_IDLE) {
        dbg_json_init(&s_out, s_out_buf, sizeof s_out_buf);
        s_job = JOB_POSTED;
        ok = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (ok) xSemaphoreTake(s_done, 0); /* a give from a job abandoned earlier */
    return ok;
}

/* True when the UI finished it; then the job is IDLE again and s_out is the main task's. */
static bool job_wait(uint32_t ms)
{
    if (xSemaphoreTake(s_done, pdMS_TO_TICKS(ms)) != pdTRUE) return false;
    portENTER_CRITICAL(&s_mux);
    s_job = JOB_IDLE;
    portEXIT_CRITICAL(&s_mux);
    return true;
}

/* Withdraws a job the UI never started, or leaves one it is running to finish on its own (it
   then frees the slot, and nothing waits for it). True if it finished in the meantime. */
static bool job_abandon(void)
{
    bool done = false;
    portENTER_CRITICAL(&s_mux);
    if (s_job == JOB_POSTED) s_job = JOB_IDLE;
    else if (s_job == JOB_RUNNING) s_job = JOB_ABANDONED;
    else if (s_job == JOB_DONE) {
        s_job = JOB_IDLE;
        done = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return done;
}

void dbg_link_ui_service(void)
{
    portENTER_CRITICAL(&s_mux);
    const bool mine = s_job == JOB_POSTED;
    if (mine) s_job = JOB_RUNNING;
    portEXIT_CRITICAL(&s_mux);
    if (!mine) return;
    dbg_cmd_run(s_req, &s_out);
    portENTER_CRITICAL(&s_mux);
    const bool abandoned = s_job == JOB_ABANDONED;
    s_job = abandoned ? JOB_IDLE : JOB_DONE;
    portEXIT_CRITICAL(&s_mux);
    if (!abandoned) xSemaphoreGive(s_done);
}

/* Runs the request on the UI task, or here when there is no UI to own LVGL. */
static bool run(uint32_t wait_ms)
{
    if (!ui_task_owns_lvgl()) {
        if (s_job != JOB_IDLE) return false;
        dbg_json_init(&s_out, s_out_buf, sizeof s_out_buf);
        dbg_cmd_run(s_req, &s_out);
        return true;
    }
    if (!job_post()) return false;
    return job_wait(wait_ms) || job_abandon();
}

/* ---- answers ---- */

static int post_json(const char *path, const char *json)
{
    return box_http_call(path, "application/json", json, strlen(json), NULL, 0, NULL, NULL,
                         BOX_TIMEOUT_MS);
}

/* What the box records when the command's own answer cannot be sent. */
static int post_failure(const char *path, const dbg_req_t *r, const char *why, uint32_t bytes)
{
    char buf[192];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_json_str(&w, "id", r->id);
    dbg_json_str(&w, "cmd", r->cmd);
    dbg_json_bool(&w, "ok", false);
    dbg_json_str(&w, "err", why);
    if (bytes) dbg_json_int(&w, "bytes", bytes);
    dbg_json_obj_end(&w);
    return post_json(path, dbg_json_text(&w));
}

static int answer_json(const char *path, const dbg_req_t *r)
{
    if (!run(UI_WAIT_MS)) return post_failure(path, r, "ui busy", 0);
    const char *text = dbg_json_text(&s_out);
    if (!text[0]) return post_failure(path, r, "result too large", 0);
    return post_json(path, text);
}

/* ---- the screenshot ---- */

static void cap_fail(const char *why)
{
    s_cap.shot.failed = true;
    if (!s_cap.why) s_cap.why = why;
}

/* UI task, inside the render: dbg_shot's 512-byte output, into the chunk being filled. */
static void cap_sink(void *ctx, const uint8_t *b, size_t n)
{
    (void)ctx;
    while (n) {
        if (s_cap.abort) {
            cap_fail("net failed");
            return;
        }
        if (!s_cap.cur) {
            const uint32_t spent = now_ms() - s_cap.t0;
            const uint32_t left = spent >= SHOT_DEADLINE_MS ? 0 : SHOT_DEADLINE_MS - spent;
            const uint32_t wait = left < SHOT_HANDOFF_MS ? left : SHOT_HANDOFF_MS;
            if (!wait || xQueueReceive(s_free_q, &s_cap.cur, pdMS_TO_TICKS(wait)) != pdTRUE) {
                s_cap.cur = NULL;
                cap_fail("net slow");
                return;
            }
            s_cap.cur->n = 0;
        }
        const size_t k = n < CHUNK - s_cap.cur->n ? n : CHUNK - s_cap.cur->n;
        memcpy(s_cap.cur->b + s_cap.cur->n, b, k);
        s_cap.cur->n += (uint16_t)k;
        b += k;
        n -= k;
        if (s_cap.cur->n == CHUNK) {
            /* Never blocks: two chunks, two slots. */
            xQueueSend(s_full_q, &s_cap.cur, 0);
            s_cap.cur = NULL;
        }
    }
}

static void cap_strip(void *ctx, int x1, int y1, int x2, int y2, const uint16_t *px)
{
    (void)ctx;
    if (s_cap.shot.failed) return;
    if (x1 != 0 || x2 != SHOT_W - 1)
        cap_fail("capture torn");
    else if (!dbg_shot_stripe(&s_cap.shot, (uint16_t)y1, (uint16_t)(y2 - y1 + 1), px))
        cap_fail("capture torn");
}

static bool cmd_screenshot(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r;
    s_cap.ok = false;
    if (!s_cap.armed || !svc_display_ok()) {
        snprintf(err, n, "no display");
        return false;
    }
    s_cap.why = NULL;
    s_cap.cur = NULL;
    s_cap.t0 = now_ms();
    dbg_shot_begin(&s_cap.shot, SHOT_W, SHOT_H, cap_sink, NULL);
    if (!svc_display_capture(cap_strip, NULL)) cap_fail("no display");
    if (!dbg_shot_end(&s_cap.shot)) cap_fail("capture torn");
    if (!s_cap.shot.failed && s_cap.cur && s_cap.cur->n) xQueueSend(s_full_q, &s_cap.cur, 0);
    s_cap.cur = NULL;
    s_cap.ms = now_ms() - s_cap.t0;
    s_cap.ok = !s_cap.shot.failed;
    if (!s_cap.ok) {
        snprintf(err, n, "%s", s_cap.why ? s_cap.why : "capture torn");
        return false;
    }
    dbg_json_int(d, "bytes", s_cap.shot.bytes);
    dbg_json_int(d, "stripes", s_cap.shot.stripes);
    dbg_json_int(d, "ms", s_cap.ms);
    return true;
}

static bool send_chunk(chunk_t *c, bool *wrote)
{
    if (*wrote && !box_http_stream_write(c->b, c->n)) {
        *wrote = false;
        s_cap.abort = true;
    }
    xQueueSend(s_free_q, &c, 0);
    return *wrote;
}

/* The stream goes up while the UI renders it: the UI fills a chunk, this task sends it and
   hands it back. A capture that fails on either side is never finished as a stream (the box
   would decode it as a torn picture); the upload is cut off and the reason sent as JSON. */
static int stream_screenshot(const char *path, const dbg_req_t *r)
{
    if (s_job != JOB_IDLE) return post_failure(path, r, "ui busy", 0);
    if (!box_http_stream_open(path, "application/octet-stream", BOX_TIMEOUT_MS, false, NULL))
        return post_failure(path, r, "box unreachable", 0);
    /* After the handshake, whose working memory is freed by now: the chunks never add to its
       peak, the heap's low-water mark. */
    if (!s_chunks) s_chunks = malloc(2 * sizeof *s_chunks);
    if (!s_chunks) {
        box_http_stream_abort();
        return post_failure(path, r, "no memory", 0);
    }
    xQueueReset(s_free_q);
    xQueueReset(s_full_q);
    for (int i = 0; i < 2; i++) {
        chunk_t *c = &s_chunks[i];
        xQueueSend(s_free_q, &c, 0);
    }
    s_cap.abort = false;
    s_cap.armed = true;
    if (!job_post()) {
        s_cap.armed = false;
        box_http_stream_abort();
        return post_failure(path, r, "ui busy", 0);
    }
    const uint32_t t0 = now_ms();
    bool wrote = true, done = false;
    chunk_t *c;
    while (!done) {
        if (xQueueReceive(s_full_q, &c, pdMS_TO_TICKS(20)) == pdTRUE) {
            send_chunk(c, &wrote);
            continue;
        }
        done = job_wait(0);
        if (!done && now_ms() - t0 > UI_WAIT_MS + SHOT_DEADLINE_MS) {
            s_cap.abort = true;
            done = job_abandon();
            break;
        }
    }
    s_cap.armed = false;
    /* The UI queues its last chunk before it says it is done. */
    while (done && xQueueReceive(s_full_q, &c, 0) == pdTRUE) send_chunk(c, &wrote);

    s_stats.shot_ms = s_cap.ms;
    s_stats.shot_bytes = done ? s_cap.shot.bytes : 0;
    /* A UI still inside an abandoned capture may touch the chunks, so they are freed only once
       it is known to be out; otherwise the next screenshot reuses them. */
    if (done) {
        free(s_chunks);
        s_chunks = NULL;
    }
    if (done && wrote && s_cap.ok) return box_http_stream_finish();
    box_http_stream_abort();
    const char *why = !done ? "ui busy" : !s_cap.ok ? (s_cap.why ? s_cap.why : "capture torn")
                                                    : "net failed";
    return post_failure(path, r, why, done ? s_cap.shot.bytes : 0);
}

static int answer_screenshot(const char *path, const dbg_req_t *r)
{
    /* Inline, with no UI, it answers "no display" without arming a stream. */
    if (!ui_task_owns_lvgl()) return answer_json(path, r);
    wifi_ps_type_t ps = WIFI_PS_MIN_MODEM;
    const bool had = esp_wifi_get_ps(&ps) == ESP_OK;
    if (had) esp_wifi_set_ps(WIFI_PS_NONE);
    const int status = stream_screenshot(path, r);
    if (had) esp_wifi_set_ps(ps);
    return status;
}

/* ---- the device's commands (the rest bind in svc_debug and the shell) ---- */

static bool cmd_set(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    for (int k = 0; k < SET__COUNT; k++)
        if (!strcmp(r->name, svc_settings_name((setting_t)k))) {
            svc_settings_set((setting_t)k, r->value);
            dbg_json_int(d, r->name, svc_settings_get((setting_t)k));
            return true;
        }
    snprintf(err, n, "no setting %s", r->name);
    return false;
}

static bool cmd_ota_check(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r;
    /* Update now's own path: a flag and a wake for the loop, which checks once this answer is
       sent. The Firmware card shows it as if the owner had pressed the button. */
    if (!svc_box_update_now()) {
        snprintf(err, n, "offline or busy");
        return false;
    }
    dbg_json_str(d, "ota", "checking");
    return true;
}

static bool cmd_report_now(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r, (void)err, (void)n;
    svc_box_report_now();
    dbg_json_str(d, "report", "queued");
    return true;
}

static bool cmd_log_level(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    if (!r->tag[0] || r->level < ESP_LOG_NONE || r->level > ESP_LOG_VERBOSE) {
        snprintf(err, n, "tag and level 0-5");
        return false;
    }
    esp_log_level_set(r->tag, (esp_log_level_t)r->level);
    dbg_json_str(d, "tag", r->tag);
    dbg_json_int(d, "level", r->level);
    /* Levels above this were compiled out, so asking for them changes nothing. */
    dbg_json_int(d, "max", CONFIG_LOG_MAXIMUM_LEVEL);
    return true;
}

/* A second of sine through the speaker at the set volume: the owner's ear, asked for from the
   box, is how the speaker is checked with nobody holding a cable. `value` is the pitch in Hz. */
static bool cmd_play_tone(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    const int32_t hz = r->value ? r->value : 880;
    if (hz < 100 || hz > 4000) {
        snprintf(err, n, "value is Hz, 100-4000");
        return false;
    }
    const char *why;
    if (!svc_audio_tone((uint16_t)hz, 1000, &why)) {
        snprintf(err, n, "%s", why);
        return false;
    }
    dbg_json_int(d, "hz", hz);
    dbg_json_int(d, "ms", 1000);
    dbg_json_int(d, "volume", svc_settings_get(SET_VOLUME));
    dbg_json_str(d, "audio", svc_audio_state());
    return true;
}

static bool cmd_reboot(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r, (void)err, (void)n;
    s_reboot = true;
    dbg_json_str(d, "reboot", "after this answer");
    return true;
}

void dbg_link_start(void)
{
    s_done = xSemaphoreCreateBinary();
    s_free_q = xQueueCreate(2, sizeof(chunk_t *));
    s_full_q = xQueueCreate(2, sizeof(chunk_t *));
    dbg_cmd_bind("screenshot", cmd_screenshot);
    dbg_cmd_bind("set", cmd_set);
    dbg_cmd_bind("ota_check", cmd_ota_check);
    dbg_cmd_bind("report_now", cmd_report_now);
    dbg_cmd_bind("log_level", cmd_log_level);
    dbg_cmd_bind("reboot", cmd_reboot);
    dbg_cmd_bind("play_tone", cmd_play_tone);
}

dbg_poll_t dbg_link_poll(void)
{
    /* A UI still running an abandoned request reads s_req: collect nothing until it is done, and
       count it a failed pickup so the schedule backs off rather than spins. */
    if (!s_done || !s_free_q || !s_full_q || s_job != JOB_IDLE) return DBG_POLL_FAIL;
    size_t got = 0;
    box_reply_t rep = {.date = {0}};
    /* The notice switches ride every pickup (0.0.19, plan §2): the box refuses a notice at send
       time from what the panel said here seconds ago. None when this panel cannot show one (no
       storage, or no UI on a boot after a fault), and the box then says `no_notice_surface`. */
    char pickup[176], prefs[48], sync[64] = "";
    svc_notice_pickup_query(ui_task_owns_lvgl(), prefs, sizeof prefs);
    /* The rest of the settings the PWA shows (0.0.21): reported whenever the UI is up, so a
       change made at the glass reaches the PWA within a pickup and settles or refuses what the
       PWA asked for. No UI (a boot after a fault): nothing the PWA could change is running. */
    if (ui_task_owns_lvgl()) settings_sync_query(svc_settings_get, sync, sizeof sync);
    snprintf(pickup, sizeof pickup, "/endpoint/debug/next?board=" OTA_BOARD_ID "%s%s", prefs, sync);
    const int status = box_http_call(pickup, NULL, NULL, 0, s_req, sizeof s_req - 1, &got, &rep,
                                     BOX_TIMEOUT_MS);
    /* The owner's voice post rides the pickup (0.0.16): a tag that moved is a fetch of the
       people's threads, so a twin's message reaches the panel within a pickup or two. Notices
       waiting ride it too: the loop collects them next (notice_link.c). */
    if (status == 200 || status == 204) {
        svc_post_tag(rep.post);
        svc_notice_waiting(rep.notice);
        /* A change the PWA asked for waits for this panel: the loop fetches it next. */
        set_link_signal(rep.settings);
    }
    if (status == 204) {
        s_stats.polls_ok++;
        return DBG_POLL_EMPTY;
    }
    if (status != 200 || got == 0 || got >= sizeof s_req - 1) {
        s_stats.polls_err++;
        ESP_LOGW(TAG, "pickup: HTTP %d, %u bytes", status, (unsigned)got);
        return status == 200 ? DBG_POLL_EMPTY : DBG_POLL_FAIL;
    }
    s_req[got] = '\0';
    s_stats.polls_ok++;
    dbg_req_t r;
    const dbg_parse_t parsed = dbg_req_parse(s_req, &r);
    if (!dbg_req_id_ok(r.id)) {
        /* No URL to answer on: the box expires it in two minutes. */
        ESP_LOGW(TAG, "a request with no usable id — dropped");
        return DBG_POLL_CMD;
    }
    char path[80];
    snprintf(path, sizeof path, "/endpoint/debug/%s", r.id);
    s_stats.cmds++;
    snprintf(s_stats.last_cmd, sizeof s_stats.last_cmd, "%s", r.cmd);
    const int64_t t0 = esp_timer_get_time();
    /* A screenshot's stream (two 4 KB chunks and the radio's send queue on a fresh session) is
       ~6 KB under any pickup's low; named as itself, it no longer reads as the pickup's. The
       loop names "idle" once the poll returns. */
    const bool shot = parsed == DBG_PARSE_OK && !strcmp(r.cmd, "screenshot");
    dbg_sys_doing(dbg_answer_phase(shot ? "screenshot" : ""));
    const int answered = shot ? answer_screenshot(path, &r) : answer_json(path, &r);
    dbg_sys_doing("pickup");
    s_stats.last_status = (int16_t)answered;
    ESP_LOGI(TAG, "%s answered: HTTP %d in %lu ms", r.cmd, answered,
             (unsigned long)((esp_timer_get_time() - t0) / 1000));
    if (s_reboot) {
        ESP_LOGW(TAG, "rebooting at the box's request");
        box_http_close();
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
    }
    return DBG_POLL_CMD;
}

void dbg_link_note(char *out, size_t len)
{
    dbg_stats_note(&s_stats, out, len);
}
