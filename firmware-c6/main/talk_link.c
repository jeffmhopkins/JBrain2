#include "talk_link.h"

#include <stdio.h>
#include <string.h>

#include "box_http.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include "post_logic.h"
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
/* The box answers with its headers the moment the upload ends and streams the turn after them
   (JT2, backend `panel_stream`), so this wait is for a reply to begin, not for the answer. */
#define HEADERS_LIMIT_MS 30000
/* JT2's idle limit: the box sends a record at least every 8 s (ALIVE while the agent or the
   speech synthesis is quiet), so this long with no byte at all is a box that has gone. There is
   no cap on the whole turn while records flow — the box caps the agent at 5 minutes and the
   speech at 5 more; TURN_LIMIT_MS only backstops a box that streams forever. */
#define IDLE_MS 45000
#define TURN_LIMIT_MS (12u * 60u * 1000u)
/* Slices, so a hold dropped on the glass is let go within one. */
#define WAIT_SLICE_MS 500
#define READ_SLICE_MS 250

static uint8_t s_buf[SLICE];
/* The stream reader keeps one text record at a time (≤ 1 KB, the answer) and hands speech on
   in the slices it arrived in: nothing holds the whole response. */
static tk_stream_t s_stream;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool dropped(void)
{
    bool mic_off;
    return talk_net_next(&mic_off) == TK_NET_ABORT;
}

/* A voice message's digest, kept as it goes up (0.0.16): ~110 bytes, static. */
static mbedtls_sha256_context s_sha;

/* The recording, sent as it is made. NULL when it all went up, or the hold was dropped. A voice
 * message (`post`) is hashed as it goes and stops at the box's 30 s; the rest is dropped. */
