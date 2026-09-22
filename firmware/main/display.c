/* The 1.8" 368x448 AMOLED, over QSPI.
 *
 * Ported from the vendor's own `13_display_colorbar` example (waveshareteam/
 * ESP32-S3-Touch-AMOLED-1.8), which is the only authoritative source for the pin map and
 * the init sequence — the product documentation publishes neither, and guessing at a
 * panel's reset timing is how a board comes back dark with nothing to read.
 *
 * TWO REVISIONS EXIST AND THIS BOARD DOES NOT SAY WHICH IT IS. V1 is SH8601 + FT3168, V2
 * is CO5300 + CST820. The vendor drives BOTH with the CO5300 driver — the two controllers
 * take the same command set here — and the only difference that reaches the screen is a
 * 16-pixel column offset on V2. So the revision is probed at runtime the way the vendor
 * probes it: the V2 touch controller answers at I2C 0x15 and the V1 one does not.
 *
 * WHY NOTHING HERE ABORTS. The vendor example is `ESP_ERROR_CHECK` throughout, which is
 * right for a bench demo and wrong for this: an abort is a boot loop, a boot loop on a
 * unit with no cable is a screwdriver, and a panel that cannot reach the box cannot be
 * sent a fix. A dark screen is a bad day; an unreachable unit is the end of the line. So
 * every failure here returns, and `app_main` continues to Wi-Fi regardless.
 */

#include "display.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_panel_io.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_ops.h"
#include "audio.h"
#include "talk.h"
#include "calib.h"
#include "caption.h"
#include "cfg.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "face.h"
#include "font.h"
#include "gesture.h"
#include "rig.h"
#include "speech.h"
#include "vocab.h"
#include "variants.h"
#include "ota.h"
#include "pmu.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "imu.h"
#include "mem.h"
#include "touch.h"

static const char *TAG = "display";

#define LCD_HOST SPI2_HOST
#define LCD_H_RES 368
#define LCD_V_RES 448
#define LCD_BPP 16

/* Identical on both revisions (examples/arduino/…/pin_config.h and its -v2 twin agree). */
#define LCD_CS GPIO_NUM_12
#define LCD_PCLK GPIO_NUM_11
#define LCD_D0 GPIO_NUM_4
#define LCD_D1 GPIO_NUM_5
#define LCD_D2 GPIO_NUM_6
#define LCD_D3 GPIO_NUM_7

#define CST816_ADDR 0x15
#define V2_X_GAP 0x10

/* Sixteen rows at a time rather than a framebuffer: 11.5 KB of DMA-capable internal RAM
   against 322 KB for the whole screen. The animated face will want the full buffer in
   PSRAM (0.2.3); a static pattern does not, and borrowing less is one less thing to be
   wrong about on the first attempt. */
#define STRIPE_ROWS 16
static DMA_ATTR uint16_t stripe[LCD_H_RES * STRIPE_ROWS];
/* THE SECOND HALF OF THE STRIPE BLIT, AND 0.2.46 SHIPPED WITHOUT IT.
 *
 * `esp_lcd_panel_draw_bitmap` QUEUES the transfer and returns; it does not wait. That is not
 * an implementation detail to look up — this panel said so in its own error message for three
 * versions, "recycle spi transactions failed", which is the driver reclaiming transactions
 * queued by EARLIER calls. With `trans_queue_depth` at the vendor macro's 10, up to ten
 * transfers can be reading the buffer while the next memcpy is writing it.
 *
 * 0.2.46 memcpy'd every stripe into one shared buffer, so the DMA read bytes that had already
 * been overwritten by the following stripe: thin horizontal bands of the figure displaced
 * sideways, in the owner's photograph, on the very version that fixed the black screen.
 *
 * Two buffers and a queue one deep is the whole fix. Depth 1 means a call blocks until the
 * previous transfer has completed, so at most ONE is ever in flight when we return — and the
 * stripe we are about to fill is by definition the other one.
 *
 * WHICH ONLY HOLDS IF THE ALTERNATION NEVER RESTARTS, and for two releases it restarted at
 * every frame boundary: each blit began with a local `odd = false`. Portrait got away with it
 * by arithmetic — 448/16 is 28 transfers, so a frame ends on `stripe_b` and the next begins
 * on `stripe`. The rotated blit does 368/16 = 23, an ODD count, so every landscape frame
 * ENDED on `stripe` and the next frame's first memcpy wrote over the transfer still sending
 * it. One 16 px column of the glass was therefore rebuilt from two different frames, 25 times
 * a second, for as long as the panel was held sideways: the owner's "weird screen artifacts
 * on the left side". The same restart corrupted the one-shot clear on the turn, and the
 * landscape-to-portrait handoff besides.
 *
 * So the toggle is FILE-SCOPE and is never reset. Three loops share it; none of them may
 * assume where the previous one stopped. */
static DMA_ATTR uint16_t stripe_b[LCD_H_RES * STRIPE_ROWS];

static bool s_stripe_odd;

static uint16_t *next_stripe(void)
{
    s_stripe_odd = !s_stripe_odd;
    return s_stripe_odd ? stripe_b : stripe;
}

static const co5300_lcd_init_cmd_t init_cmds[] = {
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x3A, (uint8_t[]){0x55}, 1, 0}, /* 16 bit/px */
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 0},
    {0x51, (uint8_t[]){0xFF}, 1, 0}, /* brightness: full, and see the 65 dB/luminance note
                                        in the plan before this becomes a bedroom default */
    {0x63, (uint8_t[]){0xFF}, 1, 0},
    {0x2A, (uint8_t[]){0x00, 0x00, 0x01, 0x6F}, 4, 0},
    {0x2B, (uint8_t[]){0x00, 0x00, 0x01, 0xBF}, 4, 0},
    {0x11, NULL, 0, 100}, /* sleep out, then the 100 ms the panel needs before display on */
    {0x29, NULL, 0, 0},
};

static bool is_v2_board(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) {
        /* Not fatal and not even unusual to be unable to answer: the gap offset is 16 px,
           so guessing V1 costs a slightly shifted image rather than a blank one. */
        ESP_LOGW(TAG, "could not probe the revision; assuming V1");
        return false;
    }
    /* Borrowed, never deleted — touch and the codec are on this same bus. */
    return i2c_master_probe(bus, CST816_ADDR, 50) == ESP_OK;
}

static void fill_stripe(void)
{
    static const uint16_t bars[8] = {
        0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000,
    };
    for (int y = 0; y < STRIPE_ROWS; y++) {
        for (int x = 0; x < LCD_H_RES; x++) {
            const int i = (x * 8) / LCD_H_RES;
            stripe[y * LCD_H_RES + x] = SPI_SWAP_DATA_TX(bars[i], LCD_BPP);
        }
    }
}

/* Kept so the render loop needs no second bring-up. */
static esp_lcd_panel_handle_t s_panel;
/* Kept so display-on and brightness can be re-asserted from the render loop. */
static esp_lcd_panel_io_handle_t s_io;

static bool paint(void)
{
    if (s_panel == NULL) return false;
    fill_stripe();
    for (int y = 0; y < LCD_V_RES; y += STRIPE_ROWS) {
        const esp_err_t err =
            esp_lcd_panel_draw_bitmap(s_panel, 0, y, LCD_H_RES, y + STRIPE_ROWS, stripe);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "draw at y=%d: %s", y, esp_err_to_name(err));
            return false;
        }
    }
    return true;
}

/* Defined with the rest of the breadcrumb machinery, below the render loop it instruments. */
static void phase_init(void);

bool display_start(void)
{
    phase_init();
    i2c_bus_scan();
    /* Before the first sample, so the history is what preceded the restart rather than a
       mixture of then and now. */
    if (pmu_start()) pmu_report_history();
    const bool v2 = is_v2_board();
    ESP_LOGI(TAG, "board revision: %s", v2 ? "V2 (CO5300/CST820)" : "V1 (SH8601/FT3168)");

    /* ONE STRIPE, BECAUSE NOTHING LARGER IS EVER SENT.
     *
     * `max_transfer_sz` is what `spi_bus_initialize` sizes its DMA descriptor chain for,
     * ONCE, here at boot; a transfer past it makes the driver allocate descriptors at
     * transfer time, out of internal RAM. 0.2.42 raised this to a whole frame to stop that,
     * which was aimed at the wrong thing — the allocation that mattered was the bounce
     * BUFFER, not the descriptors, and asking for a frame-sized one made it unsatisfiable.
     *
     * Every transfer this driver now makes is a stripe (`blit_frame()`), the colour bars
     * (`stripe`) or the meter (`s_strip`), and all three are at most `sizeof(stripe)`. So
     * the reservation says what is true rather than reserving for a transfer that no longer
     * exists. */
    const spi_bus_config_t bus = CO5300_PANEL_BUS_QSPI_CONFIG(
        LCD_PCLK, LCD_D0, LCD_D1, LCD_D2, LCD_D3, (int)sizeof(stripe));
    esp_err_t err = spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi bus: %s", esp_err_to_name(err));
        return false;
    }

    /* NO `psram_dma_direct`, AND THAT IS DELIBERATE — SEE `blit_frame()`.
     *
     * 0.2.44 set it, on the reasoning that the S3's GDMA can read PSRAM directly so the
     * driver's internal bounce buffer was pure waste. The first half was right and the
     * second was not. The panel drew a full frame for the first time in four versions, and
     * then died differently:
     *
     *   E spi_master: DMA TX underflow detected
     *   E lcd_panel.io.spi: panel_io_spi_tx_param(222): recycle spi transactions failed
     *
     * Reading the framebuffer out of PSRAM couples the SPI clock to PSRAM latency, and PSRAM
     * here is contended — ESP-SR runs continuously on the other core and holds 3 MB of it.
     * When the DMA cannot refill the SPI FIFO in time the transfer underflows and aborts,
     * the driver is left holding transactions it never recycles, and every call after that
     * returns ESP_ERR_INVALID_STATE. The render task stops; the screen goes black and stays
     * black. So the bounce buffer was not only waste: it was also decoupling the bus from
     * PSRAM, and 0.2.44 removed a function along with the bug.
     *
     * The answer is to bounce through internal RAM as the driver would, but through ONE
     * STATIC buffer instead of a fresh allocation per transfer — which is `blit_frame()`.
     * The pointer it hands the driver is already internal and DMA-capable, so
     * `setup_dma_priv_buffer()` allocates nothing and reads no PSRAM. Both faults, gone,
     * for a fixed 11,776 bytes. */
    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_spi_config_t io_cfg = CO5300_PANEL_IO_QSPI_CONFIG(LCD_CS, NULL, NULL);
    /* See `stripe_b`: two buffers only suffice if at most one transfer is ever in flight. */
    io_cfg.trans_queue_depth = 1;
    err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_cfg, &io);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "panel io: %s", esp_err_to_name(err));
        return false;
    }

    const co5300_vendor_config_t vendor = {
        .init_cmds = init_cmds,
        .init_cmds_size = sizeof(init_cmds) / sizeof(init_cmds[0]),
        .flags.use_qspi_interface = 1,
    };
    const esp_lcd_panel_dev_config_t dev = {
        /* The panel has no reset line brought out; the init sequence does the work. */
        .reset_gpio_num = GPIO_NUM_NC,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = LCD_BPP,
        .vendor_config = (void *)&vendor,
    };

    esp_lcd_panel_handle_t panel = NULL;
    err = esp_lcd_new_panel_co5300(io, &dev, &panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "panel: %s", esp_err_to_name(err));
        return false;
    }
    if ((err = esp_lcd_panel_reset(panel)) != ESP_OK ||
        (err = esp_lcd_panel_init(panel)) != ESP_OK ||
        (err = esp_lcd_panel_set_gap(panel, v2 ? V2_X_GAP : 0, 0)) != ESP_OK ||
        (err = esp_lcd_panel_disp_on_off(panel, true)) != ESP_OK) {
        ESP_LOGE(TAG, "panel bring-up: %s", esp_err_to_name(err));
        return false;
    }

    s_panel = panel;
    s_io = io;
    if (!paint()) return false;
    ESP_LOGI(TAG, "colour bars drawn, %dx%d", LCD_H_RES, LCD_V_RES);
    return true;
}


