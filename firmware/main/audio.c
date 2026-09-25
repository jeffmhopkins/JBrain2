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
 * Scaling the tone in software costs nothing (it is synthesised here) and lets the voice get
 * louder while the beep gets quieter, which is the direction that makes the toy both more
 * useful and less shrill.
 */

#include "audio.h"

#include "cue.h"
#include "ring.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "driver/i2s_std.h"
#include "es8311_codec.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_heap_caps.h"
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
#define VOLUME 90
/* What an ACKNOWLEDGEMENT should sound like on that same scale — the owner asked for 20
   against the voice's 90, back when there was one 880 Hz tone rather than twenty-six cues,
   and the reason holds for all of them: the cue is the part heard closest to a child's face
   and the voice is the part they are listening to. Applied as an amplitude ratio rather than
   a second codec call, because `esp_codec_dev` has no locking and one task owns the codec
   (§10.4al): changing the output level around every cue is exactly the kind of cross-task
   poke that panicked a panel. */
#define CUE_VOLUME 20

/* One handle for both directions. The vendor BSP builds two codec instances, one per
   direction — two objects writing the same chip's registers over the same I2C bus. A single
   IN_OUT device is the same hardware with one owner. */
static esp_codec_dev_handle_t s_codec;
/* Kept so the ALC can be read back from the audio task — see `alc_apply()`. */
static const audio_codec_ctrl_if_t *s_ctrl;

/* THE RECORDING. Ten seconds, and the box enforces the same cap: the panel must not fill
   PSRAM because a screen is face-down in a bag, and the box must not transcribe a minute of a
   room because the panel forgot to stop.

   IT WAS SIX, AND SIX CUT CHILDREN OFF. The owner: *"the babies keep getting cut off because
   they're a little bit slow."* The window has to hold the lead-in, the sentence AND the
   silence the panel waits out to decide the sentence ended (`LISTEN_HUSH_MS`, now 1.8 s) —
   three seconds of waiting plus two of talking plus the hush was already 6.8, so the cap was
   ending turns before the hush could. The extra padding costs the box nothing now that it
   trims the silence off before whisper sees it (`_trim_to_speech`).

   THIRTY NOW, AT THE OWNER'S ASK (0.2.95), up from ten. Ten was chosen as "a long message for
   a four-year-old" and left with a note to revisit it if the twins ever hit the ceiling — the
   `full` branch below logs when they do. Nobody waited for that evidence; the ask came first,
   and the cost is only memory.

   30 s of 16 kHz mono s16 is 960 KB, claimed ONCE at start-up out of the board's 8 MB of
   PSRAM. Never allocated while recording: a heap request in the middle of a four-year-old
   talking is a failure with no good outcome.

   IT IS THE LARGEST BUFFER ON THIS PANEL NOW, and by a wide margin. Streaming took the
   inbound buffer away entirely and cut the play buffer to the reply it actually holds, so the
   three come to about 1.4 MB — 960 KB here, 320 KB of playback, 128 KB of ring — beside a
   322 KB framebuffer. The number to watch is `free_psram` in telemetry rather than this
   comment. */
#define CAPTURE_MAX_MS 30000
#define CAPTURE_MAX_SAMPLES (AUDIO_RATE * CAPTURE_MAX_MS / 1000)
static int16_t *s_cap;          /* PSRAM, claimed at start-up */
static volatile int s_cap_used; /* samples written this recording */
static volatile bool s_cap_on;

/* THE REPLY, WITH ITS OWN CEILING RATHER THAN THE RECORDING'S.
 *
 * These were one constant, and that is how the reply got cut off: a reply is not a recording
 * and there is no reason the two should be the same length. The owner: *"sometimes when the
 * robot is talking back on a longer reply I get cut off."* The box's log the same afternoon
 * had replies of 221,012 and 261,290 bytes against the 192,000 this held, so the long one
 * stopped mid-word — silently, because `audio_play` truncates without a word to anyone.
 *
 * `PANEL_REPLY_MAX` in `backend/src/jbrain/api/endpoint.py` is the same ten seconds, and
 * `REPLY_MAX_BYTES` in `talk.c` is how much of a reply this panel will read. That cap belongs
 * to the REPLY and stays with it.
 *
 * IT HOLDS A REPLY, AND SINCE 0.2.96 ONLY A REPLY. Voice post used to be copied through here
 * too, which is why this was sized to the longest message the box would serve — and why it
 * grew every time that cap did. Messages stream through the ring now (`audio_stream_*`), so
 * this is back to the one caller it was ever really for: `talk.c`, whose own `REPLY_MAX_BYTES`
 * is the same ten seconds and whose `PANEL_REPLY_MAX` on the box matches both.
 *
 * The cut this warns about therefore belongs to a reply that overran, not to a child's
 * message — a message is no longer capped at all. */