static const char *upload(uint32_t *up, bool post)
{
    const uint32_t t0 = now_ms();
    for (;;) {
        bool mic_off;
        const tk_net_t next = talk_net_next(&mic_off);
        if (next == TK_NET_ABORT) return NULL;
        uint32_t n = talk_net_take(s_buf, sizeof s_buf);
        if (n) {
            if (post) n = post_upload_room(*up, n);
            if (post && n) mbedtls_sha256_update(&s_sha, s_buf, n);
            if (n && !box_http_stream_write(s_buf, n)) return "the upload failed";
            *up += n;
            continue;
        }
        /* The microphone closes before it says so, so its last chunk is already in the ring. */
        if (next == TK_NET_FINISH && mic_off) return NULL;
        if (now_ms() - t0 > HOLD_LIMIT_MS) return "the hold ran too long";
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* Speech into the ring at the speaker's pace: this read blocks while the ring is full, which is
   what paces the box (TCP backpressure). False when it is no longer wanted — stopped on the
   glass — and the connection is let go, which tells the box to stop synthesising. */
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

typedef struct {
    bool answered; /* the ANSWER record is in */
    bool speech;   /* ... and said speech follows */
    bool ended;    /* END */
    uint32_t *down, *reply_ms, statuses;
} reading_t;

/* One finished record or speech slice. NULL to carry on; otherwise why the reading stops. */
static const char *take(reading_t *r, tk_stream_ev_t ev, const uint8_t *pcm, size_t pcm_n,
                        uint32_t sent_at)
{
    switch (ev) {
    case TS_EV_HEARD: talk_net_heard(s_stream.text); break;
    case TS_EV_STATUS:
        r->statuses++;
        talk_net_status(s_stream.text);
        break;
    case TS_EV_STEP_START: talk_net_step_start(s_stream.flags, s_stream.text); break;
    case TS_EV_STEP_DONE: talk_net_step_done(s_stream.flags, tk_stream_step_ok(&s_stream)); break;
    case TS_EV_ANSWER:
        if (r->answered) break;
        r->answered = true;
        r->speech = (s_stream.flags & TS_ANSWER_SPEECH) != 0;
        *r->reply_ms = now_ms() - sent_at;
        talk_net_reply(s_stream.text, r->speech);
        break;
    case TS_EV_PCM:
        if (!r->answered || !r->speech) break;
        if (!give(pcm, (uint32_t)pcm_n)) return "stopped";
        break;
    case TS_EV_END:
        r->ended = true;
        if (s_stream.flags == TS_END_NOTHING) {
            talk_net_nothing();
        } else if (!r->answered) {
            static char why[48];
            tk_fit(why, sizeof why, s_stream.text[0] ? s_stream.text : "the box failed");
            return why;
        }
        break;
    case TS_EV_BAD: return "a reply this panel could not read";
    default: break;
    }
    return NULL;
}

/* The JT2 stream, record by record as it arrives. */
static const char *read_reply(uint32_t sent_at, uint32_t *down, uint32_t *reply_ms, bool *whole)
{
    tk_stream_init(&s_stream);
    reading_t r = {.down = down, .reply_ms = reply_ms};
    uint32_t heard_at = now_ms();
    const char *why = NULL;
    while (!why && !r.ended) {
        /* Before the answer, a hold dropped on the glass (Clear chat) lets the box go; after it, a
           stopped voice is noticed in give(). A text-only answer reads on to END. */
        if (!r.answered && dropped()) {
            why = "stopped";
            break;
        }
        if (now_ms() - sent_at > TURN_LIMIT_MS) {
            why = "the answer ran too long";
            break;
        }
        const int n = box_http_reply_read(s_buf, sizeof s_buf, READ_SLICE_MS);
        if (n == BOX_HTTP_AGAIN) {
            if (now_ms() - heard_at > IDLE_MS) why = "the box went quiet";
            continue;
        }
        if (n <= 0) {
            *whole = n == 0;
            break;
        }
        heard_at = now_ms();
        *down += (uint32_t)n;
        size_t off = 0;
        while (off < (size_t)n && !why && !r.ended) {
            tk_stream_ev_t ev;
            const uint8_t *pcm;
            size_t pcm_n;
            off += tk_stream_feed(&s_stream, s_buf + off, (size_t)n - off, &ev, &pcm, &pcm_n);
            why = take(&r, ev, pcm, pcm_n, sent_at);
            if (tk_stream_bad(&s_stream)) break;
        }
    }
    if (r.ended) {
        /* The chunked body's terminator follows END: read it so the session is kept. */
        while (box_http_reply_read(s_buf, sizeof s_buf, READ_SLICE_MS) > 0) {
        }
        *whole = true;
    }
    ESP_LOGI(TAG, "stream: %lu records (%lu status, %lu skipped)%s",
             (unsigned long)s_stream.records, (unsigned long)r.statuses,
             (unsigned long)s_stream.skipped, r.ended ? "" : ", no END");
    if (why && r.answered && !strcmp(why, "stopped")) return NULL; /* a voice stopped on the glass */
    if (why) return why;
    return r.answered || r.ended ? NULL : "the reply was cut off";
}

/* The recording up and the last chunk sent: NULL, or why not. A voice message's digest follows
 * its audio (`X-Jpanel-Sha256: trailer`, backend `panel_messages.py`). */
static const char *send_recording(const char *path, bool post, uint32_t *up)
{
    if (!box_http_stream_open(path, "application/octet-stream", SEND_TIMEOUT_MS, true,
                              post ? POST_SHA_TRAILER : NULL))
        return "the box did not answer";
    mbedtls_sha256_init(&s_sha);
    if (post) mbedtls_sha256_starts(&s_sha, 0);
    const char *why = upload(up, post);
    uint8_t digest[POST_DIGEST_BYTES] = {0};
    if (!why && !dropped() && post) mbedtls_sha256_finish(&s_sha, digest);
    mbedtls_sha256_free(&s_sha);
    if (why || dropped()) {
        /* Cut off without its last chunk, so the box hears an unfinished upload, not a turn. */
        box_http_stream_abort();
        return why ? why : "stopped";
    }
    if (post && !box_http_stream_write(digest, sizeof digest)) {
        box_http_stream_abort();
        return "the upload failed";
    }
    return box_http_stream_end() ? NULL : "the upload failed";
}

/* The reply's status once its headers are in: -2 when they did not come in time, 0 when the
 * hold was dropped meanwhile. */
static int await_status(uint32_t sent_at)
{
    /* A slice that ends with no headers yet is the box transcribing, not a fault: keep the
       client's "timed out" warning out of the log ring for the length of the wait. */
    const esp_log_level_t was = esp_log_level_get("HTTP_CLIENT");
    esp_log_level_set("HTTP_CLIENT", ESP_LOG_ERROR);
    int status = 0;
    while (!(status = box_http_reply_wait(WAIT_SLICE_MS))) {
        if (dropped()) break;
        if (now_ms() - sent_at > HEADERS_LIMIT_MS) {
            status = -2;
            break;
        }
    }
    esp_log_level_set("HTTP_CLIENT", was);
    return status;
}

/* What a status that is not the answer means, in the bubble's words. */
static const char *failed(int status)
{
    static char code[24];
    switch (status) {
    case -2: return "no answer in time";
    case -1: return "the box did not answer";
    case 0: return "stopped";
    case 403: return "this panel may not send messages";
    case 404: return "her panel is not on the box";
    case 413: return "longer than 30 seconds";
    case 422: return "the upload was damaged";
    default: snprintf(code, sizeof code, "the box said %d", status); return code;
    }
}

static const char *exchange(tk_agent_t agent, bool voice, bool fresh, uint32_t *up,
                            uint32_t *down, uint32_t *reply_ms)
{
    char path[112];
    tk_converse_path(agent, voice, fresh, path, sizeof path);
    const char *why = send_recording(path, false, up);
    if (why) return why;
    const uint32_t sent_at = now_ms();
    const int status = await_status(sent_at);
    bool whole = false;
    if (status == 200) {
        why = read_reply(sent_at, down, reply_ms, &whole);
    } else if (status == 204) {
        talk_net_nothing();
        whole = true;
    } else if (status != 0) {
        why = failed(status);
    }
    if (whole)
        box_http_reply_done();
    else
        box_http_stream_abort();
    return why;
}

/* A voice message to a twin (0.0.16): the box answers 201 with its id and her transcript once it
 * has filed it for her panel, 400 when nothing was said. */
static const char *post_exchange(const char *path, uint32_t *up, uint32_t *down)
{
    const char *why = send_recording(path, true, up);
    if (why) return why;
    const int status = await_status(now_ms());
    if (status == 201) {
        /* The upload is over: its slice buffer holds the answer. */
        _Static_assert(POST_SENT_MAX < SLICE, "the send's answer fits the slice buffer");
        char *body = (char *)s_buf;
        int got = 0, n;
        const uint32_t t0 = now_ms();
        while (got < POST_SENT_MAX && now_ms() - t0 < SEND_TIMEOUT_MS &&
               (n = box_http_reply_read(body + got, (size_t)(POST_SENT_MAX - got), READ_SLICE_MS)) != 0) {
            if (n == BOX_HTTP_AGAIN) continue;
            if (n < 0) break;
            got += n;
        }
        body[got] = '\0';
        *down += (uint32_t)got;
        char id[POST_ID_BYTES], text[256];
        uint32_t ms;
        /* Filed on the box whatever the answer's shape: the bubble says sent, the next list
           brings the box's own copy. */
        post_parse_sent(body, (size_t)got, id, text, sizeof text, &ms);
        talk_net_heard(text);
        talk_net_reply("", false);
        if (got < POST_SENT_MAX) box_http_reply_done();
        else box_http_stream_abort();
        return NULL;
    }
    if (status == 400) talk_net_nothing();
    if (status > 0) box_http_reply_done();
    else box_http_stream_abort();
    return status == 400 ? NULL : failed(status);
}

static void hex(const uint8_t *b, size_t n, char *out)
{
    static const char D[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = D[b[i] >> 4];
        out[2 * i + 1] = D[b[i] & 15];
    }
    out[2 * n] = '\0';
}

/* One of her messages played (0.0.16): the box's 16 kHz s16 into the ring at the speaker's pace,
 * hashed as it goes. It counts as heard only when the whole body arrived and matched the box's
 * digest — a message cut off by the radio stays unplayed, on the panel and in the PWA. */
static const char *listen_exchange(const char *path, uint32_t *down)
{
    box_reply_t rep = {.date = {0}};
    const int status = box_http_get_open(path, &rep, SEND_TIMEOUT_MS);
    if (status != 200) {
        if (status > 0) box_http_reply_done();
        return failed(status);
    }
    talk_net_reply("", true);
    mbedtls_sha256_init(&s_sha);
    mbedtls_sha256_starts(&s_sha, 0);
    uint32_t heard_at = now_ms();
    bool whole = false;
    const char *why = NULL;
    for (;;) {
        const int n = box_http_reply_read(s_buf, sizeof s_buf, READ_SLICE_MS);
        if (n == BOX_HTTP_AGAIN) {
            if (now_ms() - heard_at > IDLE_MS) why = "the box went quiet";
            if (why || !talk_net_wants_audio()) break;
            continue;
        }
        if (n <= 0) {
            whole = n == 0;
            break;
        }
        heard_at = now_ms();
        *down += (uint32_t)n;
        mbedtls_sha256_update(&s_sha, s_buf, (size_t)n);
        if (!give(s_buf, (uint32_t)n)) break; /* stopped on the glass */
    }
    uint8_t digest[POST_DIGEST_BYTES];
    char got[2 * POST_DIGEST_BYTES + 1];
    mbedtls_sha256_finish(&s_sha, digest);
    mbedtls_sha256_free(&s_sha);
    hex(digest, sizeof digest, got);
    const bool verified = whole && rep.sha[0] && !strcmp(got, rep.sha);
    if (whole && !verified) ESP_LOGW(TAG, "a played message did not match its digest");
    talk_net_listened(verified);
    if (whole)
        box_http_reply_done();
    else
        box_http_stream_abort();
    return why;
}

bool talk_link_run(void)
{
    bool voice, fresh;
    tk_agent_t agent;
    if (!talk_net_claim(&voice, &agent, &fresh)) return false;
    static char target[POST_PATH_BYTES];
    const tk_kind_t kind = talk_net_kind(target, sizeof target);
    /* Power save holds a TCP ACK a beacon at the access point; a 32 KB/s upload behind that
       stalls into the ring's half second (the screenshot's lesson, dbg_link.c). */
    wifi_ps_type_t ps = WIFI_PS_MIN_MODEM;
    const bool had = esp_wifi_get_ps(&ps) == ESP_OK;
    if (had) esp_wifi_set_ps(WIFI_PS_NONE);
    const uint32_t t0 = now_ms();
    uint32_t up = 0, down = 0, reply_ms = 0;
    const char *why = kind == TK_KIND_POST     ? post_exchange(target, &up, &down)
                      : kind == TK_KIND_LISTEN ? listen_exchange(target, &down)
                                               : exchange(agent, voice, fresh, &up, &down, &reply_ms);
    if (had) esp_wifi_set_ps(ps);
    if (why && strcmp(why, "stopped")) {
        ESP_LOGW(TAG, "not answered: %s", why);
        talk_net_fail(why);
    }
    static const char *const KIND[] = {"", "message", "play"};
    ESP_LOGI(TAG, "%s%s: up %lu B, down %lu B, answer in %lu ms, %lu ms in all",
             kind == TK_KIND_AGENT ? tk_agent_id(agent) : KIND[kind],
             kind == TK_KIND_AGENT && fresh ? " (new chat)" : "", (unsigned long)up,
             (unsigned long)down, (unsigned long)reply_ms, (unsigned long)(now_ms() - t0));
    talk_net_end(up, down, reply_ms);
    return true;
}
