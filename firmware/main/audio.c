/* The ES8311 codec: speaker out and microphone in, one part, one I2S link, one handle.
 *
 * Pins are the BSP's (`waveshare/esp32_s3_touch_amoled_1_8`), read from the component rather
 * than from the Arduino `pin_config.h` in the sample repo — that header carries BOTH
 * `I2S_DO_IO 8`/`I2S_DI_IO 10` and `DOPIN 10`/`DIPIN 8`, which are the same two pins named
 * from opposite ends of the link. Guessing between them gives silence AND a dead microphone,
 * with no error from either.
 *
 * VOLUME IS A SAFETY LIMIT HERE, not a preference. The vendor example ships 90/100 for V2
 * hardware. This is a 29 mm object a four-year-old will hold to his ear, and ASTM F963 /
 * EN 71-1 cap close-to-ear toys at 65 dB(A) (ROOM_ENDPOINT_PLAN.md). Nothing in this session
 * can measure decibels, so the starting point is deliberately low and the owner's ear is the
 * instrument. Raise it only against a measurement.
 */

#include "audio.h"

#include <math.h>
#include <stdint.h>

#include "driver/i2s_std.h"
#include "es8311_codec.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "speech.h"

static const char *TAG = "audio";

#define I2S_PORT I2S_NUM_0
#define PIN_MCLK GPIO_NUM_16
#define PIN_BCLK GPIO_NUM_9
#define PIN_WS GPIO_NUM_45
#define PIN_DOUT GPIO_NUM_8 /* ESP -> codec: the speaker */
#define PIN_DSIN GPIO_NUM_10 /* codec -> ESP: the microphone */
#define PIN_PA GPIO_NUM_46

#define BEEP_HZ 880
#define BEEP_MS 90
/* The microphone is ANALOGUE into the ES8311's own ADC (`digital_mic = false` in the vendor
   BSP), so it needs the codec's PGA. The part quantises to 6 dB steps up to 42; 30 is the
   middle and a first guess — the peak this firmware reports is what moves it, not a listen. */
/* 30 was a first guess with nothing able to measure it. The panel can now: at 30 dB, speech
   reached the recogniser at -33 to -20 dBFS and MultiNet decoded nothing at all (0.2.40).
   The part quantises to 6 dB steps to a maximum of 42; 36 is one step below that, and the
   front end's WebRTC AGC (`speech.c`) makes up the rest without pinning the PGA at its
   limit, where the noise floor comes up with the signal. The peak is logged every three
   seconds, so the next move after this one is a reading rather than another guess. */
#define MIC_GAIN_DB 36.0f
/* Where to go if the part refuses the number above. 30 is the value that produced the
   highest measured peaks of any build so far, which makes it the safest floor. */
#define MIC_GAIN_FALLBACK_DB 30.0f

/* See the header note: this is a cap, not a taste. 55 was the deliberate starting point with
   nothing here able to measure decibels; the owner reported it a little quiet, and confirmed
   70 as good at the distance a child holds it. That is the measurement §10.4q said it was
   waiting for — so this number is no longer a guess, and still well under the vendor's 90. */
#define VOLUME 70

/* One handle for both directions. The vendor BSP builds two codec instances, one per
   direction — two objects writing the same chip's registers over the same I2C bus. A single
   IN_OUT device is the same hardware with one owner. */
static esp_codec_dev_handle_t s_codec;

/* The one task that touches `s_codec`. Defined below, beside the requests it services. */
static void audio_task(void *arg);

/* The tone is built once. `audio_beep` runs on the face task, between two frames of a
   500 ms floor the panel needs to stay lit — so it may spend its time in the I2S write
   and not in two thousand calls to sinf. */
#define BEEP_SAMPLES (AUDIO_RATE * BEEP_MS / 1000)
static int16_t s_beep[BEEP_SAMPLES];

static void build_beep(void)
{
    const float step = 2.0f * (float)M_PI * BEEP_HZ / AUDIO_RATE;
    const int fade = BEEP_SAMPLES / 5;
    for (int i = 0; i < BEEP_SAMPLES; i++) {
        /* Raised-cosine in and out: a square-edged tone clicks, and the click is the
           loudest thing in it — which is the part a 65 dB(A) cap is really about. */
        float env = 1.0f;
        if (i < fade) {
            env = 0.5f - 0.5f * cosf((float)M_PI * (float)i / (float)fade);
        } else if (i > BEEP_SAMPLES - fade) {
            env = 0.5f - 0.5f * cosf((float)M_PI * (float)(BEEP_SAMPLES - i) / (float)fade);
        }
        s_beep[i] = (int16_t)(sinf(step * (float)i) * 9000.0f * env);
    }
}