/* THE PANEL MUST NEVER GO STILL, AND "STILL" MEANS UNCHANGING — NOT UNWRITTEN.
 *
 * §10.4o read the 0.2.5 experiment as "something must keep writing to the panel", and that
 * was the wrong reading of its own evidence: what 0.2.5 did every ten seconds was write a
 * DIFFERENT frame (it alternated the bar order). 0.2.7 honoured the rule as written —
 * identical frames every 500 ms — and went dark anyway, while a tap, whose only distinction
 * is that it changes the colour, brought it straight back. Writes are not the variable.
 * Change is.
 *
 * So the idle state is a slow bob rather than a repeated still, and it is load-bearing: if
 * the figure ever stops moving, the screen goes black and the device reads as dead. W4's
 * rig replaces this with real animation; nothing may replace it with nothing.
 */
/* THE READ PATH IS DEAD, SO STOP READING AND START RE-ASSERTING.
 *
 * 0.2.10 added a probe on the theory that the CO5300 could simply be asked. It answered
 * 0x0A=0x00 [display OFF, sleep IN] — which reads like a diagnosis and is not one. The same
 * call reports brightness 0x00 while `0x51` was explicitly written 0xFF at init, so the
 * reads are returning zeros rather than data: this panel does not answer reads over QSPI.
 * An instrument that fails by returning a plausible wrong answer is worse than one that
 * errors, and this one nearly bought a fix for a diagnosis built on nothing.
 *
 * Reading is also the only way to observe the dark state, and it cannot be done: opening the
 * USB CDC port resets the S3 whatever pyserial is told, because the kernel asserts DTR before
 * those settings apply. Every console read has therefore been of a FRESH BOOT, never of the
 * fault.
 *
 * So: re-assert instead of interrogate. Display-on (0x29) and brightness (0x51) are re-sent
 * on a slow cadence, and the experiment reads off the glass rather than the log —
 *
 *   it stays lit    -> the controller was dropping display-on or brightness, and this IS the
 *                      fix, not just the diagnosis.
 *   it still blanks -> nothing the controller is told matters, which points at the OLED rail
 *                      and the AXP2101 this firmware has never spoken to.
 *
 * Cheap enough to be unconditional: two short command writes every thirty seconds against a
 * bus already carrying five full frames a second.
 */
#define REASSERT_MS 30000
/* Twelve samples at ten seconds is two minutes of history in the RTC ring — long enough to
   cover a screen going dark and the owner noticing, short enough to read in a boot log. */
#define PMU_SAMPLE_MS 10000
/* The init sequence's value, and the starting point until the box says otherwise. Full
   brightness is still wrong for a bedroom; it is now a setting rather than a rebuild. */
#define BRIGHTNESS_DEFAULT 0xFF
static volatile uint8_t s_brightness = BRIGHTNESS_DEFAULT;
static volatile bool s_brightness_pending;
/* Set from the OTA task, honoured by the render loop — see `display_request_restart`. A flag
   rather than a call, for the reason the codec has one: the owner of a peripheral restarts
   it, never a passer-by. */
static volatile bool s_restart_pending;

void display_request_restart(void)
{
    s_restart_pending = true;
}

/* ONE TASK OWNS THE PANEL IO, AND IT IS THE RENDER TASK.
 *
 * `esp_lcd_panel_io_spi` is not thread-safe, and not in the mild way that phrase usually
 * means. `panel_io_spi_tx_param` reads `num_trans_inflight`, drains every queued transfer
 * with `spi_device_get_trans_result(..., portMAX_DELAY)`, decrements the count, and then
 * `memset`s `trans_pool[0]` — the same slot `tx_color` is filling from the other task. Two
 * tasks on one io handle can therefore drain each other's transfers (the count is a `size_t`,
 * so one decrement too many wraps it and the drain loop waits forever holding the bus) or
 * clear a descriptor that DMA is still reading.
 *
 * This was a live cross-task call until 0.2.26: `apply_settings()` runs on the main task, at
 * boot and every fifteen minutes, and the box serves a brightness unconditionally — so every
 * boot issued an 0x51 from the main task into an io handle the face task was driving at
 * ~25 fps. A panic within a minute of boot is exactly the shape that produces.
 *
 * So setting the brightness now only records it. The face task applies it, next to the
 * re-assert that was already the only other command writer. Anything else that wants to talk
 * to this panel belongs on that task too. */
void display_set_brightness(int level)
{
    if (level < 0 || level > 255) return;
    s_brightness = (uint8_t)level;
    s_brightness_pending = true;
}

/* The local copy is not a style choice: `esp_lcd_panel_io_tx_param` takes a plain `const
   void *`, and handing it a pointer into volatile storage discards the qualifier. */
static void apply_brightness(void)
{
    if (s_io == NULL) return;
    const uint8_t level = s_brightness;
    const esp_err_t err = esp_lcd_panel_io_tx_param(s_io, 0x51, &level, 1);
    if (err != ESP_OK) ESP_LOGW(TAG, "brightness: %s", esp_err_to_name(err));
}

/* THE VERSION, ON THE GLASS. The only other ways to know what a panel is running are to ask
   the box what it last SERVED — a different question — or to cable it up and read its
   console, which resets it. Top-left: the head spans x 76..292 and starts at y 60, so this
   corner is the one piece of the panel the robot never occupies. Dim on purpose; it shares a
   bedroom. */
/* Clear of the case's corner radius (`FACE_CASE_CORNER_R`), which the old 8,6 was not: the
   glyph box started ~58 px from the corner's centre of curvature against a radius of 48, so
   the leading `v` was chewed by the enclosure on every panel. Readable enough that nobody
   filed it, which is exactly how it survived — see `face.h`. */
#define LABEL_X 16
#define LABEL_Y 18
#define LABEL_SCALE 2
#define SWAP16(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))
#define LABEL_COLOUR SWAP16(0x8410) /* mid grey */
#define CUE_COLOUR SWAP16(0xFD20)   /* amber, and meant to be noticed */
/* The heard-speech ticker along the bottom. The microphone-open dot that used to sit beside it
   was removed at the owner's request (0.2.66) — `caption.h` records what it was for and why
   the compliance argument this comment made for it was overstated. */
#define CAPTION_COLOUR SWAP16(0xCE79) /* pale grey: readable, never louder than the robot */

/* The gesture itself lives in `gesture.h`, pure and host-tested: three short taps in rhythm,
   then a hold. A reboot IS the firmware re-check, because the OTA loop asks the box before
   its first sleep (main.c), which is why a gesture exists at all on a device with no buttons.
   Drawn here: an amber bar growing across the top during the hold, and one pip per counted
   tap so the sequence is visible while it is being entered rather than only when it works. */
#define PIP_W 28
#define PIP_GAP 8

/* Which pool each part of him answers with. Indexed by `face_zone_t`. */
static const pool_t ZONE_POOL[] = {
    [ZONE_NONE] = POOL_POKE,
    [ZONE_HEAD] = POOL_HEAD,
    [ZONE_BODY] = POOL_BODY,
    [ZONE_ARM] = POOL_ARM,
    [ZONE_LEG] = POOL_LEG,
};

/* THE TOUCH CONTROLLER'S ORIENTATION IS NOT ASSUMED, IT IS SHOWN. The CST820 reports in its
   own frame and nothing here has ever read a coordinate from it, so whether its axes match
   the display's is unmeasured. §10.4af spent three releases getting the accelerometer's
   orientation wrong by reasoning about it. So: a marker is drawn where the firmware believes
   the finger was, and these go out in telemetry. If the dot is not under the finger, the
   mapping is wrong and the numbers say exactly how. */
static int s_tap_x = -1;
static int s_tap_y = -1;
/* The same tap in FRAME space — see `panel_to_frame`. The zones and the marker use these;
   calibration, telemetry and the talk margin keep the panel-space pair above. */
static int s_fig_x = -1;
static int s_fig_y = -1;
static int s_tap_zone = 0;

/* THE TOUCH CALIBRATION. The owner reports the middle of the panel reading true and the outer
   20% skewed, which is the ordinary edge behaviour of these controllers and exactly what a
   fitted piecewise-linear map corrects (`calib.h`). Entered with five taps then a hold — the
   same rhythm as the reboot, a different count — because calibrating is something you do
   standing at the panel, and routing it through a setting on the box would put a round trip
   in the middle of a hands-on job. */
static calib_t s_cal;
static bool s_cal_active;
static int s_cal_i; /* which target, 0 .. CAL_KNOTS*CAL_KNOTS-1 */
static cal_samples_t s_cal_s; /* repeated taps at the current target */
static int16_t s_cal_mx[CAL_KNOTS][CAL_KNOTS];
static int16_t s_cal_my[CAL_KNOTS][CAL_KNOTS];
/* Held after a run so the glass says what happened rather than just returning to the robot. */
static int s_cal_note_ms;
static bool s_cal_ok;

#define CAL_TARGETS (CAL_KNOTS * CAL_KNOTS)
#define CAL_NOTE_MS 1500

static void cal_begin(void)
{
    s_cal_active = true;
    s_cal_i = 0;
    calib_sample_reset(&s_cal_s);
    ESP_LOGI(TAG, "calibration: %d targets, %d-%d taps each", CAL_TARGETS, CAL_SAMPLES_MIN,
             CAL_SAMPLES_MAX);
}

/* A crosshair, drawn into a cleared frame. Deliberately thin and long: a fat blob invites a
   tap at its edge, and the whole routine is only as good as where the finger actually lands. */
