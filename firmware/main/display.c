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

#include <stdlib.h>

#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_panel_io.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_ops.h"
#include "audio.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "face.h"
#include "font.h"
#include "ota.h"
#include "pmu.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "imu.h"
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

static void fill_stripe(bool reversed)
{
    static const uint16_t bars[8] = {
        0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000,
    };
    for (int y = 0; y < STRIPE_ROWS; y++) {
        for (int x = 0; x < LCD_H_RES; x++) {
            const int i = (x * 8) / LCD_H_RES;
            stripe[y * LCD_H_RES + x] =
                SPI_SWAP_DATA_TX(bars[reversed ? 7 - i : i], LCD_BPP);
        }
    }
}

/* Kept so a repaint needs no second bring-up. */
static esp_lcd_panel_handle_t s_panel;
/* Kept so the controller can be ASKED what it thinks its state is — see probe_panel(). */
static esp_lcd_panel_io_handle_t s_io;
static bool s_swap;

static bool paint(void)
{
    if (s_panel == NULL) return false;
    fill_stripe(s_swap);
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

bool display_repaint(void)
{
    s_swap = !s_swap;
    const bool ok = paint();
    /* SAYS ONLY THAT THE BUS ACCEPTED IT. The panel went dark once while the firmware kept
       running and polling on schedule, and the two candidates need opposite fixes: the
       controller dropping display-on (a repaint revives it) versus the AXP2101 cutting the
       display rail (a repaint writes happily into the dark). This line distinguishes them
       only in combination with someone looking at the screen — which is the honest state of
       this question until the PMU is read. */
    ESP_LOGI(TAG, "repaint %s (%s)", ok ? "ok" : "FAILED", s_swap ? "inverted" : "normal");
    return ok;
}

bool display_start(void)
{
    i2c_bus_scan();
    /* Before the first sample, so the history is what preceded the restart rather than a
       mixture of then and now. */
    if (pmu_start()) pmu_report_history();
    const bool v2 = is_v2_board();
    ESP_LOGI(TAG, "board revision: %s", v2 ? "V2 (CO5300/CST820)" : "V1 (SH8601/FT3168)");

    const spi_bus_config_t bus = CO5300_PANEL_BUS_QSPI_CONFIG(
        LCD_PCLK, LCD_D0, LCD_D1, LCD_D2, LCD_D3, sizeof(stripe));
    esp_err_t err = spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi bus: %s", esp_err_to_name(err));
        return false;
    }

    esp_lcd_panel_io_handle_t io = NULL;
    const esp_lcd_panel_io_spi_config_t io_cfg = CO5300_PANEL_IO_QSPI_CONFIG(LCD_CS, NULL, NULL);
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
static uint8_t s_brightness = BRIGHTNESS_DEFAULT;

void display_set_brightness(int level)
{
    if (level < 0 || level > 255) return;
    s_brightness = (uint8_t)level;
    if (s_io != NULL) esp_lcd_panel_io_tx_param(s_io, 0x51, &s_brightness, 1);
}

/* THE VERSION, ON THE GLASS. The only other ways to know what a panel is running are to ask
   the box what it last SERVED — a different question — or to cable it up and read its
   console, which resets it. Top-left: the head spans x 76..292 and starts at y 60, so this
   corner is the one piece of the panel the robot never occupies. Dim on purpose; it shares a
   bedroom. */
#define LABEL_X 8
#define LABEL_Y 6
#define LABEL_SCALE 2
#define SWAP16(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))
#define LABEL_COLOUR SWAP16(0x8410) /* mid grey */
#define CUE_COLOUR SWAP16(0xFD20)   /* amber, and meant to be noticed */

/* A HOLD LONG ENOUGH TO MEAN IT. Five seconds is not arbitrary: 4-5 year olds were measured
   producing ORDINARY taps lasting up to 4.2 s, so five is the first threshold outside a
   child's accidental press at all. The margin is 0.8 s, which is exactly why the hold is not
   silent — from 1.5 s an amber bar grows across the top edge, full width at the moment it
   reboots, in time to let go. A reboot IS the firmware re-check, because the OTA loop asks
   the box before its first sleep (main.c). */
#define HOLD_REBOOT_MS 5000
#define HOLD_CUE_MS 1500

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
#define MIC_CHUNK (AUDIO_RATE * TOUCH_POLL_MS / 1000)
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
static DMA_ATTR uint16_t s_strip[METER_W * METER_SPAN];

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

   ±60 px is what the composition allows: the head is 216 px on a 368 px panel, so there is
   76 px of slack each side and this keeps a margin rather than pressing him against the edge.

   THE SIGN DOES NEED A CASE WHEN INVERTED, and the argument that it does not was wrong in a
   way worth keeping. It claimed two negations cancel: the panel's rotation negates `ay`, and
   `flip_frame` negates the drawn offset. The second one is not a negation the VIEWER sees.
   `flip_frame` reverses the framebuffer and the panel is then physically rotated 180° in the
   viewer's hands — those two cancel each other, so the viewer reads framebuffer coordinates
   directly in both orientations. Only the accelerometer's sign actually flips, leaving the
   lean correct in one orientation and backwards in the other, which is exactly what the owner
   saw. So the tilt is taken in viewer terms explicitly. */
#define LEAN_MAX 60
/* A little over a quarter of a gravity reaches full lean: tilting a panel that far is a
   deliberate act, and anything gentler stays proportional rather than pinned. */
#define LEAN_FULL 2400
/* Smoothed, because the accelerometer is noisy at rest and a figure that twitches while the
   panel sits still reads as broken rather than alive. */
#define LEAN_SMOOTH 4
static int s_lean;

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
    /* Viewer-relative: the chip turns over with the panel, the rendered image does not. */
    const int tilt = s_upside_down ? ay : -ay;
    int target = tilt * LEAN_MAX / LEAN_FULL;
    if (target > LEAN_MAX) target = LEAN_MAX;
    if (target < -LEAN_MAX) target = -LEAN_MAX;
    s_lean += (target - s_lean) / LEAN_SMOOTH;

    const bool was = s_upside_down;
    if (ax < -FLIP_THRESHOLD) s_upside_down = true;
    else if (ax > FLIP_THRESHOLD) s_upside_down = false;
    if (was != s_upside_down) {
        ESP_LOGI(TAG, "orientation: %s (ax=%d ay=%d az=%d)",
                 s_upside_down ? "upside down" : "upright", ax, ay, az);
    }
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
    if (level >= s_shown) {
        s_shown = level;
    } else {
        s_shown -= METER_DECAY;
        if (s_shown < level) s_shown = level;
    }
    int h = s_shown * METER_SPAN / METER_FULL;
    if (h > METER_SPAN) h = METER_SPAN;
    if (h < 0) h = 0;
    for (int row = 0; row < METER_SPAN; row++) {
        const uint16_t c = (row >= METER_SPAN - h) ? METER_COLOUR : 0;
        for (int col = 0; col < METER_W; col++) s_strip[row * METER_W + col] = c;
    }
    int x0 = METER_X, y0 = METER_TOP;
    if (s_upside_down) {
        /* The strip reverses and the window moves to the opposite corner, so the bar stays on
           the viewer's left rather than travelling to the other side of the screen. */
        for (int i = 0, j = METER_W * METER_SPAN - 1; i < j; i++, j--) {
            const uint16_t t = s_strip[i];
            s_strip[i] = s_strip[j];
            s_strip[j] = t;
        }
        x0 = FACE_W - METER_X - METER_W;
        y0 = FACE_H - METER_BOTTOM;
    }
    esp_lcd_panel_draw_bitmap(s_panel, x0, y0, x0 + METER_W, y0 + METER_SPAN, s_strip);
}