#define PLAY_BUF_MS 10000
#define PLAY_BUF_SAMPLES (AUDIO_RATE * PLAY_BUF_MS / 1000)
static int16_t *s_play;
static volatile int s_play_len;  /* samples still to write */
static volatile int s_play_pos;

/* ── THE STREAM RING ────────────────────────────────────────────────────────────────────
 *
 * Four seconds, which is not a guess about messages — it is a guess about the WORST GAP this
 * panel's Wi-Fi will have in a bedroom, and the ring only has to outlast that. A message of
 * any length passes through it; what the depth buys is tolerance of a network that stalls.
 *
 * `s_ring_w` and `s_ring_r` are MONOTONIC sample counts, not indices, and the modulo happens
 * where they are used. Two cursors chasing each other around a buffer cannot tell full from
 * empty when they meet; counting forever can, and the subtraction is what every test below
 * asks. Single producer (the jpanel task), single consumer (this task), so neither needs a
 * lock — but they need that discipline, which is the same one `esp_codec_dev` demands. */
#define STREAM_RING_MS 4000
#define STREAM_RING_SAMPLES (AUDIO_RATE * STREAM_RING_MS / 1000)
/* HOW MUCH ARRIVES BEFORE THE FIRST SOUND. Long enough that an ordinary hiccup is invisible,
   short enough that the tap still feels answered: 1.5 s is ~48 KB, against the ~960 KB a
   whole message used to need before anything happened. */
#define STREAM_PREROLL_SAMPLES (AUDIO_RATE * 1500 / 1000)
static int16_t *s_ring_buf;
static ring_t s_ring;
static volatile bool s_stream_open;  /* the producer has not said it is finished */
static volatile bool s_stream_primed; /* the preroll has landed; sound has started */

static int stream_filled(void)
{
    return s_ring_buf != NULL ? ring_filled(&s_ring) : 0;
}

bool audio_stream_begin(void)
{
    if (s_ring_buf == NULL) return false;
    if (s_play_pos < s_play_len || s_stream_open || stream_filled() > 0) return false;
    ring_reset(&s_ring);
    s_stream_primed = false;
    s_stream_open = true;
    return true;
}

size_t audio_stream_write(const void *pcm, size_t bytes)
{
    if (s_ring_buf == NULL || !s_stream_open) return 0;
    return (size_t)ring_write(&s_ring, pcm, (int)bytes);
}

void audio_stream_end(void)
{
    s_stream_open = false;
}

bool audio_stream_live(void)
{
    return s_stream_open;
}

void audio_stream_abort(void)
{
    s_stream_open = false;
    ring_reset(&s_ring);
    s_stream_primed = false;
}

bool audio_play(const int16_t *pcm, size_t bytes)
{
    if (s_play == NULL || pcm == NULL || bytes < 2) return false;
    if (s_play_pos < s_play_len) return false; /* still speaking */
    int n = (int)(bytes / sizeof(int16_t));
    if (n > PLAY_BUF_SAMPLES) {
        /* SAID OUT LOUD NOW. Truncating silently is how a 261 KB reply stopped mid-word with
           nothing in any log to say it had, and a cut message would be the same bug wearing a
           different hat — a child told their father's message ended where it did not. */
        ESP_LOGW(TAG, "cutting %d ms of audio: %d ms is all this buffer holds",
                 (n - PLAY_BUF_SAMPLES) * 1000 / AUDIO_RATE, PLAY_BUF_MS);
        n = PLAY_BUF_SAMPLES;
    }
    memcpy(s_play, pcm, (size_t)n * sizeof(int16_t));
    s_play_pos = 0;
    s_play_len = n;
    return true;
}

bool audio_playing(void)
{
    /* A STREAM COUNTS, INCLUDING WHILE ITS RING IS MOMENTARILY DRY. Everything that asks this
       question — the render loop's `speaking`, the pop-up, the queue, `POST /played` — is
       really asking "is this message finished", and a Wi-Fi stall is not an answer to that.
       See `audio.h`: a panel that said no here mid-sentence would mark the message played and
       drop the rest of it. */
    if (s_play_pos < s_play_len) return true;
    return s_stream_open || stream_filled() > 0;
}

