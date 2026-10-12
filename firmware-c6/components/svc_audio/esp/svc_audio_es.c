/* The speaker (ES8311 + NS4150B) and the microphones (ES7210), and the one task that drives
 * them.
 *
 * ONE OWNER PER CHIP. esp_codec_dev has no lock on its device calls (firmware/README.md: two
 * tasks on one handle cost the twins' panels a panic), so the audio task alone opens, reads,
 * writes and closes the codecs; everyone else asks svc_audio_talk.c's state, which this task
 * reads every 20 ms. Their registers are reached through svc_power's bus lock — a control
 * interface of our own below, never a second I2C driver.
 *
 * ONE I2S, BOTH DIRECTIONS. The C6 has one I2S; every vendor source opens TX and RX on I2S0 in
 * one call, sharing MCLK (GPIO19), BCLK (20) and WS (22); DOUT (23) feeds the ES8311 and DIN
 * (21) is the ES7210's SDOUT1 (HARDWARE.md §5). The ES8311 and ES7210 are I2S slaves on MCLK =
 * 256 x 16 kHz. A talk is half duplex — record, then play — so at most one codec is open.
 *
 * THE AMPLIFIER is the AXP2101's ALDO2 (NS4150B CTRL, HARDWARE.md §1). It is on from power-up,
 * so this service turns it off once the codecs are up and on only while a reply plays: an idle
 * class-D amp is hiss and current for nothing.
 *
 * Register values are esp_codec_dev 1.6.2's own ES8311/ES7210 drivers (Apache-2.0, Espressif),
 * the drivers Waveshare's BSP for this board uses; the pins and the amp rail are the BSP's
 * (esp32_c6_touch_amoled_2_16.c, Apache-2.0).
 */

#include <stdio.h>
#include <string.h>

#include "alarm_sound.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "svc_audio_drv.h"
#include "svc_power_drv.h"
#include "svc_settings.h"

static const char *TAG = "audio";

#define PIN_MCLK 19
#define PIN_BCLK 20
#define PIN_WS 22
#define PIN_DOUT 23
#define PIN_DIN 21
#define ES8311_ADDR 0x18
#define ES7210_ADDR 0x40
#define CODEC_I2C_HZ 100000
#define XFER_MS 20

/* Four DMA buffers of one 20 ms chunk each: 80 ms of slack, against a task that shares the CPU
 * round-robin with the UI, the main task's TLS and touch at the same priority. RX carries both
 * microphones (4 B a frame), TX one channel (2 B). */
#define DMA_DESC 4
#define DMA_FRAMES TALK_CHUNK_FRAMES
/* 4.25 KB. 0.0.10's 3.5 KB measured `stack_free` 1,524 B after the first two talks on the glass
 * (2,060 B used: esp_codec_dev_open through the I2S reconfig and its log lines), under the 2 KB
 * of headroom a task that logs from a driver call wants. +768 B of internal RAM buys ~2.3 KB
 * free at that depth; `stack_free` in the snapshot stays the measure. */
#define TASK_STACK 4352

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t s_task;
static void (*s_wake_net)(void);
static esp_codec_dev_handle_t s_mic, s_spk;
static bool s_started;
static char s_state[28] = "off";
static volatile uint8_t s_level;
static uint32_t s_i2c_err, s_codec_err;
static uint8_t s_id8311[2], s_id7210[2];
static volatile uint16_t s_tone_hz, s_tone_ms;
/* The tone is a new message's chime (0.0.16): a second, lower note follows the first. */
static volatile bool s_chime;
static uint32_t s_chimes;
static uint32_t s_tones;
/* An alarm's sound: 0 silent, otherwise the sound + 1. Written by the UI task, read here. */
static volatile uint8_t s_ring;
static uint32_t s_rings, s_ring_fail;
static volatile bool s_ring_playing;
static int s_vol_set = -1;
static float s_gain_set = -1;
static bool s_amp;
/* The last reply's start, for the snapshot: how long the amp had been on when the first speech
 * sample was written (want ≥ TK_SPK_SETTLE_MS), and how much was queued then. */
static uint32_t s_settle_ms, s_start_fill;

