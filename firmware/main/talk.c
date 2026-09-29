/* The upload half of press-and-hold. See talk.h for why this is a task of its own. */

#include "talk.h"

#include <stdio.h>
#include <string.h>

#include "audio.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

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

static const cfg_t *s_cfg;
static SemaphoreHandle_t s_go;
static volatile talk_net_t s_state;
static const int16_t *s_pcm;
static volatile size_t s_bytes;
static uint8_t *s_reply; /* PSRAM; the response is read into this, then copied by audio.c */

static void trust(esp_http_client_config_t *hc)
{
    if (s_cfg->ca != NULL && s_cfg->ca[0] != '\0') {
        hc->cert_pem = s_cfg->ca;
        return;
    }
    hc->crt_bundle_attach = esp_crt_bundle_attach;
}

/* One turn: POST the recording, read the reply, hand it to the speaker. */
static void turn(void)
{
    /* CHECKED, BECAUSE snprintf TRUNCATES SILENTLY AND A TRUNCATED CREDENTIAL IS A 401.
       Both of these come out of NVS with no length bound — `ota.c` sizes its bearer with
       `malloc(strlen(token) + 8)` for exactly this reason. A fixed buffer is fine here, an
       unreported overflow is not: it would present as "the box rejected me", which is a
       sentence that sends the next person looking at the server. */
    char url[256];
    char auth[256];
    const int un = snprintf(url, sizeof(url), "%s/endpoint/converse", s_cfg->api);
    const int an = snprintf(auth, sizeof(auth), "Bearer %s", s_cfg->token);
    if (un < 0 || un >= (int)sizeof(url) || an < 0 || an >= (int)sizeof(auth)) {
        ESP_LOGE(TAG, "api url or token too long (%d, %d) — not sending", un, an);
        reach_fail(REACH_TALK, "url-too-long", now_ms());
        s_state = TALK_NET_FAILED;
        return;
    }

    esp_http_client_config_t hc = {
        .url = url, .timeout_ms = TALK_HTTP_TIMEOUT_MS, .method = HTTP_METHOD_POST};
    trust(&hc);
    esp_http_client_handle_t c = esp_http_client_init(&hc);
    if (c == NULL) {
        reach_fail(REACH_TALK, "client-init", now_ms());
        s_state = TALK_NET_FAILED;
        return;
    }
    esp_http_client_set_header(c, "Authorization", auth);
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");

    const int64_t t0 = esp_timer_get_time();
    /* Initialised to t0, not left to the upload to set: every `goto done` below jumps past
       that assignment and the log at the bottom reads it regardless. A failed connect would
       have printed an upload time made of stack garbage. */
    int64_t sent = t0;
    talk_net_t out = TALK_NET_FAILED;
    int got = 0;
    /* Set at each `goto done` rather than at the label, because the label cannot tell which
       branch reached it — and "the conversation failed" without which half is the report the box
       has always been able to make for itself. */
    const char *why = "unknown";

    if (esp_http_client_open(c, (int)s_bytes) != ESP_OK) {
        /* THE ONE THAT ACTUALLY HAPPENS, AND THE ONE NOBODY COULD SEE. This is the whole of the
           red dash on 2026-09-29: it fails before a single byte leaves the panel, so the box's
           access log has no row for it, and the only account of it was this `ESP_LOGW` on a
           serial console that does not exist in a bedroom. It is also the fast failure — the
           owner's *"it didn't time out"* is this branch and not the sixty-second one. */
        ESP_LOGW(TAG, "connect failed");
        why = "connect";
        goto done;
    }
    /* Written in chunks so a stall shows up as a short write rather than a long block. */
    const uint8_t *p = (const uint8_t *)s_pcm;
    size_t left = s_bytes;
    while (left > 0) {
        const int n = esp_http_client_write(c, (const char *)p, left > 4096 ? 4096 : (int)left);
        if (n <= 0) {
            ESP_LOGW(TAG, "upload stalled with %u bytes left", (unsigned)left);
            why = "upload-stall";
            goto done;
        }
        p += n;
        left -= (size_t)n;
    }
    sent = esp_timer_get_time();

    if (esp_http_client_fetch_headers(c) < 0) {
        why = "no-headers";
        goto done;
    }
    const int status = esp_http_client_get_status_code(c);
    if (status == 204) {
        /* Silence. Not a failure: an accidental hold on a quiet room is the most common
           recording this will ever make, and the pet should just go back to being a pet. */
        ESP_LOGI(TAG, "nothing heard");
        out = TALK_NET_IDLE;
        goto done;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "box said %d", status);
        /* The STATUS, not just "the box refused": 401 is a key the box no longer knows and 503 is
           a box still starting, and those are different evenings. Static because `reach.c` keeps
           the pointer rather than a copy — see `reach_fail`. */
        static char code[12];
        snprintf(code, sizeof(code), "http-%d", status);
        why = code;
        goto done;
    }
    while (got < REPLY_MAX_BYTES) {
        const int n = esp_http_client_read(c, (char *)s_reply + got, REPLY_MAX_BYTES - got);
        if (n <= 0) break;
        got += n;
    }
    if (got < 2) {
        ESP_LOGW(TAG, "empty reply");
        why = "empty-reply";
        goto done;
    }
    out = audio_play((const int16_t *)s_reply, (size_t)got) ? TALK_NET_SPOKE : TALK_NET_FAILED;
    if (out == TALK_NET_FAILED) why = "no-playback";

done:
    /* THE THREE NUMBERS A SLOW TURN IS DIAGNOSED WITH, and they are separable on purpose:
       upload time is the network, the rest is the box. Without the split, "it took nine
       seconds" says nothing about which end to fix — and the box's own log has the other
       half (stt/llm/tts), so the two together account for the whole wait. */
    ESP_LOGI(TAG, "turn: sent %u B in %d ms, reply %d B after %d ms total",
             (unsigned)s_bytes, (int)((sent - t0) / 1000), got,
             (int)((esp_timer_get_time() - t0) / 1000));
    esp_http_client_cleanup(c);
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
    s_cfg = cfg;
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