/* CUT IT SHORT. The one thing this panel could not do to its own speaker until voice post
   started playing a QUEUE of messages: a run a child wants out of has to end on the finger,
   not on the last message.
 *
   Done by moving the read cursor to the end rather than by zeroing the length, so the audio
   task's `s_play_pos < s_play_len` test sees a finished buffer on its next chunk and stops
   where it is. Nothing is freed and nothing is racing: the writer only ever moves `pos`
   forward and a reader that is mid-chunk finishes that chunk, which is ~20 ms. */
void audio_stop(void)
{
    s_play_pos = s_play_len;
    audio_stream_abort();
}

/* THE RUDE NOISE, AND WHY IT IS AN OSCILLATOR RATHER THAN A FILE.
 *
 * `burp` and `fart` have been in the vocabulary since bring-up and have only ever moved the
 * face. A four-year-old saying "burp" to a robot is not asking for an expression.
 *
 * What makes a noise read as a BODY rather than a horn is three things, and none of them is
 * the waveform: the pitch falls while it sounds, the amplitude flutters fast enough to be
 * heard as texture rather than as tremolo, and it ends by running out rather than stopping.
 * A sawtooth supplies the harmonics a sine has not got — a pure tone at 120 Hz is a foghorn —
 * and the wet variant adds noise on top, which is the whole difference between the two words.
 *
 * Written into `s_play` and left for the audio task, so this shares the reply's chunked,
 * interruptible playback and its deafening rather than introducing a third way to make a
 * sound. That also means it cannot interrupt a reply, which is right: the pet finishing its
 * sentence beats a burp, and the child can ask again. */
/* A CUE, RENDERED AND HANDED TO THE SPEAKER — the replacement for `audio_beep`.
 *
 * Same route as a reply: written into `s_play` and left for the audio task, so it is chunked,
 * interruptible and deafens the microphone while it sounds. It therefore cannot interrupt the
 * pet mid-sentence, which is right — an acknowledgement is worth less than the sentence it
 * would talk over, and the beep it replaces had exactly the same rule.
 *
 * Rendered at CUE_VOLUME against the speaking voice, because the original request that split
 * the two levels apart is still the right one: the tone is the part heard closest to a child's
 * face and the voice is the part they are listening to.
 *
 * THE VARIANT IS COUNTED PER CUE, not drawn at random. The complaint was repetition, and what
 * a child actually does is poke the same spot four times — so what has to differ is two
 * consecutive plays of the SAME cue, which a counter guarantees and a random draw only makes
 * likely. Kept as a byte per cue and allowed to wrap; `cue_render` accepts any value. */
void audio_cue(cue_t c)
{
    if (s_play == NULL) return;
    if (s_play_pos < s_play_len) return; /* a reply outranks an acknowledgement */
    if (c < 0 || c >= CUE_COUNT) return;

    static uint8_t s_variant[CUE_COUNT];
    const int n = cue_render(c, s_play, AUDIO_RATE, CUE_VOLUME, s_variant[c]++);
    if (n <= 0) return;
    s_play_pos = 0;
    s_play_len = n;
}