/* Both chunks static: a DMA destination and its mono copy, never on a task's stack. */
static int16_t s_lr[TALK_CHUNK_FRAMES * 2];
static int16_t s_mono[TALK_CHUNK_FRAMES];

void talk_port_lock(void)
{
    portENTER_CRITICAL(&s_mux);
}

void talk_port_unlock(void)
{
    portEXIT_CRITICAL(&s_mux);
}

void talk_port_kick(void)
{
    if (s_task) xTaskNotifyGive(s_task);
}

void talk_port_net(void)
{
    if (s_wake_net) s_wake_net();
}

const char *talk_port_refusal(void)
{
    if (!s_started) return "Sound is not running (Settings → Jerv)";
    if (!s_mic) return "The microphone did not start (Settings → Jerv)";
    if (s_ring) return "An alarm is ringing";
    if (s_tone_ms) return "One moment: a test tone is playing";
    return NULL;
}

bool talk_port_speaker(void)
{
    return s_spk != NULL;
}

uint8_t talk_port_level(void)
{
    return s_level;
}

const char *talk_port_state(void)
{
    return s_state;
}

void talk_port_describe(dbg_json_t *w)
{
    char id[8];
    snprintf(id, sizeof id, "%02x%02x", s_id8311[0], s_id8311[1]);
    dbg_json_str(w, "es8311_id", id);
    snprintf(id, sizeof id, "%02x%02x", s_id7210[0], s_id7210[1]);
    dbg_json_str(w, "es7210_id", id);
    dbg_json_bool(w, "amp", s_amp);
    dbg_json_int(w, "volume", s_vol_set);
    dbg_json_int(w, "mic_gain_db", (int32_t)s_gain_set);
    dbg_json_int(w, "i2c_errors", s_i2c_err);
    dbg_json_int(w, "codec_errors", s_codec_err);
    dbg_json_int(w, "tones", s_tones);
    dbg_json_int(w, "chimes", s_chimes);
    dbg_json_int(w, "rings", s_rings);
    dbg_json_int(w, "ring_fail", s_ring_fail);
    dbg_json_bool(w, "ring_playing", s_ring_playing);
    dbg_json_int(w, "spk_settle_ms", s_settle_ms);
    dbg_json_int(w, "spk_start_fill", s_start_fill);
    dbg_json_int(w, "stack_free", s_task ? uxTaskGetStackHighWaterMark(s_task) : 0);
}

/* ---- the codecs' registers, over svc_power's bus ---- */

typedef struct {
    audio_codec_ctrl_if_t base;
    svc_power_i2c_t dev;
    uint8_t addr8; /* esp_codec_dev keys a codec by its 8-bit address */
} bus_ctrl_t;

static bus_ctrl_t s_ctrl8311, s_ctrl7210;

static int bc_open(const audio_codec_ctrl_if_t *c, void *cfg, int n)
{
    (void)c, (void)cfg, (void)n;
    return ESP_CODEC_DEV_OK;
}

static bool bc_is_open(const audio_codec_ctrl_if_t *c)
{
    return ((const bus_ctrl_t *)c)->dev != NULL;
}

static int bc_read(const audio_codec_ctrl_if_t *c, int reg, int reg_len, void *data, int len)
{
    const uint8_t r = (uint8_t)reg;
    if (reg_len != 1 || svc_power_i2c_xfer(((const bus_ctrl_t *)c)->dev, &r, 1, data, (size_t)len,
                                           XFER_MS) != ESP_OK) {
        s_i2c_err++;
        return ESP_CODEC_DEV_READ_FAIL;
    }
    return ESP_CODEC_DEV_OK;
}

static int bc_write(const audio_codec_ctrl_if_t *c, int reg, int reg_len, void *data, int len)
{
    uint8_t b[4] = {(uint8_t)reg};
    if (reg_len != 1 || len < 0 || len > 3) return ESP_CODEC_DEV_NOT_SUPPORT;
    memcpy(b + 1, data, (size_t)len);
    if (svc_power_i2c_xfer(((const bus_ctrl_t *)c)->dev, b, (size_t)len + 1, NULL, 0, XFER_MS) !=
        ESP_OK) {
        s_i2c_err++;
        return ESP_CODEC_DEV_WRITE_FAIL;
    }
    return ESP_CODEC_DEV_OK;
}