static void cal_draw(uint16_t *fb, int cx, int cy, int done, int total, int taps, bool agree)
{
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    /* Amber while it still wants taps, green the moment they agree — so the owner can see
       the difference between "keep going" and "that one is measured", which is the whole
       reason this target takes more than one press. */
    const uint16_t c = agree ? SWAP16(0x07E0) : CUE_COLOUR;
    for (int d = -22; d <= 22; d++) {
        for (int w = -1; w <= 1; w++) {
            const int x = cx + d, y = cy + w;
            if (x >= 0 && x < FACE_W && y >= 0 && y < FACE_H) fb[y * FACE_W + x] = c;
            const int x2 = cx + w, y2 = cy + d;
            if (x2 >= 0 && x2 < FACE_W && y2 >= 0 && y2 < FACE_H) fb[y2 * FACE_W + x2] = c;
        }
    }
    /* One dot below the crosshair per tap recorded here. A target that is taking five tells
       the owner it is taking five, rather than feeling broken. */
    for (int k = 0; k < taps && k < CAL_SAMPLES_MAX; k++) {
        const int dx = cx - (CAL_SAMPLES_MAX * 12) / 2 + k * 12 + 4;
        for (int y = cy + 32; y < cy + 38; y++) {
            for (int x = dx; x < dx + 6; x++) {
                if (x >= 0 && x < FACE_W && y >= 0 && y < FACE_H) fb[y * FACE_W + x] = c;
            }
        }
    }
    /* Progress along the top edge, so the owner knows how many taps are left without
       counting crosshairs. */
    const int w = FACE_W * done / (total > 0 ? total : 1);
    for (int y = 0; y < 4; y++) {
        for (int x = 0; x < w && x < FACE_W; x++) fb[y * FACE_W + x] = LABEL_COLOUR;
    }
}

/* The verdict: a wide bar, green-ish for a map that was accepted and red for one refused. */
static void cal_note(uint16_t *fb, bool ok)
{
    memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    const uint16_t c = ok ? SWAP16(0x07E0) : SWAP16(0xF800);
    for (int y = FACE_H / 2 - 20; y < FACE_H / 2 + 20; y++) {
        for (int x = 40; x < FACE_W - 40; x++) fb[y * FACE_W + x] = c;
    }
}

void display_last_tap(int *x, int *y, int *zone)
{
    if (x != NULL) *x = s_tap_x;
    if (y != NULL) *y = s_tap_y;
    if (zone != NULL) *zone = s_tap_zone;
}

/* THE MICROPHONE, ALWAYS ON, DRAWN DOWN THE LEFT EDGE.
 *
 * A microphone has no symptom: silence could be the ADC, the PGA, the I2S receive direction,
 * or a pin map read from the wrong end of the link — and none of those announce themselves.
 * A meter that is simply always running answers it at a glance and needs no gesture to
 * operate, which matters for the four-year-old this is for as much as for debugging it.
 *
 * The left edge is free by construction: the head spans x 76..292 and the arms reach x 104 at
 * their widest, so a bar at x 4..16 never touches the robot.
 *
 * ONE CHUNK PER FRAME, and the read is what paces the loop. Sized to the frame period, the
 * capture is drained exactly as fast as the I2S DMA fills it. Reading less would show a meter
 * falling further behind the room every second — the kind of fault that looks like bad
 * calibration and is actually a backlog. */
#define METER_X 4
#define METER_W 12
#define METER_TOP 40
#define METER_BOTTOM (FACE_H - 40)
/* Full scale is 32767 and a child at arm's length lands nowhere near it, so the meter is
   scaled to what he actually produces rather than to the codec's range. */
#define METER_FULL 12000
#define METER_COLOUR SWAP16(0x07E0) /* green: unmistakably not the robot's blue */

/* THE METER GETS ITS OWN BLIT, and that is the whole fix for a sluggish bar.
 *
 * The microphone is sampled 25 times a second and the meter was only redrawn when the WHOLE
 * face was, which is five times a second — so four readings in five were captured, used for
 * the peak, and never shown. The bar was not lagging the room, it was showing one frame in
 * five of it.
 *
 * A strip 12 px wide is 8.8 KB against 322 KB for the frame, so it can be pushed on every
 * capture while the face keeps its slower cadence. Internal RAM because it is a DMA source. */
#define METER_SPAN (METER_BOTTOM - METER_TOP)
/* TWO, FOR THE SAME REASON THE FACE STRIPE NEEDS TWO — see `stripe_b`. The meter is refilled
   25 times a second and pushed on every one, so the buffer being written is the buffer the
   previous transfer may still be reading. It is a narrower window than the face's back-to-back
   stripes (8,832 bytes clears in well under the 40 ms between meter updates), which is exactly
   why it would have survived testing and corrupted the bar in the owner's bedroom. */
static DMA_ATTR uint16_t s_strip[2][METER_W * METER_SPAN];

/* Off until the box says otherwise — see `display_set_debug_overlay`. */
static volatile bool s_debug_overlay;

/* PRESS AND HOLD TO TALK — the owner's interaction, in four states.
 *
 *   "when we long press ... it should make a [sound] when it activates the listening and
 *    then when we release it should show the thinking box"
 *
 * THE HOLD THRESHOLD IS THE WHOLE DESIGN PROBLEM. `gesture.h` records that 4-5 year olds
 * produce ordinary presses lasting up to 4.2 s, which is why the maintenance gestures stopped
 * being a bare hold. A talk gesture cannot wait 4.2 s — nobody holds a button that long
 * before speaking — so it fires at 700 ms, comfortably past the 600 ms that still counts as a
 * tap, and accepts that ordinary play will sometimes start a listen. That is survivable here
 * in a way it was not for "reboot the panel": the cost of a false listen is a beep and a
 * discarded recording, not a toy restarting in a child's hands.
 *
 * It never fires mid-maintenance-gesture: those are taps THEN a hold, so a hold that begins
 * while a tap run is live belongs to them. */
#define HOLD_TALK_MS 700
/* AND IT HAS TO LAND ON THE PET. 700 ms is short enough that carrying the panel starts a
 * recording — the owner picked a unit up to photograph it sideways and found the red dot
 * already lit, which is the same false positive the paragraph above waved through as "a beep
 * and a discarded recording". It is not that any more: a listen now uploads six seconds of a
 * child's bedroom and makes the pet answer something nobody asked.
 *
 * The discriminator is free and already computed. A hand carrying a 32 mm panel touches its
 * RIM; a press meant for the pet lands on the pet, which occupies the middle. So the hold has
 * to begin inside an inset rectangle — ~6 mm in on every side, about the half-width of an
 * adult thumb pad, leaving a target of 224x304 that a four-year-old cannot miss. In PANEL
 * coordinates on purpose: the rim is the rim whichever way up the thing is mounted.
 *
 * This is a margin, not a fix for grip contact that lands squarely on the pet's face. The
 * complete answer is the rhythm `gesture.h` uses (slots 1 and 2 are still free, and it was
 * measured at 12 false fires per 20 000 child presses against a bare hold's 1937) — but a
 * rhythm is a thing to teach, and the owner asked for press-and-hold. Teach it only if this
 * is not enough. */
#define TALK_MARGIN_PX 72
/* Long enough that a slow answer is not mistaken for a broken one, short enough that a child
   is not staring at a bubble. Beyond it the panel says it failed rather than returning to
   idle, because "it didn't hear you" and "it broke" must not look the same (§10.4bc).
 *
 * 25 s, AND 12 WAS A GUESS THAT COST A WORKING REPLY BY 777 MILLISECONDS. The first warm turn
 * ever measured from the room took 12,777 ms end to end — whisper 10,668, the model 1,540,
 * Kokoro 568 — and returned 200 OK with 118 KB of speech. The panel had given up at 12,000,
 * called `talk_clear()`, and shown a failure face. Every part of the system worked and the
 * answer was thrown away three quarters of a second before it landed.
 *
 * The budget this has to clear is now measured rather than hoped for: whisper is a FLAT ~10.7 s
 * (it pads every clip to 30 s regardless of length — 10,715 and 10,668 on two utterances of
 * very different length), and the prompt holds replies to one or two sentences, so the model
 * and the speech together run a few seconds more. ~17 s is a bad-but-real turn; 25 leaves
 * headroom without waiting on something that is never coming.
 *
 * This is a SAFETY NET, NOT A TARGET. A longer net costs nothing when turns are fast; it only
 * matters when they are slow, and a slow turn currently produces NOTHING, which is strictly
 * worse than a late answer. The actual fix for the wait is whisper — 10.7 s of a 12.8 s turn
 * is 83% of it, and no timeout value improves that. */
#define TALK_TIMEOUT_MS 25000
#define TALK_FAILED_MS 2500

typedef enum { TALK_IDLE = 0, TALK_LISTENING, TALK_THINKING, TALK_FAILED } talk_t;
static talk_t s_talk;
static uint32_t s_talk_since;
/* WHEN the finger landed, not HOW MANY passes ago. The first cut counted `+= TOUCH_POLL_MS`
   per iteration, which silently assumes this loop runs every 40 ms — it does not. The delay
   is 40 ms and then the frame's work happens, so a tally of nominal ticks always lags the
   wall clock and the hold felt longer than the 700 ms it claimed. A timestamp cannot drift. */
static uint32_t s_down_since;
/* Where the current press landed, sampled once at the down edge rather than read from
   `s_tap_x` at the threshold: the tap coordinates outlive their press, so a finger already
   down when this loop started would otherwise inherit the last press's position. -1 when
   the touch gave no point, which the margin test rejects for free. */
static int s_down_x = -1;
static int s_down_y = -1;

/* A filled rounded box. `display.c` has no drawing library and does not need one: the bubble
   is one rectangle and four corners, and the corners are the difference between a speech
   bubble and a dialog from 1994. */
static void bubble(uint16_t *fb, int x0, int y0, int w, int h, int r, uint16_t c)
{
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            const int dx = x < r ? r - x : (x >= w - r ? x - (w - r - 1) : 0);
            const int dy = y < r ? r - y : (y >= h - r ? y - (h - r - 1) : 0);
            if (dx * dx + dy * dy > r * r) continue;
            const int px = x0 + x, py = y0 + y;
            if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) fb[py * FACE_W + px] = c;
        }
    }
}

/* THE THINKING BOX, AND IT IS NOT DECORATION — IT IS THE LATENCY BUDGET.
 *
 * The round trip is capture, upload, transcribe, a language model and speech synthesis, and
 * the transcription alone is the slowest part (see `../proposed/PANEL_CONVERSATION_PLAN.md`).
 * A pet that is visibly thinking the instant the finger lifts buys a second or more of that
 * for nothing, because a wait you can see someone working through is not the same wait.
 *
 * Three dots filling in turn, which is the one idiom a four-year-old already reads. */
static void draw_thinking(uint16_t *fb, int y0, int h, uint32_t now, bool failed)
{
    const int bw = 118, bh = 54;
    const int bx = FACE_W - bw - 12;
    const int by = y0 + 14;
    bubble(fb, bx, by, bw, bh, 16, failed ? SWAP16(0x4208) : SWAP16(0x2965));
    (void)h;
    const int cy = by + bh / 2;
    if (failed) {
        /* A single flat dash: it tried and has nothing. Deliberately not a third dot — the
           shape has to differ from thinking at a glance, not merely in colour. */
        for (int y = cy - 2; y <= cy + 2; y++) {
            for (int x = bx + 34; x < bx + bw - 34; x++) fb[y * FACE_W + x] = SWAP16(0xF800);
        }
        return;
    }
    const int lit = (int)((now / 320) % 4); /* 0..3, so all three are briefly up */
    for (int i = 0; i < 3; i++) {
        const int cx = bx + 30 + i * 29;
        const int r = (i < lit) ? 9 : 5;
        const uint16_t c = (i < lit) ? SWAP16(0xFFFF) : SWAP16(0x6B4D);
        for (int dy = -r; dy <= r; dy++) {
            for (int dx = -r; dx <= r; dx++) {
                if (dx * dx + dy * dy > r * r) continue;
                const int px = cx + dx, py = cy + dy;
                if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) {
                    fb[py * FACE_W + px] = c;
                }
            }
        }
    }
}

