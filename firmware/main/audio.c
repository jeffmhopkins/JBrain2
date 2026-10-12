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
 * can measure decibels, so the starting point was deliberately low and the owner's ear is the
 * instrument.
 *
 * RAISED TO 90 AT THE OWNER'S REQUEST (0.2.71), which is the vendor's own figure and the
 * thing the paragraph above says to do only against a measurement. There is still no
 * measurement — there is a parent who has listened to it in the room it lives in, which is
 * the only instrument this project has ever had for this number. Recorded rather than
 * quietly changed, because the next person to read this should know the limit was crossed
 * deliberately and by whom, and because a sound level meter would settle it in a minute.
 *
 * THE BEEP AND THE VOICE ARE NOW SEPARATE, which is the better half of the same request.
 * They share one codec output, so for the whole of this project's life the acknowledgement
 * tone has been exactly as loud as the pet's speech — and the beep is the part held closest
 * to an ear, the part that fires on every poke, and the part with a hard transient in it.
 * Scaling the tone in software costs nothing (render synthesises it, at `STREAM_CUE_GAIN`)
 * and lets the voice get louder while the beep gets quieter, which is the direction that makes
 * the toy both more useful and less shrill.
 *
 * WHAT THIS FILE IS NOW: codec I/O, the ALC, levels, the capture buffer and `speech_feed`.
 * Every decision about what the speaker says — cues, the reply, the message ring, pause,
 * deafness — is `stream_tick`'s (`stream.c`, host-tested), and `make check_no_logic_in_audio`
 * keeps it out of here.
 */

#include "audio.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "driver/i2s_std.h"
#include "es8311_codec.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "speech.h"
#include "stream.h"

static const char *TAG = "audio";

#define I2S_PORT I2S_NUM_0
#define PIN_MCLK GPIO_NUM_16
#define PIN_BCLK GPIO_NUM_9
#define PIN_WS GPIO_NUM_45
#define PIN_DOUT GPIO_NUM_8 /* ESP -> codec: the speaker */
#define PIN_DSIN GPIO_NUM_10 /* codec -> ESP: the microphone */
#define PIN_PA GPIO_NUM_46

/* The microphone is ANALOGUE into the ES8311's own ADC (`digital_mic = false` in the vendor
   BSP), so it needs the codec's PGA. The part quantises to 6 dB steps up to 42; 30 is the
   middle and a first guess — the peak this firmware reports is what moves it, not a listen. */
/* 30 was a first guess with nothing able to measure it. The panel can now: at 30 dB, speech
   reached the recogniser at -33 to -20 dBFS and MultiNet decoded nothing at all (0.2.40).
   The part quantises to 6 dB steps to a maximum of 42; 36 is one step below that, and the
   front end's WebRTC AGC (`speech.c`) makes up the rest without pinning the PGA at its
   limit, where the noise floor comes up with the signal. The peak is logged every three
   seconds, so the next move after this one is a reading rather than another guess. */
/* STAYS AT 36, AND THE VERSION THAT ALMOST DROPPED IT TO 27 IS THE REASON THIS SAYS SO.
   The first successful decode arrived with peak 32768 at -2.0 dBFS, which looked like an
   obvious argument for backing the gain off. It was not, for two reasons the same capture
   contains:

     - the panel was being HELD AT THE OWNER'S FACE for a photograph. Ambient in the same
       room reads -46 to -54 dBFS, so a voice at the distance this thing is actually used
       from lands near -20 dBFS, which is about where MultiNet wants it. Nine dB down would
       have put room-distance speech at -29 to fix a case that only happens at arm's length;
     - and BOTH successful decodes happened while it was clipping. "pick a new color"
       changed the colour and "jump up" fired, at -2.0 dBFS. The premise that clipping was
       preventing recognition is contradicted by the only evidence there is for it.

   The real shape of this is dynamic range — close talk and across the room want different
   gains — and a single fixed number only chooses which end to fail at. That is what AGC is
   for, and §10.4bf records why it is not affordable yet and what changed that might make it
   so. `clipped` in the 3 s report is here to measure the trade rather than argue about it. */