static int bc_info(const audio_codec_ctrl_if_t *c, audio_codec_ctrl_info_t *info)
{
    info->type = AUDIO_CODEC_CTRL_I2C;
    info->i2c.addr = ((const bus_ctrl_t *)c)->addr8;
    info->i2c.port = 0;
    return ESP_CODEC_DEV_OK;
}

static int bc_close(const audio_codec_ctrl_if_t *c)
{
    (void)c;
    return ESP_CODEC_DEV_OK;
}

static bool bus_ctrl(bus_ctrl_t *c, uint8_t addr)
{
    c->base = (audio_codec_ctrl_if_t){.open = bc_open, .is_open = bc_is_open, .read_reg = bc_read,
                                      .write_reg = bc_write, .get_info = bc_info, .close = bc_close};
    c->addr8 = (uint8_t)(addr << 1);
    return svc_power_i2c_attach(addr, CODEC_I2C_HZ, &c->dev) == ESP_OK;
}

/* Chip-id bytes, read for the snapshot and never trusted to gate anything: no datasheet for
 * the ES7210 was found (HARDWARE.md §5), so a value here is a measurement, not a check. */
static bool probe(bus_ctrl_t *c, uint8_t r0, uint8_t out[2])
{
    return bc_read(&c->base, r0, 1, &out[0], 1) == ESP_CODEC_DEV_OK &&
           bc_read(&c->base, r0 + 1, 1, &out[1], 1) == ESP_CODEC_DEV_OK;
}

/* ---- bring-up ---- */

static void amp(bool on)
{
    if (on == s_amp) return;
    if (svc_power_amp(on) == ESP_OK)
        s_amp = on;
    else
        s_codec_err++;
}

static const char *bring_up(void)
{
    if (!bus_ctrl(&s_ctrl8311, ES8311_ADDR) || !bus_ctrl(&s_ctrl7210, ES7210_ADDR)) return "bus";
    const bool has8311 = probe(&s_ctrl8311, 0xFD, s_id8311);
    const bool has7210 = probe(&s_ctrl7210, 0x3D, s_id7210);
    if (!has8311 && !has7210) return "i2c";

    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.dma_desc_num = DMA_DESC;
    cc.dma_frame_num = DMA_FRAMES;
    /* TX sends zeros when it has nothing: RX needs TX's clocks running, and without this the
       DMA would replay the last reply's buffers into the speaker. */
    cc.auto_clear = true;
    i2s_chan_handle_t tx, rx;
    if (i2s_new_channel(&cc, &tx, &rx) != ESP_OK) return "i2s";
    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(TALK_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.mclk = PIN_MCLK, .bclk = PIN_BCLK, .ws = PIN_WS, .dout = PIN_DOUT,
                     .din = PIN_DIN},
    };
    if (i2s_channel_init_std_mode(tx, &sc) != ESP_OK) return "i2s-tx";
    sc.slot_cfg = (i2s_std_slot_config_t)I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
        I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
    if (i2s_channel_init_std_mode(rx, &sc) != ESP_OK) return "i2s-rx";
    audio_codec_i2s_cfg_t ic = {.port = I2S_NUM_0, .rx_handle = rx, .tx_handle = tx};
    const audio_codec_data_if_t *data = audio_codec_new_i2s_data(&ic);
    if (!data) return "data";

    if (has8311) {
        es8311_codec_cfg_t c = {
            .ctrl_if = &s_ctrl8311.base,
            .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
            .pa_pin = -1,
            .use_mclk = true,
            /* The NS4150B runs from the same 3.3 V as the DAC (HARDWARE.md §5). */
            .hw_gain = {.pa_voltage = 3.3f, .codec_dac_voltage = 3.3f},
            .no_dac_ref = true,
        };
        const audio_codec_if_t *ci = es8311_codec_new(&c);
        esp_codec_dev_cfg_t dc = {.dev_type = ESP_CODEC_DEV_TYPE_OUT, .codec_if = ci, .data_if = data};
        if (ci) s_spk = esp_codec_dev_new(&dc);
    }
    if (has7210) {
        es7210_codec_cfg_t c = {
            .ctrl_if = &s_ctrl7210.base,
            .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2,
        };
        const audio_codec_if_t *ci = es7210_codec_new(&c);
        esp_codec_dev_cfg_t dc = {.dev_type = ESP_CODEC_DEV_TYPE_IN, .codec_if = ci, .data_if = data};
        if (ci) s_mic = esp_codec_dev_new(&dc);
    }
    if (!s_mic && !s_spk) return "codecs";
    return NULL;
}

