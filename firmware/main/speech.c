/* ESP-SR, wired for ALWAYS LISTENING. The design and its limits are in `speech.h`; this file
 * is the wiring, and three things in it are not obvious.
 *
 * NO WAKENET. The owner asked the panel to just listen, so the front end runs with the wake
 * word disabled and MultiNet sees every frame the VAD calls speech. That is a supported AFE
 * configuration and it is why `vocab.c` is short and its phrases are two words or more.
 *
 * NO AEC EITHER, and not by choice: this board has one ES8311 and no ES7210, so there is no
 * playback reference channel to cancel against (ROOM_ENDPOINT_PLAN.md §10.5 A). The front end
 * is told the truth about its input — one microphone, no reference — rather than being handed
 * a fake channel, which would make it cancel against silence.
 *
 * THE FEED SIZE IS NOT THE CAPTURE SIZE. `audio.c` reads 40 ms chunks because that is a good
 * period for a task that also has to service a beep; the AFE asks for its own chunk, which is
 * neither 40 ms nor guaranteed to be any particular number. So this file accumulates. Feeding
 * a short buffer is not a soft failure in esp-sr — it reads `get_feed_chunksize` samples from
 * the pointer regardless.
 */

#include "speech.h"

#include <ctype.h>
#include <string.h>

#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "freertos/FreeRTOS.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "model_path.h"
#include "vocab.h"

static const char *TAG = "speech";

/* The partition the one USB flash writes, and the only one OTA cannot reach — which is why
   the models went onto the board before this code existed (firmware/README.md). */
#define MODEL_PARTITION "model"

/* How long MultiNet will chase a phrase before giving up on it. Espressif's own default for
   an always-on command mode; long enough for "make a rude noise" said by a four-year-old. */
#define MN_TIMEOUT_MS 5760

/* Core 1. Core 0 carries Wi-Fi, and a MultiNet pass that lands on the same core as the radio
   is the classic way to make both stutter. */
/* Below this much free INTERNAL heap the recogniser does not start at all. Internal RAM is
   what Wi-Fi's DMA descriptors and mbedTLS's handshake buffers must come from, and there is
   only 140 KB of it on this board. */
#define SPEECH_MIN_INTERNAL (48 * 1024)

/* How often the detect loop says what it is hearing. Often enough to watch someone talk to
   it over a console, rare enough not to bury the log. */
#define SPEECH_REPORT_MS 3000

#define SR_CORE 1
#define SR_STACK 6144
#define SR_PRIO 5

static const esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t *s_afe_data;
static const esp_mn_iface_t *s_mn;
static model_iface_data_t *s_mn_data;

static int s_feed_chunk;      /* samples per feed call, per channel */
static int s_fetch_chunk;     /* samples the front end hands back per fetch */
static int s_mn_chunk;        /* samples MultiNet wants per detect call */
static int16_t *s_mn_fill;    /* accumulator, because the two are not the same number */
static int s_mn_filled;
static volatile uint32_t s_fed, s_fetched;  /* counted so "is audio arriving" is answerable */
static volatile int s_peak;   /* loudest sample the front end has handed back lately */
static int16_t *s_fill;       /* accumulator, s_feed_chunk samples */
static int s_filled;

static volatile bool s_live;
static volatile bool s_hearing;
static int s_accepted, s_rejected;

/* One-deep mailbox: the render task reads it once a frame, so a second phrase inside 40 ms is
   a phrase nobody could have read anyway. A queue here would only buffer the panel's own
   latency. */
static char s_heard[64];
static volatile int s_heard_id = -1;
static volatile bool s_have;

bool speech_live(void)
{
    return s_live;
}

bool speech_hearing(void)
{
    return s_hearing;
}

void speech_vocab(int *accepted, int *rejected)
{
    if (accepted) *accepted = s_accepted;
    if (rejected) *rejected = s_rejected;
}

bool speech_take(char *out, int cap, int *id)
{
    if (out == NULL || cap <= 0 || !s_have) return false;
    strncpy(out, s_heard, (size_t)cap - 1);
    out[cap - 1] = '\0';
    if (id != NULL) *id = s_heard_id;
    s_have = false;
    return true;
}

