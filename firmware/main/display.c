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

#include "driver/gpio.h"
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
#include "confirm.h"
#include "cfg.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "face.h"
#include "font.h"
#include "gesture.h"
#include "jpanel.h"
#include "rig.h"
#include "speech.h"
#include "vocab.h"
#include "variants.h"
#include "orient.h"
#include "screen.h"
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
/* Whether the hardware reset above actually landed. Reported, because "the panel came up" and
   "the panel came up after a real reset" are different facts and only one of them is evidence
   about the black screen. */
static bool s_panel_reset;

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
    /* AND THEN PULL THE PANEL'S RESET, which this firmware has never done. `esp_restart()`
       leaves the CO5300 powered and holding whatever state it was in, so a controller stopped
       mid memory-write swallows the init sequence below as pixel data and the screen stays
       black — the software reset included, because it goes down the same bus. Only the
       hardware line can reach it, and it hangs off the expander `pmu.c` already talks to.
       Best-effort: if the expander does not answer, the bring-up below is exactly what it has
       always been. */
    s_panel_reset = pmu_reset_panel();
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
/* How dim the first sleep stage is, as a percentage of the configured brightness.
   The box's answer; the shipped default reproduces the old fixed quarter exactly. */
static volatile int s_dim_percent = SCREEN_DIM_PERCENT_DEFAULT;
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
 * boot and every three seconds, and the box serves a brightness unconditionally — so every
 * boot issued an 0x51 from the main task into an io handle the face task was driving at
 * ~25 fps. A panic within a minute of boot is exactly the shape that produces.
 *
 * So setting the brightness now only records it. The face task applies it, next to the
 * re-assert that was already the only other command writer. Anything else that wants to talk
 * to this panel belongs on that task too. */
void display_set_brightness(int level)
{
    if (level < 0 || level > 255) return;
    /* ON CHANGE, like the dim percentage below. `apply_settings` asks every three seconds now
       rather than every fifteen minutes, and the box serves a brightness unconditionally, so
       an unguarded assignment raised `s_brightness_pending` twenty times a minute forever —
       twenty backlight register writes for a value that had not moved. Harmless on the glass, because
       `apply_brightness` recomputes through `screen_level` and so re-asserts the DIMMED level
       while dim rather than waking the screen; pointless all the same. */
    if ((uint8_t)level == s_brightness) return;
    s_brightness = (uint8_t)level;
    s_brightness_pending = true;
}

/* WHICH BODY THE PANEL COMES BACK AS. Four taps and a hold still toggles it live; this is the
   answer it starts from, and until the box could hold one it was always the ostrich — so every
   reboot and every OTA quietly undid a child who had chosen the robot.
   Deferred to the render task like the brightness above, and for a weaker version of the same
   reason: `st` belongs to that task, and writing a field of it from `apply_settings()` would be
   a cross-task write into a struct being tweened at ~25 fps. */
static volatile int s_form_box = -1;     /* what the box last SAID, not what is on screen */
static volatile int s_form_pending = -1; /* a change for the render task to take */

/* TAKEN ON CHANGE, NOT ON EVERY FETCH, and the difference is a child's afternoon. `apply_settings`
   runs every three seconds and the box serves a form unconditionally, so acting on each answer
   would re-assert the owner's choice twenty times a minute — Elora switches to the robot, and the
   panel silently switches her back before she has finished playing with it. Comparing against
   what the box last said means the gesture wins until the OWNER actually changes his mind.
   (This guard was load-bearing at four times an hour; at twenty times a minute it is the only
   thing standing between a gesture and a child who cannot keep the body she picked.) */
void display_set_dim_percent(int percent)
{
    if (percent < 0 || percent > 100) return;
    if (percent == s_dim_percent) return;
    s_dim_percent = percent;
    /* Applied on the next frame rather than here: this is called from the main task and the
       backlight register belongs to the render task — the same hand-off the brightness itself
       uses, and for the same reason a cross-task write used to panic this firmware at boot.
       Pending unconditionally so a change made WHILE dim takes effect without waiting for the
       next stage transition, which could be ten minutes away. */
    s_brightness_pending = true;
}

void display_set_form(int form)
{
    if (form != 0 && form != 1) return;
    if (form == s_form_box) return;
    s_form_box = form;
    s_form_pending = form;
}

/* THE SLEEP ITSELF IS `screen.h` — thresholds, levels and the movement test, all pure
   arithmetic and all host-tested. What lives here is the half that needs the panel: which
   stage we are in, what wakes it, and the frame that does not get drawn. */
static screen_stage_t s_sleep = SCREEN_AWAKE;
/* Set in `update_orientation()` and consumed by the same task a few lines later — the IMU
   read is where these numbers already are, so movement costs no extra bus traffic. */
static bool s_moved;
static int s_move_mag;

/* Only ever called on the render task: the write itself is deferred to the same
   `s_brightness_pending` hand-off the box's setting uses, for the reason above it. */
static void sleep_wake(const char *why)
{
    if (s_sleep == SCREEN_AWAKE) return;
    ESP_LOGI(TAG, "screen: waking (%s)", why);
    s_sleep = SCREEN_AWAKE;
    s_brightness_pending = true;
}

/* The local copy is not a style choice: `esp_lcd_panel_io_tx_param` takes a plain `const
   void *`, and handing it a pointer into volatile storage discards the qualifier. */