/* ---- the task ---- */

static esp_codec_dev_sample_info_t fs(uint8_t channels)
{
    return (esp_codec_dev_sample_info_t){.bits_per_sample = 16, .channel = channels,
                                         .sample_rate = TALK_RATE};
}

static bool open_mic(void)
{
    esp_codec_dev_sample_info_t f = fs(2);
    if (esp_codec_dev_open(s_mic, &f) != ESP_CODEC_DEV_OK) {
        s_codec_err++;
        return false;
    }
    const float gain = (float)svc_settings_get(SET_MIC_GAIN);
    if (esp_codec_dev_set_in_gain(s_mic, gain) == ESP_CODEC_DEV_OK) s_gain_set = gain;
    else s_codec_err++;
    return true;
}

static bool open_spk(int vol)
{
    esp_codec_dev_sample_info_t f = fs(1);
    if (esp_codec_dev_open(s_spk, &f) != ESP_CODEC_DEV_OK) {
        s_codec_err++;
        return false;
    }
    if (esp_codec_dev_set_out_vol(s_spk, vol) == ESP_CODEC_DEV_OK) s_vol_set = vol;
    else s_codec_err++;
    /* After the DAC is up and unmuted, so the amp never amplifies its start-up. */
    amp(true);
    return true;
}

static void close_spk(void)
{
    amp(false);
    esp_codec_dev_close(s_spk);
}