/* LISTENING: a red dot, pulsing so it cannot be mistaken for a dead pixel or a bit of the pet.
 *
 * AND A WORD, because the dot did need explaining. This comment used to claim it was "the one
 * symbol for recording that needs no explaining", and then the owner — who specified the
 * feature — photographed it and asked what it indicated. A symbol only reads as recording
 * next to a camera; on a pet's face it reads as part of the pet. The dot stays for the twins,
 * who cannot read it, and the word is for whoever has to work out why the panel is doing
 * something. It is also the fastest way to notice a listen nobody started, which is the
 * failure this release is otherwise chasing. */
#define LISTEN_SCALE 2
static void draw_listening(uint16_t *fb, int y0, uint32_t now)
{
    const int r = 13 + (int)((now / 140) % 4);
    const int cx = FACE_W - 34, cy = y0 + 34;
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx, py = cy + dy;
            if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) {
                fb[py * FACE_W + px] = SWAP16(0xF800);
            }
        }
    }
    const int w = font_text_w("LISTENING", LISTEN_SCALE);
    font_draw(fb, FACE_W, FACE_H, FACE_W - 8 - w, cy + 22, LISTEN_SCALE, "LISTENING",
              SWAP16(0xF800));
}

/* 0 upright, 1 clockwise, 2 upside down, 3 anticlockwise — a quarter turn each. */
static int s_quarter;
static bool s_quarter_changed = true;

void display_set_debug_overlay(bool on)
{
    s_debug_overlay = on;
}

/* WHICH WAY IS UP. The owner asked for the flip now rather than after a reporting round:
   getting the sign wrong costs one release and is obvious on sight, which is cheaper than
   waiting. 0.2.18 guessed `ay` and the panel's own telemetry settled it in one cycle:
   gravity is on **X** — `ay` sat well inside the hysteresis band, where nothing would ever
   have flipped. The sign then came from a reading taken in a KNOWN orientation, which the
   first one was not: charging port right and the robot's head up reads `[8446, 78, -563]`,
   so right way up is POSITIVE ax. The earlier `[-7637, 381, 530]` was the panel lying
   inverted on its charger, and reading a sign off an unknown pose is how 0.2.19 shipped
   backwards. Two readings, two axes eliminated, one orientation named.

   Hysteresis at about half a gravity, because a panel lying near flat has almost nothing on
   this axis and a bare sign test would flip it back and forth on noise. */
#define FLIP_THRESHOLD 4000
static bool s_upside_down;

/* THE LEAN. The robot slides downhill in proportion to the sideways component of gravity, so
   the flip at the end of a rotation has something leading up to it instead of being a jump
   cut. Tilt a little, he leans a little; tilt past the threshold and he comes all the way
   round.

   HOW FAR is `face.h`'s to say, because it is a property of the drawn geometry rather than of
   the accelerometer: ±60 upright, ±110 on the side, both measured by walking the lean until
   the bounding box touches an edge. The limit is also the GAIN — `tilt * max / LEAN_FULL` —
   so side-mounting nearly doubles the travel for the same tilt, which is the owner's *"he
   should be able to tilt and slide all over to the right and I'll put it to the left, not
   restrained as much."* Portrait is unchanged; the room it has has not grown.

   THE SIGN DOES NEED A CASE WHEN INVERTED, and the argument that it does not was wrong in a
   way worth keeping. It claimed two negations cancel: the panel's rotation negates `ay`, and
   `flip_frame` negates the drawn offset. The second one is not a negation the VIEWER sees.
   `flip_frame` reverses the framebuffer and the panel is then physically rotated 180° in the
   viewer's hands — those two cancel each other, so the viewer reads framebuffer coordinates
   directly in both orientations. Only the accelerometer's sign actually flips, leaving the
   lean correct in one orientation and backwards in the other, which is exactly what the owner
   saw. So the tilt is taken in viewer terms explicitly. */
/* A little over a quarter of a gravity reaches full lean: tilting a panel that far is a
   deliberate act, and anything gentler stays proportional rather than pinned. */
#define LEAN_FULL 2400
/* Smoothed, because the accelerometer is noisy at rest and a figure that twitches while the
   panel sits still reads as broken rather than alive. */
#define LEAN_SMOOTH 4
static int s_lean;
/* Consecutive failed frame pushes, so the log can rate-limit and still say it recovered. */
static int s_blit_fails;
static int s_blit_ok;

void display_blit_counts(int *ok, int *fail)
{
    if (ok != NULL) *ok = s_blit_ok;
    if (fail != NULL) *fail = s_blit_fails;
}

/* THE WHOLE FRAME, THROUGH ONE STATIC INTERNAL BUFFER, A STRIPE AT A TIME.
 *
 * Three versions argued about who should copy the frame out of PSRAM and where the copy
 * should live, and every answer that let the DRIVER decide was wrong:
 *
 *   0.2.41  driver copies, allocating 11,776 B per chunk    28 allocations a frame; one
 *                                                           fails and the rest of the frame
 *                                                           is never written
 *   0.2.42  driver copies, allocating 329,728 B per frame   cannot succeed at all once
 *                                                           anything else is running
 *   0.2.44  driver does not copy; DMA reads PSRAM           underflows under PSRAM
 *                                                           contention and poisons the SPI
 *                                                           driver for good
 *
 * The buffer wants to be internal (so the DMA never waits on PSRAM) and it wants to already
 * exist (so a blit can never depend on the heap). `stripe` is both: static, DMA_ATTR, and
 * idle after the boot colour bars. Handing the driver a pointer that is already internal and
 * DMA-capable makes `setup_dma_priv_buffer()` a no-op — no allocation, no PSRAM read.
 *
 * The cost is 28 memcpys of 11,776 bytes per frame, which at the face's 5 fps is 1.6 MB/s
 * against a core doing nothing else with those cycles. That is the cheap half of the trade. */
/* LANDSCAPE: THE PANEL MOUNTED WITH ITS CABLE OUT THE SIDE.
 *
 * `rig.h` refuses to rotate the figure, and that reasoning still holds — an arbitrary angle
 * is a per-pixel resample this panel cannot afford 25 times a second, and in source space it
 * tears holes in filled shapes. **A quarter turn is neither.** It is an index permutation:
 * every destination pixel is exactly one source pixel, no interpolation, no gaps. The same
 * class of operation as the 180 flip this panel has always done.
 *
 * The square is what makes it cheap. The figure renders scaled into a 368x368 region of the
 * frame (`face_set_fit`), and a quarter turn maps that square onto itself — so the source and
 * destination are the same shape and no second framebuffer is needed. The 40 px above and
 * below are never written; on an AMOLED an unwritten black pixel is an unlit one, so the bars
 * are invisible rather than grey.
 *
 * AND THE DIRECTION OF THE SCAN IS THE WHOLE PERFORMANCE STORY. The obvious loop reads the
 * source across a row and writes down a column, which on a framebuffer in PSRAM is 368 cache
 * misses per stripe. Blitting COLUMN stripes instead — `draw_bitmap` takes any rectangle —
 * inverts it: for a fixed destination column the source addresses are consecutive, so PSRAM
 * is read sequentially and the scattered writes land in internal SRAM, where a stride costs
 * nothing. Same buffers, same 11,776 bytes, no second frame. */
#define SQ 368                        /* the side of the square a quarter turn preserves */
#define SQ_Y0 ((FACE_H - SQ) / 2)     /* 40: where it sits in the portrait frame */
#define COL_STRIPE 16                 /* columns per transfer; 368 / 16 = 23 exactly */

static esp_err_t blit_frame_rotated(const uint16_t *fb, bool clockwise)
{
    if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
    for (int x0 = 0; x0 < FACE_W; x0 += COL_STRIPE) {
        uint16_t *dst = next_stripe();
        for (int c = 0; c < COL_STRIPE; c++) {
            const int x = x0 + c;
            /* Both directions walk the source consecutively; only the sign differs. */
            const uint16_t *src = clockwise ? &fb[(size_t)(SQ_Y0 + x) * FACE_W + (SQ - 1)]
                                            : &fb[(size_t)(SQ_Y0 + SQ - 1 - x) * FACE_W];
            const int step = clockwise ? -1 : 1;
            for (int r = 0; r < SQ; r++) dst[r * COL_STRIPE + c] = src[r * step];
        }
        const esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, x0, SQ_Y0, x0 + COL_STRIPE,
                                                        SQ_Y0 + SQ, dst);
        if (err != ESP_OK) return err;
    }
    return ESP_OK;
}

/* PANEL COORDINATES ARE NOT FRAME COORDINATES ONCE THE PANEL IS ON ITS SIDE, and everything
 * downstream of a finger was reading them as if they were. The owner: *"while horizontal the
 * touch screen indicators do not indicate where I actually tapped, it's like rotated 90° or
 * something."* They are rotated 90°, exactly — by `blit_frame_rotated`, on the way out.
 *
 * The touch controller reports where the finger is on the GLASS. The figure is drawn in frame
 * coordinates and permuted into panel coordinates at blit time, so a tap marker drawn into the
 * frame at the glass position lands wherever the permutation sends it — a quarter turn away.
 * The reaction picker had the same fault silently: `face_zone` was asked where on the figure a
 * point landed using a point that was not in the figure's space, so poking the bird's head
 * sideways answered as a leg.
 *
 * This inverts the mapping `blit_frame_rotated` applies, and it must stay the inverse of that
 * function and no other. Upright is the identity. Upside down is ALSO the identity here,
 * because `flip_frame` reverses the whole buffer after the marker is drawn and the panel is
 * then physically turned over — the two cancel, which is the same argument §10.4bu had to get
 * right for the lean.
 *
 * What stays in PANEL coordinates: the calibration map, the telemetry, and the talk margin —
 * the rim of the glass is the rim of the glass whichever way up the thing is mounted. */
static void panel_to_frame(int px, int py, int *fx, int *fy)
{
    switch (s_quarter) {
    case 1:
        *fx = SQ_Y0 + SQ - 1 - py;
        *fy = SQ_Y0 + px;
        break;
    case 3:
        *fx = py - SQ_Y0;
        *fy = SQ_Y0 + SQ - 1 - px;
        break;
    default:
        *fx = px;
        *fy = py;
        break;
    }
}

static esp_err_t blit_frame(const uint16_t *fb)
{
    if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
    if (s_quarter == 1 || s_quarter == 3) {
        if (s_quarter_changed) {
            /* The bars beside the square are never written again, so whatever the portrait
               frame last left there would stay forever. Once, on the turn, not per frame. */
            s_quarter_changed = false;
            memset(stripe, 0, sizeof(stripe));
            memset(stripe_b, 0, sizeof(stripe_b));
            for (int y = 0; y < FACE_H; y += STRIPE_ROWS) {
                esp_lcd_panel_draw_bitmap(s_panel, 0, y, FACE_W, y + STRIPE_ROWS,
                                          next_stripe());
            }
        }
        return blit_frame_rotated(fb, s_quarter == 1);
    }
    s_quarter_changed = false;
    for (int y = 0; y < FACE_H; y += STRIPE_ROWS) {
        int rows = FACE_H - y;
        if (rows > STRIPE_ROWS) rows = STRIPE_ROWS;
        /* ALTERNATING, because the previous stripe may still be in flight — see `stripe_b`. */
        uint16_t *dst = next_stripe();
        memcpy(dst, fb + (size_t)y * FACE_W, (size_t)rows * FACE_W * sizeof(uint16_t));
        const esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, 0, y, FACE_W, y + rows, dst);
        if (err != ESP_OK) return err;
    }
    return ESP_OK;
}

