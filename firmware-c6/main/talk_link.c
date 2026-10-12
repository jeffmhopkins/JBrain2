#include "talk_link.h"

#include <stdio.h>

#include "box_http.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "svc_audio_drv.h"

static const char *TAG = "talk";

/* One write each: about 30 ms of speech, one TLS record. */
#define SLICE 1024
/* Connect and each write. A box this slow to take bytes is one the ring is already overflowing
   for. */
#define SEND_TIMEOUT_MS 8000
/* The recording's own cap is the ring's 1:00 (TALK_MAX_MS); this backstops a release that never
   came. */
#define HOLD_LIMIT_MS (TALK_MAX_MS + 5000)
/* Whisper, the model and the speech, on a shared box: the S3s wait 55 s (firmware/main/talk.c). */
#define ANSWER_LIMIT_MS 60000
/* Slices, so a hold dropped on the glass is let go within one. */
#define WAIT_SLICE_MS 500
#define READ_SLICE_MS 250
/* A reply whose bytes stop arriving for this long is over. */
#define STALL_MS 10000

static uint8_t s_buf[SLICE];
static tk_frame_t s_frame;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool dropped(void)
{
    bool mic_off;
    return talk_net_next(&mic_off) == TK_NET_ABORT;
}

/* The recording, sent as it is made. NULL when it all went up, or the hold was dropped. */
static const char *upload(uint32_t *up)
{
    const uint32_t t0 = now_ms();
    for (;;) {
        bool mic_off;
        const tk_net_t next = talk_net_next(&mic_off);
        if (next == TK_NET_ABORT) return NULL;
        const uint32_t n = talk_net_take(s_buf, sizeof s_buf);
        if (n) {
            if (!box_http_stream_write(s_buf, n)) return "the upload failed";
            *up += n;
            continue;
        }
        /* The microphone closes before it says so, so its last chunk is already in the ring. */
        if (next == TK_NET_FINISH && mic_off) return NULL;
        if (now_ms() - t0 > HOLD_LIMIT_MS) return "the hold ran too long";
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* The speech after the frame's words, into the ring at the speaker's pace. False when it is no
   longer wanted (stopped on the glass, or text only). */
static bool give(const uint8_t *b, uint32_t n)
{
    while (n) {
        if (!talk_net_wants_audio()) return false;
        const uint32_t k = talk_net_give(b, n);
        b += k;
        n -= k;
        if (!k) vTaskDelay(pdMS_TO_TICKS(10) + 1);
    }
    return true;
}

/* The reply: the words as soon as they are in, then the speech. */
static const char *read_reply(uint32_t sent_at, uint32_t *down, uint32_t *reply_ms, bool *whole)
{
    tk_frame_init(&s_frame);
    bool words = false;
    uint32_t heard_at = now_ms();
    for (;;) {
        const int n = box_http_reply_read(s_buf, sizeof s_buf, READ_SLICE_MS);
        if (n == BOX_HTTP_AGAIN) {
            if ((!words && dropped()) || now_ms() - heard_at > STALL_MS) break;
            continue;
        }
        if (n < 0) break;
        if (n == 0) {
            *whole = true;
            break;
        }
        heard_at = now_ms();
        *down += (uint32_t)n;
        size_t used = 0;
        if (!words) {
            used = tk_frame_feed(&s_frame, s_buf, (size_t)n);
            if (tk_frame_bad(&s_frame)) return "a reply this panel could not read";
            if (!tk_frame_done(&s_frame)) continue;
            words = true;
            *reply_ms = now_ms() - sent_at;
            talk_net_reply(s_frame.heard, s_frame.reply, (s_frame.flags & TF_FLAG_AUDIO) != 0);
        }
        if (used < (size_t)n && !give(s_buf + used, (uint32_t)((size_t)n - used))) break;
    }
    return words ? NULL : "the reply was cut off";
}

static const char *exchange(bool voice, uint32_t *up, uint32_t *down, uint32_t *reply_ms)
{
    char path[64];
    snprintf(path, sizeof path, "/endpoint/converse?persona=jerv&voice=%s", voice ? "true" : "false");
    if (!box_http_stream_open(path, "application/octet-stream", SEND_TIMEOUT_MS, true))
        return "the box did not answer";
    const char *why = upload(up);
    if (why || dropped()) {
        /* Cut off without its last chunk, so the box hears an unfinished upload, not a turn. */
        box_http_stream_abort();
        return why;
    }
    if (!box_http_stream_end()) return "the upload failed";
    const uint32_t sent_at = now_ms();
    /* A slice that ends with no headers yet is the box thinking, not a fault: keep the client's
       "timed out" warning out of the log ring for the length of the wait. */
    const esp_log_level_t was = esp_log_level_get("HTTP_CLIENT");
    esp_log_level_set("HTTP_CLIENT", ESP_LOG_ERROR);
    int status = 0;
    while (!(status = box_http_reply_wait(WAIT_SLICE_MS))) {
        if (dropped()) break;
        if (now_ms() - sent_at > ANSWER_LIMIT_MS) {
            status = -2;
            break;
        }
    }
    bool whole = false;
    static char code[24];
    if (status == 200) {
        why = read_reply(sent_at, down, reply_ms, &whole);
    } else if (status == 204) {
        talk_net_nothing();
        whole = true;
    } else if (status == -2) {
        why = "Jerv took too long";
    } else if (status == -1) {
        why = "the box did not answer";
    } else if (status != 0) {
        snprintf(code, sizeof code, "the box said %d", status);
        why = code;
    }
    esp_log_level_set("HTTP_CLIENT", was);
    if (whole)
        box_http_reply_done();
    else
        box_http_stream_abort();
    return why;
}

bool talk_link_run(void)
{
    bool voice;
    if (!talk_net_claim(&voice)) return false;
    /* Power save holds a TCP ACK a beacon at the access point; a 32 KB/s upload behind that
       stalls into the ring's half second (the screenshot's lesson, dbg_link.c). */
    wifi_ps_type_t ps = WIFI_PS_MIN_MODEM;
    const bool had = esp_wifi_get_ps(&ps) == ESP_OK;
    if (had) esp_wifi_set_ps(WIFI_PS_NONE);
    const uint32_t t0 = now_ms();
    uint32_t up = 0, down = 0, reply_ms = 0;
    const char *why = exchange(voice, &up, &down, &reply_ms);
    if (had) esp_wifi_set_ps(ps);
    if (why) {
        ESP_LOGW(TAG, "not answered: %s", why);
        talk_net_fail(why);
    }
    ESP_LOGI(TAG, "up %lu B, down %lu B, reply in %lu ms, %lu ms in all", (unsigned long)up,
             (unsigned long)down, (unsigned long)reply_ms, (unsigned long)(now_ms() - t0));
    talk_net_end(up, down, reply_ms);
    return true;
}