#define BOB_PX 5
#define FACE_FLOOR_MS 200
#define TOUCH_POLL_MS 40

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

int display_mic_peak(void)
{
    const int p = s_mic_peak;
    s_mic_peak = 0;
    return p;
}

static void draw_meter(uint16_t *fb, int level)
{
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
    const esp_err_t br = esp_lcd_panel_io_tx_param(s_io, 0x51, &s_brightness, 1);
    if (on != ESP_OK || br != ESP_OK) {
        ESP_LOGW(TAG, "re-assert failed (0x29 %s, 0x51 %s)", esp_err_to_name(on),
                 esp_err_to_name(br));
    }
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
    /* Silence is a failure mode with no symptom, so it is logged rather than inferred: a
       beep that never comes could be the codec, the amplifier pin, the volume, or a tap
       that was never registered, and only the first of those is visible from here. */
    const bool sound = audio_start();
    if (!sound) ESP_LOGW(TAG, "no codec — taps will be silent");
    int colour = 0;
    int frame = 0;
    int since_draw = FACE_FLOOR_MS; /* draw immediately */
    float s_open = 1.0f;
    int s_drawn_lean = 0;
    int since_reassert = 0;
    int since_sample = 0;
    int level = 0;
    /* Internal RAM, not PSRAM: this is an I2S DMA destination on every frame. */
    int16_t *mic = malloc(MIC_CHUNK * sizeof(int16_t));
    if (mic == NULL) ESP_LOGW(TAG, "no mic buffer — the meter will stay empty");
    int held = 0;

    while (true) {
        bool dirty = false;
        if (touch && touch_tapped()) {
            colour = (colour + 1) % face_colour_count();
            ESP_LOGI(TAG, "tap -> colour %d", colour);
            s_flinch = 1.0f;
            /* Before the repaint, not after: the beep is ~90 ms and a full frame is ~330 KB
               over QSPI, and the tap feels answered by whichever lands first. */
            if (sound) audio_beep();
            dirty = true;
        }
        update_orientation();
        s_open = blink_open(TOUCH_POLL_MS);
        s_flinch *= FLINCH_DECAY;
        if (s_flinch < 0.02f) s_flinch = 0.0f;
        /* Animating means every poll is a frame. A blink at the 200 ms idle floor would be one
           frame long and read as a glitch; the floor is for a face that is holding still. */
        if (s_flinch > 0.0f || s_open < 1.0f) dirty = true;
        /* A moved figure is a new frame, so tilting redraws at the poll rate rather than
           waiting out the idle floor — but only once it has moved enough to see, or every
           frame would be a full 322 KB blit for a pixel of accelerometer noise. */
        if (s_lean - s_drawn_lean > 2 || s_drawn_lean - s_lean > 2) dirty = true;

        if (touch && touch_is_down()) {
            held += TOUCH_POLL_MS;
            if (held >= HOLD_CUE_MS) dirty = true; /* keep the cue growing under the finger */
        } else if (held != 0) {
            held = 0;
            dirty = true; /* and clear it the frame after it lifts */
        }
        /* Decided before the draw, acted on after it: the frame carrying a full-width cue has
           to reach the glass first, or a reboot is indistinguishable from the fault we are
           chasing. */
        const bool rebooting = held >= HOLD_REBOOT_MS;

        if (dirty || since_draw >= FACE_FLOOR_MS) {
            s_drawn_lean = s_lean;
            const face_state_t st = {
                .bob = bob_step(frame++),
                .lean = s_lean,
                .dip = (int)(FLINCH_DIP * s_flinch),
                .open = s_open,
                .startle = s_flinch,
            };
            face_draw(fb, colour, &st);
            font_draw(fb, FACE_W, FACE_H, LABEL_X, LABEL_Y, LABEL_SCALE,
                      ota_running_version(), LABEL_COLOUR);
            draw_meter(fb, level);
            if (s_upside_down) flip_frame(fb);
            if (held >= HOLD_CUE_MS) {
                /* Grows left to right across the top edge, full width at the moment it
                   reboots. Drawn into the frame rather than flashed separately so it cannot
                   outlive the finger. */
                int w = FACE_W * held / HOLD_REBOOT_MS;
                if (w > FACE_W) w = FACE_W;
                for (int y = 0; y < 4; y++) {
                    for (int x = 0; x < w; x++) fb[y * FACE_W + x] = CUE_COLOUR;
                }
            }
            /* One call for the whole frame: the panel takes a full-window write happily and
               it is simpler to be right about than a stripe loop. */
            const esp_err_t err =
                esp_lcd_panel_draw_bitmap(s_panel, 0, 0, FACE_W, FACE_H, fb);
            if (err != ESP_OK) ESP_LOGE(TAG, "blit: %s", esp_err_to_name(err));
            since_draw = 0;
        }
        if (rebooting) {
            ESP_LOGW(TAG, "held %d ms — rebooting to re-check firmware", held);
            vTaskDelay(pdMS_TO_TICKS(150));
            esp_restart();
        }

        if (since_reassert >= REASSERT_MS) {
            reassert_panel();
            since_reassert = 0;
        }
        if (since_sample >= PMU_SAMPLE_MS) {
            pmu_sample();
            since_sample = 0;
        }
        /* The capture is the clock when it is available: one frame's worth, which blocks for
           about TOUCH_POLL_MS and drains the DMA at exactly the rate it fills. */
        if (sound && mic != NULL && audio_record(mic, MIC_CHUNK)) {
            level = audio_peak(mic, MIC_CHUNK);
            if (level > s_mic_peak) s_mic_peak = level;
            /* Every capture, not every face: 8.8 KB against 322 KB is what makes 25 fps
               affordable for the one part of the screen that has something new to say. */
            blit_meter(level);
        } else {
            vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
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
    xTaskCreate(face_task, "face", 4096, NULL, 4, NULL);
}