/* THE RIG, first slice. W4's ~17 tweened floats start here with two: a blink and a flinch.
 *
 * Blink is what makes a face read as alive rather than as a picture of a face, and it is the
 * cheapest of the seventeen — one number, no new geometry. JITTERED, because a blink exactly
 * every four seconds is a metronome: the regularity is what gives away a machine, and the
 * irregularity is most of the effect.
 *
 * Flinch is the interaction the design settled: touch means "I am paying attention to you",
 * expressed as a sub-100 ms movement toward the finger. Here it is a dip and wide eyes that
 * decay over about half a second — startled, then recovering, which is what makes a poke feel
 * answered rather than merely registered. */
#define BLINK_EVERY_MS 4000
#define BLINK_JITTER_MS 1800
#define BLINK_MS 240
#define FLINCH_DIP 18
/* ~0.86 per 40 ms frame reaches negligible in about half a second: fast enough to feel like a
   reflex, slow enough to be seen. */
#define FLINCH_DECAY 0.86f

static int s_blink_t;
static int s_blink_next = BLINK_EVERY_MS;
static float s_flinch;

static float blink_open(int dt)
{
    s_blink_t += dt;
    if (s_blink_t < s_blink_next) return 1.0f;
    const int into = s_blink_t - s_blink_next;
    if (into >= BLINK_MS) {
        s_blink_t = 0;
        s_blink_next = BLINK_EVERY_MS - BLINK_JITTER_MS / 2 +
                       (int)(esp_random() % (uint32_t)BLINK_JITTER_MS);
        return 1.0f;
    }
    const float half = BLINK_MS / 2.0f;
    return into < half ? 1.0f - (float)into / half : ((float)into - half) / half;
}

static void update_orientation(void)
{
    int16_t ax = 0, ay = 0, az = 0;
    if (!imu_read(&ax, &ay, &az)) return;
    /* FOUR WAYS UP, FROM THE TWO AXES THE FLIP ALREADY USED. Gravity on X is portrait and
       its sign says which way; gravity on Y is landscape, mounted with the cable out the
       side, and its sign says which. Whichever axis is larger wins, with the same half-a-
       gravity hysteresis the two-way version needed — a panel lying near flat has almost
       nothing on either axis, and a bare comparison would flip it back and forth on noise. */
    const int mag_x = ax < 0 ? -ax : ax;
    const int mag_y = ay < 0 ? -ay : ay;
    const int was = s_quarter;
    if (mag_x > mag_y) {
        if (ax < -FLIP_THRESHOLD) s_quarter = 2;
        else if (ax > FLIP_THRESHOLD) s_quarter = 0;
    } else {
        if (ay < -FLIP_THRESHOLD) s_quarter = 1;
        else if (ay > FLIP_THRESHOLD) s_quarter = 3;
    }
    s_upside_down = (s_quarter == 2);
    /* Function scope: both the fit and the lean limit depend on it. */
    const bool side = (s_quarter == 1 || s_quarter == 3);
    if (was != s_quarter) {
        static const char *NAMES[] = {"upright", "clockwise", "upside down", "anticlockwise"};
        s_quarter_changed = true;
        /* The figure is composed for 448 of height and gets 368 on its side, so the whole
           thing scales by 368/448 into the square a quarter turn preserves. */
        face_set_fit(side ? (float)SQ / (float)FACE_H : 1.0f,
                     side ? SQ_Y0 + (int)(SQ * 0.545f) : -1);
        ESP_LOGI(TAG, "orientation: %s (ax=%d ay=%d az=%d)", NAMES[s_quarter], ax, ay, az);
    }

    /* AND THE LEAN FOLLOWS THE VIEWER'S HORIZONTAL AXIS, WHICH A QUARTER TURN MOVES.
     *
     * This read `-ay` unconditionally, which was right for the only two orientations that
     * existed when it was written and is wrong the moment the panel is on its side: in
     * landscape `ay` carries GRAVITY, about 8000 against a LEAN_FULL of 2400, so the target
     * clamps to LEAN_MAX and the figure sits pinned at full lean for ever. That is the
     * owner's "it doesn't tilt side to side" — not a dead sensor, a saturated one.
     *
     * The mapping is derivable rather than guessed. Upright, gravity is on +X (§10.4 named
     * that from a reading in a known pose) and the lean was `-ay`, so the viewer's right is
     * board -Y. Turn the panel a quarter clockwise and what pointed right now points down —
     * so viewer-right becomes board -X, and each further quarter turn walks the same circle:
     *
     *     upright        viewer right = -Y   ->  lean from -ay
     *     clockwise      viewer right = -X   ->  lean from -ax
     *     upside down    viewer right = +Y   ->  lean from +ay
     *     anticlockwise  viewer right = +X   ->  lean from +ax
     *
     * Which is also why the magnitudes work: whichever axis is NOT carrying gravity is the
     * small signal a tilt moves, in every orientation. */
    int tilt;
    switch (s_quarter) {
    case 1: tilt = -ax; break;
    case 2: tilt = ay; break;
    case 3: tilt = ax; break;
    default: tilt = -ay; break;
    }
    const int lean_max = side ? FACE_LEAN_MAX_SIDE : FACE_LEAN_MAX;
    int target = tilt * lean_max / LEAN_FULL;
    if (target > lean_max) target = lean_max;
    if (target < -lean_max) target = -lean_max;
    s_lean += (target - s_lean) / LEAN_SMOOTH;
}

/* A 180 degree rotation of a row-major buffer is exactly its reversal, which is why this is
   one pass and not a resampling — and why it takes the version label and the meter with it.
   Ninety degrees is not available: the panel is 368x448 and a quarter turn does not fit. */
static void flip_frame(uint16_t *fb)
{
    for (int i = 0, j = FACE_W * FACE_H - 1; i < j; i++, j--) {
        const uint16_t t = fb[i];
        fb[i] = fb[j];
        fb[j] = t;
    }
}

/* Fast attack, slow decay. A raw per-chunk peak pushed 25 times a second is honest and looks
   like noise; rising instantly and falling over about a second is what makes it read as a
   meter. The fall is what is smoothed — a loud moment still registers on the frame it
   happened. */
#define METER_DECAY (METER_FULL / 25)
static int s_shown;

static void blit_meter(int level)
{
    if (s_panel == NULL) return;
    if (!s_debug_overlay) {
        /* Zeroed rather than merely skipped, so switching the overlay on shows the room as it
           is now instead of a peak the bar was holding when it was switched off. */
        s_shown = 0;
        return;
    }
    if (level >= s_shown) {
        s_shown = level;
    } else {
        s_shown -= METER_DECAY;
        if (s_shown < level) s_shown = level;
    }
    int h = s_shown * METER_SPAN / METER_FULL;
    if (h > METER_SPAN) h = METER_SPAN;
    if (h < 0) h = 0;
    static int slot;
    slot ^= 1;
    uint16_t *strip = s_strip[slot];
    for (int row = 0; row < METER_SPAN; row++) {
        const uint16_t c = (row >= METER_SPAN - h) ? METER_COLOUR : 0;
        for (int col = 0; col < METER_W; col++) strip[row * METER_W + col] = c;
    }
    int x0 = METER_X, y0 = METER_TOP;
    if (s_upside_down) {
        /* The strip reverses and the window moves to the opposite corner, so the bar stays on
           the viewer's left rather than travelling to the other side of the screen. */
        for (int i = 0, j = METER_W * METER_SPAN - 1; i < j; i++, j--) {
            const uint16_t t = strip[i];
            strip[i] = strip[j];
            strip[j] = t;
        }
        x0 = FACE_W - METER_X - METER_W;
        y0 = FACE_H - METER_BOTTOM;
    }
    esp_lcd_panel_draw_bitmap(s_panel, x0, y0, x0 + METER_W, y0 + METER_SPAN, strip);
}

#define BOB_PX 5
#define FACE_FLOOR_MS 200
#define TOUCH_POLL_MS 40
/* Rare enough not to crowd the log, often enough that a frozen panel is named in seconds. */
#define BEAT_MS 10000
/* Ten seconds of a panel that cannot draw, and only once it has been up long enough that a
   restart is a recovery rather than a loop. At 25 fps the loop attempts a blit every frame. */
#define BLIT_HEAL_FAILS 250
#define BLIT_HEAL_AFTER_MS 60000

/* A triangle in whole pixels, one step per frame, so CONSECUTIVE FRAMES ARE NEVER EQUAL.
   A sine was the obvious shape and the wrong one: rounded to integers it repeats a value at
   each turning point, which hands the panel exactly the still frame this exists to prevent.
   0,1,..,5,4,..,-5,..,-1 — twenty frames, 4 s at the floor cadence, every step a change. */
static int bob_step(int frame)
{
    const int k = frame % (4 * BOB_PX);
    if (k <= BOB_PX) return k;
    if (k <= 3 * BOB_PX) return 2 * BOB_PX - k;
    return k - 4 * BOB_PX;
}

/* The loudest thing heard since the last telemetry report, so the box can see the microphone
   working without anyone describing a noise into a chat window. Reading it clears it. */
static volatile int s_mic_peak;

/* Words of stack the face task has never touched — the smallest headroom seen since boot.
   Reported so a near-overflow is visible BEFORE it becomes a panic, rather than inferred from
   one afterwards. */
static volatile int s_stack_free;

/* WHERE IT DIED. A breadcrumb, because a backtrace is unreachable.
 *
 * The panel panics (`reset_reason: "panic"`), and neither suspect survived measurement: the
 * render task's stack peaked at ~2.7 KB of 8 KB, and eight PMU samples across a blackout were
 * byte-identical with every rail up. The panic handler prints a backtrace to the USB console —
 * which this panel does not have, being on a charger in another room, and which resets the
 * chip the moment it is opened anyway.
 *
 * So the loop writes where it is into RTC memory, which survives the reset a panic performs.
 * The next boot reports the last phase reached. That is not a line number, but it is the
 * difference between "somewhere in the firmware" and "in the I2S read" — and it costs one
 * store per stage.
 *
 * 1 loop top, 2 touch, 3 beep request, 4 stack probe, 5 IMU, 6 face_draw, 7 label, 8 flip,
 * 9 full blit, 10 frame delay, 11 meter blit, 12 panel re-assert, 13 PMU sample,
 * 14 brightness. Keep this list and the one in ROOM_ENDPOINT_PLAN.md §10.4aj together; a
 * number whose stage nobody can name is worth nothing.
 *
 * STAGE 10 CHANGED MEANING IN 0.2.33 and readings do not compare across that line. It was the
 * blocking microphone read, where this loop spent most of its wall clock — which is why every
 * breadcrumb so far reported 10 whatever actually failed. The capture moved to `audio.c`'s own
 * task, so 10 is now an ordinary frame delay and a crash there means something quite different.
 * Stage 15, the codec levels, went with it.
 *
 * READ IT AS THE RENDER TASK'S POSITION, NOT AS THE CRASH SITE. This records where THIS task
 * was; a fault in the main task reports whatever stage the render loop happened to be parked
 * in, and it parks in stage 10 — the capture blocks for a full 40 ms frame, most of the
 * loop's wall clock. The first breadcrumb to come back read 10 and §10.4ak had already
 * called that exonerating for a main-task race. It is not. See §10.4al. */