void audio_capture_open(void)
{
    if (s_cap == NULL) return;
    s_cap_used = 0;
    s_cap_on = true;
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

    /* Priority 5, one above the render task: a late frame is a slightly janky robot, a late
       capture is a dropped chunk and a meter that lags the room. The stack is small because
       this task calls into the codec and computes a peak, and nothing else. */
    /* PSRAM, claimed once, before anything else wants it. Failure is not fatal: the panel
       keeps its voice commands and its meter and simply cannot record a message, which is a
       smaller loss than refusing to start. */
    s_cap = heap_caps_malloc((size_t)CAPTURE_MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    s_play = heap_caps_malloc((size_t)PLAY_BUF_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    s_ring_buf =
        heap_caps_malloc((size_t)STREAM_RING_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    ring_init(&s_ring, s_ring_buf, STREAM_RING_SAMPLES);
    /* ALL THREE AT START-UP AND NEVER AGAIN. A heap request in the middle of a four-year-old
       talking — or of her father's message playing — is a failure with no good outcome. */
    ESP_LOGI(TAG, "capture %s (%d ms), playback %s (%d ms), stream %s (%d ms)",
             s_cap != NULL ? "ready" : "UNAVAILABLE", CAPTURE_MAX_MS,
             s_play != NULL ? "ready" : "UNAVAILABLE", PLAY_BUF_MS,
             s_ring_buf != NULL ? "ready" : "UNAVAILABLE", STREAM_RING_MS);

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

/* Chunks to ignore after the speaker runs. The codec routes the DAC into the ADC by design
   (REG44), so the microphone hears every beep — and feeding our own tone to the recogniser is
   worse than useless, because it is a false trigger with a loudspeaker behind it. The beep is
   90 ms and a chunk is 40; this covers it and a tail. */
#define DEAF_CHUNKS 6
static int s_deaf;


static void audio_task(void *arg)
{
    (void)arg;
    /* From THIS task, before the loop. esp_codec_dev has no lock of any kind, so a register
       poke from anywhere else is the race that panicked a panel on 2026-09-21. */
    alc_apply();
    while (true) {
        if (s_play_pos < s_play_len) {
            /* ONE CHUNK PER PASS, NOT THE WHOLE REPLY. `esp_codec_dev_write` blocks, so
               handing it two seconds of audio would stop this task — and this task's read is
               the clock for the level meter, the recogniser and the capture. A chunk at a
               time keeps the loop turning and lets a reply be interrupted by a reboot or an
               OTA rather than wedging the panel until it finishes talking. */
            const int left = s_play_len - s_play_pos;
            const int take = left < AUDIO_CHUNK ? left : AUDIO_CHUNK;
            esp_codec_dev_write(s_codec, &s_play[s_play_pos], (int)(take * sizeof(int16_t)));
            s_play_pos += take;
            /* The codec routes the DAC into the ADC by design, so everything we say is also
               heard. Feeding our own reply to the recogniser would have the pet answering
               itself. */
            s_deaf = DEAF_CHUNKS;
        } else if (s_ring_buf != NULL && stream_filled() > 0) {
            /* THE SAME ONE-CHUNK DISCIPLINE, for the same reason — this task is the clock. */
            if (!s_stream_primed) {
                /* Wait for the preroll, unless the producer has already finished: a message
                   shorter than the preroll would otherwise sit in the ring forever. */
                if (stream_filled() >= STREAM_PREROLL_SAMPLES || !s_stream_open) {
                    s_stream_primed = true;
                }
            }
            if (s_stream_primed) {
                const int have = stream_filled();
                /* AN UNDERRUN WAITS RATHER THAN PLAYING SHORT. A partial chunk into a blocking
                   codec write is a click; doing nothing for one pass is 40 ms of silence the
                   ear does not catch. Only drain below a chunk once nothing more is coming. */
                if (have >= AUDIO_CHUNK || !s_stream_open) {
                    int at = 0;
                    const int run = ring_read_run(&s_ring, AUDIO_CHUNK, &at);
                    if (run > 0) {
                        esp_codec_dev_write(s_codec, &s_ring_buf[at],
                                            (int)(run * sizeof(int16_t)));
                        ring_advance(&s_ring, run);
                        s_deaf = DEAF_CHUNKS;
                    }
                }
            }
        }
        apply_levels();

        /* THE READ IS THE CLOCK. It blocks for exactly one chunk and drains the DMA at the
           rate it fills, so this task needs no delay of its own and cannot fall behind the
           room. On failure it would spin, so that path delays instead. */
        if (esp_codec_dev_read(s_codec, s_chunk, sizeof(s_chunk)) == 0) {
            if (s_deaf > 0) {
                /* THE PANEL DOES NOT LISTEN TO ITSELF. Reported as the meter railing for
                   seconds after a beep; the chunk is still read, because this read is the
                   task's clock and skipping it would stall everything. */
                s_deaf--;
                s_level = 0;
                continue;
            }
            s_level = audio_peak(s_chunk, AUDIO_CHUNK);
            if (s_cap_on && s_cap != NULL) {
                /* Straight off the same chunk the recogniser gets. One microphone, one
                   owner, one read — a second reader would be the two-owners fault this
                   file's header is about. Stops at the cap rather than wrapping: a ring
                   buffer would hand the box the END of a long hold, and what a child said
                   is at the start. */
                const int room = CAPTURE_MAX_SAMPLES - s_cap_used;
                const int take = room < AUDIO_CHUNK ? room : AUDIO_CHUNK;
                if (take > 0) {
                    memcpy(&s_cap[s_cap_used], s_chunk, (size_t)take * sizeof(int16_t));
                    s_cap_used += take;
                }
            }
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