static void apply_brightness(void)
{
    if (s_io == NULL) return;
    const uint8_t level = screen_level(s_brightness, s_sleep, s_dim_percent);
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
/* WHOSE PANEL THIS IS, BY DEFAULT — and the version only when asked for.
 *
 * The owner: *"top left where we have the version number, if I touch that it should change
 * between the version number and the panel name. Default to only showing the panel name."*
 *
 * Which is the right default and was not the right default for most of this project's life:
 * the version mattered while every other message was "which build is it on", and it stops
 * mattering the moment there are two panels in two bedrooms and the question is whose. A
 * four-year-old cannot read `0.2.75` and can read their pet's name.
 *
 * The version is one touch away rather than gone, because it is still the first thing anyone
 * debugging this asks for, and telemetry is not in the room with you. */
static bool s_show_version;
/* The name in the font's own alphabet — it has uppercase, digits and a lowercase `v`, so a
   name has to be shouted. Built once; `vocab_name()` is derived from the wake phrase. */
static char s_name_up[24];

/* Rebuilt when the owner renames the pet. The label is drawn from `s_name_up` every frame, so
   this is the whole of it — but it must happen on a task, not in an interrupt, and the render
   task is the only one that reads the buffer. A torn read here costs one frame of a wrong name,
   which is why this is a plain rebuild rather than a hand-off: the alternative is holding a
   second buffer to fix a glitch nobody can see. */
static void build_name(void);

void display_refresh_name(void)
{
    build_name();
}

static void build_name(void)
{
    const char *n = vocab_name();
    if (n == NULL) n = "PET";
    size_t i = 0;
    for (; n[i] != '\0' && i + 1 < sizeof(s_name_up); i++) {
        s_name_up[i] = (n[i] >= 'a' && n[i] <= 'z') ? (char)(n[i] - 'a' + 'A') : n[i];
    }
    s_name_up[i] = '\0';
}
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

/* HANDS-FREE, AND THE WHOLE PROBLEM IS KNOWING WHEN THEY STOPPED.
 *
 * The owner: *"a wake word that will allow the same interaction as if I held the panel and it
 * was listening ... but we just need a way for emptiness at the end to stop it."*
 *
 * A hold has a release. A name does not, so the end of the sentence has to be FOUND. The
 * signal already exists and already runs: `speech.c` sets `s_hearing` from the front end's
 * own `vad_state`, which is what gates MultiNet and drives the indicator. Nothing new is
 * computed here — the recogniser has been deciding "is someone talking" every frame since
 * bring-up and nobody had asked it.
 *
 * Three ways out, and each is a different sentence to a four-year-old:
 *
 *   HUSH   they finished  -> send it. 1.8 s. It was 900 ms, and 900 ms was wrong: the owner,
 *                            after watching them use it — *"the babies keep getting cut off
 *                            because they're a little bit slow."* A four-year-old assembling
 *                            a sentence stops for longer than an adult does, and every one of
 *                            those pauses ended their turn for them. The old number was
 *                            reasoned from the SIX-SECOND cap rather than from a child: a
 *                            longer hush used to risk the cap eating the tail. The cap is ten
 *                            seconds now (`CAPTURE_MAX_MS`) and the box trims the silence
 *                            before whisper sees it, so waiting longer costs nothing at all.
 *   LEAD   they said the name and nothing else -> drop it, silently, back to idle. An
 *                            accidental "hey fish" from the television must not become an
 *                            upload, and this is the branch that stops it.
 *   the cap `audio.c` already enforces -> send what we have rather than truncating to nothing.
 */
#define LISTEN_HUSH_MS 1800
#define LISTEN_LEAD_MS 3000

/* AND THEN IT LISTENS AGAIN, WITHOUT BEING ASKED.
 *
 * The owner: *"after the text-to-speech comes back and finishes talking, we should just turn
 * the microphone on and start recording again, and if I start talking within 2 seconds, just
 * automatically record all that until I stopped talking again and send that as the next turn.
 * That way I can have fluid conversations."*
 *
 * Which is the difference between a toy you operate and one you talk to. The machinery is
 * already here — this is the hands-free listen from `VOCAB_LISTEN` with a different trigger
 * and a shorter lead — so the whole feature is: notice the reply finished, and open the same
 * window the name opens.
 *
 * NO BEEP ON THIS ONE. A tone after every reply is the toy interrupting the conversation it
 * just started; the red indicator is the affordance, and by the second turn a child knows what
 * it means.
 *
 * A CAP, BECAUSE THIS IS A LOOP WITH A LOUDSPEAKER IN IT. Every reply reopens the microphone,
 * and a television talking in the room can therefore hold a conversation with the panel
 * indefinitely — each turn costing whisper, a model and a voice. Six consecutive follow-ups is
 * far more than a four-year-old's exchange and bounds the runaway; after that it wants a
 * deliberate start again, which resets the count. */
#define FOLLOW_LEAD_MS 2000
#define FOLLOW_MAX_TURNS 6

/* VOICE POST ON THE GLASS (`docs/plans/JPANEL_PLAN.md`, W3).
 *
 * RECORDING IS A STATE BESIDE LISTENING, NOT A FLAG ON IT, and the three differences are why:
 * where the audio goes, what is drawn, and what ends it. A flag would have every branch of
 * the machine below asking "but which kind" — and the one that forgot would upload a child's
 * message to the pet, which would answer it out loud.
 *
 * WHAT IT SHARES is everything that was tuned for a four-year-old: the same hush (they stop
 * for longer than an adult does), the same lead, the same cap, and the same finger-cancels
 * rule the owner asked for. A second set of numbers would be a second thing to get wrong.
 *
 * A BLUE DOT, NOT THE RED ONE, and that is the owner's actual requirement rather than a
 * palette choice: red means *the robot is listening to you*, and talking to your sister must
 * not look like that. */
#define RECORD_LEAD_MS 4000
typedef enum {
    TALK_IDLE = 0,
    TALK_LISTENING,
    TALK_RECORDING,
    TALK_THINKING,
    TALK_FAILED
} talk_t;
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
/* The hands-free listen: whether this turn was started by the name rather than by a finger,
   whether anyone has actually spoken yet, and when the room went quiet. */
static bool s_listen_voice;
static bool s_listen_heard;
static uint32_t s_listen_hush;
/* How long this particular listen waits for someone to start: the name gives 3 s, a follow-up
   2 s, and a hold does not use it at all. */
static uint32_t s_listen_lead;
/* A reply has finished playing and has not yet been followed up, and how many turns this
   exchange has run without a deliberate start. */
static bool s_follow_armed;
static int s_follow_turns;
/* Last frame's speaking state, so the follow-up fires on the EDGE where the speaker falls
   silent rather than on every frame after it. */
static bool s_was_speaking;
/* Declared here because `tap_to_overlay` below needs it and the orientation block that owns
   it comes later in the file. */
static bool s_upside_down;

/* Who the message being recorded is for, and the hush machinery for it — separate from the
   listen's so a message cannot inherit half of a conversation that was in flight. */
static jpanel_to_t s_rec_to;
static bool s_rec_heard;
static uint32_t s_rec_hush;
/* THE REPEAT ICON, and it is a deadline rather than a flag. The owner asked for five seconds
   after a message plays: it is for *"what did she say?"*, not a permanent control, and a
   button that never leaves would become another thing on the glass to poke. */
#define REPEAT_MS 5000
static uint32_t s_repeat_until;
/* THE POP-UP SHRINKS RATHER THAN NAGS.
 *
 * The owner: *"the notification on the panel is very large when it shows which is fine, but if
 * it's not acknowledged within say 15 seconds, it should kind of be a smaller one up on the
 * top left."*
 *
 * A box over the pet's face is right for the first fifteen seconds — it has to interrupt, the
 * reader is four and is not auditing the screen. It is wrong for the next hour: a message
 * nobody has come to yet should not hold a child's toy hostage. So it stands down to a badge
 * and the pet is a pet again, with the message still there and still tappable.
 *
 * `s_popup_since` is when the CURRENT run of waiting messages began — reset when the count
 * goes to zero, not on every poll, or a panel that polls every thirty seconds would restart
 * the clock forever and never shrink. */
#define POPUP_BIG_MS 15000
static uint32_t s_popup_since;

/* A TOUCH THAT MAKES A SOUND AND STILL PLAYS AT ONCE.
 *
 * The owner asked for a sound on these two controls and the first attempt produced none, on an
 * argument that was simply wrong: the cue was made conditional on nothing already sounding, and
 * once the prefetch landed the message ALWAYS starts on the same frame — so the condition was
 * never true. "The message is its own acknowledgement" is not an answer to a four-year-old who
 * pressed something; the press has to answer.
 *
 * The real constraint is that `audio_play` refuses while anything else sounds, so a cue and a
 * message cannot overlap: an unconditional cue would simply eat the message. So the cue plays
 * and the audio is DEFERRED by one speaker — `CUE_BLIP` is 55 ms, which is under the 100 ms a
 * press and its sound can be apart and still feel like one event, and far under the ~2 s this
 * release removed.
 *
 * A DEADLINE, because a deferral that never fires is a button that did nothing. If the speaker
 * is somehow still busy after this, the action is dropped rather than firing late into silence
 * a child has stopped associating with their finger. */
#define PENDING_MS 1500
typedef enum { PEND_NONE = 0, PEND_PLAY, PEND_REPLAY } pending_t;
static pending_t s_pending;
static uint32_t s_pending_until;
/* Where the pop-up and the repeat icon were drawn, in the space they were drawn in — which
   is NOT the space `panel_to_frame` hands back; see `tap_to_overlay` immediately below. Both
   are rectangles; -1 in the first slot means not on screen. */
static int s_popup_box[4] = {-1, -1, -1, -1};

static bool in_box(const int box[4], int x, int y)
{
    return box[0] >= 0 && x >= box[0] && x < box[2] && y >= box[1] && y < box[3];
}

/* THE OVERLAYS ARE DRAWN BEFORE THE 180 FLIP AND THE TAP MARKER IS DRAWN AFTER IT, so the two
 * live in different coordinate spaces and a hit test has to say which one it means.
 *
 * `panel_to_frame` stops at the quarter turns on purpose (the marker needs it to): upside down
 * it returns the touch unchanged, because by the time the marker is plotted `flip_frame` has
 * already run and the buffer is in panel order. Anything drawn EARLIER — the pop-up, the
 * repeat icon, the label, the caption — was written in frame order and then flipped, so its
 * rectangle has to be compared against a touch that has been flipped the same way. Merging
 * this into `panel_to_frame` would put the marker back under the finger's mirror image. */
static void tap_to_overlay(int fx, int fy, int *ox, int *oy)
{
    *ox = s_upside_down ? FACE_W - 1 - fx : fx;
    *oy = s_upside_down ? FACE_H - 1 - fy : fy;
}

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
/* ONE SHAPE, TWO MEANINGS, AND THE COLOUR IS THE DIFFERENCE. Factored out when voice post
   arrived rather than copied: a second pulsing dot drawn by a second function is two things
   to keep in step, and the pulse rate IS the affordance — a recording indicator that breathed
   at a different speed would read as a different kind of thing entirely. */
static void draw_indicator(uint16_t *fb, int y0, uint32_t now, uint16_t colour, const char *word)
{
    const int r = 13 + (int)((now / 140) % 4);
    const int cx = FACE_W - 34, cy = y0 + 34;
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = cx + dx, py = cy + dy;
            if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) {
                fb[py * FACE_W + px] = colour;
            }
        }
    }
    const int w = font_text_w(word, LISTEN_SCALE);
    font_draw(fb, FACE_W, FACE_H, FACE_W - 8 - w, cy + 22, LISTEN_SCALE, word, colour);
}

static void draw_listening(uint16_t *fb, int y0, uint32_t now)
{
    draw_indicator(fb, y0, now, SWAP16(0xF800), "LISTENING");
}

/* RECORDING A MESSAGE. Blue, and the word names the RECIPIENT rather than the act, because
   the act is the part a child already knows — they just asked for it — and who it is going to
   is the part they cannot see.
 *
 * THE SIBLING'S NAME USED TO BE UNKNOWABLE HERE, and the placeholder MESSAGE was the honest
 * way to say so: a panel is flashed with its OWN name and the box mints the other one's at the
 * other unit's flash, so nothing on this device could answer "who is my twin". `GET /waiting`
 * now carries it — it is a poll that was already happening — and the fallback stays for the
 * cases the box deliberately declines to answer: before the first poll, on a box with one
 * panel, and on a box with three, where "the other one" is a question rather than a name and a
 * guess would put the wrong child on the glass. */
static void draw_recording(uint16_t *fb, int y0, uint32_t now, jpanel_to_t to)
{
    if (to == JPANEL_TO_DAD) {
        draw_indicator(fb, y0, now, SWAP16(0x001F), "TO DAD");
        return;
    }
    /* "TO " plus the longest name the box will accept, uppercased: the font has no lowercase
       (`font.c`), so a name typed in the PWA has to be shouted here exactly as the pop-up
       shouts it. */
    char who[40];
    char name[32];
    if (jpanel_sibling(name, sizeof(name)) <= 0) {
        draw_indicator(fb, y0, now, SWAP16(0x001F), "MESSAGE");
        return;
    }
    snprintf(who, sizeof(who), "TO %s", name);
    for (char *q = who; *q != '\0'; q++) {
        if (*q >= 'a' && *q <= 'z') *q = (char)(*q - 'a' + 'A');
    }
    draw_indicator(fb, y0, now, SWAP16(0x001F), who);
}

/* THE POP-UP, AND IT IS THE ONLY THING ON THIS GLASS THAT COVERS THE PET.
 *
 * The owner: *"a little pop-up box would show up if a message is available to play."* A
 * badge in a corner would be the polite version and would be the wrong one — the reader is
 * four, cannot read, and is not auditing the screen for changes. It has to interrupt, and it
 * has to be tappable anywhere inside, because a four-year-old aiming at a small target with
 * an excited finger is a miss.
 *
 * IT DOES NOT AUTO-PLAY. A message that starts talking on its own would be the panel making
 * noise in a bedroom at a moment nobody chose; the pop-up survives a reboot (the state lives
 * on the box) and waits.
 *
 * Drawn centred in the SQUARE, not the frame, so a side-mounted panel keeps it — the same
 * rule the caption and the label were moved to obey. */