#define PHASE_MAGIC 0x50484131u
static RTC_NOINIT_ATTR uint32_t s_phase_magic;
static RTC_NOINIT_ATTR uint32_t s_phase;
static RTC_NOINIT_ATTR uint32_t s_phase_prev;

/* Read once at boot, before the loop overwrites it. */
static int s_phase_at_crash = -1;

#define PHASE(n)      \
    do {              \
        s_phase = (n); \
    } while (0)

int display_crash_phase(void)
{
    return s_phase_at_crash;
}

static void phase_init(void)
{
    if (s_phase_magic == PHASE_MAGIC) {
        s_phase_at_crash = (int)s_phase;
        s_phase_prev = s_phase;
    } else {
        /* Cold boot: nothing survived, and -1 says so rather than claiming phase 0. */
        s_phase_at_crash = -1;
        s_phase_magic = PHASE_MAGIC;
    }
    s_phase = 0;
}

int display_stack_free(void)
{
    return s_stack_free;
}

int display_mic_peak(void)
{
    const int p = s_mic_peak;
    s_mic_peak = 0;
    return p;
}

static void draw_meter(uint16_t *fb, int level)
{
    /* THE SAME GATE AS `blit_meter`, AND MISSING IT IS WHY THE OWNER STILL SAW THE BAR.
       The meter is drawn twice by design — once into the frame here, so it survives a full
       repaint, and once as its own narrow blit so it can update at 25 fps while the face
       redraws at 5 (see `blit_meter`). 0.2.50 put the debug switch on one of them. A feature
       with two draw sites needs the condition at both, and "I changed the meter" read as done
       because the code that came to mind was the one that had the interesting comment. */
    if (!s_debug_overlay) return;
    const int span = METER_BOTTOM - METER_TOP;
    int h = level * span / METER_FULL;
    if (h > span) h = span;
    if (h <= 0) return;
    for (int y = METER_BOTTOM - h; y < METER_BOTTOM; y++) {
        for (int x = METER_X; x < METER_X + METER_W; x++) fb[y * FACE_W + x] = METER_COLOUR;
    }
}

static void reassert_panel(void)
{
    if (s_io == NULL) return;
    const esp_err_t on = esp_lcd_panel_io_tx_param(s_io, 0x29, NULL, 0);
    if (on != ESP_OK) ESP_LOGW(TAG, "re-assert failed (0x29 %s)", esp_err_to_name(on));
    apply_brightness();
}