static uint32_t ms_now(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* 20 ms of zeros: the pre-roll while the amp settles, a pause while a reply's next sentence is
   still on its way, and the tail that lets the DMA's last real buffers out before the amp goes
   off. Each write blocks on the DMA, so a loop of these is paced, never busy. */
static void silence(int chunks)
{
    memset(s_mono, 0, TALK_CHUNK_BYTES);
    for (int i = 0; i < chunks; i++) esp_codec_dev_write(s_spk, s_mono, TALK_CHUNK_BYTES);
}

/* After open_spk: zeros until the amp has had TK_SPK_SETTLE_MS, whatever came before — the same
   start every time, so the first word is never the amp's start-up. Returns when the amp came on. */
static uint32_t settle(void)
{
    const uint32_t amp_at = ms_now();
    while (ms_now() - amp_at < TK_SPK_SETTLE_MS) silence(1);
    return amp_at;
}

/* 32 steps of a sine, Q15: a test tone needs no libm on a core with no FPU. */
static const int16_t SINE[32] = {0,      6393,   12540,  18205,  23170,  27246,  30274,  32138,
                                 32767,  32138,  30274,  27246,  23170,  18205,  12540,  6393,
                                 0,      -6393,  -12540, -18205, -23170, -27246, -30274, -32138,
                                 -32767, -32138, -30274, -27246, -23170, -18205, -12540, -6393};

/* `chunks` of a sine at `hz`, `shift` quieter than full scale, unless something takes the speaker. */
static void sine(uint32_t hz, uint32_t chunks, int shift)
{
    uint32_t phase = 0;
    const uint32_t step = (uint32_t)(((uint64_t)hz << 21) / TALK_RATE); /* 32 steps << 16 */
    for (uint32_t c = 0; c < chunks && talk_audio_want() == TK_AUDIO_OFF && !s_ring; c++) {
        for (int i = 0; i < TALK_CHUNK_FRAMES; i++, phase += step)
            s_mono[i] = (int16_t)(SINE[(phase >> 16) & 31] >> shift);
        esp_codec_dev_write(s_spk, s_mono, TALK_CHUNK_BYTES);
    }
}

static void play_tone(void)
{
    const uint32_t hz = s_tone_hz, chunks = s_tone_ms / 20;
    const bool chime = s_chime;
    if (!s_spk || !open_spk((int)svc_settings_get(SET_VOLUME))) {
        s_tone_ms = 0;
        s_chime = false;
        return;
    }
    talk_audio_on(false, true);
    settle();
    /* -12 dBFS for the test tone; the chime is a soft E6 then B5 at -18 dBFS, under the voice. */
    sine(hz, chunks, chime ? 3 : 2);
    if (chime) sine(988, 220 / 20, 3);
    silence(TK_SPK_TAIL_MS / 20);
    close_spk();
    talk_audio_on(false, false);
    if (chime) s_chimes++;
    else s_tones++;
    s_chime = false;
    s_tone_ms = 0;
}

static void speak(void)
{
    if (!open_spk((int)svc_settings_get(SET_VOLUME))) {
        talk_audio_drop();
        return;
    }
    const uint32_t amp_at = ms_now();
    talk_audio_on(false, true);
    /* Gated (tk_spk_may_start): silence until the amp has settled AND the ring holds 150 ms or the
       box says no more is coming; again after an underrun, so a sentence still being synthesised
       on the box is a pause, not a stutter. */
    bool started = false, waiting = true;
    while (talk_audio_want() == TK_AUDIO_SPK && !s_ring) {
        const bool more = talk_audio_more();
        if (waiting) {
            const uint32_t since = started ? TK_SPK_SETTLE_MS : ms_now() - amp_at;
            const uint32_t queued = talk_audio_queued();
            if (!tk_spk_may_start(since, queued, more)) {
                silence(1);
                continue;
            }
            if (!started) {
                started = true;
                s_settle_ms = ms_now() - amp_at;
                s_start_fill = queued;
                ESP_LOGI(TAG, "reply: amp on %lu ms before the first sample, %lu B queued",
                         (unsigned long)s_settle_ms, (unsigned long)s_start_fill);
            }
            waiting = false;
        }
        bool more_after;
        const uint32_t got = talk_audio_get(s_mono, TALK_CHUNK_BYTES, &more_after);
        if (!got && !more_after) {
            silence(TK_SPK_TAIL_MS / 20);
            talk_audio_drained();
            break;
        }
        if (got < TALK_CHUNK_BYTES) {
            memset((uint8_t *)s_mono + got, 0, TALK_CHUNK_BYTES - got);
            if (more_after) {
                talk_audio_underrun();
                waiting = true;
            }
        }
        esp_codec_dev_write(s_spk, s_mono, TALK_CHUNK_BYTES);
    }
    close_spk();
    talk_audio_on(false, false);
}

static void record(void)
{
    if (!open_mic()) {
        talk_net_fail("microphone did not open");
        return;
    }
    talk_audio_on(true, false);
    while (talk_audio_want() == TK_AUDIO_MIC && !s_ring) {
        if (esp_codec_dev_read(s_mic, s_lr, sizeof s_lr) != ESP_CODEC_DEV_OK) {
            s_codec_err++;
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        uint16_t pl, pr;
        talk_mono(s_lr, TALK_CHUNK_FRAMES, s_mono, &pl, &pr);
        s_level = talk_level(pl > pr ? pl : pr);
        talk_audio_put(s_mono, TALK_CHUNK_BYTES, pl, pr);
    }
    s_level = 0;
    esp_codec_dev_close(s_mic);
    /* After the close: the last chunk is in the ring before the upload is told it can finish. */
    talk_audio_on(false, false);
}

/* The alarm volume is never under this, whatever the speaker is set to for Jerv: a bedside panel
   turned down for replies must still wake its owner. The ramp (alarm_sound.c) is on top. */
#define ALARM_MIN_VOLUME 70

/* The alarm, looped until it is stopped: the one sound that takes the speaker from anything else
   (svc_audio.h). The volume climbs over ALARM_RAMP_MS from the ring's start; a different sound
   asked for mid-ring (another alarm took over) starts its own pattern at once. */
static void play_ring(void)
{
    const int set = (int)svc_settings_get(SET_VOLUME);
    if (!s_spk || !open_spk(set > ALARM_MIN_VOLUME ? set : ALARM_MIN_VOLUME)) {
        s_ring_fail++;
        while (s_ring) vTaskDelay(pdMS_TO_TICKS(100));
        return;
    }
    s_rings++;
    s_ring_playing = true;
    settle();
    alarm_synth_t syn;
    uint8_t playing = s_ring;
    alarm_synth_init(&syn, (uint8_t)(playing - 1));
    const uint32_t t0 = ms_now();
    while (s_ring) {
        if (s_ring != playing) {
            playing = s_ring;
            alarm_synth_init(&syn, (uint8_t)(playing - 1));
        }
        alarm_synth_fill(&syn, s_mono, TALK_CHUNK_FRAMES, alarm_ramp_q15(ms_now() - t0));
        esp_codec_dev_write(s_spk, s_mono, TALK_CHUNK_BYTES);
    }
    silence(TK_SPK_TAIL_MS / 20);
    close_spk();
    s_ring_playing = false;
}

void svc_audio_ring(uint8_t sound)
{
    s_ring = (uint8_t)(sound + 1);
    if (s_task) xTaskNotifyGive(s_task);
}

void svc_audio_ring_stop(void)
{
    s_ring = 0;
}

bool svc_audio_ringing(void)
{
    return s_ring != 0;
}

static void audio_task(void *arg)
{
    (void)arg;
    for (;;) {
        if (s_ring) {
            play_ring();
            continue;
        }
        switch (talk_audio_want()) {
        case TK_AUDIO_MIC:
            if (s_mic) record();
            break;
        case TK_AUDIO_SPK:
            if (s_spk) speak();
            break;
        default:
            if (s_tone_ms) {
                play_tone();
                break;
            }
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(500));
            break;
        }
    }
}

/* ---- the debug command ---- */

bool svc_audio_tone(uint16_t hz, uint16_t ms, const char **why)
{
    *why = NULL;
    if (!s_spk) *why = "no speaker";
    else if (talk_audio_want() != TK_AUDIO_OFF || svc_audio_talking() || svc_audio_replying())
        *why = "busy talking";
    else if (s_tone_ms) *why = "a tone is playing";
    else if (s_ring) *why = "an alarm is ringing";
    if (*why) return false;
    s_tone_hz = hz;
    s_tone_ms = ms;
    if (s_task) xTaskNotifyGive(s_task);
    return true;
}

bool svc_audio_chime(void)
{
    const char *why;
    if (s_tone_ms) return false;
    /* Before the tone is asked for: the audio task reads it the moment it is woken. */
    s_chime = true;
    if (svc_audio_tone(1319, 140, &why)) return true;
    s_chime = false;
    return false;
}

void svc_audio_start(void (*wake_net)(void))
{
    svc_audio_init();
    s_wake_net = wake_net;
    const char *fail = bring_up();
    if (fail) {
        snprintf(s_state, sizeof s_state, "failed:%s", fail);
        ESP_LOGE(TAG, "no sound: %s (ES8311 id %02x%02x, ES7210 id %02x%02x)", fail, s_id8311[0],
                 s_id8311[1], s_id7210[0], s_id7210[1]);
        return;
    }
    if (!s_mic) snprintf(s_state, sizeof s_state, "speaker-only:es7210");
    else if (!s_spk) snprintf(s_state, sizeof s_state, "mic-only:es8311");
    else snprintf(s_state, sizeof s_state, "ok");
    amp(false);
    /* Priority 1, the update loop's own (README "The safety rules"): it blocks on the I2S DMA
       for each 20 ms chunk, so it takes its share and never more. */
    if (xTaskCreate(audio_task, "audio", TASK_STACK, NULL, 1, &s_task) != pdPASS) {
        snprintf(s_state, sizeof s_state, "failed:task");
        return;
    }
    s_started = true;
    ESP_LOGI(TAG, "sound %s (ES8311 id %02x%02x, ES7210 id %02x%02x)", s_state, s_id8311[0],
             s_id8311[1], s_id7210[0], s_id7210[1]);
}