static void publish(int id, const char *phrase)
{
    /* Uppercased here rather than in `caption.c`, because the 5x7 font is uppercase-only and
       the ticker should not have to know why. */
    size_t i = 0;
    for (; phrase[i] != '\0' && i < sizeof(s_heard) - 1; i++) {
        s_heard[i] = (char)toupper((unsigned char)phrase[i]);
    }
    s_heard[i] = '\0';
    s_heard_id = id;
    s_have = true;
}

/* NO LOCK, and that is the one-owner rule rather than an omission: `audio.c`'s task is the
   only reader of the microphone and therefore the only caller here, exactly as the render
   task is the only caller into the panel. A mutex would document a second caller that must
   not exist. */
void speech_feed(const int16_t *pcm, int samples)
{
    if (s_afe_data == NULL || pcm == NULL || samples <= 0) return;
    while (samples > 0) {
        const int want = s_feed_chunk - s_filled;
        const int take = samples < want ? samples : want;
        memcpy(s_fill + s_filled, pcm, (size_t)take * sizeof(int16_t));
        s_filled += take;
        pcm += take;
        samples -= take;
        if (s_filled == s_feed_chunk) {
            s_afe->feed(s_afe_data, s_fill);
            s_filled = 0;
            s_fed++;
        }
    }
}

/* Hand MultiNet exactly the number of samples it asks for, however many the front end
 * happened to return.
 *
 * THIS IS WHY NOTHING WAS RECOGNISED. `detect()` reads `get_samp_chunksize()` samples from the
 * pointer it is given and trusts the caller; the front end's `get_fetch_chunksize()` is a
 * different number. Feeding one to the other reads the wrong length every frame, so the model
 * sees an audio stream that skips or repeats a few milliseconds at every boundary — which
 * decodes to nothing at all, silently and forever, while every log line says "listening".
 * Espressif's own examples assert the two are equal rather than handling it; asserting would
 * have made this loud, and buffering makes it correct. */
static esp_mn_state_t feed_multinet(const int16_t *pcm, int samples)
{
    esp_mn_state_t st = ESP_MN_STATE_DETECTING;
    while (samples > 0) {
        const int want = s_mn_chunk - s_mn_filled;
        const int take = samples < want ? samples : want;
        memcpy(s_mn_fill + s_mn_filled, pcm, (size_t)take * sizeof(int16_t));
        s_mn_filled += take;
        pcm += take;
        samples -= take;
        if (s_mn_filled == s_mn_chunk) {
            const esp_mn_state_t r = s_mn->detect(s_mn_data, s_mn_fill);
            s_mn_filled = 0;
            if (r != ESP_MN_STATE_DETECTING) st = r;
        }
    }
    return st;
}

static void detect_task(void *arg)
{
    (void)arg;
    uint32_t last_report = 0;
    while (true) {
        afe_fetch_result_t *res = s_afe->fetch(s_afe_data);
        if (res == NULL || res->ret_value == ESP_FAIL) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        s_fetched++;
        s_hearing = res->vad_state == VAD_SPEECH;

        /* The loudest sample in this frame, so "the microphone is dead" and "the model is
           deaf" are distinguishable from a console without anyone describing a noise. */
        const int n = res->data_size / (int)sizeof(int16_t);
        int peak = 0;
        for (int i = 0; i < n; i++) {
            const int v = res->data[i] < 0 ? -res->data[i] : res->data[i];
            if (v > peak) peak = v;
        }
        s_peak = peak;

        const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        if (now - last_report >= SPEECH_REPORT_MS) {
            last_report = now;
            ESP_LOGI(TAG, "fed %u fetched %u | peak %5d | vad %s | %.1f dBFS", (unsigned)s_fed,
                     (unsigned)s_fetched, peak, s_hearing ? "SPEECH " : "silence",
                     (double)res->data_volume);
        }

        const esp_mn_state_t st = feed_multinet(res->data, n);
        if (st == ESP_MN_STATE_DETECTED) {
            esp_mn_results_t *r = s_mn->get_results(s_mn_data);
            const vocab_t *v = r->num > 0 ? vocab_get(r->command_id[0]) : NULL;
            if (v != NULL) {
                ESP_LOGI(TAG, "heard '%s' p=%.2f", v->phrase, (double)r->prob[0]);
                publish(r->command_id[0], v->phrase);
            }
            /* MUST be cleaned after a detection or the next phrase decodes against this
               one's state. Timeout is the same: the model has to be told the phrase is over
               whether it resolved or not. */
            s_mn->clean(s_mn_data);
        } else if (st == ESP_MN_STATE_TIMEOUT) {
            /* What the decoder HEARD, before the command graph rejected it. Without this a
               phrase that missed and a microphone that is dead look identical from here. */
            esp_mn_results_t *r = s_mn->get_results(s_mn_data);
            ESP_LOGI(TAG, "timeout, raw decode: '%s'", r != NULL ? r->raw_string : "?");
            s_mn->clean(s_mn_data);
        }
    }
}

