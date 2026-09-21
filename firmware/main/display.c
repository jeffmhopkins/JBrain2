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
#include "calib.h"
#include "cfg.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "face.h"
#include "font.h"
#include "gesture.h"
#include "rig.h"
#include "variants.h"
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
static volatile uint8_t s_brightness = BRIGHTNESS_DEFAULT;
static volatile bool s_brightness_pending;

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
#define LABEL_X 8
#define LABEL_Y 6
#define LABEL_SCALE 2
#define SWAP16(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))
#define LABEL_COLOUR SWAP16(0x8410) /* mid grey */
#define CUE_COLOUR SWAP16(0xFD20)   /* amber, and meant to be noticed */

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
    int colour = 0;
    int frame = 0;
    int since_draw = FACE_FLOOR_MS; /* draw immediately */
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
        if (tapped) {
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
            s_tap_zone = (int)face_zone(s_tap_x, s_tap_y, s_upside_down, s_lean);
            const pool_t pool = ZONE_POOL[s_tap_zone];
            action = (action_t)variants_pick(pool, &mem[pool], now, esp_random());
            action_mag = variants_penalty(pool, &mem[pool], now);
            action_start = now;
            ESP_LOGI(TAG, "tap (%d,%d) zone %d -> colour %d, action %d, mag %.2f", s_tap_x,
                     s_tap_y, s_tap_zone, colour, (int)action, (double)action_mag);
            /* Before the repaint, not after: the beep is ~90 ms and a full frame is ~330 KB
               over QSPI, and the tap feels answered by whichever lands first. */
            PHASE(3);
            if (sound) audio_beep();
            dirty = true;
        }
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
            const esp_err_t cerr = esp_lcd_panel_draw_bitmap(s_panel, 0, 0, FACE_W, FACE_H, fb);
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
        const bool rebooting = act == GESTURE_REBOOT;
        if (act == GESTURE_CALIBRATE) cal_begin();
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
            face_params_t target;
            emotion_resolve(rig_spec(action)->face, &target);
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
            /* The poke recoil rides on top of whatever the action is already doing. */
            st.fig.oy += FLINCH_DIP * s_flinch;
            PHASE(6);
            face_draw(fb, colour, &st);
            PHASE(7);
            font_draw(fb, FACE_W, FACE_H, LABEL_X, LABEL_Y, LABEL_SCALE,
                      ota_running_version(), LABEL_COLOUR);
            draw_meter(fb, level);
            PHASE(8);
            if (s_upside_down) flip_frame(fb);
            /* After the flip, because the finger is in PANEL coordinates and the flip has
               already turned the figure the other way up. Rides the flinch, so it fades with
               the recoil instead of leaving a dot on the glass. */
            if (s_flinch > 0.25f && s_tap_x >= 0) {
                for (int dy = -9; dy <= 9; dy++) {
                    for (int dx = -9; dx <= 9; dx++) {
                        const int d = dx * dx + dy * dy;
                        if (d > 81 || d < 36) continue;
                        const int px2 = s_tap_x + dx, py2 = s_tap_y + dy;
                        if (px2 < 0 || px2 >= FACE_W || py2 < 0 || py2 >= FACE_H) continue;
                        fb[py2 * FACE_W + px2] = CUE_COLOUR;
                    }
                }
            }
            if (cue > 0.0f) {
                /* Grows left to right across the top edge, full width at the moment it
                   reboots. Drawn into the frame rather than flashed separately so it cannot
                   outlive the finger. */
                int w = (int)(FACE_W * cue);
                if (w > FACE_W) w = FACE_W;
                for (int y = 0; y < 4; y++) {
                    for (int x = 0; x < w; x++) fb[y * FACE_W + x] = CUE_COLOUR;
                }
            } else if (gest.taps > 0) {
                /* One pip per counted tap. Without it the three taps are invisible until the
                   hold succeeds, and a gesture with no feedback until it works is one an
                   owner cannot tell from a broken panel. They clear themselves half a second
                   after the rhythm lapses, so ordinary play leaves nothing on screen. */
                for (int i = 0; i < gest.taps && i < GESTURE_TAPS_MAX; i++) {
                    const int x0 = i * (PIP_W + PIP_GAP);
                    for (int y = 0; y < 4; y++) {
                        for (int x = x0; x < x0 + PIP_W && x < FACE_W; x++) {
                            fb[y * FACE_W + x] = CUE_COLOUR;
                        }
                    }
                }
            }
            /* One call for the whole frame: the panel takes a full-window write happily and
               it is simpler to be right about than a stripe loop. */
            PHASE(9);
            const esp_err_t err =
                esp_lcd_panel_draw_bitmap(s_panel, 0, 0, FACE_W, FACE_H, fb);
            if (err != ESP_OK) ESP_LOGE(TAG, "blit: %s", esp_err_to_name(err));
            since_draw = 0;
        }
        if (rebooting) {
            ESP_LOGW(TAG, "reboot gesture completed — re-checking firmware");
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