#define MIC_GAIN_DB 36.0f
/* Where to go if the part refuses the number above, and it has to stay BELOW it — briefly it
   did not, when the primary came down to 27 and this was left at 30. A fallback louder than
   the value it backs up turns "the part refused your setting" into "the part is now
   clipping". 30 is correct again only because the primary is 36 again; the two move together
   or not at all. */
#define MIC_GAIN_FALLBACK_DB 30.0f

/* See the header note: this is a cap, not a taste. 55 was the deliberate starting point with
   nothing here able to measure decibels; the owner reported it a little quiet, and confirmed
   70 as good at the distance a child holds it. That is the measurement §10.4q said it was
   waiting for — so this number is no longer a guess, and still well under the vendor's 90. */
/* THE COMPILE-TIME VOLUME, AND IT IS THE ONE THAT HAS BEEN IN FORCE. Measured 2026-09-27: the
   box's value has never reached Tamsin's panel (`levels` empty at 941 s of uptime), so this
   number — not the slider — is what the room has been hearing, and the owner's verdict on it
   was *"the speaker just is not loud enough."* 95, which is what they asked the box for and
   could not have, because `VOLUME_MAX` clamped the slider at 85.
   A panel that never hears from the box is now loud enough on its own. */
#define VOLUME 95

/* One handle for both directions. The vendor BSP builds two codec instances, one per
   direction — two objects writing the same chip's registers over the same I2C bus. A single
   IN_OUT device is the same hardware with one owner. */
static esp_codec_dev_handle_t s_codec;
/* Kept so the ALC can be read back from the audio task — see `alc_apply()`. */
static const audio_codec_ctrl_if_t *s_ctrl;

/* THE RECORDING, claimed once at start-up out of the board's 8 MB of PSRAM: 35 s of 16 kHz mono
   s16 is 1.1 MB, the largest buffer on this panel. Never allocated while recording — a heap
   request in the middle of a four-year-old talking is a failure with no good outcome. The cap
   (`CAPTURE_MAX_MS`) lives in `stream.h` so the machine and the box's pins read one number. */
#define CAPTURE_MAX_SAMPLES (AUDIO_RATE * CAPTURE_MAX_MS / 1000)
static int16_t *s_cap;          /* PSRAM, claimed at start-up */
static volatile int s_cap_used; /* samples written this recording */
static volatile bool s_cap_on;

/* The panel's one speaker state. Static and zeroed, so it is valid before `audio_start` and on a
   panel with no codec. Internal RAM: the lease in it is used with compare-and-swap. */
static stream_t s_stream;

struct stream_s *audio_stream(void)
{
    return &s_stream;
}

bool audio_capture_open(void)
{
    if (s_cap == NULL || stream_leased(&s_stream)) return false;
    s_cap_used = 0;
    s_cap_on = true;
    return true;
}

const int16_t *audio_capture_close(size_t *len_bytes)
{
    s_cap_on = false;
    const int used = s_cap_used;
    if (len_bytes != NULL) *len_bytes = (size_t)used * sizeof(int16_t);
    return (s_cap != NULL && used > 0) ? s_cap : NULL;
}

int audio_capture_cap_ms(void)
{
    return CAPTURE_MAX_MS;
}

int audio_capture_ms(void)
{
    return s_cap_used * 1000 / AUDIO_RATE;
}