static void load_vocabulary(void)
{
    esp_mn_commands_alloc(s_mn, s_mn_data);
    const vocab_t *all = vocab_all();
    for (int i = 0; i < vocab_count(); i++) esp_mn_commands_add(i, all[i].phrase);

    /* THE REFUSALS ARE THE INTERESTING PART. A phrase MultiNet cannot tokenise is dropped
       silently and the panel is then deaf to that one thing with nothing on the glass to say
       so — which is indistinguishable, from the room, from a broken microphone. */
    esp_mn_error_t *err = esp_mn_commands_update();
    s_rejected = err != NULL ? err->num : 0;
    s_accepted = vocab_count() - s_rejected;
    for (int i = 0; err != NULL && i < err->num; i++) {
        ESP_LOGE(TAG, "phrase refused by the model: '%s'", err->phrases[i]->string);
    }
    ESP_LOGI(TAG, "vocabulary: %d accepted, %d refused", s_accepted, s_rejected);
}

bool speech_start(void)
{
    /* THE RECOGNISER IS NEVER ALLOWED TO COST THE PANEL ITS RADIO. Everything else here is
       an optimisation; this is the invariant. A panel that cannot reach the box is the one
       state this whole design calls unrecoverable, because it is the one an OTA cannot fix
       — so if starting would leave too little internal RAM for Wi-Fi and a TLS handshake,
       the ticker simply does not exist and the panel says so. Measured on a working
       0.2.38 boot: the radio is already up by the time this runs, and what it needs is
       already spoken for. The floor is what a TLS handshake and an OTA still want on top. */
    const size_t free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    if (free_internal < SPEECH_MIN_INTERNAL) {
        ESP_LOGW(TAG, "only %u B of internal heap free — not starting the recogniser, because "
                      "a panel that cannot be updated is worse than one that cannot listen",
                 (unsigned)free_internal);
        return false;
    }
    ESP_LOGI(TAG, "starting with %u B internal heap free", (unsigned)free_internal);

    srmodel_list_t *models = esp_srmodel_init(MODEL_PARTITION);
    if (models == NULL || models->num <= 0) {
        ESP_LOGW(TAG, "no models in the '%s' partition — no ticker", MODEL_PARTITION);
        return false;
    }
    char *mn_name = esp_srmodel_filter(models, ESP_MN_PREFIX, ESP_MN_ENGLISH);
    if (mn_name == NULL) {
        ESP_LOGW(TAG, "no english command model on the board");
        return false;
    }

    /* "M": one microphone channel, no playback reference and no unused channels. See the
       file header — this board has no ES7210, so there is nothing to cancel against. */
    afe_config_t *cfg = afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
    if (cfg == NULL) return false;
    cfg->wakenet_init = false; /* always listening, by request */
    cfg->aec_init = false;     /* no reference channel exists on this board */
    cfg->vad_init = true;      /* gates MultiNet, and drives the recording indicator */
    cfg->afe_perferred_core = SR_CORE;
    /* PSRAM, AND THIS IS THE LINE THAT COST A BOOT LOOP. The front end defaults to
       allocating out of INTERNAL RAM, of which this board has 140 KB total against 8 MB of
       PSRAM — and the Wi-Fi driver's DMA descriptors can live nowhere else. 0.2.37 came up
       listening perfectly and then could not start a radio: `esp_wifi_init` returned
       ESP_ERR_NO_MEM three seconds into every boot. The recogniser has no such constraint;
       it is a compute pipeline reading a ring buffer, and PSRAM at 80 MHz feeds it fine. */
    cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    /* AGC, AND WITHOUT THIS THE MODEL IS HANDED AUDIO TOO QUIET TO DECODE.
       The default mode is AFE_AGC_MODE_WAKENET, whose own header says the gain is
       "calculated by wakenet model IF WAKENET IS ACTIVATED" — and this firmware disables
       wakenet, because the owner asked it to just listen. So the default silently applies no
       gain at all. Measured on 0.2.40: speech reached the model at -33 to -20 dBFS, VAD
       agreed someone was talking, and MultiNet's raw decode came back an EMPTY STRING every
       time. WebRTC's AGC needs no wake word and is the only mode that works in this
       configuration. */
    cfg->agc_init = true;
    cfg->agc_mode = AFE_AGC_MODE_WEBRTC;
    cfg->agc_target_level_dbfs = 3;      /* peak target -3 dBFS, the component's own default */
    cfg->agc_compression_gain_db = 9;
    afe_config_check(cfg);
    ESP_LOGI(TAG, "front end: agc %s mode %d target -%d dBFS, ns %s, vad %s, wakenet %s",
             cfg->agc_init ? "on" : "off", (int)cfg->agc_mode, cfg->agc_target_level_dbfs,
             cfg->ns_init ? "on" : "off", cfg->vad_init ? "on" : "off",
             cfg->wakenet_init ? "on" : "off");

    s_afe = esp_afe_handle_from_config(cfg);
    s_afe_data = s_afe->create_from_config(cfg);
    afe_config_free(cfg);
    if (s_afe_data == NULL) {
        ESP_LOGE(TAG, "the front end would not start");
        return false;
    }

    s_feed_chunk = s_afe->get_feed_chunksize(s_afe_data) * s_afe->get_feed_channel_num(s_afe_data);
    /* PSRAM too: this is a staging copy on its way into `feed`, never a DMA target, and
       internal RAM here is the scarcest thing on the board. */
    s_fill = heap_caps_malloc((size_t)s_feed_chunk * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (s_fill == NULL) {
        ESP_LOGE(TAG, "no room for a %d sample feed buffer", s_feed_chunk);
        return false;
    }

    s_mn = esp_mn_handle_from_name(mn_name);
    s_mn_data = s_mn->create(mn_name, MN_TIMEOUT_MS);
    if (s_mn_data == NULL) {
        ESP_LOGE(TAG, "the command model would not load");
        return false;
    }
    /* THE TWO CHUNK SIZES ARE NOT THE SAME NUMBER, and assuming they were is what made this
       feature silently deaf. Both are logged so the next reader never has to wonder. */
    s_fetch_chunk = s_afe->get_fetch_chunksize(s_afe_data);
    s_mn_chunk = s_mn->get_samp_chunksize(s_mn_data);
    s_mn_fill = heap_caps_malloc((size_t)s_mn_chunk * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (s_mn_fill == NULL) {
        ESP_LOGE(TAG, "no room for a %d sample recogniser buffer", s_mn_chunk);
        return false;
    }
    ESP_LOGI(TAG, "chunks: feed %d, fetch %d, multinet %d%s", s_feed_chunk, s_fetch_chunk,
             s_mn_chunk, s_fetch_chunk == s_mn_chunk ? " (equal)" : " (DIFFERENT — buffered)");

    load_vocabulary();

    if (xTaskCreatePinnedToCore(detect_task, "sr", SR_STACK, NULL, SR_PRIO, NULL, SR_CORE) !=
        pdPASS) {
        ESP_LOGE(TAG, "no room for the recogniser task");
        return false;
    }
    s_live = true;
    ESP_LOGI(TAG, "listening: %s, %d samples per feed, %u B internal heap left", mn_name,
             s_feed_chunk, (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    return true;
}