static void face_task(void *arg)
{
    (void)arg;
    /* The framebuffer PSRAM was enabled for: 368x448x2 = 322 KB, which does not fit in the
       332 KB of internal RAM with Wi-Fi and TLS also to feed. */
    uint16_t *fb = heap_caps_malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t),
                                    MALLOC_CAP_SPIRAM);
    if (fb == NULL) {
        ESP_LOGE(TAG, "no framebuffer — falling back to the test pattern");
        vTaskDelete(NULL);
        return;
    }

    const bool touch = touch_start();
    {
        uint8_t blob[CAL_BLOB_BYTES];
        const int n = cfg_calibration_load(blob, sizeof(blob));
        if (n > 0 && calib_load(blob, n, &s_cal)) {
            ESP_LOGI(TAG, "touch calibration loaded");
        } else if (n > 0) {
            /* Stored but unusable. Saying so matters: a silently ignored calibration and a
               panel that was never calibrated look identical from the outside, and they need
               completely different fixes. */
            ESP_LOGW(TAG, "stored touch calibration rejected — running uncalibrated");
        }
    }
    /* Silence is a failure mode with no symptom, so it is logged rather than inferred: a
       beep that never comes could be the codec, the amplifier pin, the volume, or a tap
       that was never registered, and only the first of those is visible from here. */
    const bool sound = audio_start();
    if (!sound) ESP_LOGW(TAG, "no codec — taps will be silent");
    /* THE RECOGNISER IS NOT STARTED HERE, and that is deliberate rather than tidy. This
       task runs ~1.5 s into boot and `net_connect` runs at ~3.0 s, so starting ESP-SR here
       meant it won the race for internal RAM and the radio never came up (0.2.37). `main.c`
       starts it once the panel is settled and updatable; this loop just asks whether it is
       live, which is false until then and false forever if it could not have the memory. */
    caption_t cap;
    caption_reset(&cap);
    int colour = 0;
    int frame = 0;
    int since_draw = FACE_FLOOR_MS; /* draw immediately */
    int since_beat = 0;
    /* The rig's running state. `st` persists between frames because the emotion TWEENS: a
       pose system that snaps is a slide show, and the halving approach in `emotion.c` is what
       turns one into an animation system. */
    face_state_t st;
    face_rest(&st);
    action_t action = ACT_NONE;
    uint32_t action_start = 0;
    float action_mag = 1.0f;
    gesture_t gest;
    gesture_reset(&gest);
    pool_memory_t mem[POOL_COUNT];
    for (int i = 0; i < POOL_COUNT; i++) variants_reset(&mem[i]);
    float s_open = 1.0f;
    int s_drawn_lean = 0;
    int since_reassert = 0;
    int since_sample = 0;
    int level = 0;

    while (true) {
        PHASE(1);
        bool dirty = false;
        /* One clock read per frame, shared by the rig, the pools and the cooldowns, so every
           part of a frame agrees about when it is. */
        const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        PHASE(2);
        /* Read the edge ONCE. `touch_tapped()` is what refreshes the cached level that
           `touch_is_down()` returns, so calling it twice in a frame would consume the edge
           for whichever caller ran first. */
        const bool tapped = touch && touch_tapped();
        const bool down = touch && touch_is_down();
        /* SPEAKING, AND IT OUTRANKS THE TOY. The owner, after the first real conversation:
           "when the agent is talking we should prohibit beeps from cutting it off, and we
           should also stop poke interactions making other animations."
         *
           Both of those are the same rule — a reply is the panel's one sustained utterance and
           everything else on this device is an interjection. A beep over it is a toy talking
           over a person; a new action mid-sentence throws away the talking animation that is
           the only thing on screen explaining the sound. Decided once a frame so every branch
           below agrees about it. */
        const bool speaking = audio_playing();
        if (tapped && !speaking) {
            colour = (colour + 1) % face_colour_count();
            s_flinch = 1.0f;
            /* THE POKE IS THE PRODUCT, AND WHERE YOU POKE IS HALF OF IT. The zone picks the
               pool; the pool picks the reaction, weighted, cooled-down, and softened if you
               are hammering it (`variants.c`). The colour cycle stays, because it is the one
               thing a child can steer deliberately. */
            int rx = -1, ry = -1;
            touch_point(&rx, &ry);
            /* Corrected before anything reads it, so the zones, the marker and the telemetry
               all speak the same coordinates. The identity until a calibration exists. */
            calib_apply(&s_cal, rx, ry, &s_tap_x, &s_tap_y);
            panel_to_frame(s_tap_x, s_tap_y, &s_fig_x, &s_fig_y);
            s_tap_zone = (int)face_zone(st.form, s_fig_x, s_fig_y, s_upside_down, s_lean);
            const pool_t pool = ZONE_POOL[s_tap_zone];
            action = (action_t)variants_pick(pool, &mem[pool], now, esp_random());
            action_mag = variants_penalty(pool, &mem[pool], now);
            action_start = now;
            /* BOTH PAIRS, because they agree only when the panel is upright and a
               disagreement is the whole diagnosis: a tap the glass and the figure place
               differently is a rotation fault, one they place identically but in the wrong
               zone is a calibration fault, and the owner has no terminal to tell them apart
               with. */
            ESP_LOGI(TAG, "tap glass (%d,%d) figure (%d,%d) zone %d -> colour %d, action %d, "
                          "mag %.2f",
                     s_tap_x, s_tap_y, s_fig_x, s_fig_y, s_tap_zone, colour, (int)action,
                     (double)action_mag);
            /* Before the repaint, not after: the beep is ~90 ms and a full frame is ~330 KB
               over QSPI, and the tap feels answered by whichever lands first. */
            PHASE(3);
            if (sound) audio_beep();
            dirty = true;
        } else if (tapped) {
            /* Poked mid-sentence. The flinch stays — ignoring the finger entirely would read
               as a frozen pet — but no beep, no colour change and no new action, so the reply
               finishes with the mouth still moving. */
            s_flinch = 1.0f;
            dirty = true;
        }
        /* WHAT THE PANEL HEARD. Popped once a frame, so a phrase cannot arrive between two
           frames and be lost, and acted on in exactly the way a tap is — the voice is
           another way to ask, never a second animation path. */
        char said[64];
        int said_id = -1;
        if (speech_live() && speech_take(said, sizeof(said), &said_id)) {
            caption_say(&cap, said);
            const vocab_t *v = vocab_get(said_id);
            if (v != NULL) {
                switch (v->kind) {
                case VOCAB_FORM:
                    st.form = (face_form_t)v->arg;
                    break;
                case VOCAB_COLOUR:
                    /* A named colour lands on its index; "pick a new color" still steps. */
                    colour = v->arg < 0 ? (colour + 1) % face_colour_count()
                                        : v->arg % face_colour_count();
                    break;
                case VOCAB_ACTION:
                default:
                    action = (action_t)v->arg;
                    action_mag = 1.0f;
                    action_start = now;
                    break;
                }
                if (sound) audio_beep();
                dirty = true;
            }
        }
        if (speech_live() && !caption_idle(&cap)) dirty = true;
        caption_tick(&cap, now, FACE_W);

        /* THE CALIBRATION ROUTINE OWNS THE FRAME while it runs. It deliberately bypasses the
           rig rather than drawing over it: a robot reacting to the taps being measured would
           move the thing the owner is aiming at. */
        if (s_cal_active || s_cal_note_ms > 0) {
            if (s_cal_note_ms > 0) {
                s_cal_note_ms -= TOUCH_POLL_MS;
                cal_note(fb, s_cal_ok);
            } else {
                const int i = s_cal_i % CAL_KNOTS, j = s_cal_i / CAL_KNOTS;
                if (tapped) {
                    int rx = -1, ry = -1;
                    touch_point(&rx, &ry);
                    /* RAW, not corrected: a calibration measured through the previous
                       calibration would fit the correction on top of itself. */
                    calib_sample_add(&s_cal_s, rx, ry);
                    if (sound) audio_beep();
                }
                /* Advance when the taps AGREE, or when this target has had its cap — a
                   target that will not settle must not trap the owner on it, so past the cap
                   it contributes its median and the run continues. */
                const bool settled = calib_sample_settled(&s_cal_s);
                const bool capped = s_cal_s.n >= CAL_SAMPLES_MAX;
                if (settled || capped) {
                    calib_sample_result(&s_cal_s, &s_cal_mx[j][i], &s_cal_my[j][i]);
                    if (!settled) {
                        ESP_LOGW(TAG, "target %d never settled (spread %d) — using the median",
                                 s_cal_i, calib_sample_spread(&s_cal_s));
                    }
                    calib_sample_reset(&s_cal_s);
                    s_cal_i++;
                    if (s_cal_i >= CAL_TARGETS) {
                        s_cal_active = false;
                        s_cal_ok = calib_build(s_cal_mx, s_cal_my, &s_cal);
                        if (s_cal_ok) {
                            uint8_t blob[CAL_BLOB_BYTES];
                            const int n = calib_save(&s_cal, blob, sizeof(blob));
                            if (n > 0) cfg_calibration_save(blob, n);
                            ESP_LOGI(TAG, "calibration accepted: x %d %d %d %d, y %d %d %d %d",
                                     s_cal.raw_x[0], s_cal.raw_x[1], s_cal.raw_x[2],
                                     s_cal.raw_x[3], s_cal.raw_y[0], s_cal.raw_y[1],
                                     s_cal.raw_y[2], s_cal.raw_y[3]);
                        } else {
                            /* Refused rather than stored: a non-monotone map folds the panel
                               onto itself, and the only way out of that is the touchscreen it
                               just broke. The previous calibration, if any, is untouched. */
                            ESP_LOGW(TAG, "calibration refused — not monotone, keeping the old");
                        }
                        s_cal_note_ms = CAL_NOTE_MS;
                    }
                }
                cal_draw(fb, calib_target_x(s_cal_i % CAL_KNOTS),
                         calib_target_y(s_cal_i / CAL_KNOTS), s_cal_i, CAL_TARGETS, s_cal_s.n,
                         calib_sample_settled(&s_cal_s));
            }
            PHASE(9);
            const esp_err_t cerr = blit_frame(fb);
            if (cerr != ESP_OK) ESP_LOGE(TAG, "cal blit: %s", esp_err_to_name(cerr));
            PHASE(10);
            vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
            continue;
        }

        PHASE(4);
        s_stack_free = (int)uxTaskGetStackHighWaterMark(NULL);
        PHASE(5);
        update_orientation();
        s_open = blink_open(TOUCH_POLL_MS);
        s_flinch *= FLINCH_DECAY;
        if (s_flinch < 0.02f) s_flinch = 0.0f;
        /* Animating means every poll is a frame. A blink at the 200 ms idle floor would be one
           frame long and read as a glitch; the floor is for a face that is holding still. */
        if (s_flinch > 0.0f || s_open < 1.0f) dirty = true;
        /* A running action animates at the poll rate; the idle floor is for a face holding
           still, and a wave drawn five times a second is a twitch. */
        if (action != ACT_NONE) dirty = true;
        /* A moved figure is a new frame, so tilting redraws at the poll rate rather than
           waiting out the idle floor — but only once it has moved enough to see, or every
           frame would be a full 322 KB blit for a pixel of accelerometer noise. */
        if (s_lean - s_drawn_lean > 2 || s_drawn_lean - s_lean > 2) dirty = true;

        const int prev_taps = gest.taps;
        const float prev_cue = gesture_cue(&gest);
        /* Decided before the draw, acted on after it: the frame carrying a full-width cue has
           to reach the glass first, or a reboot is indistinguishable from the fault we are
           chasing. */
        const gesture_action_t act = gesture_poll(&gest, tapped, down, TOUCH_POLL_MS);

        /* PRESS AND HOLD TO TALK. After `gesture_poll`, so `gest.taps` is this frame's count:
           the maintenance gestures are taps THEN a hold, so a hold that begins while a tap
           run is live belongs to them and must not also start a listen. */
        if (!down) {
            s_down_since = 0;
        } else if (s_down_since == 0) {
            s_down_since = now;
            /* Same frame as the edge that set them, so this is THIS press's origin. */
            s_down_x = tapped ? s_tap_x : -1;
            s_down_y = tapped ? s_tap_y : -1;
        }
        const uint32_t held = (down && s_down_since != 0) ? now - s_down_since : 0;
        const bool on_the_pet =
            s_down_x >= TALK_MARGIN_PX && s_down_x < FACE_W - TALK_MARGIN_PX &&
            s_down_y >= TALK_MARGIN_PX && s_down_y < FACE_H - TALK_MARGIN_PX;
        /* NOT WHILE A TURN IS STILL IN FLIGHT, and this is a lifetime rule rather than a
           politeness one. `talk.c` uploads straight out of the capture buffer, and this
           renderer gives up at 12 s while the HTTP timeout is 20 — so without this guard a
           child who holds again after a failure face would call `audio_capture_open()` and
           overwrite the bytes still being read by the socket. A five-second window, on the
           one path a frustrated four-year-old is most likely to take. */
        /* NOT WHILE WE ARE SPEAKING, and this one is measured rather than tidy: the owner
           held the panel to ask a second question while the first reply was still playing and
           the recording came back EMPTY (`heard: ""`, 2026-09-22 01:53:54). `audio.c` goes
           deaf for six chunks whenever the speaker runs — the codec routes the DAC into the
           ADC, so without that the pet would transcribe itself — which means a hold taken over
           our own voice can only ever capture silence. Refusing it costs nothing and saves a
           child from being ignored by a toy that looked like it was listening. */
        if (s_talk == TALK_IDLE && down && on_the_pet && !speaking && gest.taps == 0 &&
            held >= HOLD_TALK_MS && talk_state() != TALK_NET_BUSY) {
            s_talk = TALK_LISTENING;
            s_talk_since = now;
            /* The beep IS the affordance. Nothing else tells a child holding a 29 mm screen
               that the thing is now listening rather than merely being held. */
            if (sound) audio_beep();
            /* AFTER the beep, deliberately: `audio.c` goes deaf for six chunks once the
               speaker runs (§10.4bi), so opening the recording here keeps our own tone out
               of the front of every message. */
            audio_capture_open();
            ESP_LOGI(TAG, "talk: listening");
        } else if (s_talk == TALK_IDLE && down && !on_the_pet && gest.taps == 0 &&
                   held >= HOLD_TALK_MS && held < HOLD_TALK_MS + TOUCH_POLL_MS) {
            /* Once per press, on the frame the threshold passes — the owner has no terminal
               but does have the log, and a margin that is too wide looks exactly like a
               microphone that stopped working unless the panel says which it is. */
            ESP_LOGI(TAG, "talk: hold at (%d,%d) is on the rim, not the pet", s_down_x,
                     s_down_y);
        } else if (s_talk == TALK_LISTENING && !down) {
            size_t got = 0;
            const int16_t *pcm = audio_capture_close(&got);
            /* HELD LENGTH AND CAPTURED LENGTH ARE DIFFERENT NUMBERS, and printing only the
               first is how a dead microphone looks like a working one. They diverge when the
               capture buffer failed to allocate, when the six-second cap bites, or when the
               deaf window after the beep ate the start — and each of those is a different
               bug. The length BEFORE the timestamp is reused, or the hold reads as zero. */
            ESP_LOGI(TAG, "talk: held %u ms, captured %u ms (%u bytes)",
                     (unsigned)(now - s_talk_since), (unsigned)audio_capture_ms(),
                     (unsigned)got);
            if (pcm == NULL || !talk_send(pcm, got)) {
                /* Nothing recorded, or a turn already in flight. Either way the pet goes
                   straight back to being a pet rather than showing a bubble that cannot
                   resolve — a thinking box with nothing behind it is the silent hang this
                   whole state machine exists to avoid. */
                s_talk = TALK_IDLE;
            } else {
                s_talk = TALK_THINKING;
                s_talk_since = now;
            }
        } else if (s_talk == TALK_THINKING && talk_state() == TALK_NET_SPOKE) {
            /* Speaking. The bubble goes and the pet reacts, and the state is held on
               `audio_playing()` rather than a timer so a long reply cannot end on screen
               mid-sentence. */
            s_talk = TALK_IDLE;
            talk_clear();
            action = ACT_NOD;
            action_mag = 1.0f;
            action_start = now;
        } else if (s_talk == TALK_THINKING && talk_state() == TALK_NET_IDLE) {
            s_talk = TALK_IDLE; /* the box heard silence; nothing to say about it */
            talk_clear();
        } else if (s_talk == TALK_THINKING &&
                   (talk_state() == TALK_NET_FAILED || now - s_talk_since > TALK_TIMEOUT_MS)) {
            talk_clear();
            /* NOT a silent return to idle. On a panel whose owner has no terminal, "it did
               not hear you" and "it is broken" must not look identical (§10.4bc). */
            s_talk = TALK_FAILED;
            s_talk_since = now;
            ESP_LOGW(TAG, "talk: no reply");
        } else if (s_talk == TALK_FAILED && now - s_talk_since > TALK_FAILED_MS) {
            s_talk = TALK_IDLE;
        }
        if (s_talk != TALK_IDLE) dirty = true; /* the dot pulses and the dots cycle */
        const bool rebooting = act == GESTURE_REBOOT;
        if (act == GESTURE_CALIBRATE) cal_begin();
        if (act == GESTURE_FORM) {
            /* Until "change into merc" exists — the command list needs ESP-SR, which is not
               wired up yet — four taps and a hold is how the twins get the other body. */
            st.form = st.form == FORM_OSTRICH ? FORM_ROBOT : FORM_OSTRICH;
            dirty = true;
            ESP_LOGI(TAG, "form -> %s", st.form == FORM_OSTRICH ? "ostrich" : "robot");
        }
        const float cue = gesture_cue(&gest);
        if (cue != prev_cue || gest.taps != prev_taps) dirty = true;

        if (dirty || since_draw >= FACE_FLOOR_MS) {
            s_drawn_lean = s_lean;
            /* Where we are in the running action, and what face it wears. An action that has
               run out returns to ACT_NONE, whose face is happy — so the robot always settles
               rather than holding the last frame of a sneeze forever. */
            float p = 0.0f;
            if (action != ACT_NONE) {
                const int dur = rig_spec(action)->dur_ms;
                const uint32_t el = now - action_start;
                if (dur <= 0 || (int)el >= dur) {
                    action = ACT_NONE;
                } else {
                    p = (float)el / (float)dur;
                }
            }
            /* The face follows the conversation when there is one: attentive while it is
               listening, bewildered when it has nothing to say. A pet that keeps grinning
               through a failure is a pet that looks like it did not notice. */
            face_params_t target;
            emotion_resolve(s_talk == TALK_LISTENING  ? FACE_CURIOUS
                            : s_talk == TALK_FAILED   ? FACE_BEWILDERED
                                                      : rig_spec(action)->face,
                            &target);
            /* TWICE PER FRAME, ON PURPOSE. `emotion_approach` halves per FRAME, and it was
               written against a 50 fps surface; this panel renders at 25. Applying it once
               here would make every emotion arrive at half its intended speed, and speed is
               part of the emotion — sleepy and excited differ by `rate` alone. Two steps of a
               20 ms tween is exactly one 40 ms frame. */
            emotion_approach_face(&st.eyes, &target, target.rate);
            emotion_approach_face(&st.eyes, &target, target.rate);
            rig_for(action, p, action_mag, now, &st.rig);
            rig_figure(action, p, action_mag, now, st.eyes.face_ang, &st.fig);
            st.bob = bob_step(frame++);
            st.lean = s_lean;
            st.open = s_open;
            st.startle = s_flinch;
            /* THE MOUTH, WHILE THERE IS SOUND COMING OUT OF IT.
             *
             * TWO FREQUENCIES, NOT ONE, for the reason the blink is jittered: a mouth opening
             * and closing on a single sine is a metronome, and the regularity is exactly what
             * gives away a machine. 6.3 Hz carries the syllable rate and 2.7 Hz the phrase,
             * and the product never quite repeats — so it reads as speech rather than as a
             * hinge. Never fully shut while talking (the 0.25 floor), because a beak that
             * closes completely between syllables reads as chewing.
             *
             * NOT DRIVEN BY THE ACTUAL AUDIO, and that is a deliberate limit rather than an
             * oversight: `audio.c` deafens the microphone whenever the speaker runs, so the
             * one signal that could give a real envelope is the one signal this panel throws
             * away on purpose (it would otherwise transcribe itself). An honest fake at the
             * right rate beats a real envelope the hardware cannot supply. */
            if (speaking) {
                const float t = (float)now * 0.001f;
                const float syll = sinf(t * 6.3f), phrase = sinf(t * 2.7f + 1.1f);
                float open = 0.55f + 0.30f * syll + 0.15f * phrase;
                if (open < 0.25f) open = 0.25f;
                if (open > 1.0f) open = 1.0f;
                st.talk = open;
                dirty = true; /* a mouth redrawn five times a second is a glitch, not speech */
            } else {
                st.talk = 0.0f;
            }
            /* The poke recoil rides on top of whatever the action is already doing. */
            st.fig.oy += FLINCH_DIP * s_flinch;
            PHASE(6);
            face_draw(fb, colour, &st);
            PHASE(7);
            const bool side = (s_quarter == 1 || s_quarter == 3);
            /* EVERYTHING OVERLAID HAS TO LAND IN THE SQUARE TOO, and both of these sat
               outside it: the version label at y=6 is above the square, the caption is
               anchored to the bottom of the frame and is below it. A quarter turn simply
               would not carry them, so the first thing lost on a side-mounted panel would
               have been the caption — the one piece of feedback that says a command was
               heard. Both take the square's bounds instead of the frame's. */
            const int over_y0 = side ? SQ_Y0 : 0;
            const int over_h = side ? SQ_Y0 + SQ : FACE_H;
            font_draw(fb, FACE_W, FACE_H, LABEL_X, over_y0 + LABEL_Y, LABEL_SCALE,
                      ota_running_version(), LABEL_COLOUR);
            draw_meter(fb, level);
            caption_draw(&cap, fb, FACE_W, over_h, CAPTION_COLOUR);
            if (s_talk == TALK_LISTENING) draw_listening(fb, over_y0, now);
            else if (s_talk != TALK_IDLE) {
                draw_thinking(fb, over_y0, over_h - over_y0, now, s_talk == TALK_FAILED);
            }
            PHASE(8);
            if (s_upside_down) flip_frame(fb);
            /* In FRAME coordinates (`panel_to_frame`), and after the flip: upside down that
               mapping is the identity precisely because `flip_frame` has already run, and on
               the side there is no flip to be after. Rides the flinch, so it fades with the
               recoil instead of leaving a dot on the glass. */
            if (s_flinch > 0.25f && s_fig_x >= 0) {
                for (int dy = -9; dy <= 9; dy++) {
                    for (int dx = -9; dx <= 9; dx++) {
                        const int d = dx * dx + dy * dy;
                        if (d > 81 || d < 36) continue;
                        const int px2 = s_fig_x + dx, py2 = s_fig_y + dy;
                        if (px2 < 0 || px2 >= FACE_W || py2 < 0 || py2 >= FACE_H) continue;
                        fb[py2 * FACE_W + px2] = CUE_COLOUR;
                    }
                }
            }
            if (cue > 0.0f) {
                /* Grows left to right across the top edge, full width at the moment it
                   reboots. Drawn into the frame rather than flashed separately so it cannot
                   outlive the finger.
                 *
                 * `over_y0`, like the label and the caption above — rows 0..3 are outside the
                 * square a quarter turn carries, so on a side-mounted panel this bar and the
                 * tap pips below it were simply never blitted. A maintenance gesture with no
                 * feedback is one an owner cannot tell from a dead panel, which is the exact
                 * thing `gesture.h` added the pips to prevent. */
                int w = (int)(FACE_W * cue);
                if (w > FACE_W) w = FACE_W;
                for (int y = over_y0; y < over_y0 + 4; y++) {
                    for (int x = 0; x < w; x++) fb[y * FACE_W + x] = CUE_COLOUR;
                }
            } else if (gest.taps > 0) {
                /* One pip per counted tap. Without it the three taps are invisible until the
                   hold succeeds, and a gesture with no feedback until it works is one an
                   owner cannot tell from a broken panel. They clear themselves half a second
                   after the rhythm lapses, so ordinary play leaves nothing on screen. */
                for (int i = 0; i < gest.taps && i < GESTURE_TAPS_MAX; i++) {
                    const int x0 = i * (PIP_W + PIP_GAP);
                    for (int y = over_y0; y < over_y0 + 4; y++) {
                        for (int x = x0; x < x0 + PIP_W && x < FACE_W; x++) {
                            fb[y * FACE_W + x] = CUE_COLOUR;
                        }
                    }
                }
            }
            /* A stripe loop, not one full-window write — see `blit_frame()` for which of
               the three ways to get the frame out of PSRAM actually survives contention. */
            PHASE(9);
            const esp_err_t err = blit_frame(fb);
            if (err != ESP_OK) {
                /* ONCE, THEN EVERY HUNDREDTH, AND ALWAYS WITH THE HEAP. A frame fails 25
                   times a second, so logging each one buried the boot in 150 identical
                   lines and said nothing about WHY — the failure is nearly always
                   ESP_ERR_NO_MEM for a DMA buffer, and the number that explains it is the
                   largest free INTERNAL block, which `mem_log` prints. Diagnosing 0.2.38
                   needed a host toolchain because the panel would not say this itself. */
                if (s_blit_fails++ % 100 == 0) {
                    ESP_LOGE(TAG, "blit: %s (failure %d)", esp_err_to_name(err),
                             s_blit_fails);
                    mem_log("blit-fail");
                }
                s_blit_ok = 0;
                /* SELF-HEAL, BECAUSE THE OWNER CANNOT REACH THIS PANEL.
                 *
                 * A DMA underflow leaves the SPI driver holding transactions it never
                 * recycles, and from then on EVERY call returns ESP_ERR_INVALID_STATE —
                 * permanently, until someone power-cycles the unit. 0.2.46 removes the cause,
                 * but "the display can enter a state only a human with hands on the hardware
                 * can leave" is a property worth removing on its own: this panel lives in a
                 * child's bedroom and its owner has no terminal.
                 *
                 * A reboot costs about four seconds of colour bars and is recoverable. A
                 * black screen is not. The uptime guard is what keeps that trade honest — a
                 * fault present from boot would otherwise cycle forever, and a reboot loop is
                 * no better than a freeze. Past the guard, a panel that cannot draw for ten
                 * seconds has nothing to lose by starting over. */
                if (now > BLIT_HEAL_AFTER_MS && s_blit_fails >= BLIT_HEAL_FAILS) {
                    ESP_LOGE(TAG, "%d consecutive blit failures — restarting", s_blit_fails);
                    mem_log("blit-heal");
                    vTaskDelay(pdMS_TO_TICKS(150)); /* let the log drain */
                    esp_restart();
                }
            } else {
                if (s_blit_fails > 0) {
                    ESP_LOGI(TAG, "blit recovered after %d failures", s_blit_fails);
                    s_blit_fails = 0;
                }
                s_blit_ok++;
            }
            since_draw = 0;
        }
        /* BOTH REBOOTS LEAVE FROM HERE, and that is the point. This is the one place in the
           firmware where a frame has just finished and nothing is in flight on the QSPI bus,
           which is the difference between a panel that comes back and one the owner has to
           power-cycle by hand (`display.h`). The gesture has always left from here; the OTA
           used to restart from its own task, mid-transfer. */
        if (rebooting || s_restart_pending) {
            ESP_LOGW(TAG, "%s — parking the renderer and restarting",
                     rebooting ? "reboot gesture completed" : "restart requested");
            vTaskDelay(pdMS_TO_TICKS(150));
            esp_restart();
        }

        if (s_brightness_pending) {
            s_brightness_pending = false;
            PHASE(14);
            apply_brightness();
        }
        if (since_reassert >= REASSERT_MS) {
            PHASE(12);
            reassert_panel();
            since_reassert = 0;
        }
        if (since_sample >= PMU_SAMPLE_MS) {
            PHASE(13);
            pmu_sample();
            since_sample = 0;
        }
        /* THE CAPTURE USED TO BE THIS LOOP'S CLOCK, and it is not any more: `audio.c` owns
           the codec on its own task (see its header — two tasks on one `esp_codec_dev` handle
           is the race that cost a panic). So the pacing is an honest delay, and the level is
           whatever the audio task last measured. */
        PHASE(10);
        vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
        if (sound) {
            level = audio_level();
            if (level > s_mic_peak) s_mic_peak = level;
            /* Every frame, not every face: 8.8 KB against 322 KB is what makes the one part
               of the screen with something new to say affordable at this rate. */
            PHASE(11);
            blit_meter(level);
        }
        /* A RENDER HEARTBEAT, BECAUSE A STOPPED RENDER TASK LOOKED EXACTLY LIKE A QUIET ONE.
           0.2.44 logged one blit failure and then nothing — and the nothing WAS the symptom:
           the loop had stopped attempting blits, so the every-hundredth rate limit never
           fired again and the panel sat frozen behind a clean-looking log. It took a photo
           from the owner to find out. Anything that can stop has to say so on a timer, and
           the largest free internal block comes along because it is the number that has
           explained this fault twice. */
        since_beat += TOUCH_POLL_MS;
        if (since_beat >= BEAT_MS) {
            since_beat = 0;
            ESP_LOGI(TAG, "render: %d frames ok, %d failed | internal largest %u",
                     s_blit_ok, s_blit_fails,
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                                MALLOC_CAP_DMA));
        }
        since_draw += TOUCH_POLL_MS;
        since_reassert += TOUCH_POLL_MS;
        since_sample += TOUCH_POLL_MS;
    }
}

void display_run_face(void)
{
    if (s_panel == NULL) {
        ESP_LOGE(TAG, "no panel; not starting the face");
        return;
    }
    /* Its own task so a frame rate can never delay an OTA check — the update path outranks
       the picture, always. */
    /* 8192, raised from 4096 on the theory that the field panic was an overflowing stack.
       It was not: `stack_free` came back 5532, so the deepest use was ~2.7 KB and it was never
       close even at 4096. The size stays — this task runs audio capture, an IMU read, font
       rendering, PMU sampling, float tweening and two LCD blits per frame, and the headroom is
       cheap — but the number that matters is the one it reports, not the one it was given. */
    xTaskCreate(face_task, "face", 8192, NULL, 4, NULL);
}