/* The one task that touches `s_codec`. Defined below, beside the requests it services. */
static void audio_task(void *arg);

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
    s_ctrl = ctrl_if;
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
    ESP_LOGI(TAG, "es8311 ready: out %d/100 (%s), in %.0f dB (%s), %d Hz", VOLUME,
             vol_err ? "REFUSED" : "accepted", MIC_GAIN_DB,
             gain_err ? "REFUSED" : "accepted",
             AUDIO_RATE);

    /* PSRAM, claimed once, before anything else wants it, and never again: a heap request in the
       middle of a four-year-old talking — or of her father's message playing — is a failure
       with no good outcome. Failure is not fatal. No capture buffer: the panel keeps its voice
       commands and meter and cannot record. No ring or cue buffer: `stream_can_play` is false
       and a PLAY fails at once. No slot buffers: render writes no cues at all, all or none. */
    s_cap = heap_caps_malloc((size_t)CAPTURE_MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    int16_t *ring =
        heap_caps_malloc((size_t)STREAM_RING_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    int16_t *play = heap_caps_malloc((size_t)CUE_MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    stream_cue_buf_t *bufs[STREAM_SLOT_N] = {0};
    bool slots = true;
    for (int i = 0; i < STREAM_SLOT_N; i++) {
        bufs[i] = heap_caps_malloc(sizeof(stream_cue_buf_t), MALLOC_CAP_SPIRAM);
        slots = slots && bufs[i] != NULL;
    }
    if (!slots) {
        for (int i = 0; i < STREAM_SLOT_N; i++) {
            heap_caps_free(bufs[i]);
            bufs[i] = NULL;
        }
    }
    stream_init(&s_stream, ring, play, bufs);
    ESP_LOGI(TAG, "capture %s (%d ms), stream %s, cues %s",
             s_cap != NULL ? "ready" : "UNAVAILABLE", CAPTURE_MAX_MS,
             ring != NULL && play != NULL ? "ready" : "UNAVAILABLE",
             slots ? "ready" : "UNAVAILABLE");

    /* Priority 5, one above the render task: a late frame is a slightly janky robot, a late
       capture is a dropped chunk and a meter that lags the room. */
    if (xTaskCreate(audio_task, "audio", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "audio task");
        return false;
    }
    /* ONLY NOW: a speaker flag raised before the task exists would have render write slots that
       nothing ever drains, and the busy facts would latch. */
    stream_set_arbiter(&s_stream, true);
    return true;
}

/* The latest chunk's peak, and a pending tone. Both are single words touched by two tasks:
   the writer sets, the audio task clears or overwrites. No lock is needed for that and none
   would help — what needed a lock was the CODEC, and the answer to that is that only one task
   touches it at all. */
static volatile int s_level;

/* Defined with the rest of the level machinery, below the task that is its only caller. */
static void apply_levels(void);

int audio_level(void)
{
    return s_level;
}

/* Internal RAM, not PSRAM: this is an I2S DMA destination on every chunk. */
static int16_t s_chunk[AUDIO_CHUNK];

/* ADC register 0x18: ALC enable in bit 7, window size in the rest. */
#define ES8311_REG_ALC 0x18
#define ES8311_ALC_ENABLE 0x80

/* THE ONE SETTING ON THIS CHIP NOBODY HAD EVER READ.
 *
 * The owner: "it seems like when it beeps that it kind of rails the audio gain meter for 4 to
 * 5 seconds after it beeps. Maybe we need to disable the auto gain control if it's on."
 *
 * There are two candidates and this firmware could answer for neither:
 *
 *   - The ES8311 has its own ALC — an automatic gain control in the codec, ahead of
 *     everything this firmware can see. `es8311.c` writes REG1B and REG1C (automute, HPF)
 *     and NEVER WRITES REG18, the register that enables it. So ALC has been at whatever the
 *     chip's reset default is since the first bring-up, and seconds of gain ramp after a loud
 *     sound is exactly what an ALC release does.
 *   - `es8311.c` also sets REG44 = 0x58, which the driver's own comment calls the "internal
 *     reference signal (ADCL + DACR)" — the DAC deliberately routed into the ADC. The panel
 *     is wired to hear its own speaker.
 *
 * So: read it, log what it actually was, clear the enable bit while preserving the window
 * size, and READ IT BACK. Four values in this sequence were set and never read back — the
 * memory mode, the chunk sizes, the AGC mode and the mic gain — and every wrong diagnosis
 * traced to exactly that. This one is not joining them. */
/* WRITTEN DOWN WHERE SOMEONE CAN READ IT. Every line below is also an ESP_LOG, and an
   ESP_LOG only exists on a serial console — which this panel no longer has, because the owner
   moved it to a plain charger, which was always the point (§10.4ab). A diagnosis that only
   reaches a cable is not a diagnosis on this product, so the answer rides telemetry too. */
static char s_alc[24] = "unread";

const char *audio_alc_state(void)
{
    return s_alc;
}

/* What the box last asked for, so a re-apply after a settings fetch knows which way to go.
   Off until told otherwise, which is the state this panel has always been in. */
static bool s_alc_want;

/* ENABLE OR DISABLE THE CODEC'S OWN AGC, and read back what actually happened.
 *
 * The owner: *"we need the auto gain control from panel mic too, it was way too quiet."* Both
 * panels run `mic_gain_db = 30` and their last readings were `mic_peak` 32767 — full scale,
 * clipping — and 814. One constant cannot serve both; that is what an ALC is for.
 *
 * ONLY BIT 7 IS TOUCHED, and the restraint is deliberate. REG18's lower bits are the window
 * size and REG19 is the max level, and this firmware has no datasheet behind it — only the
 * register map's one-line names. Writing values nobody here can justify is how the four
 * unverified settings above got their comment ("set and never read back, and every wrong
 * diagnosis traced to exactly that"). So the window and the target keep the chip's own
 * defaults, the enable bit moves, and `mic_peak` in telemetry says whether it helped. If the
 * defaults turn out wrong, that will be visible in the number rather than guessed at twice.
 *
 * Read back either way, because a write this chip refuses must not read as a write that
 * worked — that distinction is the entire value of the telemetry field. */
static void alc_apply(void)
{
    if (s_ctrl == NULL || s_ctrl->read_reg == NULL || s_ctrl->write_reg == NULL) {
        ESP_LOGW(TAG, "alc: no control interface; state unknown");
        snprintf(s_alc, sizeof(s_alc), "no-ctrl");
        return;
    }
    uint8_t v = 0;
    if (s_ctrl->read_reg(s_ctrl, ES8311_REG_ALC, 1, &v, 1) != 0) {
        ESP_LOGW(TAG, "alc: register unreadable; state unknown");
        snprintf(s_alc, sizeof(s_alc), "unreadable");
        return;
    }
    const uint8_t before = v;
    const bool on_now = (before & ES8311_ALC_ENABLE) != 0;
    if (on_now == s_alc_want) {
        ESP_LOGI(TAG, "alc: already %s (reg18 0x%02x)", on_now ? "on" : "off", before);
        snprintf(s_alc, sizeof(s_alc), "%02x already-%s", before, on_now ? "on" : "off");
        return;
    }
    v = s_alc_want ? (uint8_t)(before | ES8311_ALC_ENABLE)
                   : (uint8_t)(before & (uint8_t)~ES8311_ALC_ENABLE);
    if (s_ctrl->write_reg(s_ctrl, ES8311_REG_ALC, 1, &v, 1) != 0) {
        ESP_LOGW(TAG, "alc: write REFUSED (reg18 still 0x%02x)", before);
        snprintf(s_alc, sizeof(s_alc), "%02x REFUSED", before);
        return;
    }
    uint8_t after = 0;
    if (s_ctrl->read_reg(s_ctrl, ES8311_REG_ALC, 1, &after, 1) != 0) {
        ESP_LOGW(TAG, "alc: wrote 0x%02x but cannot read back", v);
        snprintf(s_alc, sizeof(s_alc), "%02x no-readback", before);
        return;
    }
    const bool landed = ((after & ES8311_ALC_ENABLE) != 0) == s_alc_want;
    ESP_LOGI(TAG, "alc: 0x%02x -> 0x%02x (%s%s)", before, after, s_alc_want ? "on" : "off",
             landed ? "" : " REFUSED");
    snprintf(s_alc, sizeof(s_alc), "%02x-%02x %s%s", before, after, s_alc_want ? "on" : "off",
             landed ? "" : "-REFUSED");
}

void audio_set_agc(bool on)
{
    if (on == s_alc_want) return; /* the box says this every fetch; only a CHANGE costs a write */
    s_alc_want = on;
    alc_apply();
}

/* Internal RAM: the I2S transmit source, filled by `stream_tick`. */
static int16_t s_out[AUDIO_CHUNK];

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Appends one chunk to an open recording, as heard or as silence. Stops at the cap rather than
   wrapping: a ring would hand the box the END of a long hold, and what a child said is at the
   start. */
static void capture_keep(const int16_t *chunk, bool silent)
{
    if (!s_cap_on || s_cap == NULL) return;
    const int room = CAPTURE_MAX_SAMPLES - s_cap_used;
    const int take = room < AUDIO_CHUNK ? room : AUDIO_CHUNK;
    if (take <= 0) return;
    if (silent) {
        memset(&s_cap[s_cap_used], 0, (size_t)take * sizeof(int16_t));
    } else {
        memcpy(&s_cap[s_cap_used], chunk, (size_t)take * sizeof(int16_t));
    }
    s_cap_used += take;
}

static void audio_task(void *arg)
{
    (void)arg;
    /* From THIS task, before the loop. esp_codec_dev has no lock of any kind, so a register
       poke from anywhere else is the race that panicked a panel on 2026-09-21. */
    alc_apply();
    while (true) {
        /* ONE CHUNK PER PASS: `esp_codec_dev_write` blocks, and this task's read is the clock
           for the level meter, the recogniser and the capture. */
        const stream_tick_t t = stream_tick(&s_stream, now_ms(), s_out, AUDIO_CHUNK);
        if (t.n > 0) esp_codec_dev_write(s_codec, s_out, (int)(t.n * sizeof(int16_t)));
        apply_levels();

        /* THE READ IS THE CLOCK. It blocks for exactly one chunk and drains the DMA at the
           rate it fills, so this task needs no delay of its own and cannot fall behind the
           room. On failure it would spin, so that path delays instead. */
        if (esp_codec_dev_read(s_codec, s_chunk, sizeof(s_chunk)) != 0) {
            vTaskDelay(pdMS_TO_TICKS(AUDIO_CHUNK_MS));
            continue;
        }
        if (t.capture == STREAM_CAPTURE_SILENCE) {
            /* THE PANEL DOES NOT LISTEN TO ITSELF, BUT A RECORDING KEEPS ITS TIMELINE. The
               codec routes the DAC into the ADC, so this chunk is our own sound: the meter
               and the recogniser skip it, and a recording gets silence of the same length.
               Dropping it spliced "I went to the park today" into "I went to— today", and the
               transcript read as though she had said the shorter thing. */
            s_level = 0;
            capture_keep(s_chunk, true);
            continue;
        }
        s_level = audio_peak(s_chunk, AUDIO_CHUNK);
        capture_keep(s_chunk, false);
        /* THE RECOGNISER IS FED FROM HERE because this task is the microphone's one owner;
           `speech_feed` is a memcpy and a hand-off, and the model runs on its own core. */
        speech_feed(s_chunk, AUDIO_CHUNK);
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
 * `apply_settings()` calls this from the MAIN task, at boot and every three seconds, while
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

/* What the codec last ACCEPTED, "90/36" or "90!/36" when the volume was refused. Read by
   telemetry, because a refused setting the owner cannot see is a setting they will keep
   trying. */
static char s_levels[16];

/* Called only from the audio task, between two captures. */
static void apply_levels(void)
{
    if (!s_levels_pending) return;
    s_levels_pending = false;
    if (s_codec == NULL) return;
    const int vol = s_want_volume;
    const int gain = s_want_gain;
    /* BOTH RETURNS READ, because the line below used to be a CLAIM rather than a reading —
       the exact defect this file's header documents catching at boot, reintroduced on the
       runtime path. And it matters MORE here: this is the path the owner drives from the box.
       They turn the volume down remotely, the part refuses the value, nothing changes, and
       every channel they can see agrees it worked. A `!` marks a refusal. */
    const int vr = (vol >= 0) ? esp_codec_dev_set_out_vol(s_codec, vol) : 0;
    const int gr = (gain >= 0) ? esp_codec_dev_set_in_gain(s_codec, (float)gain) : 0;
    snprintf(s_levels, sizeof(s_levels), "%d%s/%d%s", vol, vr == 0 ? "" : "!", gain,
             gr == 0 ? "" : "!");
    ESP_LOGI(TAG, "levels: out %d/100%s, in %d dB%s", vol, vr == 0 ? "" : " REFUSED", gain,
             gr == 0 ? "" : " REFUSED");
}

const char *audio_levels_state(void)
{
    return s_levels;
}
