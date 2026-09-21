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
#include "esp_system.h"
#include "face.h"
#include "font.h"
#include "ota.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"
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
/* ASK THE CONTROLLER, STOP GUESSING.
 *
 * Two hypotheses have now been built from the symptom alone and both were wrong: that the
 * panel needs continuous writes (0.2.7 wrote every 500 ms and went dark), and that it needs
 * consecutive frames to DIFFER (0.2.9 bobs every frame and went dark). A third guess is not
 * worth an OTA cycle. The CO5300 can be read, and RDDPM (0x0A) answers the question that has
 * been inferred past since §10.4n: does the controller still believe the display is on?
 *
 *   display bit set, brightness high, screen dark -> not the controller. The rail to the
 *     OLED, or the AXP2101 this firmware has never spoken to.
 *   display bit CLEAR -> the controller dropped display-on, and re-issuing 0x29 is the fix.
 *   idle-mode bit SET -> the part has an idle mode nobody asked for, which would explain
 *     every observation including why a tap helps.
 *   the read itself fails -> the panel is not answering at all, which is its own answer.
 *
 * Read-only, and it gives up after a few failures rather than logging every ten seconds
 * forever: this is an instrument, and an instrument that floods the console is one more
 * thing hiding the evidence.
 */
#define PROBE_MS 10000
#define PROBE_GIVE_UP 3

/* THE VERSION, ON THE GLASS. Until now the only way to know what a panel was running was to
   ask the box what it last served, or to cable it up and read its console — and both have
   been wrong at least once tonight. Top-left: the head spans x 76..292 and starts at y 60, so
   this corner is the one piece of the panel the robot never occupies. Dim on purpose; it
   shares a bedroom. */
#define LABEL_X 8
#define LABEL_Y 6
#define LABEL_SCALE 2
#define SWAP16(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))
#define LABEL_COLOUR SWAP16(0x8410) /* mid grey */
#define CUE_COLOUR SWAP16(0xFD20)   /* amber, and meant to be noticed */

/* A HOLD LONG ENOUGH TO MEAN IT. The owner asked for five seconds, and the number is not
   arbitrary: §10.4p measured 4-5 year olds producing ORDINARY taps lasting up to 4.2 s, so
   five is the first threshold that sits outside a child's accidental press at all. It is a
   thin margin — 0.8 s — which is exactly why the cue below exists rather than a silent
   count: a hold that is about to reboot the panel says so, in time to let go.
   A reboot IS the firmware re-check, because the OTA loop asks the box before its first
   sleep (main.c), so this doubles as "go and get the update now". */
#define HOLD_REBOOT_MS 5000
#define HOLD_CUE_MS 1500

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

static void probe_panel(void)
{
    static int failures;
    if (s_io == NULL || failures >= PROBE_GIVE_UP) return;

    uint8_t pm = 0, bv = 0;
    const esp_err_t e1 = esp_lcd_panel_io_rx_param(s_io, 0x0A, &pm, 1);
    const esp_err_t e2 = esp_lcd_panel_io_rx_param(s_io, 0x52, &bv, 1);
    if (e1 != ESP_OK || e2 != ESP_OK) {
        failures++;
        ESP_LOGW(TAG, "panel read failed (0x0A %s, 0x52 %s)%s", esp_err_to_name(e1),
                 esp_err_to_name(e2), failures >= PROBE_GIVE_UP ? " — giving up" : "");
        return;
    }
    failures = 0;
    /* MIPI DCS RDDPM: D7 booster, D6 idle, D5 partial, D4 sleep-out, D3 normal, D2 display. */
    ESP_LOGI(TAG, "panel 0x0A=0x%02x [display %s, sleep %s, idle %s, booster %s] 0x52=0x%02x",
             pm, (pm & 0x04) ? "ON" : "OFF", (pm & 0x10) ? "OUT" : "IN",
             (pm & 0x40) ? "ON" : "off", (pm & 0x80) ? "on" : "OFF", bv);
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
    int since_probe = 0;
    int held = 0;

    while (true) {
        bool dirty = false;
        if (touch && touch_tapped()) {
            colour = (colour + 1) % face_colour_count();
            ESP_LOGI(TAG, "tap -> colour %d", colour);
            /* Before the repaint, not after: the beep is ~90 ms and a full frame is ~330 KB
               over QSPI, and the tap feels answered by whichever lands first. */
            if (sound) audio_beep();
            dirty = true;
        }
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
            face_draw(fb, colour, bob_step(frame++));
            font_draw(fb, FACE_W, FACE_H, LABEL_X, LABEL_Y, LABEL_SCALE,
                      ota_running_version(), LABEL_COLOUR);
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
        if (since_probe >= PROBE_MS) {
            probe_panel();
            since_probe = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
        since_draw += TOUCH_POLL_MS;
        since_probe += TOUCH_POLL_MS;
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