bool audio_start(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) return false;

    i2s_chan_handle_t tx = NULL;
    i2s_chan_handle_t rx = NULL;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    if (i2s_new_channel(&chan_cfg, &tx, &rx) != ESP_OK) {
        ESP_LOGE(TAG, "i2s channel");
        return false;
    }
    const i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = PIN_MCLK,
            .bclk = PIN_BCLK,
            .ws = PIN_WS,
            .dout = PIN_DOUT,
            .din = PIN_DSIN,
            .invert_flags = {0},
        },
    };
    /* Both directions take the same config; the codec layer enables and disables each
       channel around a read or a write, so neither is enabled here. */
    if (i2s_channel_init_std_mode(tx, &std_cfg) != ESP_OK ||
        i2s_channel_init_std_mode(rx, &std_cfg) != ESP_OK) {
        ESP_LOGE(TAG, "i2s std mode");
        return false;
    }

    audio_codec_i2s_cfg_t i2s_cfg = {.port = I2S_PORT, .tx_handle = tx, .rx_handle = rx};
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);
    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = bus};
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    if (data_if == NULL || ctrl_if == NULL || gpio_if == NULL) {
        ESP_LOGE(TAG, "codec interfaces");
        return false;
    }

    es8311_codec_cfg_t es_cfg = {
        .ctrl_if = ctrl_if,
        .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .pa_pin = PIN_PA,
        .use_mclk = true,
        .hw_gain = {.pa_voltage = 5.0f, .codec_dac_voltage = 3.3f},
    };
    const audio_codec_if_t *dev = es8311_codec_new(&es_cfg);
    if (dev == NULL) {
        ESP_LOGE(TAG, "es8311 not found");
        return false;
    }
    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT, .codec_if = dev, .data_if = data_if};
    s_codec = esp_codec_dev_new(&dev_cfg);
    if (s_codec == NULL) return false;

    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel = 1,
        .channel_mask = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0),
        .sample_rate = AUDIO_RATE,
    };
    if (esp_codec_dev_open(s_codec, &fs) != 0) {
        ESP_LOGE(TAG, "codec open");
        s_codec = NULL;
        return false;
    }
    /* CHECKED, BECAUSE THE LOG LINE BELOW USED TO BE A CLAIM RATHER THAN A READING.
       It printed MIC_GAIN_DB — the number ASKED FOR — while the return value went on the
       floor. 0.2.42 raised the gain 30 -> 42 and the measured level went DOWN by about the
       same 12 dB, which is what an out-of-range value that wraps looks like and what an
       unchecked setter lets you believe never happened. Same defect as "listening" meaning
       "a task started": a claim nothing could falsify. */
    const int vol_err = esp_codec_dev_set_out_vol(s_codec, VOLUME);
    const int gain_err = esp_codec_dev_set_in_gain(s_codec, MIC_GAIN_DB);
    if (vol_err != 0) ESP_LOGE(TAG, "set_out_vol(%d) refused: %d", VOLUME, vol_err);
    if (gain_err != 0) {
        ESP_LOGE(TAG, "set_in_gain(%.0f) refused: %d — falling back to %.0f dB", MIC_GAIN_DB,
                 gain_err, (double)MIC_GAIN_FALLBACK_DB);
        const int retry = esp_codec_dev_set_in_gain(s_codec, MIC_GAIN_FALLBACK_DB);
        if (retry != 0) ESP_LOGE(TAG, "fallback gain refused too: %d — mic level is unknown",
                                 retry);
    }
    build_beep();
    ESP_LOGI(TAG, "es8311 ready: out %d/100 (%s), in %.0f dB (%s), %d Hz", VOLUME,
             vol_err ? "REFUSED" : "accepted", MIC_GAIN_DB,
             gain_err ? "REFUSED" : "accepted",
             AUDIO_RATE);

    /* Priority 5, one above the render task: a late frame is a slightly janky robot, a late
       capture is a dropped chunk and a meter that lags the room. The stack is small because
       this task calls into the codec and computes a peak, and nothing else. */
    if (xTaskCreate(audio_task, "audio", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "audio task");
        return false;
    }
    return true;
}

