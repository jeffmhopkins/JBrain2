/* The upload half of press-and-hold. See talk.h for why this is a task of its own. */

#include "talk.h"

#include <stdio.h>
#include <string.h>

#include "audio.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "link.h"
#include "reach.h"

static const char *TAG = "talk";

/* Local for `ota.c`'s reason: `reach.c` is on the host suite and takes its clock as an argument. */
static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Generous, because the box may be transcribing on a shared GPU behind a chat model. The
   renderer gives up sooner than this and shows the failure face; that is deliberate — a child
   should be told "say that again" long before a socket decides it has waited enough. */
/* ABOVE the renderer's `TALK_TIMEOUT_MS`, always, and that ordering is the point rather than
   the value: the socket must not die while the face is still willing to wait, or the panel
   gives up on a turn the box would have finished. The gap is also why the capture buffer is
   guarded on `TALK_NET_BUSY` — between the renderer giving up and this timeout firing, a
   frustrated child can hold again while the socket is still reading the last recording. */
/* SHORTER THAN `TALK_TIMEOUT_MS` ON PURPOSE, and the ordering is the point. That one is the
   renderer's backstop for a task that has stopped answering; this one is the actual wait. If the
   backstop fired first the panel would show the failure dash while this task was still running —
   and then speak the reply into a turn the child had already been told had failed. Letting the
   network give up first means the failure is reported once, by the thing that knows why. */
#define TALK_HTTP_TIMEOUT_MS 55000
/* What the box returns at most, and THE REPLY'S OWN CEILING — `audio.c`'s buffer is bigger
   (it also holds voice-post messages), so this is the only thing bounding a reply here.
   It said six, the box never capped its audio at all, and a 261 KB reply stopped here mid-word
   with nothing said about it. Ten, matching `PANEL_REPLY_MAX` on the box, which now logs when
   it has to cut. */
#define REPLY_MAX_BYTES (16000 * 2 * 10)

static SemaphoreHandle_t s_go;
static volatile talk_net_t s_state;
static const int16_t *s_pcm;
static volatile size_t s_bytes;
static uint8_t *s_reply; /* PSRAM; the response is read into this, then copied by audio.c */

/* One turn: POST the recording, read the reply, hand it to the speaker. */
static void turn(void)
{
    /* OVER THE ONE SOCKET (`link.c`). This was its own HTTPS session, and on 0.3.38 it was the
       one that collided: a turn's handshake landing while the main task's settings poll held
       another is the "connect" of every red dash after the second turn. */
    const link_req_t req = {.method = "POST",
                            .path = "/endpoint/converse",
                            .content_type = "application/octet-stream",
                            .body = (const uint8_t *)s_pcm,
                            .body_len = s_bytes,
                            .timeout_ms = TALK_HTTP_TIMEOUT_MS,
                            .buf = s_reply,
                            .cap = REPLY_MAX_BYTES};

    const int64_t t0 = esp_timer_get_time();
    talk_net_t out = TALK_NET_FAILED;
    int got = 0;
    /* Set at each `goto done` rather than at the label, because the label cannot tell which
       branch reached it — and "the conversation failed" without which half is the report the box
       has always been able to make for itself. */
    const char *why = "unknown";

    const link_res_t res = link_request(&req);
    if (res.status < 0) {
        /* THE ONE THAT ACTUALLY HAPPENS, AND THE ONE NOBODY COULD SEE. On 2026-09-29 and again
           on 2026-10-04 this was "connect", which named nothing. `res.err` is the real reason now
           — the socket's esp-tls code, errno and upgrade status, or the fallback session's —
           and it rides the talk row of telemetry like every other reason. */
        ESP_LOGW(TAG, "no answer: %s", res.err);
        why = res.err[0] ? res.err : "connect";
        goto done;
    }
    if (res.status == 204) {
        /* Silence. Not a failure: an accidental hold on a quiet room is the most common
           recording this will ever make, and the pet should just go back to being a pet. */
        ESP_LOGI(TAG, "nothing heard");
        out = TALK_NET_IDLE;
        goto done;
    }
    if (res.status != 200) {
        ESP_LOGW(TAG, "box said %d", res.status);
        /* The STATUS, not just "the box refused": 401 is a key the box no longer knows and 503 is
           a box still starting, and those are different evenings. Static because `reach.c` keeps
           the pointer rather than a copy — see `reach_fail`. */
        static char code[16];
        snprintf(code, sizeof(code), "http-%d", res.status);
        why = code;
        goto done;
    }
    got = (int)res.got;
    if (res.cut && got < 2) {
        why = "reply-cut";
        goto done;
    }
    if (got < 2) {
        ESP_LOGW(TAG, "empty reply");
        why = "empty-reply";
        goto done;
    }
    out = audio_play((const int16_t *)s_reply, (size_t)got) ? TALK_NET_SPOKE : TALK_NET_FAILED;
    if (out == TALK_NET_FAILED) why = "no-playback";

done:
    /* Two numbers now rather than three: the upload and the wait share one socket and one call,
       so the split the HTTPS path could make is the box's to report (stt/llm/tts in its log). */
    ESP_LOGI(TAG, "turn: sent %u B, reply %d B after %d ms total", (unsigned)s_bytes, got,
             (int)((esp_timer_get_time() - t0) / 1000));
    /* IDLE IS NOT A FAULT. A 204 is the box hearing silence, which is the commonest recording a
       panel on a wall will ever make: counting it would bury the failures that matter under an
       accidental lean on the pet. */
    if (out == TALK_NET_FAILED) {
        reach_fail(REACH_TALK, why, now_ms());
    } else {
        reach_ok(REACH_TALK, now_ms());
    }
    s_state = out;
}

static void talk_task(void *arg)
{
    (void)arg;
    while (true) {
        if (xSemaphoreTake(s_go, portMAX_DELAY) != pdTRUE) continue;
        turn();
    }
}

bool talk_start(const cfg_t *cfg)
{
    if (cfg == NULL) return false;
    s_reply = heap_caps_malloc(REPLY_MAX_BYTES, MALLOC_CAP_SPIRAM);
    s_go = xSemaphoreCreateBinary();
    if (s_reply == NULL || s_go == NULL) {
        ESP_LOGE(TAG, "no memory for a conversation");
        return false;
    }
    /* Off the render core: this task blocks on a socket for seconds at a time. */
    if (xTaskCreatePinnedToCore(talk_task, "talk", 6144, NULL, 4, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "task failed");
        return false;
    }
    ESP_LOGI(TAG, "ready");
    return true;
}

bool talk_send(const int16_t *pcm, size_t bytes)
{
    if (s_go == NULL || pcm == NULL || bytes < 2) return false;
    if (s_state == TALK_NET_BUSY) return false;
    s_pcm = pcm;
    s_bytes = bytes;
    s_state = TALK_NET_BUSY;
    return xSemaphoreGive(s_go) == pdTRUE;
}

talk_net_t talk_state(void)
{
    return s_state;
}

void talk_clear(void)
{
    if (s_state != TALK_NET_BUSY) s_state = TALK_NET_IDLE;
}