#define POPUP_SCALE 3
#define POPUP_NAME_SCALE 4
static void draw_popup(uint16_t *fb, int y0, int h, const char *from, int count)
{
    const int bw = 296, bh = 156;
    const int bx = (FACE_W - bw) / 2;
    const int by = y0 + (h - y0 - bh) / 2;
    bubble(fb, bx, by, bw, bh, 22, SWAP16(0x001F));
    bubble(fb, bx + 5, by + 5, bw - 10, bh - 10, 18, SWAP16(0x0010));

    char line[40];
    /* Uppercase because that is the alphabet the font has, and the box's names arrive in
       whatever case the owner typed at flash time. */
    snprintf(line, sizeof(line), "%s", from != NULL && from[0] != '\0' ? from : "SOMEONE");
    for (char *q = line; *q != '\0'; q++) {
        if (*q >= 'a' && *q <= 'z') *q = (char)(*q - 'a' + 'A');
    }
    int w = font_text_w(line, POPUP_NAME_SCALE);
    /* A long name is shrunk rather than clipped: a name cut in half names nobody. */
    const int name_scale = w > bw - 32 ? POPUP_SCALE : POPUP_NAME_SCALE;
    w = font_text_w(line, name_scale);
    font_draw(fb, FACE_W, FACE_H, bx + (bw - w) / 2, by + 34, name_scale, line, SWAP16(0xFFFF));

    /* THE NUMBER, not "SOME". Five messages used to be five pop-ups and five taps, which is
       indistinguishable from the panel repeating itself — and it is the count that tells a
       child whether one press is about to cost them ten seconds or a minute. */
    char sub[28];
    if (count > 1) {
        snprintf(sub, sizeof(sub), "SENT YOU %d", count);
    } else {
        snprintf(sub, sizeof(sub), "SENT YOU ONE");
    }
    w = font_text_w(sub, 2);
    font_draw(fb, FACE_W, FACE_H, bx + (bw - w) / 2, by + 84, 2, sub, SWAP16(0xFFFF));
    const char *act = count > 1 ? "TAP FOR ALL" : "TAP TO HEAR";
    w = font_text_w(act, POPUP_SCALE);
    font_draw(fb, FACE_W, FACE_H, bx + (bw - w) / 2, by + 112, POPUP_SCALE, act,
              SWAP16(0x07FF));

    s_popup_box[0] = bx;
    s_popup_box[1] = by;
    s_popup_box[2] = bx + bw;
    s_popup_box[3] = by + bh;
}

/* THE BADGE THE POP-UP BECOMES. Top-left, small, and still the whole tap target it was —
 * shrinking the box must not shrink what a four-year-old has to hit, so the rectangle stays
 * generous around a small mark.
 *
 * A DOT AND A NAME, not a count. "3" is a number a four-year-old cannot act on; who it is from
 * is the thing they care about, and one glance at it is the whole content. */
static void draw_popup_badge(uint16_t *fb, int y0, const char *from)
{
    char line[20];
    snprintf(line, sizeof(line), "%s", from != NULL && from[0] != '\0' ? from : "SOMEONE");
    for (char *q = line; *q != '\0'; q++) {
        if (*q >= 'a' && *q <= 'z') *q = (char)(*q - 'a' + 'A');
    }
    const int tw = font_text_w(line, 2);
    const int bw = tw + 46, bh = 44;
    const int bx = 14, by = y0 + 14;
    bubble(fb, bx, by, bw, bh, 12, SWAP16(0x001F));
    /* The same pulsing dot the pop-up's colour carries, so the two read as one thing at two
       sizes rather than as two different notices. */
    const int r = 7;
    for (int dy = -r; dy <= r; dy++) {
        for (int dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r * r) continue;
            const int px = bx + 18 + dx, py = by + bh / 2 + dy;
            if (px >= 0 && px < FACE_W && py >= 0 && py < FACE_H) fb[py * FACE_W + px] = CUE_COLOUR;
        }
    }
    font_draw(fb, FACE_W, FACE_H, bx + 32, by + 14, 2, line, SWAP16(0xFFFF));
    s_popup_box[0] = bx;
    s_popup_box[1] = by;
    s_popup_box[2] = bx + bw;
    s_popup_box[3] = by + bh;
}

/* PLAYING, AND TAPPABLE TO STOP. A run a child cannot see the end of needs a way out they can
 * SEE — "touch it and it stops" is not discoverable on a pet's face, and the whole reason this
 * is a queue rather than one message at a time is that a press should be able to cost a minute.
 *
 * Deliberately small and at the bottom, not over the face: the pet is animating and talking
 * through this and that is the thing worth watching. It says how many are left, because the
 * question a child sitting through four messages has is how many more. */
static void draw_run(uint16_t *fb, int y0, int h, int left)
{
    (void)y0;
    /* THE ICON CARRIES "STOP" AND THE NUMERAL CARRIES "HOW MANY MORE", which is the split the
       text bar could not make: "2 MORE  TAP TO STOP" is one sentence for an adult, and the
       reader here cannot read either half. The count stays because the question a child
       sitting through four messages actually has is how many are left — a digit they can
       count on their fingers answers it and the word never did. Above the disc, so it does
       not fight the square. */
    confirm_draw_stop(fb, FACE_W, FACE_H, h);
    if (left > 0) {
        /* Sized for the FORMAT, not the expected value — the rule the text bar this replaced
           already followed: `left` is an int, so eleven digits and a sign must fit or the
           compiler is right to refuse it. */
        char n[12];
        snprintf(n, sizeof(n), "%d", left);
        /* BESIDE THE DISC, NOT ABOVE IT, and the first version had it above: white numerals
           landed on the pet's own light body and all but vanished. Out here it sits on the
           black margin the bottom third already has, in the slot the cross occupies on the
           recording screen — so the row keeps its balance and the digit keeps its contrast. */
        const int tw = font_text_w(n, 3);
        font_draw(fb, FACE_W, FACE_H, CONFIRM_CX_CANCEL - tw / 2,
                  confirm_cy(h) - (FONT_H * 3) / 2, 3, n, SWAP16(0xFFFF));
    }
}

/* THE REPEAT ICON: top-left, five seconds, then gone (`REPEAT_MS`).
 *
 * Top-left is where the owner asked for it, and it is NOT an empty corner — the panel's name
 * label lives there. It covers the label for those five seconds, deliberately: everything
 * else on this glass is on the right (the indicator, the thinking box, the meter), and a
 * label that says what the panel is called is worth less for five seconds than a button that
 * says what your sister said. It is tested before `label_hit`, so the tap goes to the replay
 * rather than flipping the name to a version number.
 *
 * A WORD RATHER THAN A GLYPH. There is no drawing library here and a hand-plotted circular
 * arrow at this size reads as a smudge; "AGAIN" is what the adult in the room needs, and the
 * twins learn a box that appears where the sound just came from by pressing it once. */
static void draw_repeat(uint16_t *fb, int over_h)
{
    /* THE GLYPH, AT LAST. The word was here because "a hand-plotted circular arrow at this
       size reads as a smudge" — true of a 52 px corner box and not of a 112 px disc. It also
       moves from the top-left corner to the bottom third, where every other thing a finger is
       meant to press now lives; a control whose location a child has to learn separately is a
       control they will not find. No stored rectangle any more: it is a circle and it is
       hit-tested as one. */
    confirm_draw_repeat(fb, FACE_W, FACE_H, over_h);
}

/* 0 upright, 1 clockwise, 2 upside down, 3 anticlockwise — a quarter turn each. */
static int s_quarter;
static bool s_quarter_changed = true;

void display_set_debug_overlay(bool on)
{
    s_debug_overlay = on;
}

/* WHICH WAY IS UP — the reading's own trustworthiness. The BAND that decides which quarter a
   trusted reading means now lives in `orient.h`, where it can be tested.

   The owner asked for the flip now rather than after a reporting round:
   getting the sign wrong costs one release and is obvious on sight, which is cheaper than
   waiting. 0.2.18 guessed `ay` and the panel's own telemetry settled it in one cycle:
   gravity is on **X** — `ay` sat well inside the hysteresis band, where nothing would ever
   have flipped. The sign then came from a reading taken in a KNOWN orientation, which the
   first one was not: charging port right and the robot's head up reads `[8446, 78, -563]`,
   so right way up is POSITIVE ax. The earlier `[-7637, 381, 530]` was the panel lying
   inverted on its charger, and reading a sign off an unknown pose is how 0.2.19 shipped
   backwards. Two readings, two axes eliminated, one orientation named.

   Hysteresis at about half a gravity, because a panel lying near flat has almost nothing on
   this axis and a bare sign test would flip it back and forth on noise. That figure now lives
   in `orient.h` as `ORIENT_MIN_MAG`, beside the band it belongs with.
   (Declared above, where `tap_to_overlay` needs it.) */

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
/* THE TOTALS, because the reported pair is "since the last recovery" and therefore erases
   exactly the history worth having. A panel that failed 249 consecutive blits, self-healed,
   and has drawn cleanly for the last fourteen minutes reports `blit_ok` climbing and
   `blit_fail` at zero — indistinguishable from one that never faltered. The near miss is the
   reading you want BEFORE the self-heal reboot fires, not after. */
static int s_blit_fail_total;
static int s_blit_recoveries;
/* Meter blits are their own transfer and their failures reached no counter at all. The meter
   redraws at 25 Hz against the face's 5, so it is by far the more frequent transfer on this
   bus — a panel whose meter blits are all failing while face blits succeed was reporting
   perfect health. */
static int s_meter_fail;

/* GPIO0 on an ESP32-S3 is the BOOT strap: held low through a reset it enters download mode,
   and read at runtime it is an ordinary input with an external pull-up. Configured as an input
   and never driven, so nothing here can interfere with flashing. */
#define BOOT_BTN GPIO_NUM_0
static int s_boot_presses;
static bool s_boot_was_down;

/* Did that tap land on the label? In FRAME coordinates, so it follows the quarter turn like
   everything else the finger touches (§10.4bw). Padded well beyond the glyphs: the text is
   ~14 px tall and a four-year-old's fingertip is not, so the target is the corner rather than
   the letters. */
static bool label_hit(int fx, int fy, int over_y0)
{
    if (fx < 0 || fy < 0) return false;
    const int w = font_text_w(s_show_version ? "0.0.00" : s_name_up, LABEL_SCALE);
    const int x0 = LABEL_X - 14, x1 = LABEL_X + w + 14;
    const int y0 = over_y0 + LABEL_Y - 14, y1 = over_y0 + LABEL_Y + FONT_H * LABEL_SCALE + 14;
    return fx >= x0 && fx <= x1 && fy >= y0 && fy <= y1;
}

static void boot_button_poll(void)
{
    const bool down = gpio_get_level(BOOT_BTN) == 0; /* active low, pulled up */
    if (down && !s_boot_was_down) {
        s_boot_presses++;
        ESP_LOGI(TAG, "boot button: press %d", s_boot_presses);
    }
    s_boot_was_down = down;
}

int display_boot_presses(void)
{
    return s_boot_presses;
}

void display_blit_counts(int *ok, int *fail)
{
    if (ok != NULL) *ok = s_blit_ok;
    if (fail != NULL) *fail = s_blit_fails;
}