/* The latest chunk's peak, and a pending tone. Both are single words touched by two tasks:
   the writer sets, the audio task clears or overwrites. No lock is needed for that and none
   would help — what needed a lock was the CODEC, and the answer to that is that only one task
   touches it at all. */
static volatile int s_level;
static volatile bool s_beep_want;

/* Defined with the rest of the level machinery, below the task that is its only caller. */
static void apply_levels(void);

void audio_beep(void)
{
    s_beep_want = true;
}

int audio_level(void)
{
    return s_level;
}

/* Internal RAM, not PSRAM: this is an I2S DMA destination on every chunk. */
static int16_t s_chunk[AUDIO_CHUNK];

static void audio_task(void *arg)
{
    (void)arg;
    while (true) {
        /* Before the read, so a tap is answered within one chunk rather than after it. */
        if (s_beep_want) {
            s_beep_want = false;
            esp_codec_dev_write(s_codec, s_beep, sizeof(s_beep));
        }
        apply_levels();

        /* THE READ IS THE CLOCK. It blocks for exactly one chunk and drains the DMA at the
           rate it fills, so this task needs no delay of its own and cannot fall behind the
           room. On failure it would spin, so that path delays instead. */
        if (esp_codec_dev_read(s_codec, s_chunk, sizeof(s_chunk)) == 0) {
            s_level = audio_peak(s_chunk, AUDIO_CHUNK);
            /* THE RECOGNISER IS FED FROM HERE because this task is the microphone's one
               owner, and esp-sr's usual arrangement — its own task reading I2S directly —
               would be a second one. `speech_feed` is a memcpy and a hand-off; the model
               runs on its own core (`speech.c`), so nothing below this line can stall the
               read that is also this task's clock. */
            speech_feed(s_chunk, AUDIO_CHUNK);
        } else {
            vTaskDelay(pdMS_TO_TICKS(AUDIO_CHUNK_MS));
        }
    }
}

int audio_peak(const int16_t *buf, int samples)
{
    int peak = 0;
    for (int i = 0; i < samples; i++) {
        /* INT16_MIN has no positive counterpart; clamp rather than negate it. */
        const int v = buf[i] == INT16_MIN ? 32767 : (buf[i] < 0 ? -buf[i] : buf[i]);
        if (v > peak) peak = v;
    }
    return peak;
}

/* ONE TASK OWNS THE CODEC, AND IT IS THE RENDER TASK — the same rule display.c states for
 * the panel, and for the same reason. `esp_codec_dev.c` contains no lock of any kind: read,
 * write, set_out_vol and set_in_gain all walk straight into the device struct and the codec's
 * I2C registers. The component's one mutex lives in `audio_codec_data_i2s.c` and serialises
 * the DATA path only, so it does nothing for a control write.
 *
 * `apply_settings()` calls this from the MAIN task, at boot and every fifteen minutes, while
 * the render task sits inside `esp_codec_dev_read` for about 40 ms of every 40 ms frame. On
 * 2026-09-21 the panel panicked in exactly that window: the box logged `GET /settings`
 * answered and then NO telemetry post, which puts the fault between this call and the next
 * two lines of `report()` (ROOM_ENDPOINT_PLAN.md §10.4al).
 *
 * So this records, and the render task applies. */
static volatile int s_want_volume = -1;
static volatile int s_want_gain = -1;
static volatile bool s_levels_pending;

void audio_set_levels(int volume, int mic_gain_db)
{
    bool any = false;
    if (volume >= 0 && volume <= 100) {
        s_want_volume = volume;
        any = true;
    }
    if (mic_gain_db >= 0 && mic_gain_db <= 42) {
        s_want_gain = mic_gain_db;
        any = true;
    }
    if (any) s_levels_pending = true;
}

/* Called only from the audio task, between two captures. */
static void apply_levels(void)
{
    if (!s_levels_pending) return;
    s_levels_pending = false;
    if (s_codec == NULL) return;
    const int vol = s_want_volume;
    const int gain = s_want_gain;
    if (vol >= 0) esp_codec_dev_set_out_vol(s_codec, vol);
    if (gain >= 0) esp_codec_dev_set_in_gain(s_codec, (float)gain);
    ESP_LOGI(TAG, "levels: out %d/100, in %d dB", vol, gain);
}