void display_blit_totals(int *fail_total, int *recoveries, int *meter_fail)
{
    if (fail_total != NULL) *fail_total = s_blit_fail_total;
    if (recoveries != NULL) *recoveries = s_blit_recoveries;
    if (meter_fail != NULL) *meter_fail = s_meter_fail;
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
    /* The sleep timer's other input, taken here because this is where the accelerometer
       has already been read — `screen.h` says why it is a difference and not a tilt. */
    {
        static bool seen;
        static int16_t prev[3];
        const int16_t cur[3] = {ax, ay, az};
        if (seen) {
            const int d = screen_motion(prev, cur);
            if (screen_moved(d)) {
                s_moved = true;
                s_move_mag = d;
            }
        }
        seen = true;
        for (int i = 0; i < 3; i++) prev[i] = cur[i];
    }
    /* FOUR WAYS UP, FROM THE TWO AXES THE FLIP ALREADY USED. Gravity on X is portrait and
       its sign says which way; gravity on Y is landscape, mounted with the cable out the
       side, and its sign says which. Whichever axis is larger wins, with the same half-a-
       gravity hysteresis the two-way version needed — a panel lying near flat has almost
       nothing on either axis, and a bare comparison would flip it back and forth on noise. */
    const int was = s_quarter;
    /* `orient.c`, and the band is the whole reason it moved out of here: `|ax| > |ay|` turns
       over at exactly 45 degrees, so a panel HELD at 45 had its orientation chosen by
       accelerometer noise several times a second. The angle and the hysteresis are pure C and
       host-tested, because an orientation rule that is only reasoned about is how the first
       flip shipped backwards (§10.4). */
    s_quarter = orient_quarter(s_quarter, ax, ay);
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
    /* The one blit that does not go through the frame gate, so the sleep has to be repeated
       here or a dark screen with the debug overlay on would keep a bar lit all night. */
    if (s_sleep == SCREEN_DARK) return;
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
    if (esp_lcd_panel_draw_bitmap(s_panel, x0, y0, x0 + METER_W, y0 + METER_SPAN, strip) !=
        ESP_OK) {
        s_meter_fail++;
    }
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
/* WAS DEAD, AND IS NOW THE ANSWER TO "WHY DID IT REBOOT". Written at `phase_init` and read
   by nobody, ever. Three unrelated callers reach `esp_restart()` — the blit self-heal below,
   the owner's reboot gesture, and the post-update second boot — and all three arrive at the
   box as `reset_reason: "sw(3)"` with the first two also sharing `PHASE(9)`. So "the panel
   restarted itself because it could not draw", which is a fault, reads exactly like "a
   four-year-old did the gesture", which is not. The reason is known at each call site and was
   being logged to a console this panel does not have. */
static RTC_NOINIT_ATTR uint32_t s_phase_prev;
#define RESTART_MAGIC 0x52535441u
static RTC_NOINIT_ATTR uint32_t s_restart_magic;
static RTC_NOINIT_ATTR uint32_t s_restart_why;
static const char *s_restart_why_at_boot = "";

void display_note_restart(display_restart_t why)
{
    s_restart_magic = RESTART_MAGIC;
    s_restart_why = (uint32_t)why;
}

const char *display_restart_reason(void)
{
    return s_restart_why_at_boot;
}

/* Read once at boot, before the loop overwrites it. */
static int s_phase_at_crash = -1;

#define PHASE(n)      \
    do {              \
        s_phase = (n); \
    } while (0)

bool display_panel_reset(void)
{
    return s_panel_reset;
}

int display_crash_phase(void)
{
    return s_phase_at_crash;
}

static void phase_init(void)
{
    if (s_restart_magic == RESTART_MAGIC) {
        switch ((display_restart_t)s_restart_why) {
        case DISPLAY_RESTART_BLIT_HEAL: s_restart_why_at_boot = "blit-heal"; break;
        case DISPLAY_RESTART_GESTURE:   s_restart_why_at_boot = "gesture";   break;
        case DISPLAY_RESTART_OTA_PARK:  s_restart_why_at_boot = "ota-park";  break;
        default:                        s_restart_why_at_boot = "";          break;
        }
        /* Consumed, so the NEXT restart has to say so for itself — otherwise a power cycle
           after a self-heal keeps reporting the self-heal forever. */
        s_restart_magic = 0;
    }
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

const char *display_screen(void)
{
    return s_sleep == SCREEN_DARK ? "dark" : s_sleep == SCREEN_DIM ? "dim" : "awake";
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
    /* Input with its pull-up, never an output: this pin is the BOOT strap and driving it would
       be a way to make the panel unflashable. */
    const gpio_config_t boot_cfg = {
        .pin_bit_mask = 1ULL << BOOT_BTN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&boot_cfg) != ESP_OK) ESP_LOGW(TAG, "boot button: gpio_config refused");
    build_name();
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
    /* Whatever the box said before this task started; -1 until it has said anything, in which
       case `face_rest` has already chosen the shipped default. Left PENDING rather than cleared:
       the loop below takes it on the first pass and logs it, so a boot and a later change read
       the same way in the log. */
    action_t action = ACT_NONE;
    uint32_t action_start = 0;
    float action_mag = 1.0f;
    gesture_t gest;
    gesture_reset(&gest);
    pool_memory_t mem[POOL_COUNT];
    for (int i = 0; i < POOL_COUNT; i++) variants_reset(&mem[i]);
    float s_open = 1.0f;
    int s_drawn_lean = 0;
    /* The shuffle's own state, and the lean it last saw — the walk is driven by how far the
       figure MOVED this frame, so it needs the previous position rather than the current one
       (`rig.h`). */
    rig_walk_t walk = {0};
    int walked_from = 0;
    int since_reassert = 0;
    int since_sample = 0;
    int level = 0;
    /* The interval the delay below last served, which is what every accumulator in this loop
       is measuring. Decided at the end of a pass and read at the start of the next, so a
       stage change never mis-counts the pass that carried it. */
    int poll_ms = TOUCH_POLL_MS;

    while (true) {
        PHASE(1);
        bool dirty = false;
        /* Set where the recogniser is drained, read by the sleep timer far below. */
        bool heard_voice = false;
        /* And the touch that woke the screen, which has to be remembered rather than read:
           it is cleared out of `tapped` below so nothing else acts on it, and the idle timer
           further down would otherwise see a frame with no activity in it and put the panel
           straight back to sleep on the same pass. */
        bool woke_by_touch = false;
        /* One clock read per frame, shared by the rig, the pools and the cooldowns, so every
           part of a frame agrees about when it is. */
        const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        PHASE(2);
        /* Read the edge ONCE. `touch_tapped()` is what refreshes the cached level that
           `touch_is_down()` returns, so calling it twice in a frame would consume the edge
           for whichever caller ran first. */
        bool tapped = touch && touch_tapped();
        bool down = touch && touch_is_down();
        /* A FINGER ON A DARK SCREEN BUYS THE SCREEN, AND NOTHING ELSE. The child cannot see
           what they are aiming at, so letting that touch also poke the pet, arm a gesture or
           acknowledge a message would make the first tap after a nap do something nobody
           chose. It wakes, it is spent, and the next tap — aimed at a face that is now
           visible — lands normally. Dim is not included: the pet is still on screen there,
           and a tap that hits what you can see should do what it looks like it does. */
        if (s_sleep == SCREEN_DARK && (tapped || down)) {
            sleep_wake("touch");
            woke_by_touch = true;
            tapped = false;
            down = false;
        }
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
        /* THE EXCHANGE CONTINUES ITSELF. Armed when a reply starts playing, fired on the edge
           where the speaker falls silent — not on a timer, because a long reply must not have
           the microphone opened underneath it. `audio.c` stays deaf for six chunks after the
           speaker runs, which conveniently keeps the tail of our own voice out of the front of
           the next recording. */
        if (s_was_speaking && !speaking && s_follow_armed) {
            s_follow_armed = false;
            if (s_talk == TALK_IDLE && talk_state() != TALK_NET_BUSY &&
                s_follow_turns < FOLLOW_MAX_TURNS) {
                s_talk = TALK_LISTENING;
                s_talk_since = now;
                s_listen_voice = true;
                s_listen_heard = false;
                s_listen_hush = 0;
                s_listen_lead = FOLLOW_LEAD_MS;
                s_follow_turns++;
                audio_capture_open();
                ESP_LOGI(TAG, "talk: listening (follow-up %d)", s_follow_turns);
                dirty = true;
            } else if (s_follow_turns >= FOLLOW_MAX_TURNS) {
                ESP_LOGW(TAG, "talk: %d follow-ups without a deliberate start — stopping",
                         s_follow_turns);
            }
        }
        s_was_speaking = speaking;
        if (tapped && !speaking) {
            /* WHERE THE FINGER LANDED, RESOLVED ONCE, BEFORE ANY BRANCH READS IT.
             *
             * It used to be resolved down in the poke block, which was fine while the poke
             * was the only branch that cared. It is not any more: the pop-up and the repeat
             * icon are hit-tested, and a branch that returns before the poke block would have
             * left `s_fig_x` holding the PREVIOUS tap — so the recoil ring would appear where
             * the last finger was, which is the same class of bug `s_down_x` exists to
             * document. Corrected once here and every branch below speaks the same
             * coordinates. */
            int rx = -1, ry = -1;
            touch_point(&rx, &ry);
            calib_apply(&s_cal, rx, ry, &s_tap_x, &s_tap_y);
            panel_to_frame(s_tap_x, s_tap_y, &s_fig_x, &s_fig_y);
            /* OVERLAY COORDINATES, RESOLVED ONCE BESIDE THE FRAME ONES, because every hit test
               below wants these and one of them forgot. Anything drawn BEFORE `flip_frame` —
               the pop-up, the repeat icon, the label, the caption, the tick and the cross — is
               written in frame order and then reversed, so its rectangle has to be compared
               against a touch reversed the same way. The tap MARKER is the exception and the
               reason this is easy to get wrong: it is drawn AFTER the flip, so it sits under
               the finger using `s_fig` directly, which makes a panel look like it is tracking
               touch correctly while every pre-flip target on it is 180 degrees away.
               MEASURED 2026-09-25, upside down: a press on the tick at frame (276,368) arrives
               here as (91,79) and missed by 289 px. */
            int ox = -1, oy = -1;
            tap_to_overlay(s_fig_x, s_fig_y, &ox, &oy);
            /* The overlay BAND, resolved here for the same reason the coordinates are: the
               centred controls are placed against it and a hit test that guessed a different
               band would miss by the difference. */
            const int over_h_tap = (s_quarter == 1 || s_quarter == 3) ? SQ_Y0 + SQ : FACE_H;
            /* THE POP-UP AND THE REPEAT ICON OUTRANK EVERYTHING, tested before the cancels
             * and the poke for exactly the reason the label is: a tap that both played a
             * message and made the pet fart reads as two things happening, and the child
             * cannot tell which one they asked for.
             *
             * Against overlay coordinates, not frame ones — see `tap_to_overlay`. */
            {
                if (in_box(s_popup_box, ox, oy)) {
                    s_flinch = 1.0f;
                    /* Cleared the moment it is pressed, not when the audio arrives: a box
                       that stays up through a fetch invites a second press, and
                       `jpanel_play_next` refuses that one — so the child would be pressing a
                       button that had stopped working. */
                    s_popup_box[0] = -1;
                    /* The press sounds FIRST and the message follows it — see `PENDING_MS`. */
                    if (sound) audio_cue(CUE_HEARD);
                    s_pending = PEND_PLAY;
                    s_pending_until = now + PENDING_MS;
                    dirty = true;
                    goto tap_done;
                }
                if (s_repeat_until != 0 && confirm_hit_centre(ox, oy, over_h_tap)) {
                    s_flinch = 1.0f;
                    /* IT ASKS THE BOX NOW, and that is a real change from what this comment
                       used to promise. The message was replayed from this panel's own buffer
                       until 0.2.96; streaming discards the audio as it plays, so "again" is a
                       fetch (`GET /message/{id}/pcm`) and it needs the link to be up. The
                       failure is reported through `jpanel_state()` like any other fetch rather
                       than being silent, because a control that answers with nothing is the
                       thing the cue below was added to stop. */
                    /* Same shape as the pop-up: a sound for the finger, then the audio. This
                       had NO cue at all, which made the one control a child presses when they
                       missed something the one that answered with silence. */
                    if (sound) audio_cue(CUE_HEARD);
                    s_pending = PEND_REPLAY;
                    s_pending_until = now + PENDING_MS;
                    s_repeat_until = now + REPEAT_MS; /* they are still asking; keep it up */
                    dirty = true;
                    goto tap_done;
                }
            }
            /* THE TICK AND THE CROSS, AND THEY REPLACE "A TOUCH ANYWHERE CANCELS".
             *
             * That rule was right while it was the only way out — the owner asked for it
             * ("when it's listening, if I touch the screen it should stop and discard") when a
             * hands-free listen had no other exit for someone who was not going to speak. It
             * is the wrong rule the moment there is somewhere deliberate to press: the same
             * finger that means "send this" is one bad aim away from destroying it, and the
             * reader is four. So the cross cancels, the tick sends, and ANYTHING ELSE ON THE
             * GLASS DOES NOTHING — including the pet, which cannot be poked mid-message.
             *
             * GOING QUIET STILL SENDS (see the hush branch below). The tick is "I am done, do
             * not wait it out", not a button the child has to find every time.
             *
             * ONLY THE HANDS-FREE TURNS. A held listen ends on the release of the finger that
             * started it, so it never reaches here and keeps its gesture intact. */
            if ((s_talk == TALK_LISTENING && s_listen_voice) || s_talk == TALK_RECORDING) {
                const confirm_hit_t pressed = confirm_hit(ox, oy, over_h_tap);
                const bool recording = (s_talk == TALK_RECORDING);
                const char *who = recording ? "jpanel" : "talk";
                if (pressed == CONFIRM_CANCEL) {
                    /* Exactly what "stop stop" and the old touch-anywhere did, so the two ways
                       to say stop still behave identically: the capture dropped, the follow-up
                       window closed, and the turn counter parked at its cap. */
                    size_t dropped = 0;
                    (void)audio_capture_close(&dropped);
                    s_talk = TALK_IDLE;
                    s_listen_voice = false;
                    if (!recording) {
                        s_follow_armed = false;
                        s_follow_turns = FOLLOW_MAX_TURNS;
                    }
                    s_flinch = 1.0f;
                    if (sound) audio_cue(CUE_STOP);
                    ESP_LOGI(TAG, "%s: cancelled by cross, %u bytes discarded", who,
                             (unsigned)dropped);
                    dirty = true;
                    goto tap_done;
                }
                if (pressed == CONFIRM_SEND) {
                    /* NOTHING HEARD IS NOT A SEND. The hush branch already refuses to send a
                       room nobody spoke into, and a tick pressed into that same silence must
                       refuse too — otherwise the one exit that skips the silence check becomes
                       the way six seconds of a bedroom reaches dad. Said out loud, because a
                       child who pressed the tick and heard nothing has been told it went. */
                    if (!(recording ? s_rec_heard : s_listen_heard)) {
                        size_t got = 0;
                        (void)audio_capture_close(&got);
                        s_talk = TALK_IDLE;
                        s_listen_voice = false;
                        s_flinch = 1.0f;
                        if (sound) audio_cue(CUE_OOPS);
                        ESP_LOGI(TAG, "%s: tick pressed but nobody spoke — dropped", who);
                        dirty = true;
                        goto tap_done;
                    }
                    /* A SOUND FOR THE FINGER, THEN THE OUTCOME — the pop-up's rule, and the
                       tick needed it more than the pop-up did. `CUE_SENT` exists and is played,
                       but only when the BOX confirms (`JPANEL_SENT`), which is a network round
                       trip away; the cross answers instantly with `CUE_STOP`. So the two
                       targets felt different in the hand, and the owner reported exactly that:
                       *"there is a sound effect when hitting cancel, but not the check mark"* —
                       while the messages were arriving the whole time. The one control a child
                       presses to send their voice must not be the one that answers with
                       silence. */
                    if (sound) audio_cue(CUE_HEARD);
                    size_t got = 0;
                    const int16_t *pcm = audio_capture_close(&got);
                    s_listen_voice = false;
                    ESP_LOGI(TAG, "%s: sent by tick after %u ms (%u bytes)", who,
                             (unsigned)audio_capture_ms(), (unsigned)got);
                    if (recording) {
                        s_talk = TALK_IDLE;
                        if (pcm == NULL || !jpanel_send(pcm, got, s_rec_to)) {
                            if (sound) audio_cue(CUE_OOPS);
                            ESP_LOGW(TAG, "jpanel: nothing to send");
                        }
                    } else if (pcm == NULL || !talk_send(pcm, got)) {
                        s_talk = TALK_IDLE;
                    } else {
                        s_talk = TALK_THINKING;
                        s_talk_since = now;
                    }
                    s_flinch = 1.0f;
                    dirty = true;
                    goto tap_done;
                }
                /* Off both targets: CONSUMED, and deliberately without a flinch. Every other
                   tap on this glass answers somehow, and that is exactly what must not happen
                   here — a pet that twitches while a child is talking to it is the panel
                   inviting the next poke mid-sentence.
                 *
                 * BUT IT SAYS SO, and the first version of this did not. A miss that is silent
                 * on the glass AND silent in the log is a control that cannot be diagnosed
                 * without a cable: 0.3.07 shipped with the coordinates 180 degrees out on an
                 * upside-down panel and the only symptom available to the owner was "it does
                 * not respond". One line here would have named it. Both pairs, for the same
                 * reason the poke logs both: frame and overlay agreeing places the fault in
                 * calibration, disagreeing places it in the flip. */
                ESP_LOGI(TAG, "%s: tap at frame (%d,%d) overlay (%d,%d) hit neither target "
                              "(tick %d, cross %d, cy %d)",
                         who, s_fig_x, s_fig_y, ox, oy, CONFIRM_CX_SEND, CONFIRM_CX_CANCEL,
                         confirm_cy(over_h_tap));
                goto tap_done;
            }
            colour = (colour + 1) % face_colour_count();
            s_flinch = 1.0f;
            /* THE POKE IS THE PRODUCT, AND WHERE YOU POKE IS HALF OF IT. The zone picks the
               pool; the pool picks the reaction, weighted, cooled-down, and softened if you
               are hammering it (`variants.c`). The colour cycle stays, because it is the one
               thing a child can steer deliberately. The coordinates were corrected at the top
               of this block, so the zones, the marker and the telemetry all speak the same
               ones. */
            /* THE LABEL IS ITS OWN BUTTON, checked before the zones so a corner of the glass
               that says something cannot also be a poke. It sits above the pet's head where
               `face_zone` returns nothing anyway, so no reaction is lost — and a tap that both
               flipped the label and made the pet sneeze would read as two things happening. */
            if (label_hit(ox, oy, (s_quarter == 1 || s_quarter == 3) ? SQ_Y0 : 0)) {
                s_show_version = !s_show_version;
                ESP_LOGI(TAG, "label -> %s", s_show_version ? "version" : "name");
                if (sound) audio_cue(CUE_TOGGLE);
                dirty = true;
                goto tap_done;
            }
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
            /* Before the repaint, not after: the cue is ~90 ms and a full frame is ~330 KB
               over QSPI, and the tap feels answered by whichever lands first.
               THE SOUND FOLLOWS THE ACTION, which is what makes poking the head coo: the
               head's pool is weighted towards a blush, and CUE_BLUSH is the warm one. No zone
               is special-cased, so the sound and the animation cannot drift apart and they
               stay in step for free the next time `variants.c` is re-weighted. */
            PHASE(3);
            if (sound) audio_cue(cue_for_action((int)action));
            dirty = true;
        tap_done:;
        } else if (tapped) {
            /* A FINGER STOPS A RUN OF MESSAGES, and this is the third place that rule applies —
               it already ends a listen and abandons a recording. A child who has heard enough
               of their sister must be able to get out without waiting for the last one, and the
               gesture they would reach for is the one they already know.
             *
               Only a RUN. A poke during the pet's own reply still just flinches: that is one
               sustained utterance the panel is making, not a queue the child is sitting
               through, and cutting it off was never asked for. */
            if (jpanel_running()) {
                jpanel_stop();
                s_pending = PEND_NONE; /* a deferred play must not resurrect the run */
                s_flinch = 1.0f;
                ESP_LOGI(TAG, "jpanel: run stopped by touch");
                dirty = true;
                /* No cue: the silence IS the answer, and a sound here would be the panel
                   making noise in the half-second a child asked it to stop making noise. */
            } else {
                /* Poked mid-sentence. The flinch stays — ignoring the finger entirely would
                   read as a frozen pet — but no beep, no colour change and no new action, so
                   the reply finishes with the mouth still moving. */
                s_flinch = 1.0f;
                dirty = true;
            }
        }
        /* WHAT THE PANEL HEARD. Popped once a frame, so a phrase cannot arrive between two
           frames and be lost, and acted on in exactly the way a tap is — the voice is
           another way to ask, never a second animation path. */
        char said[64];
        int said_id = -1;
        if (speech_live() && speech_take(said, sizeof(said), &said_id)) {
            /* A WORD UNDERSTOOD IS THE PANEL BEING USED, even when it resolves to nothing
               this frame can draw. The recogniser stays live while the screen is dark — a
               panel that cannot hear its name in the dark is a panel that is OFF, and the
               owner asked for a screen timer rather than a power switch. */
            heard_voice = true;
            caption_say(&cap, said);
            const vocab_t *v = vocab_get(said_id);
            if (v != NULL) {
                /* Understanding a word is not the same event as a finger landing, and used
                   to sound identical. `stop` gets the falling gesture, the name gets the
                   rising one that means the microphone is open, and everything else gets the
                   coin. */
                cue_t heard_cue = CUE_HEARD;
                switch (v->kind) {
                case VOCAB_FORM:
                    st.form = (face_form_t)v->arg;
                    break;
                case VOCAB_COLOUR:
                    /* A named colour lands on its index; "pick a new color" still steps. */
                    colour = v->arg < 0 ? (colour + 1) % face_colour_count()
                                        : v->arg % face_colour_count();
                    break;
                case VOCAB_STOP:
                    /* THE WAY OUT OF A CONVERSATION THAT CONTINUES ITSELF. The owner: "now
                       that it auto continues for six turns, it wants to keep going even if I
                       say stop."
                     *
                       Three states to leave, because "stop" has to mean stop wherever it is
                       said: a recording in progress is dropped rather than sent, a reply
                       already in flight is abandoned rather than spoken, and the follow-up
                       window is closed so the microphone does not reopen. The turn counter
                       goes to its cap rather than to a separate flag — the next deliberate
                       start (the name, or a finger) resets it, which is exactly the rule that
                       already governs the loop. */
                    if (s_talk == TALK_LISTENING || s_talk == TALK_RECORDING) {
                        size_t dropped = 0;
                        (void)audio_capture_close(&dropped);
                    } else if (s_talk != TALK_IDLE) {
                        talk_clear();
                    }
                    s_talk = TALK_IDLE;
                    s_listen_voice = false;
                    s_follow_armed = false;
                    s_follow_turns = FOLLOW_MAX_TURNS;
                    ESP_LOGI(TAG, "talk: stopped by voice");
                    heard_cue = CUE_STOP;
                    break;
                case VOCAB_SEND:
                    /* VOICE POST. The same capture machinery a conversation uses, pointed
                       somewhere else — which is the whole design: a second recorder with its
                       own buffer, its own hush and its own cap would be a second thing to get
                       wrong for a four-year-old, and the numbers here were tuned against one.
                     *
                       A LONGER LEAD THAN A CONVERSATION GETS (`RECORD_LEAD_MS`). Asking the
                       pet something is a sentence already formed; telling your sister
                       something is one being composed, out loud, by a four-year-old who has
                       just watched a blue dot appear. Four seconds rather than three, and the
                       silent branch below still throws away a message nobody spoke into.
                     *
                       Refused while a turn is in flight or while the pet is speaking, exactly
                       as the name is: one microphone, one thing at a time. */
                    if (s_talk == TALK_IDLE && !speaking && talk_state() != TALK_NET_BUSY &&
                        jpanel_state() != JPANEL_BUSY) {
                        s_talk = TALK_RECORDING;
                        s_talk_since = now;
                        s_rec_to = v->arg == (int)JPANEL_TO_DAD ? JPANEL_TO_DAD
                                                                : JPANEL_TO_PANEL;
                        s_rec_heard = false;
                        s_rec_hush = 0;
                        /* The follow-up window is closed: a message is not a turn, and the
                           microphone must not reopen after one. */
                        s_follow_armed = false;
                        audio_capture_open();
                        ESP_LOGI(TAG, "jpanel: recording for %s",
                                 s_rec_to == JPANEL_TO_DAD ? "dad" : "the other panel");
                        heard_cue = CUE_LISTEN;
                        dirty = true;
                    } else {
                        ESP_LOGI(TAG, "jpanel: busy — not recording");
                    }
                    break;
                case VOCAB_LISTEN:
                    /* THE SAME STATE A HOLD REACHES, deliberately: one path to the box, not
                       two. Everything after this — the bubble, the upload, the reply, the
                       failure face — is the press-and-hold machine, and the only difference is
                       how the turn ends (`LISTEN_HUSH_MS`). Refused while a turn is in flight
                       or while we are speaking, for the same reasons the hold is. */
                    if (s_talk == TALK_IDLE && !speaking && talk_state() != TALK_NET_BUSY) {
                        s_talk = TALK_LISTENING;
                        s_talk_since = now;
                        s_listen_voice = true;
                        s_listen_heard = false;
                        s_listen_hush = 0;
                        s_listen_lead = LISTEN_LEAD_MS;
                        s_follow_turns = 0; /* a deliberate start is a fresh exchange */
                        audio_capture_open();
                        ESP_LOGI(TAG, "talk: listening (name)");
                        heard_cue = CUE_LISTEN;
                    }
                    break;
                case VOCAB_ACTION:
                default:
                    action = (action_t)v->arg;
                    action_mag = 1.0f;
                    action_start = now;
                    /* ASKING FOR A THING SOUNDS LIKE THE THING. The acknowledgement IS the
                       action's cue, not a bleep followed by one — "can you burp" answering
                       with a tone and then a burp is the toy answering twice, and that was
                       already true of the two rude ones before the other seventeen had
                       voices of their own. One rule now covers all nineteen. */
                    heard_cue = cue_for_action((int)action);
                    break;
                }
                if (sound) audio_cue(heard_cue);
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
                    if (sound) audio_cue(CUE_TICK);
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
        boot_button_poll();
        s_open = blink_open(poll_ms);
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
        /* AND WHILE HE IS STILL PUTTING HIS FEET DOWN. The lean stops changing the moment it
           reaches its target, so without this the loop drops to the 200 ms idle floor while
           the stride is still settling — five frames a second, which turns a half-second
           settle into nearly three and leaves the pet standing on a shelf with one leg out.
           That is what the owner saw. Same rule as the flinch and the blink above: animating
           means every poll is a frame. */
        if (walk.amp > 0.0f) dirty = true;

        const int prev_taps = gest.taps;
        const float prev_cue = gesture_cue(&gest);
        /* Decided before the draw, acted on after it: the frame carrying a full-width cue has
           to reach the glass first, or a reboot is indistinguishable from the fault we are
           chasing. */
        const gesture_action_t act = gesture_poll(&gest, tapped, down, poll_ms);

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
            /* A finger, not the name — so this turn ends on the release, not on silence. Set
               explicitly rather than relied upon: the two paths share one state machine, and a
               stale flag here would leave a held turn waiting for a hush that never comes. */
            s_listen_voice = false;
            s_follow_turns = 0; /* a finger is a deliberate start, like the name */
            /* The beep IS the affordance. Nothing else tells a child holding a 29 mm screen
               that the thing is now listening rather than merely being held, and a rising
               sweep says it better than a flat tone: rising is the prosody of a question,
               which is what an open microphone is. */
            if (sound) audio_cue(CUE_LISTEN);
            /* AFTER the beep, deliberately: `audio.c` goes deaf for six chunks once the
               speaker runs (§10.4bi), so opening the recording here keeps our own tone out
               of the front of every message. */
            audio_capture_open();
            ESP_LOGI(TAG, "talk: listening");
        } else if (s_talk == TALK_IDLE && down && !on_the_pet && gest.taps == 0 &&
                   held >= HOLD_TALK_MS && held < HOLD_TALK_MS + poll_ms) {
            /* Once per press, on the frame the threshold passes — the owner has no terminal
               but does have the log, and a margin that is too wide looks exactly like a
               microphone that stopped working unless the panel says which it is. */
            ESP_LOGI(TAG, "talk: hold at (%d,%d) is on the rim, not the pet", s_down_x,
                     s_down_y);
        } else if (s_talk == TALK_LISTENING && s_listen_voice) {
            /* WAITING FOR THE ROOM TO GO QUIET. A held turn ends when the finger lifts; this
               one has to be read off the front end's VAD, which `speech.c` has been computing
               all along. */
            const bool voice = speech_hearing();
            if (voice) {
                s_listen_heard = true;
                s_listen_hush = 0;
            } else if (s_listen_heard && s_listen_hush == 0) {
                s_listen_hush = now;
            }
            const bool hushed =
                s_listen_heard && s_listen_hush != 0 && now - s_listen_hush >= LISTEN_HUSH_MS;
            const bool full = audio_capture_ms() >= audio_capture_cap_ms();
            const bool nothing = !s_listen_heard && now - s_talk_since > s_listen_lead;
            if (nothing) {
                /* The name and then silence — a television, or a child who changed their mind.
                   Dropped without a bubble: an accidental wake must cost nothing, which is the
                   whole reason this branch exists rather than sending six seconds of a room. */
                size_t got = 0;
                (void)audio_capture_close(&got);
                s_talk = TALK_IDLE;
                s_listen_voice = false;
                ESP_LOGI(TAG, "talk: named but nobody spoke — dropped");
            } else if (hushed || full) {
                size_t got = 0;
                const int16_t *pcm = audio_capture_close(&got);
                ESP_LOGI(TAG, "talk: %s after %u ms (%u bytes)", full ? "full" : "hushed",
                         (unsigned)audio_capture_ms(), (unsigned)got);
                s_listen_voice = false;
                if (pcm == NULL || !talk_send(pcm, got)) {
                    s_talk = TALK_IDLE;
                } else {
                    s_talk = TALK_THINKING;
                    s_talk_since = now;
                }
                dirty = true;
            }
        } else if (s_talk == TALK_RECORDING) {
            /* THE SAME THREE WAYS OUT A HANDS-FREE LISTEN HAS, and deliberately the same
               numbers: hush sends, silence drops it, the cap sends what there is. The only
               difference is where it goes. */
            const bool voice = speech_hearing();
            if (voice) {
                s_rec_heard = true;
                s_rec_hush = 0;
            } else if (s_rec_heard && s_rec_hush == 0) {
                s_rec_hush = now;
            }
            const bool hushed =
                s_rec_heard && s_rec_hush != 0 && now - s_rec_hush >= LISTEN_HUSH_MS;
            const bool full = audio_capture_ms() >= audio_capture_cap_ms();
            const bool nothing = !s_rec_heard && now - s_talk_since > RECORD_LEAD_MS;
            if (nothing) {
                size_t got = 0;
                (void)audio_capture_close(&got);
                s_talk = TALK_IDLE;
                ESP_LOGI(TAG, "jpanel: asked to send but nobody spoke — dropped");
                dirty = true;
            } else if (hushed || full) {
                size_t got = 0;
                const int16_t *pcm = audio_capture_close(&got);
                ESP_LOGI(TAG, "jpanel: %s after %u ms (%u bytes)", full ? "full" : "hushed",
                         (unsigned)audio_capture_ms(), (unsigned)got);
                s_talk = TALK_IDLE;
                if (pcm == NULL || !jpanel_send(pcm, got, s_rec_to)) {
                    /* Nothing recorded, or something already in flight. Said out loud, not
                       swallowed: a child who has just spoken into a blue dot and hears
                       nothing has been told the message went. */
                    if (sound) audio_cue(CUE_OOPS);
                    ESP_LOGW(TAG, "jpanel: nothing to send");
                }
                dirty = true;
            }
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
            /* Armed, not opened: the reply has not started coming out of the speaker yet, let
               alone finished. The edge above does the opening. */
            s_follow_armed = true;
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
               not hear you" and "it is broken" must not look identical (§10.4bc).
               AND NOT A SILENT ONE TO A CHILD EITHER, which it was: the bewildered face
               arrived with no sound at all, and to a four-year-old who has just spoken to a
               toy, silence IS the failure — it is what a broken one does. A low falling pair
               says try again. It is deliberately gentle; it must not read as being told off. */
            s_talk = TALK_FAILED;
            s_talk_since = now;
            if (sound) audio_cue(CUE_OOPS);
            ESP_LOGW(TAG, "talk: no reply");
        } else if (s_talk == TALK_FAILED && now - s_talk_since > TALK_FAILED_MS) {
            s_talk = TALK_IDLE;
        }
        /* THE DEFERRED HALF OF A TOUCH: the cue has finished, so the audio it announced starts
           now. Checked every frame rather than on a timer, so it fires on the first frame the
           speaker is free — the 55 ms cue and this are what a child experiences as one press. */
        if (s_pending != PEND_NONE) {
            if (!audio_playing()) {
                if (s_pending == PEND_PLAY) {
                    if (!jpanel_play_next()) ESP_LOGW(TAG, "jpanel: busy, not fetching");
                } else if (!jpanel_replay()) {
                    ESP_LOGW(TAG, "jpanel: nothing to replay");
                }
                s_pending = PEND_NONE;
                dirty = true;
            } else if (now > s_pending_until) {
                ESP_LOGW(TAG, "jpanel: speaker never freed — dropping the tap");
                s_pending = PEND_NONE;
            }
        }
        /* WHAT THE BOX SAID ABOUT THE MESSAGE, answered in sound because the child who sent
           it is four and the screen is showing a pet. Each outcome gets its OWN cue: "it
           went", "there is nobody to send it to", and "it did not go" are three different
           sentences, and a single beep for all three would teach that pressing makes a noise
           rather than that the message arrived. */
        switch (jpanel_state()) {
        case JPANEL_SENT:
            jpanel_clear();
            if (sound) audio_cue(CUE_SENT);
            jpanel_poll_soon(); /* the twin may already have answered */
            ESP_LOGI(TAG, "jpanel: sent");
            break;
        case JPANEL_NOBODY:
            jpanel_clear();
            if (sound) audio_cue(CUE_OOPS);
            caption_say(&cap, "nobody to send to");
            ESP_LOGW(TAG, "jpanel: no one to send to");
            dirty = true;
            break;
        case JPANEL_FAILED:
            jpanel_clear();
            if (sound) audio_cue(CUE_OOPS);
            ESP_LOGW(TAG, "jpanel: failed");
            break;
        case JPANEL_PLAYING:
            /* Held until the speaker stops, so the repeat window starts when the message
               ENDS rather than when it began — five seconds measured from the wrong end
               would expire before a twenty-second message finished.
             *
               `audio_playing()` READ AGAIN HERE, not the frame's `speaking`. That flag is
               sampled at the top of the frame and a tap LATER in the same frame is what starts
               the message, so on exactly the frame a child presses the pop-up `speaking` still
               says false — and this branch would fire the instant playback began. It used to
               clear the state that armed the acknowledgement, which is how one message came
               back every thirty seconds forever. */
            if (!audio_playing()) {
                jpanel_clear();
                s_repeat_until = now + REPEAT_MS;
                dirty = true;
            }
            break;
        default:
            break;
        }
        /* A MESSAGE ARRIVING IS A FRAME, and it would otherwise not be one. The render loop
           repaints on `dirty`, and nothing about a poll on another task sets it — so the
           pop-up would appear whenever the pet next happened to blink, which is a wait a
           child would spend looking at a panel that knows something and is not saying it. */
        {
            static int s_waiting_shown = -1;
            const int waiting_now = jpanel_waiting(NULL, 0);
            if (waiting_now != s_waiting_shown) {
                /* A SOUND ON THE WAY UP ONLY. The count falls when a message is played, and
                   announcing that would be the panel telling a child about the thing they
                   just did. It also has to survive the first poll after a boot, where -1
                   becomes whatever was already waiting — that IS news to a child who has just
                   turned the panel on, so the initial value is deliberately not 0. */
                if (sound && waiting_now > 0 && waiting_now > s_waiting_shown) {
                    audio_cue(CUE_MESSAGE);
                }
                /* THE CLOCK STARTS WHEN THE WAIT DOES, not when the count last moved. A
                   second message arriving while the first is still unheard must not restore
                   the big box — the child has already been interrupted once and has chosen
                   not to come yet. It restarts only from nothing waiting to something. */
                if (waiting_now > 0 && s_waiting_shown <= 0) s_popup_since = now;
                s_waiting_shown = waiting_now;
                dirty = true;
            }
        }
        /* THE SHRINK IS A FRAME NOBODY ELSE ASKS FOR. The count has not changed, no finger has
           landed and the pet may be perfectly still — so without this the big box would sit
           there until the next blink happened to repaint it. */
        {
            static bool s_popup_was_big;
            const bool big = jpanel_waiting(NULL, 0) > 0 && now - s_popup_since < POPUP_BIG_MS;
            if (big != s_popup_was_big) {
                s_popup_was_big = big;
                dirty = true;
            }
        }
        if (s_repeat_until != 0 && now > s_repeat_until) {
            s_repeat_until = 0;
            dirty = true;
        }
        if (jpanel_running()) dirty = true; /* the count in the run bar has to stay true */
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

        /* ── AWAKE OR NOT, DECIDED ONCE, AFTER EVERYTHING THAT COULD COUNT AS ALIVE ──
         *
         * Idle is not "nobody touched it". A reply playing, a listening turn, a message
         * waiting to be acknowledged, a running animation and a finger mid-gesture are all
         * the panel being USED, and a screen that dimmed under any of them would be a bug
         * with a very visible symptom. So the test reads the same signals the frame above
         * was drawn from, and it reads them here — below every branch that sets them. */
        {
            /* Zero is the never-been-active value and `now` passes through it once a boot, so
               the first frame claims the millisecond after it rather than carrying a second
               flag around all night. */
            static uint32_t active_ms;
            /* Read before anything below can clear it — and `woke_by_touch` is the same fact
               from the branch far above, which has already put the stage back to awake. */
            const bool was_dark = s_sleep == SCREEN_DARK || woke_by_touch;
            if (active_ms == 0) active_ms = now == 0 ? 1 : now;
            /* A MESSAGE ARRIVING WAKES THE SCREEN; A MESSAGE WAITING DOES NOT HOLD IT.
               Counting the queue as activity would mean one unacknowledged good-night left
               the panel lit until morning — the exact thing this feature exists to stop. The
               pop-up is still there when the child touches it awake, because nothing about
               sleeping drops the queue. */
            static int prev_wait;
            const int waiting = jpanel_waiting(NULL, 0);
            const bool arrived = waiting > prev_wait;
            prev_wait = waiting;
            /* VOICE DOES NOT HOLD THE SCREEN AWAKE. The owner: *"the time out should not
               consider voice to be something that extends the activity... You should only be
               on longer from a poke or accelerometer data saying that it was moved."*

               `speech_hearing()` is the VAD — ANY speech in the room. In a bedroom that is a
               parent in the hall, a sibling, a television: all of it held the panel lit, which
               is the opposite of what a sleep timer is for. `heard_voice` went with it, because
               a recognised word is still voice rather than a finger.

               WHAT STAYS IS THE PET ANSWERING, and that is the owner's call between two shapes
               he was offered: ambient speech is ignored, but `speaking` and a live `s_talk`
               hold the screen while a reply is actually coming out. A child who asks a question
               and watches the screen dim mid-sentence reads that as broken, not as asleep. An
               action holds it for the same reason — it is the pet doing the thing it was asked
               to do, and the asking is the part being discounted, not the doing. */
            const bool used = tapped || down || woke_by_touch || s_moved ||
                              gest.taps > 0 || cue > 0.0f || action != ACT_NONE ||
                              speaking || s_talk != TALK_IDLE || s_repeat_until != 0 ||
                              arrived || jpanel_running() ||
                              jpanel_state() != JPANEL_IDLE;
            (void)heard_voice; /* still set for the caption; no longer extends the timer */
            if (s_moved) {
                /* Logged only where it MATTERS — a waking nudge — and with the number that
                   would justify moving the threshold. A panel awake and being played with
                   trips this constantly and has nothing to say about it. */
                if (s_sleep != SCREEN_AWAKE) {
                    ESP_LOGI(TAG, "screen: movement %d counts (threshold %d)", s_move_mag,
                             SCREEN_MOVE_COUNTS);
                }
                s_moved = false;
            }
            if (used) {
                active_ms = now == 0 ? 1 : now;
                if (s_sleep != SCREEN_AWAKE) {
                    sleep_wake(arrived ? "message" : "activity");
                    dirty = true; /* the first frame back is a whole one, not a delta */
                }
            } else {
                const uint32_t idle = now - active_ms;
                const screen_stage_t want = screen_stage(idle);
                if (want != s_sleep) {
                    s_sleep = want;
                    s_brightness_pending = true;
                    ESP_LOGI(TAG, "screen: %s after %u min idle",
                             want == SCREEN_DARK ? "dark" : want == SCREEN_DIM ? "dim" : "awake",
                             (unsigned)(idle / 60000u));
                }
            }
            /* WAKING TO A MESSAGE LEFT OVERNIGHT SHOWS THE BIG BOX AGAIN.
             *
             * The pop-up stands down to a badge after fifteen seconds and deliberately does
             * NOT come back when another message arrives: the child has been interrupted once
             * and has chosen not to come. A night is not that. Whoever is looking at this
             * panel now was not in the room when it shrank, and a corner badge is not how a
             * four-year-old finds out their sister sent them something.
             *
             * Only out of DARK. At dim the screen was visible the whole time, so the reason
             * the badge shrank still holds. And no sound: a message that chirped every time
             * somebody walked past the table would be the panel nagging, which is the thing
             * the stand-down exists to prevent. */
            if (was_dark && s_sleep == SCREEN_AWAKE && waiting > 0) {
                s_popup_since = now;
                dirty = true;
            }
            /* Pinned rather than left to accumulate: the gate below cannot fire while dark,
               so an unbounded counter would be a counter nothing ever reads — and the frame
               that wakes wants it already over the floor so the face comes straight back. */
            if (s_sleep == SCREEN_DARK) since_draw = FACE_FLOOR_MS;
            poll_ms = s_sleep == SCREEN_DARK ? SCREEN_POLL_MS : TOUCH_POLL_MS;
        }

        /* DARK SKIPS THE DRAW AS WELL AS THE LIGHT. Brightness 0 already hides the picture;
           composing and shipping 322 KB to a screen nobody can see is the part that actually
           costs something, and it is the part worth not doing all night. */
        if (s_sleep != SCREEN_DARK && (dirty || since_draw >= FACE_FLOOR_MS)) {
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
            /* DIM MEANS ASLEEP, NOT MERELY DARKER. The owner: *"as the unit starts to time
               out and goes into the dim mode, the robot should go to sleep and should have a
               zzz animation with eyes closed."* Before this the pet carried on with its idle
               loop at a quarter brightness, which reads as neither awake nor asleep.

               Only DIM wears it. At DARK the render loop stops blitting entirely, so there is
               nothing to see and nothing to spend frames on — the zzz lives in the five-to-
               fifteen-minute window and the night is simply dark.

               It defers to a live conversation on purpose: the screen can still be dim while
               the pet answers something asked of it (voice no longer resets the timer), and a
               pet that replied with its eyes shut would look broken rather than sleepy. */
            const bool dozing = screen_dozing(s_sleep, s_talk != TALK_IDLE, speaking);
            face_params_t target;
            emotion_resolve(dozing                    ? FACE_SLEEPY
                            : s_talk == TALK_LISTENING  ? FACE_CURIOUS
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
            /* AFTER the action posed the limbs and before anything reads them: the shuffle
               rides on top, the way the lean itself does, so a pet tilted mid-wave keeps
               waving and moves its feet. */
            rig_walk(&walk, (float)(s_lean - walked_from), &st.rig);
            walked_from = s_lean;
            rig_figure(action, p, action_mag, now, st.eyes.face_ang, &st.fig);
            st.bob = bob_step(frame++);
            st.lean = s_lean;
            /* TWEENED SHUT RATHER THAN SNAPPED, so falling asleep is something you can watch
               happen: the same halving `emotion_approach` uses everywhere else, twice per frame
               for the 25 fps reason given above. The blink value is left alone underneath, so
               waking restores it without a seam. */
            st.open = dozing ? emotion_approach(emotion_approach(st.open, 0.0f, 0.12f), 0.0f, 0.12f)
                             : s_open;
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
                      s_show_version ? ota_running_version() : s_name_up, LABEL_COLOUR);
            /* THE ZZZ, and it drifts rather than blinks. Three Z's rising one after another on
               a slow cycle reads as breathing; all three appearing at once reads as an icon,
               and an icon is a status light rather than a sleeping animal.
               Drawn from the pet's own head rather than a fixed corner, so it follows the
               quarter turn the way the label and the caption do. */
            if (dozing) {
                const uint32_t phase = (now / 700u) % 4u;   /* 0..3: one Z, two, three, rest */
                char z[4] = {0};
                for (uint32_t i = 0; i < phase && i < 3; i++) z[i] = 'Z';
                if (z[0] != '\0') {
                    /* Each Z a little higher and to the right than the last would need per-
                       glyph placement; the font draws a run, so the RUN climbs instead — one
                       step per phase, which gives the same lift for a fraction of the code. */
                    const int zx = FACE_W / 2 + 26 + (int)phase * 4;
                    const int zy = over_y0 + 40 - (int)phase * 6;
                    font_draw(fb, FACE_W, FACE_H, zx, zy, 2, z, LABEL_COLOUR);
                }
            }
            draw_meter(fb, level);
            caption_draw(&cap, fb, FACE_W, over_h, CAPTION_COLOUR);
            if (s_talk == TALK_LISTENING) draw_listening(fb, over_y0, now);
            else if (s_talk == TALK_RECORDING) draw_recording(fb, over_y0, now, s_rec_to);
            /* ONLY WHERE THERE IS SOMETHING TO CONFIRM, and a HELD listen is not it: that turn
               ends on the release of the finger that started it, so a tick would be a second
               way to finish a gesture that already has one, and a cross would be a target the
               finger is not free to reach. Voice-started listens ("hey fish") and messages to
               dad or a sister are the two that run hands-free and need somewhere to press. */
            if ((s_talk == TALK_LISTENING && s_listen_voice) || s_talk == TALK_RECORDING) {
                confirm_draw(fb, FACE_W, FACE_H, over_h);
            }
            else if (s_talk != TALK_IDLE) {
                draw_thinking(fb, over_y0, over_h - over_y0, now, s_talk == TALK_FAILED);
            }
            /* THE POP-UP LAST, OVER EVERYTHING, and only when the panel is otherwise idle.
               A box announcing a message on top of a pet that is mid-sentence, or mid-
               recording, would be two demands on a four-year-old at once — and the one it
               covers is the one they are already doing. It is not lost: the count lives on
               the box and the next idle frame draws it. */
            s_popup_box[0] = -1;
            /* AND NOT WHILE THE FETCH IT STARTED IS STILL RUNNING. Clearing the rectangle on
               the tap is not enough on its own: the count does not drop until the box hands
               the message over, so the very next frame would draw the pop-up again and the
               child would press a button that `jpanel_play_next` now refuses. The BUSY state
               is the gap between the press and the sound, and it belongs to the fetch. */
            if (s_talk == TALK_IDLE && !speaking && jpanel_state() != JPANEL_BUSY) {
                char from[32];
                const int waiting = jpanel_waiting(from, sizeof(from));
                if (waiting > 0) {
                    /* Big for the first fifteen seconds, then a badge. The AGAIN button owns
                       the same corner for its five seconds and wins there — it is transient
                       and it answers a question the child is asking right now ("what did she
                       say?"), where the badge answers one they have already declined. */
                    if (now - s_popup_since < POPUP_BIG_MS) {
                        draw_popup(fb, over_y0, over_h, from, waiting);
                    } else if (s_repeat_until == 0) {
                        draw_popup_badge(fb, over_y0, from);
                    }
                }
            }
            /* Drawn over everything else while a run is sounding, including the caption: a
               control that can end what the child is hearing outranks a ticker telling them
               what it heard. */
            if (jpanel_running()) {
                draw_run(fb, over_y0, over_h, jpanel_waiting(NULL, 0));
            } else if (s_repeat_until != 0) {
                draw_repeat(fb, over_h);
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
                s_blit_fail_total++;
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
                    display_note_restart(DISPLAY_RESTART_BLIT_HEAL);
                    vTaskDelay(pdMS_TO_TICKS(150)); /* let the log drain */
                    esp_restart();
                }
            } else {
                if (s_blit_fails > 0) {
                    ESP_LOGI(TAG, "blit recovered after %d failures", s_blit_fails);
                    s_blit_fails = 0;
                    s_blit_recoveries++;
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
            /* The two paths through here are the child's gesture and the OTA's parked
               restart, and they are the two the box could not tell apart. */
            display_note_restart(rebooting ? DISPLAY_RESTART_GESTURE : DISPLAY_RESTART_OTA_PARK);
            vTaskDelay(pdMS_TO_TICKS(150));
            esp_restart();
        }

        if (s_brightness_pending) {
            s_brightness_pending = false;
            PHASE(14);
            apply_brightness();
        }
        /* A settings refresh lands every three seconds, so a body chosen from the PWA reaches
           a panel on a wall without anyone touching it. Taken on the RENDER task, which is the
           only one allowed to write `st`, and only when it actually differs — assigning every
           cycle would fight the four-tap toggle, snapping a child's live choice back before she
           had finished the gesture. */
        if (s_form_pending >= 0) {
            st.form = (face_form_t)s_form_pending;
            s_form_pending = -1;
            dirty = true;
            ESP_LOGI(TAG, "form <- box: %s", st.form == FORM_OSTRICH ? "ostrich" : "robot");
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
        vTaskDelay(pdMS_TO_TICKS(poll_ms));
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
        since_beat += poll_ms;
        if (since_beat >= BEAT_MS) {
            since_beat = 0;
            /* THE STAGE IS ON THIS LINE BECAUSE OF WHAT THE LINE IS FOR. It exists so that a
               loop which has stopped blitting says so rather than going quiet — and a
               sleeping screen stops blitting ON PURPOSE. Without the stage here the two are
               the same log. */
            ESP_LOGI(TAG, "render: %d frames ok, %d failed | screen %s | internal largest %u",
                     s_blit_ok, s_blit_fails, display_screen(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                                MALLOC_CAP_DMA));
        }
        since_draw += poll_ms;
        since_reassert += poll_ms;
        since_sample += poll_ms;
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
