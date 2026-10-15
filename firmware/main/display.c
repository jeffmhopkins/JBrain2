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
#include "esp_sleep.h"
#include "esp_timer.h"
#include "face.h"
#include "font.h"
#include "gesture.h"
#include "jpanel.h"
#include "panel_sm.h"
#include "stream.h"
#include "rig.h"
#include "speech.h"
#include "vocab.h"
#include "variants.h"
#include "orient.h"
#include "poop.h"
#include "game.h"
#include "tickle.h"
#include "screen.h"
#include "ota.h"
#include "overlay.h"
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
static void frame_note(uint32_t pass_start);
static uint32_t s_fr_blit_at; /* when this frame's blit began */

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
static volatile display_restart_t s_restart_why_req = DISPLAY_RESTART_OTA_PARK;

void display_request_restart(void)
{
    display_request_restart_for(DISPLAY_RESTART_OTA_PARK);
}

void display_request_restart_for(display_restart_t why)
{
    s_restart_why_req = why;
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

/* WHICH BODY THE PANEL COMES BACK AS. Seven taps and a hold still toggles it live; this is the
   answer it starts from, and until the box could hold one it was always the ostrich — so every
   reboot and every OTA quietly undid a child who had chosen the robot.
   Deferred to the render task like the brightness above, and for a weaker version of the same
   reason: `st` belongs to that task, and writing a field of it from `apply_settings()` would be
   a cross-task write into a struct being tweened at ~25 fps. */
static volatile int s_form_box = -1;     /* what the box last SAID, not what is on screen */
static volatile int s_form_pending = -1; /* a change for the render task to take */

/* TAKEN ON CHANGE, NOT ON EVERY FETCH, and the difference is a child's afternoon. `apply_settings`
   runs every three seconds and the box serves a form unconditionally, so acting on each answer
   would re-assert the owner's choice twenty times a minute — Quorra switches to the robot, and the
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

/* STANDBY — the button's short press: *"single press blinks the screen and stops listening. But it
 * keeps looking for incoming messages and will light up if a new message comes in."* It is the
 * machine's (`sm_t.standby`, the button's effects); this file only paints it dark and keeps it
 * dark. It is NOT the idle sleep wearing a different hat: the microphone is off as well, and the
 * network stays up so a message from Dad still lights the room. */
/* DARK HAS TO BE PAINTED, NOT SWITCHED OFF, and that is a consequence of the brightness revert
 * rather than a choice. `0x51` is inert on these panels (`nudge.h` and `QSPI_CMD` in this file
 * carry why), so `screen_level()` returning 0 changes nothing on the glass — and "dark" was
 * only ever *stop blitting*. On an AMOLED the last frame then simply stays there.
 *
 * The owner, pressing the button: *"it still just shows a frozen robot when you press it."*
 * Exactly right, and it is the same fault behind the idle stages never visibly dimming. A
 * black FRAME costs one blit and needs no working brightness register, so the screen actually
 * goes out — which is what both the standby press and the fifteen-minute dark always meant. */
static bool s_blank_pending;
/* The hold completed; the render loop parks and powers down at the safe point. */
static bool s_power_off;
/* Set in `update_orientation()` and consumed by the same task a few lines later — the IMU
   read is where these numbers already are, so movement costs no extra bus traffic. */
static bool s_moved;
static int s_move_mag;
/* A shake (`screen_shaken`), raised here and taken by the render loop as the dizzy action. */
static shake_t s_shake;
static bool s_shaken;

/* Only ever called on the render task: the write itself is deferred to the same
   `s_brightness_pending` hand-off the box's setting uses, for the reason above it. */
static void sleep_wake(const char *why)
{
    if (s_sleep == SCREEN_AWAKE) return;
    ESP_LOGI(TAG, "screen: waking (%s)", why);
    s_sleep = SCREEN_AWAKE;
    s_brightness_pending = true;
}

/* A COMMAND, FRAMED FOR QSPI — AND ITS ABSENCE IS WHY BRIGHTNESS NEVER WORKED.
 *
 * THE BUG: this panel is opened in QSPI mode (`use_qspi_interface`, `lcd_cmd_bits = 32`), and
 * on that bus a command is a 32-bit prologue, not a byte. CO5300 datasheet V0.01 p.21, "QSPI
 * write protocol":
 *
 *     Instruction[7:0] = 02h                 Command write
 *     AD[23:0] = {8'h00, CMD[7:0], 8'h00}    Driver IC command address
 *
 * The vendor driver wraps every command it sends (`esp_lcd_co5300_spi.c`, `tx_param`), which
 * is why the `init_cmds` array above — including `0x51 = 0xFF` — has always worked. The two
 * calls this file made DIRECTLY did not wrap, so a bare `0x51` went out as instruction `0x00`,
 * which is not a defined QSPI instruction. The controller discarded it. The SPI transfer
 * succeeded, so `esp_lcd_panel_io_tx_param` returned ESP_OK every time.
 *
 * MEASURED FROM THE OTHER END, BY THE OWNER: brightness has never once changed on real
 * hardware — not from the box's setting, and not from the panel's OWN dim-on-sleep stage,
 * which needs no network at all. That second half is what proves it was never the settings
 * fetch: the dim path is entirely local and it was equally dead.
 *
 * AND IT INVALIDATES TWO CONCLUSIONS ALREADY WRITTEN ABOVE. The `REASSERT_MS` experiment was
 * re-sending `0x29` and `0x51` through this same unwrapped path, so its negative result says
 * nothing about the OLED rail or the AXP2101. And "this panel does not answer reads over QSPI"
 * was concluded from unwrapped READS, which need their own opcode (0x03, datasheet p.28) —
 * the zeros were the bus, not the panel. Both are now open questions again rather than
 * settled ones.
 *
 * Verified against the datasheet before changing anything: `0x51` takes ONE byte (p.184), the
 * `0x53 = 0x20` we already write is sufficient because bit 5 (BCTRL) is what gates it and the
 * CO5300 has no backlight bit at all (p.186), and `0x63` is HBM-mode brightness which is inert
 * while `0x66`'s HBM_EN stays 0, as it does (p.196, p.199). None of those needed changing.
 *
 * ---------------------------------------------------------------------------------------------
 * AND THEN IT TOOK BOTH PANELS DOWN, so the framing is BACKED OUT and this macro is kept only
 * as the record of what the right command word is.
 *
 * MEASURED 2026-09-27, within minutes of deploying 0.3.16: both panels went black, and — unlike
 * every previous black screen on this hardware — they made NO SOUND and did not answer touch.
 * The owner spotted that distinction: *"this is not responding to screen clicks. There's no
 * audible sound effect like there was before, so this I feel is different."* Display, cues and
 * touch are all the render task, so losing all three at once is that ONE task dying, not three
 * faults. The panels stayed on the network throughout — their jpanel task kept polling — and
 * only a power cycle brought them back.
 *
 * THE MECHANISM, AS FAR AS IT IS ESTABLISHED: `esp_lcd_panel_draw_bitmap` QUEUES and returns
 * (see the stripe-blit comment at the top of this file), so a colour transfer can still be in
 * flight when it does. `reassert_panel` fires a command write into the same interface every
 * thirty seconds. Before 0.3.16 those writes were malformed and the controller discarded them;
 * after it they are valid and the controller ACTS on them — and it did not survive being told
 * to do so mid-frame. That last step is inference, not proof: what is measured is that framing
 * them correctly hangs the renderer and unframing them does not.
 *
 * SO BRIGHTNESS IS BROKEN AGAIN, DELIBERATELY. A panel that cannot dim is a nuisance; a black
 * panel in a four-year-old's bedroom that answers nothing is not, and it cannot be diagnosed
 * from the box because the part that would report it is the part that died. Re-landing this
 * needs the writes ordered against the blit — which needs a panel on a bench with a cable, not
 * a guess deployed to a bedroom. */
#define QSPI_WRITE_OPCODE 0x02
#define QSPI_CMD(c) (((QSPI_WRITE_OPCODE) << 24) | ((c) << 8))

/* The local copy is not a style choice: `esp_lcd_panel_io_tx_param` takes a plain `const
   void *`, and handing it a pointer into volatile storage discards the qualifier. */
static void apply_brightness(void)
{
    if (s_io == NULL) return;
    const uint8_t level = screen_level(s_brightness, s_sleep, s_dim_percent);
    /* UNFRAMED ON PURPOSE, AND THIS IS A RETREAT RATHER THAN A DESIGN. See `QSPI_CMD` above:
       framing these correctly in 0.3.16 made brightness work for the first time, and took BOTH
       panels' renderers down with it within minutes. */
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
static calib_t s_touch_cal;
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

/* HOW LONG A CROSSHAIR WAITS BEFORE GIVING UP. Long enough that an owner reading the instruction
   for the first target is not hurried, short enough that a panel left showing one is a pet again
   before the child who triggered it has gone to find somebody. */
#define CAL_IDLE_MS 30000
static uint32_t s_cal_last_tap;

static void cal_begin(void)
{
    s_cal_active = true;
    s_cal_i = 0;
    calib_sample_reset(&s_cal_s);
    /* Zero means "not started yet" to the idle timer, which stamps it on its first pass. Cleared
       here rather than stamped, because `esp_timer` is not read from this function. */
    s_cal_last_tap = 0;
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

/* WHAT A PRESS, A PHRASE OR A MESSAGE DOES IS NOT DECIDED HERE ANY MORE (0.4.0).
 *
 * Press and hold, listening, recording, playing, the again/reply offer, the notice and the chime
 * are one state machine, `panel_sm.c`, with every timing number and the reason for it in
 * `panel_sm.h`. Until 0.4.0 that logic lived twice — a chain of `if`s here and a table in `ui.c`
 * — and every touch fault the owner reported was the two disagreeing. This file now samples the
 * inputs once per frame, runs the machine's effects in order, and draws its `sm_view_t`.
 * `make check_no_logic_in_display` fails the host build if a piece of the machine's state, or a
 * call into the engine or the speaker outside the executor's list, comes back into this file. */

/* The machine and its two frame structs: static because the render stack is 8 KB and these are
   kilobytes, and in PSRAM because internal RAM is what the radio and TLS starve for (plan §6a).
   Nothing here is touched by DMA, an ISR or a compare-and-swap. */
EXT_RAM_BSS_ATTR static sm_t s_sm;
EXT_RAM_BSS_ATTR static sm_in_t s_in;
EXT_RAM_BSS_ATTR static sm_out_t s_out;
/* The engine's snapshot as render last copied it: a torn read keeps this one (plan §7.2). */
EXT_RAM_BSS_ATTR static jp_view_t s_snap;
EXT_RAM_BSS_ATTR static sm_view_t s_drawn;

/* What the last swipe measured, and how many there have been. Telemetry only, and they are here
   because the CST820 reporting a LIVE position while a finger moves is an assumption this firmware
   makes and cannot check from a desk — the same reason the raw tap point already rides telemetry
   (`touch.h`). A panel whose `swipes` stays 0 while the owner swears he swiped is a controller that
   only reports edges, and `swipe_dx` says how far it thought the finger got. */
static uint16_t s_swipes;
static int s_swipe_dx;
/* Which cause the last red dash had. The machine counts them (`sm_t.dashes`) because it is the
   one that raises them; the name is the reason it reported, kept for the report. */
static const char *s_dash_why = "";

void display_dashes(unsigned *n, const char **why, uint32_t *ago_ms)
{
    if (n != NULL) *n = s_sm.dashes;
    if (why != NULL) *why = s_dash_why;
    /* Unsigned, so this stays right across the ~49-day rollover of the millisecond clock — a
       panel that has been up seven weeks is exactly the one whose faults nobody has looked at. */
    if (ago_ms != NULL) {
        const uint32_t at = s_sm.dash_at;
        *ago_ms = at == 0 ? 0 : (uint32_t)((uint32_t)(esp_timer_get_time() / 1000) - at);
    }
}

void display_swipes(unsigned *n, int *last_dx, int *sel)
{
    if (n != NULL) *n = s_swipes;
    if (last_dx != NULL) *last_dx = s_swipe_dx;
    if (sel != NULL) *sel = s_sm.sel.index;
}

/* The machine's counters for the report's `jp` object (plan §7.7). Written on the render task
   only; the main task's read of a word is a snapshot, which is all a report is. */
static volatile uint32_t s_violations;
static volatile uint32_t s_late_outcomes;
static volatile uint32_t s_lease_refusals;

void display_jp_stats(jp_stats_t *st)
{
    st->sm_violations = s_violations;
    st->late_outcome = s_late_outcomes;
    st->lease_refusals += s_lease_refusals;
    st->cue_expired = stream_facts(audio_stream()).cue_expired;
}

/* THE EXECUTOR'S ANSWERS FOR THE NEXT FRAME. `in` is sampled before the step and the effects run
   after it, so a post the queue refused and a capture the lease refused are reported back as the
   next frame's inputs (`post_failed`, `capture_refused`), never acted on here. */
static uint32_t s_post_failed_next;
static bool s_refused_next;
/* A wake the sleep stage decided AFTER the step (an arrival, activity), handed to the next one:
   standby is the machine's to clear, through its wake input, so there is one writer. */
static bool s_wake_next;
static bool s_dark_wake_next;

/* THE TOUCH SAMPLER'S OWN STATE — where this press landed (panel coordinates, for the rim margin
   and the swipe's origin), whether a finger is on the glass for the up edge, and whether this
   press has already been reported as a swipe, so one drag is one swipe however long it lasts. */
static int s_down_x = -1;
static int s_down_y = -1;
static bool s_finger;
static bool s_swipe_latched;

/* PLACEHOLDERS UNTIL THE F4 MOCKS ARE APPROVED (plan §15 row F): a send in flight and its outcome
   had nothing on the glass at all (B5) — ten to twenty seconds of a pet that looked as if it had
   ignored her. Each outcome gets its own word and colour so NOBODY and a failure no longer look
   alike. The shapes are deliberately plain; F4 replaces them. */
#define RESULT_SHOW_MS 2500
static const char *s_result_word;
static uint16_t s_result_colour;
static uint32_t s_result_until;

/* Declared here because `tap_to_overlay` below needs it and the orientation block that owns
   it comes later in the file. */
static bool s_upside_down;
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
/* The tilt along the viewer's horizontal, -1..1 of LEAN_FULL, unsmoothed: what the poop slides
   on (`poop.h`). The lean is a position the tilt picks; the poop has momentum of its own. */
static float s_tilt_g;
/* THE OTHER TILT, toward and away from the viewer: the accelerometer across the glass, raw.
   The game (`game.h`) steers by its change from where it was when the game began, so it works
   held upright or leaning back alike (0.4.14). */
static int s_az;
/* Gravity along the glass, toward the viewer's down (`orient_axes`): the pitch's other leg. */
static int s_grav_down;
/* THE GAME, when one is on, and the tilt it started from. Static rather than on the render
   task's stack: half a kilobyte the 8 KB stack has better uses for. */
static game_t s_game;
static float s_game_tx0;
static float s_game_pitch0;
/* THE HEAD RIGHTING (`face_state_t.upright`), in degrees — `orient_upright`. */
static float s_upright;
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

/* A PRESS, DEBOUNCED — AND THE DEBOUNCE IS WHAT MAKES A TOGGLE POSSIBLE AT ALL.
 *
 * This was a one-second hold for two releases, because the contact BOUNCES: measured
 * 2026-09-27, fifteen deliberate presses were reported as `boot_btn` 21, about 1.4 edges per
 * press. A hold is immune to that by construction — bounce at the start only re-times it.
 *
 * The owner, after using it: *"let's change from holding the button to just the press of the
 * button. Press the button shows it press the button hides it."* A toggle is the right feel
 * and a hold is a thing you have to be taught. But a toggle on a bouncing contact is the worst
 * of both: the second phantom edge arrives a few milliseconds after the first and closes what
 * the same press just opened, so the menu would flicker and appear not to work — and that
 * would look exactly like the panel ignoring a child, which is the failure this whole feature
 * exists to remove.
 *
 * So the bounce is handled here instead of being designed around. A press is a falling edge
 * that is at least `BOOT_DEBOUNCE_MS` after the last one this accepted. That window is far
 * longer than any contact bounce and far shorter than a four-year-old's second deliberate
 * press, so every real press counts once and no phantom one counts at all.
 *
 * WHAT THIS GIVES UP, deliberately: a brush against the button now opens the menu, where a
 * hold could not be triggered by accident. That is the owner's call and it is the right one —
 * the menu is modal, it times out on its own, and the cost of an accidental open is one press
 * of the cross, against the cost of a control that children could not work. */
#define BOOT_DEBOUNCE_MS 250

/* HOW LONG IS "OFF". Five seconds, as the owner asked, and it is deliberately far past
   anything a child produces by leaning on the button: this is the one control on the panel
   whose outcome cannot be undone from the panel, because a unit in deep sleep answers nothing
   but its own button. */
#define BOOT_SLEEP_MS 5000
/* How long to wait for the finger to leave the button before sleeping anyway. Generous: a
   deliberate five-second hold is usually released within a second of the panel acknowledging
   it, and the cost of waiting is a panel that is awake slightly longer. */
#define POWER_RELEASE_MAX_MS 10000

static uint32_t s_boot_last_ms;  /* when this last ACCEPTED a press; 0 = never */
static uint32_t s_boot_down_at;  /* when the current press began; 0 = not down */
static bool s_boot_short;        /* a completed SHORT press, consumed by the frame loop */
static bool s_boot_sleep;        /* the hold completed; the frame loop parks and sleeps */

/* THE ACTION IS ON THE RELEASE, AND THAT IS THE WHOLE REASON THIS IS NOT A TOGGLE ANY MORE.
 *
 * A five-second hold begins with exactly the same falling edge a tap does. Acting on the edge
 * would mean every attempt to turn the panel OFF first turned the screen off and the microphone
 * off — the short-press action — and the child would be watching for the result of a gesture
 * that had already half-fired. So a press is only a press once it ends, and it only counts as
 * one if it ended soon enough. */
static void boot_button_poll(void)
{
    const bool down = gpio_get_level(BOOT_BTN) == 0; /* active low, pulled up */
    const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    if (down && !s_boot_was_down &&
        (s_boot_last_ms == 0 || now - s_boot_last_ms >= BOOT_DEBOUNCE_MS)) {
        s_boot_last_ms = now == 0 ? 1 : now;
        s_boot_down_at = now == 0 ? 1 : now;
        s_boot_presses++;
        ESP_LOGI(TAG, "boot button: press %d", s_boot_presses);
    }
    if (down && s_boot_down_at != 0 && now - s_boot_down_at >= BOOT_SLEEP_MS) {
        /* FIRES WHILE THE FINGER IS STILL DOWN, on purpose: the person holding it needs to
           find out they have succeeded without having to guess when to let go. */
        s_boot_down_at = 0;
        s_boot_sleep = true;
        ESP_LOGW(TAG, "boot button: held — powering down");
    }
    if (!down && s_boot_was_down && s_boot_down_at != 0) {
        s_boot_short = true;
        s_boot_down_at = 0;
    }
    s_boot_was_down = down;
}

/* A completed SHORT press. Consumed, so one press is one answer. */
static bool boot_button_take(void)
{
    const bool was = s_boot_short;
    s_boot_short = false;
    return was;
}

static bool boot_button_sleep_requested(void)
{
    const bool was = s_boot_sleep;
    s_boot_sleep = false;
    return was;
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
#define SQ ORIENT_SQ                  /* the side of the square a quarter turn preserves */
#define SQ_Y0 ORIENT_SQ_Y0            /* where it sits in the portrait frame */
_Static_assert(SQ == FACE_W && SQ_Y0 == (FACE_H - SQ) / 2, "the square is centred in the frame");
#define COL_STRIPE 16                 /* columns per transfer; 368 / 16 = 23 exactly */

/* THE SQUARE SLIDES, because the glass is 448 wide on its side and the square is 368. The
 * owner, landscape: *"his feathers are cutting off before we even get into the left extent of
 * the screen when tilting."* The lean is clamped inside the square, so at full lean the bird
 * was cropped against an edge 40 px short of the glass's.
 *
 * So the whole square follows the lean into the bands (`orient_window`): at full lean its edge
 * IS the glass's. Moving the window rather than widening the frame keeps every drawing, overlay
 * and hit test in the same 368 square they were written for, and the blit and the touch both
 * go through `orient_panel_to_frame`, so they cannot disagree about where it is.
 *
 * `s_win` is what the next blit will use, `s_win_shown` is what is on the glass — a touch is
 * read against the picture the finger saw. */
static int s_win;
static int s_win_shown;

/* Black over panel rows [y0, y1): the strip of band the square just slid out of. */
static esp_err_t clear_rows(int y0, int y1)
{
    while (y0 < y1) {
        int rows = y1 - y0;
        if (rows > STRIPE_ROWS) rows = STRIPE_ROWS;
        uint16_t *dst = next_stripe();
        memset(dst, 0, (size_t)rows * FACE_W * sizeof(uint16_t));
        const esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, 0, y0, FACE_W, y0 + rows, dst);
        if (err != ESP_OK) return err;
        y0 += rows;
    }
    return ESP_OK;
}

static esp_err_t blit_frame_rotated(const uint16_t *fb, bool clockwise)
{
    if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
    const int win = s_win, wy = SQ_Y0 + win;
    for (int x0 = 0; x0 < FACE_W; x0 += COL_STRIPE) {
        uint16_t *dst = next_stripe();
        for (int c = 0; c < COL_STRIPE; c++) {
            const int x = x0 + c;
            /* Both directions walk the source consecutively; only the sign differs. */
            int fx = 0, fy = 0;
            orient_panel_to_frame(clockwise ? 1 : 3, win, x, wy, &fx, &fy);
            const uint16_t *src = &fb[(size_t)fy * FACE_W + fx];
            const int step = clockwise ? -1 : 1;
            for (int r = 0; r < SQ; r++) dst[r * COL_STRIPE + c] = src[r * step];
        }
        const esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, x0, wy, x0 + COL_STRIPE,
                                                        wy + SQ, dst);
        if (err != ESP_OK) return err;
    }
    /* What the old window covered and the new one does not still shows the old picture. */
    const int was = s_win_shown;
    s_win_shown = win;
    if (win > was) return clear_rows(SQ_Y0 + was, wy);
    if (win < was) return clear_rows(wy + SQ, SQ_Y0 + was + SQ);
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
    orient_panel_to_frame(s_quarter, s_win_shown, px, py, fx, fy);
}

static esp_err_t blit_frame(const uint16_t *fb)
{
    if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
    if (s_quarter == 1 || s_quarter == 3) {
        if (s_quarter_changed) {
            /* The bars beside the square are never written again, so whatever the portrait
               frame last left there would stay forever. Once, on the turn, not per frame. */
            s_quarter_changed = false;
            s_win_shown = 0;
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
        static int16_t last_accel[3];
        const int16_t cur[3] = {ax, ay, az};
        if (seen) {
            const int d = screen_motion(last_accel, cur);
            if (screen_moved(d)) {
                s_moved = true;
                s_move_mag = d;
            }
            if (screen_shaken(&s_shake, d, (uint32_t)(esp_timer_get_time() / 1000))) {
                s_shaken = true;
            }
        }
        seen = true;
        for (int i = 0; i < 3; i++) last_accel[i] = cur[i];
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
    int tilt = 0, down = 0;
    orient_axes(s_quarter, ax, ay, &tilt, &down);
    s_tilt_g = (float)tilt / (float)LEAN_FULL;
    s_az = az;
    s_grav_down = down;
    const int lean_max = side ? FACE_LEAN_MAX_SIDE : FACE_LEAN_MAX;
    int target = tilt * lean_max / LEAN_FULL;
    if (target > lean_max) target = lean_max;
    if (target < -lean_max) target = -lean_max;
    s_lean += (target - s_lean) / LEAN_SMOOTH;

    /* The square follows the lean into the bands (`orient_window`). */
    s_win = orient_window(s_quarter, s_lean, lean_max);

    /* AND THE HEAD RIGHTS ITSELF while the body slides (`orient_upright`). */
    const float up = orient_upright(tilt, down, ORIENT_UPRIGHT_SHARE, ORIENT_UPRIGHT_MAX_DEG);
    s_upright += (up - s_upright) / (float)LEAN_SMOOTH;
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
        case DISPLAY_RESTART_WD_ENGINE: s_restart_why_at_boot = "wd-engine"; break;
        case DISPLAY_RESTART_WD_ARBITER: s_restart_why_at_boot = "wd-arbiter"; break;
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

/* FRAME TIMING, for the report: how many frames were drawn since the last read, how long one took
   to draw and blit, and the gap between them. What a person calls smooth is the gap. */
static uint32_t s_fr_n, s_fr_draw_sum, s_fr_draw_max, s_fr_blit_sum, s_fr_blit_max;
static uint32_t s_fr_gap_sum, s_fr_gap_max, s_fr_last;

static void frame_note(uint32_t pass_start)
{
    const uint32_t t = (uint32_t)(esp_timer_get_time() / 1000);
    /* Split at the blit (0.4.4): the pass up to it is sampling, the machine and the drawing; the
       rest is the QSPI transfer. Which one to work on next depends on which is bigger. */
    const uint32_t draw = s_fr_blit_at - pass_start, blit = t - s_fr_blit_at;
    s_fr_draw_sum += draw;
    if (draw > s_fr_draw_max) s_fr_draw_max = draw;
    s_fr_blit_sum += blit;
    if (blit > s_fr_blit_max) s_fr_blit_max = blit;
    /* A gap over a second is the screen resting at its floor, not a dropped frame. */
    if (s_fr_last != 0 && t - s_fr_last < 1000) {
        const uint32_t gap = t - s_fr_last;
        s_fr_gap_sum += gap;
        if (gap > s_fr_gap_max) s_fr_gap_max = gap;
        s_fr_n++;
    }
    s_fr_last = t;
}

void display_frame_stats(display_frame_stats_t *out)
{
    const uint32_t n = s_fr_n;
    out->frames = n;
    out->draw_avg = n ? s_fr_draw_sum / n : 0;
    out->draw_max = s_fr_draw_max;
    out->blit_avg = n ? s_fr_blit_sum / n : 0;
    out->blit_max = s_fr_blit_max;
    out->gap_avg = n ? s_fr_gap_sum / n : 0;
    out->gap_max = s_fr_gap_max;
    s_fr_n = s_fr_draw_sum = s_fr_draw_max = s_fr_blit_sum = s_fr_blit_max = 0;
    s_fr_gap_sum = s_fr_gap_max = 0;
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

/* ── THE EXECUTOR ─────────────────────────────────────────────────────────────────────────────
 *
 * The machine's effects, run in the order it emitted them, after the step that emitted them. The
 * only calls into the engine and the speaker this file makes are here (and the per-frame copies
 * the sampler takes); `check_no_logic_in_display` holds the list. */

/* WHICH "HAPPY BIRTHDAY" THIS IS, so all three phrases of one song share a variant and stay
   in one key, while the next birthday is sung a little differently (`cue_is_tune`). */
static unsigned s_song;

/* An action's sound at progress `p`: a tune's piece in this song's key, anything else with
   its own per-cue counter. */
static void play_action_cue(stream_t *st, action_t a, float p)
{
    const cue_t c = cue_for_action_at((int)a, p);
    if (cue_is_tune(c)) {
        stream_cue(st, STREAM_SLOT_PRESS, c, STREAM_CUE_GAIN, s_song);
    } else {
        stream_cue_play(st, STREAM_SLOT_PRESS, c);
    }
}

/* A SEND takes the capture into the post: the buffer is what the engine uploads, and lease 1 —
   taken BEFORE the post, released by the engine at every end of the SEND, or here if the post
   fails — is what stops a recording opening over it (A5). A PLAY or REPLAY is stamped with the
   stop gen it was posted under, so a STOP that follows makes it stale. */
static void post_cmd(stream_t *st, const jp_cmd_t *posted)
{
    jp_cmd_t c = *posted;
    if (c.kind == JP_CMD_SEND) {
        size_t bytes = 0;
        const int16_t *pcm = audio_capture_close(&bytes);
        if (pcm == NULL || bytes < sizeof(int16_t)) {
            ESP_LOGW(TAG, "jpanel: nothing recorded to send");
            s_post_failed_next = c.seq;
            return;
        }
        if (!stream_lease_take(st, STREAM_LEASE_SEND)) {
            s_lease_refusals++;
            ESP_LOGW(TAG, "jpanel: capture lease held — not sending");
            s_post_failed_next = c.seq;
            return;
        }
        c.pcm = pcm;
        c.bytes = bytes;
        ESP_LOGI(TAG, "jpanel: sending %u bytes to %s", (unsigned)bytes,
                 c.to == JP_TO_DAD ? "dad" : "the other panel");
        if (!jpanel_post(&c)) {
            stream_lease_release(st, STREAM_LEASE_SEND);
            s_post_failed_next = c.seq;
        }
        return;
    }
    c.gen = stream_gen(st);
    if (!jpanel_post(&c)) {
        ESP_LOGW(TAG, "jpanel: post refused (queue full or no task)");
        s_post_failed_next = c.seq;
    }
}

/* Lease 2 before `talk_send` (plan §7.3): talk uploads straight out of the capture buffer, and
   releases the lease when its request returns. Refused here, the machine's turn reads the idle
   talk state on the next frame and clears itself. */
static void send_talk(stream_t *st)
{
    size_t bytes = 0;
    const int16_t *pcm = audio_capture_close(&bytes);
    if (pcm == NULL || bytes < sizeof(int16_t)) {
        ESP_LOGI(TAG, "talk: nothing recorded");
        return;
    }
    if (!stream_lease_take(st, STREAM_LEASE_TALK)) {
        s_lease_refusals++;
        ESP_LOGW(TAG, "talk: capture lease held — not sending");
        return;
    }
    if (!talk_send(pcm, bytes)) {
        stream_lease_release(st, STREAM_LEASE_TALK);
        ESP_LOGW(TAG, "talk: a turn is already in flight");
    } else {
        ESP_LOGI(TAG, "talk: sent %u bytes", (unsigned)bytes);
    }
}

/* PLACEHOLDER (F4): a send's outcome, top right, until RESULT_SHOW_MS has passed. */
static void show_result(cue_t c, bool nobody, uint32_t now)
{
    overlay_result(c, nobody, &s_result_word, &s_result_colour);
    s_result_until = now + RESULT_SHOW_MS;
}

/* Returns whether the pet should nod (a reply arrived). */
static bool execute(stream_t *st, bool sound, uint32_t now)
{
    const sm_out_t *o = &s_out;
    bool nod = false;
    /* THE STOP WORD GOES OUT BEFORE THE SILENCE IT PAIRS WITH (plan §7.3): an arbiter that saw
       the silence without the new gen would discard the stream once and then play on whatever the
       producer wrote next. The machine lists SILENCE first because that is the order a child
       hears; a STOP later in the same frame is therefore hoisted to the silence. */
    bool hoisted[SM_FX_MAX] = {false};
    for (int i = 0; i < o->n; i++) {
        const sm_fx_t *f = &o->fx[i];
        switch (f->kind) {
        case SM_FX_SILENCE: {
            bool any = false, heard = false;
            for (int j = i + 1; j < o->n; j++) {
                if (o->fx[j].kind != SM_FX_STOP || hoisted[j]) continue;
                any = true;
                heard |= o->fx[j].arg != 0;
                hoisted[j] = true;
            }
            if (any) stream_stop(st, heard);
            stream_silence(st);
            break;
        }
        case SM_FX_STOP:
            if (!hoisted[i]) stream_stop(st, f->arg != 0);
            break;
        case SM_FX_CUE:
            if (sound) stream_cue_play(st, (stream_slot_t)f->slot, (cue_t)f->arg);
            if (f->slot == STREAM_SLOT_SEND_RESULT) show_result((cue_t)f->arg, o->say != NULL, now);
            break;
        case SM_FX_PAUSE:
            stream_pause_req(st, (uint8_t)f->slot, f->arg != 0);
            break;
        case SM_FX_CAPTURE_OPEN:
            if (!audio_capture_open()) {
                ESP_LOGW(TAG, "capture refused (leased, or no buffer)");
                s_refused_next = true;
            }
            break;
        case SM_FX_CAPTURE_DROP: {
            size_t dropped = 0;
            (void)audio_capture_close(&dropped);
            ESP_LOGI(TAG, "capture dropped, %u bytes discarded", (unsigned)dropped);
            break;
        }
        case SM_FX_POST:
            post_cmd(st, &o->post);
            break;
        case SM_FX_SEND_TALK:
            send_talk(st);
            break;
        case SM_FX_TALK_CLEAR:
            talk_clear();
            break;
        case SM_FX_POLL_SOON:
            jpanel_poll_soon();
            break;
        case SM_FX_STANDBY:
            if (f->arg) {
                s_sleep = SCREEN_DARK;
                s_brightness_pending = true;
                ESP_LOGI(TAG, "standby: screen off, microphone off, still reachable");
            } else {
                sleep_wake("button");
            }
            break;
        case SM_FX_BLANK:
            s_blank_pending = true;
            break;
        case SM_FX_DEEP_SLEEP:
            s_power_off = true;
            break;
        case SM_FX_NOD:
            nod = true;
            break;
        case SM_FX_FLINCH:
            s_flinch = 1.0f;
            break;
        case SM_FX_MUTE_COMMANDS:
            /* Derived by the machine every frame (recording, listening, standby), so no exit can
               leave the recogniser deaf; `speech_mute_commands` logs only on a change. */
            speech_mute_commands(f->arg != 0);
            break;
        }
    }
    return nod;
}

static sm_net_t net_now(void)
{
    switch (talk_state()) {
    case TALK_NET_BUSY: return SM_NET_BUSY;
    case TALK_NET_SPOKE: return SM_NET_SPOKE;
    case TALK_NET_FAILED: return SM_NET_FAILED;
    case TALK_NET_IDLE:
    default: return SM_NET_IDLE;
    }
}

/* Panel pixels to the overlay space every target is drawn in: the quarter turn, then the 180. */
static void panel_to_overlay(int px, int py, int *ox, int *oy)
{
    int fx = 0, fy = 0;
    panel_to_frame(px, py, &fx, &fy);
    tap_to_overlay(fx, fy, ox, oy);
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
        if (n > 0 && calib_load(blob, n, &s_touch_cal)) {
            ESP_LOGI(TAG, "touch calibration loaded");
        } else if (n > 0) {
            /* Stored but unusable. Saying so matters: a silently ignored calibration and a
               panel that was never calibrated look identical from the outside, and they need
               completely different fixes. */
            ESP_LOGW(TAG, "stored touch calibration rejected — running uncalibrated");
        }
    }
    /* Silence is a failure mode with no symptom, so it is logged rather than inferred. With no
       codec the speaker state is the all-zero instance: no arbiter, every slot write a no-op, so
       nothing waits on a speaker that is not there and the arbiter watchdog never arms. */
    const bool sound = audio_start();
    if (!sound) ESP_LOGW(TAG, "no codec — taps will be silent");
    stream_t *const st = audio_stream();
    /* THE RECOGNISER IS NOT STARTED HERE: `main.c` starts it once the panel is settled and
       updatable (it lost the race for internal RAM to the radio when it started here, 0.2.37). */
    caption_t cap;
    caption_reset(&cap);
    int colour = 0;
    int frame = 0;
    int since_draw = FACE_FLOOR_MS; /* draw immediately */
    int since_beat = 0;
    /* The rig's running state. `st` persists between frames because the emotion TWEENS: a
       pose system that snaps is a slide show. */
    face_state_t fst;
    face_rest(&fst);
    action_t action = ACT_NONE;
    rig_run_t run = {0}; /* what follows `action` (`rig_run_next`), if anything */
    uint32_t action_start = 0;
    uint32_t recue_from = 0;
    float recue_p = 0.0f;
    poop_t poop;
    poop_reset(&poop);
    tickle_t tickle;
    tickle_reset(&tickle);
    uint32_t poop_last = 0;
    float action_mag = 1.0f;
    gesture_t gest;
    gesture_reset(&gest);
    pool_memory_t mem[POOL_COUNT];
    for (int i = 0; i < POOL_COUNT; i++) variants_reset(&mem[i]);
    float s_open = 1.0f;
    int s_drawn_lean = 0;
    float s_drawn_upright = 0.0f;
    /* The shuffle's own state, and the lean it last saw (`rig.h`). */
    rig_walk_t walk = {0};
    int walked_from = 0;
    int since_reassert = 0;
    int since_sample = 0;
    int level = 0;
    /* The delay, decided at the end of a pass and read at the start of the next. */
    int poll_ms = TOUCH_POLL_MS;
    /* HOW LONG THE LAST PASS ACTUALLY TOOK, from `esp_timer`, which is what every accumulator
       here wants: they used to add `poll_ms`, and this loop's real period is roughly twice that
       when it is drawing, so every duration measured in this task ran at half speed. */
    uint32_t last_pass = 0;
    int dt_ms = 0;
    /* IDLE IS MEASURED FROM HERE: the last frame anything counted as the panel being used. */
    uint32_t active_ms = 0;
    memset(&s_snap, 0, sizeof(s_snap));
    memset(&s_drawn, 0, sizeof(s_drawn));
    sm_init(&s_sm, (uint32_t)(esp_timer_get_time() / 1000));

    /* ONE SAMPLE BEFORE THE LOOP (#1690): the beat below refreshes it every 10 s, and until the
       first beat the boot report, the one carrying `reset_reason`, would say 0, a stack used up. */
    s_stack_free = (int)uxTaskGetStackHighWaterMark(NULL);
    while (true) {
        PHASE(1);
        bool dirty = false;
        const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        dt_ms = (last_pass == 0) ? 0 : (int)(now - last_pass);
        last_pass = now;
        PHASE(2);
        /* DRAIN A PRESS, DO NOT SAMPLE FOR ONE. `touch.c` samples the controller on its own task,
           so a press that began and ended while this loop was composing a frame is waiting here
           rather than having never happened. One per pass: each press gets its own frame, so two
           that arrive together still read as two presses. The point comes back WITH the press. */
        int raw_x = -1, raw_y = -1;
        bool tapped = touch && touch_take(&raw_x, &raw_y);
        bool down = touch && touch_is_down();
        bool wake = s_wake_next;
        bool dark_wake = s_dark_wake_next;
        s_wake_next = false;
        s_dark_wake_next = false;
        /* A FINGER ON A DARK SCREEN BUYS THE SCREEN, AND NOTHING ELSE. The child cannot see what
           they are aiming at, so letting that touch also poke the pet or play a message would make
           the first tap after a nap do something nobody chose. In standby it is also the wake the
           machine is told about: a screen that came back on but stayed deaf would be a pet looking
           straight at a child and ignoring her. Dim is not included: the pet is visible there. */
        bool woke_by_touch = false;
        if (s_sleep == SCREEN_DARK && (tapped || down)) {
            woke_by_touch = true;
            dark_wake = true;
            if (s_sm.standby) wake = true;
            sleep_wake("touch");
            tapped = false;
            down = false;
            /* The last press's point would otherwise anchor this one: a finger that drags on
               the next frame would read as a swipe measured from where the previous press was. */
            s_down_x = -1;
            s_down_y = -1;
        }
        /* WHERE THE FINGER IS, IN THE SPACE THE TARGETS WERE DRAWN IN. Anything drawn before
           `flip_frame` is in frame order and then reversed, so a touch has to be reversed the same
           way before it is compared with it (MEASURED 2026-09-25, upside down: a press on the tick
           at frame (276,368) arrived as (91,79) and missed by 289 px). The tap MARKER is drawn after
           the flip and uses `s_fig` directly. */
        if (tapped) {
            calib_apply(&s_touch_cal, raw_x, raw_y, &s_tap_x, &s_tap_y);
            panel_to_frame(s_tap_x, s_tap_y, &s_fig_x, &s_fig_y);
            s_down_x = s_tap_x;
            s_down_y = s_tap_y;
            s_swipe_latched = false;
        }
        int ox = -1, oy = -1;
        tap_to_overlay(s_fig_x, s_fig_y, &ox, &oy);
        const bool up = (tapped && !down) || (!tapped && s_finger && !down);
        s_finger = tapped ? down : (s_finger && down);
        const bool side = (s_quarter == 1 || s_quarter == 3);
        const int over_y0 = side ? SQ_Y0 : 0;
        const int over_h = side ? SQ_Y0 + SQ : FACE_H;

        /* WHAT THE PANEL HEARD, popped once a frame. The four phrases the machine owns (the
           name, stop, tell sister, tell dad) go to it; forms, colours and actions are the pet
           being a pet and stay here. */
        sm_voice_t voice = SM_VOICE_NONE;
        char said[64];
        int said_id = -1;
        if (speech_live() && speech_take(said, sizeof(said), &said_id)) {
            caption_say(&cap, said);
            const vocab_t *v = vocab_get(said_id);
            /* PLAYING, ONLY "STOP" ENDS THE GAME (`game_deaf`): the rest is the children
               playing, and goes by unheeded. */
            if (v != NULL && game_deaf(&s_game) && v->kind != VOCAB_STOP &&
                v->kind != VOCAB_GAME) {
                ESP_LOGI(TAG, "game: not for the pet: \"%s\"", said);
                v = NULL;
            }
            if (v != NULL) {
                cue_t heard_cue = CUE_COUNT;
                if (v->kind == VOCAB_STOP) game_stop(&s_game, now);
                switch (v->kind) {
                case VOCAB_LISTEN:
                    voice = SM_VOICE_WAKE;
                    break;
                case VOCAB_GAME:
                    /* LET'S PLAY A GAME (`game.h`): he walks to the middle and shrinks. The
                       tilt he is held at now is level, for the whole game. */
                    if (s_out.view.mode == SM_PET) { /* last frame's screen: the pet's */
                        float hx, hy;
                        face_game_home(s_lean, &hx, &hy);
                        action = ACT_NONE;
                        game_start(&s_game, now, esp_random(), hx, hy);
                        s_game_tx0 = s_tilt_g;
                        s_game_pitch0 = game_pitch(s_az, s_grav_down);
                        ESP_LOGI(TAG, "game: start (tilt %.2f, az %d, down %d)", (double)s_tilt_g,
                                 s_az, s_grav_down);
                        heard_cue = CUE_HEARD;
                    }
                    break;
                case VOCAB_STOP:
                    voice = SM_VOICE_STOP;
                    break;
                case VOCAB_SEND:
                    voice = v->arg == (int)JP_TO_DAD ? SM_VOICE_SEND_DAD : SM_VOICE_SEND_PANEL;
                    break;
                case VOCAB_FORM:
                    fst.form = (face_form_t)v->arg;
                    heard_cue = CUE_HEARD;
                    break;
                case VOCAB_COLOUR:
                    /* A named colour lands on its index; "pick a new color" still steps. */
                    colour = v->arg < 0 ? (colour + 1) % face_colour_count()
                                        : v->arg % face_colour_count();
                    heard_cue = CUE_HEARD;
                    break;
                case VOCAB_ACTION:
                default:
                    /* ASKING FOR A THING SOUNDS LIKE THE THING: the acknowledgement IS the
                       action's cue, not a bleep followed by one. */
                    action = (action_t)v->arg;
                    /* One "fart" in ten is the seven-second one (`rig_fart_pick`). */
                    if (action == ACT_FART) action = rig_fart_pick(esp_random());
                    action_mag = 1.0f;
                    action_start = now;
                    heard_cue = rig_cue_on_start(action) ? cue_for_action((int)action) : CUE_COUNT;
                    if (action == ACT_BIRTHDAY) {
                        s_song++;
                        heard_cue = CUE_COUNT;
                        if (sound) play_action_cue(st, action, 0.0f);
                    }
                    if (action == ACT_BRACE) tickle_arm(&tickle, now);
                    /* A different snack every time (`food_pick`): never the last one again. */
                    if (action == ACT_EAT) fst.food = food_pick(fst.food, esp_random());
                    if (action == ACT_EAT && poop_ate(&poop, now)) {
                        ESP_LOGI(TAG, "three meals in a minute: a poop is owed");
                    }
                    break;
                }
                if (heard_cue != CUE_COUNT && sound) {
                    stream_cue_play(st, STREAM_SLOT_PRESS, heard_cue);
                }
                dirty = true;
            }
        }
        if (speech_live() && !caption_idle(&cap)) dirty = true;
        caption_tick(&cap, now, FACE_W);

        /* The calibration routine owns the frame and the button while it runs; the machine is
           told once and then left alone (it drops an open recording silently, keeps the offer
           and any send, and reads the pet turn on the first frame back). */
        const bool calibrating = s_cal_active || s_cal_note_ms > 0;
        bool btn_short = false, btn_hold = false;
        int swipe = 0;
        float cue = 0.0f;
        int prev_taps = gest.taps;
        float prev_cue = 0.0f;
        gesture_action_t act = GESTURE_NONE;
        if (!calibrating) {
            PHASE(4);
            PHASE(5);
            update_orientation();
            boot_button_poll();
            btn_short = boot_button_take();
            btn_hold = boot_button_sleep_requested();
            prev_taps = gest.taps;
            prev_cue = gesture_cue(&gest);
            /* BEFORE THE STEP, so the hold reads this frame's tap count: the maintenance gestures
               are taps THEN a hold, and a hold that begins while a tap run is live is theirs. */
            act = gesture_poll(&gest, tapped, down, dt_ms);
            /* ONE STEP PER PRESS, MEASURED IN THE SPACE THE SCREEN IS DRAWN IN. "Left" has to
               mean left as the CHILD sees it, so both points go through the same pair the hit
               test uses; upside down, `panel_to_frame` alone returns the touch unchanged and a
               swipe would have the right magnitude and the WRONG SIGN. */
            int drag_x = 0, drag_y = 0;
            if (down && !s_swipe_latched && s_down_x >= 0 && touch_drag(&drag_x, &drag_y)) {
                int cx = 0, cy = 0, ox0 = 0, oy0 = 0, ox1 = 0, oy1 = 0;
                calib_apply(&s_touch_cal, drag_x, drag_y, &cx, &cy);
                panel_to_overlay(s_down_x, s_down_y, &ox0, &oy0);
                panel_to_overlay(cx, cy, &ox1, &oy1);
                const int dx = ox1 - ox0, dy = oy1 - oy0;
                const int adx = dx < 0 ? -dx : dx, ady = dy < 0 ? -dy : dy;
                if (adx >= SWIPE_MIN_PX && adx > ady) {
                    swipe = dx > 0 ? 1 : -1;
                    s_swipe_latched = true;
                    s_swipe_dx = dx;
                    if (s_swipes < 65535) s_swipes++;
                    ESP_LOGI(TAG, "swipe %s (dx %d)", swipe > 0 ? "right" : "left", dx);
                }
            }
            /* The finger's current point, for the hold: it fires where the finger is NOW (a
               finger may drift up to 59 px without becoming a swipe). */
            if (down && !tapped && touch_drag(&drag_x, &drag_y)) {
                int cx = 0, cy = 0;
                calib_apply(&s_touch_cal, drag_x, drag_y, &cx, &cy);
                panel_to_overlay(cx, cy, &ox, &oy);
            }
            cue = gesture_cue(&gest);
        }

        /* ── SAMPLE, STEP, EXECUTE ─────────────────────────────────────────────────────────── */
        (void)jpanel_snapshot(&s_snap); /* a torn read keeps the last good copy */
        const stream_facts_t facts = stream_facts(st);
        memset(&s_in, 0, sizeof(s_in));
        s_in.now = now;
        s_in.snap = s_snap;
        s_in.post_failed = s_post_failed_next;
        s_in.capture_refused = s_refused_next;
        s_post_failed_next = JP_SEQ_NONE;
        s_refused_next = false;
        s_in.audio = facts;
        if (!calibrating) {
            s_in.down = tapped;
            s_in.up = up;
            s_in.x = ox;
            s_in.y = oy;
            /* On the pet, in PANEL coordinates on purpose: the rim is the rim whichever way up
               the thing is mounted. -1 (no point) fails the margin for free. */
            s_in.on_pet = s_down_x >= TALK_MARGIN_PX && s_down_x < FACE_W - TALK_MARGIN_PX &&
                          s_down_y >= TALK_MARGIN_PX && s_down_y < FACE_H - TALK_MARGIN_PX;
            s_in.gest_taps = gest.taps;
            s_in.swipe = swipe;
            s_in.voice = voice;
            s_in.btn_short = btn_short;
            s_in.btn_hold = btn_hold;
        }
        s_in.over_y0 = over_y0;
        s_in.over_h = over_h;
        s_in.wake = wake;
        s_in.hearing = speech_hearing();
        s_in.capture_ms = audio_capture_ms();
        s_in.leased = stream_leased(st);
        s_in.net = net_now();
        s_in.speech_ok = speech_live();
        s_in.calibrating = calibrating;
        s_in.dark_wake = dark_wake;
        s_in.sound = sound;
        sm_step(&s_sm, &s_in, &s_out);
        const sm_view_t *const view = &s_out.view;
        if (execute(st, sound, now)) {
            action = ACT_NOD;
            action_mag = 1.0f;
            action_start = now;
        }
        /* SHAKEN: he sits down hard, dazed. Always the picture; the sound only when nothing
           else owns the speaker, since a message playing is not the pet's to talk over. */
        /* A game is played by tilting, and tilting hard is how a shake reads: not dizzy then. */
        if (game_on(&s_game)) s_shaken = false;
        if (s_shaken) {
            s_shaken = false;
            action = ACT_DIZZY;
            action_mag = 1.0f;
            action_start = now;
            dirty = true;
            ESP_LOGI(TAG, "shaken (%d counts)", s_move_mag);
            if (sound && view->mode == SM_PET && !facts.speaker_busy) {
                stream_cue_play(st, STREAM_SLOT_PRESS, CUE_DIZZY);
            }
        }
        /* A FRAME FOR WHAT THE MACHINE DID, not for the fact that it spoke: since 0.4.0 it emits
           MUTE_COMMANDS on every frame (derived, so no exit can leave the recogniser deaf), and
           counting that made every pass a redraw. Lydian's panel drew 13.5 frames a second with
           nobody touching it, which is processor time and PSRAM bandwidth taken from speech. */
        for (int i = 0; i < s_out.n; i++) {
            if (s_out.fx[i].kind != SM_FX_MUTE_COMMANDS) dirty = true;
        }
        if (s_out.say != NULL) caption_say(&cap, s_out.say);
        if (s_out.dash_why != SM_DASH_NONE) {
            /* COUNTED AND NAMED, at the owner's ask: *"anytime we get a red – I want you to make
               sure that it's logged"*. "net" has its reason in the talk reach row; "timeout" has
               no other record anywhere. */
            s_dash_why = sm_dash_name(s_out.dash_why);
            ESP_LOGW(TAG, "talk: no reply (%s)", s_dash_why);
        }
        if (s_out.rim_hold) {
            /* Once per press: a margin that is too wide looks exactly like a menu that stopped
               working unless the panel says which it is. */
            ESP_LOGI(TAG, "hold at (%d,%d) is on the rim, not the pet", s_down_x, s_down_y);
        }
        if (s_out.tap_missed) {
            /* A press the machine claimed and ignored (a grace, the transport before the message
               is audible, a stray touch while recording). Both pairs, because frame and overlay
               agreeing places a fault in calibration and disagreeing places it in the flip. */
            ESP_LOGI(TAG, "tap at frame (%d,%d) overlay (%d,%d) ignored", s_fig_x, s_fig_y, ox,
                     oy);
        }
        if (s_out.backstop) ESP_LOGW(TAG, "jpanel: the UI backstop fired");
        if (s_out.late_outcome) {
            s_late_outcomes++;
            ESP_LOGW(TAG, "jpanel: an outcome arrived after its backstop — not sounded");
        }
        if (s_out.violations != 0) {
            s_violations++;
            ESP_LOGW(TAG, "panel_sm: invariants 0x%02x", (unsigned)s_out.violations);
        }

        /* THE POKE, which the machine hands back when a press reached the pet. SPEAKING, AND IT
           OUTRANKS THE TOY: *"when the agent is talking we should prohibit beeps from cutting it
           off, and we should also stop poke interactions making other animations."* A poke over
           sound only flinches, because ignoring a finger entirely reads as a frozen pet.
           EXCEPT OVER THE PET'S OWN NOISE (`pet_only`, 0.4.10): a meal crunches through two
           thirds of its 4.8 s, and the speaker cuts that cue for the poke's, so it is heard. */
        if (s_out.pet_press && game_on(&s_game)) {
            /* PLAYING, a tap is the game's: the next round once one is over (`game_tap`). */
            game_tap(&s_game, now);
            dirty = true;
        } else if (s_out.pet_press) {
            s_flinch = 1.0f;
            dirty = true;
            if (!facts.pet_only || rig_holds_pokes(action)) {
                /* the flinch alone */
            } else if (label_hit(ox, oy, over_y0)) {
                /* THE LABEL IS ITS OWN BUTTON, checked before the zones so a corner of the glass
                   that says something cannot also be a poke. */
                s_show_version = !s_show_version;
                ESP_LOGI(TAG, "label -> %s", s_show_version ? "version" : "name");
                if (sound) stream_cue_play(st, STREAM_SLOT_PRESS, CUE_TOGGLE);
            } else {
                /* THE POKE IS THE PRODUCT, AND WHERE YOU POKE IS HALF OF IT. The zone picks the
                   pool; the pool picks the reaction, weighted, cooled down and softened if you are
                   hammering it (`variants.c`). THE SOUND FOLLOWS THE ACTION. */
                s_tap_zone = (int)face_zone(fst.form, s_fig_x, s_fig_y, s_upside_down, s_lean);
                /* WARNED, A POKE IS A TICKLE (`tickle.h`), and must not look like a poke (owner:
                   *"if I touch him he still acts like I'm poking him versus tickling"*). So no
                   flinch and no colour change, which are what a poke does; a squirm with the
                   tickle marks and a held-in "hehehe" for each he holds out against, harder each
                   time, and the laughing fit on the fifth. Otherwise the zone's pool. */
                const tickle_poke_t tk = tickle_poke(&tickle, now);
                if (tk != TICKLE_NO) {
                    s_flinch = 0.0f;
                    action = tk == TICKLE_BIG ? ACT_TICKLE : ACT_TICKLED;
                    action_mag = tk == TICKLE_BIG ? 1.0f : tickle_mag(&tickle);
                    if (tk == TICKLE_BIG) rig_run_start(&run, rig_run_tickled_out(), now);
                } else {
                    colour = (colour + 1) % face_colour_count();
                    const pool_t pool = ZONE_POOL[s_tap_zone];
                    action = (action_t)variants_pick(pool, &mem[pool], now, esp_random());
                    action_mag = variants_penalty(pool, &mem[pool], now);
                }
                action_start = now;
                ESP_LOGI(TAG, "tap glass (%d,%d) figure (%d,%d) zone %d -> colour %d, action %d, "
                              "mag %.2f",
                         s_tap_x, s_tap_y, s_fig_x, s_fig_y, s_tap_zone, colour, (int)action,
                         (double)action_mag);
                PHASE(3);
                if (sound && rig_cue_on_start(action)) {
                    play_action_cue(st, action, 0.0f);
                }
            }
        }

        /* THE CALIBRATION ROUTINE OWNS THE FRAME while it runs. It deliberately bypasses the
           rig rather than drawing over it: a robot reacting to the taps being measured would
           move the thing the owner is aiming at. */
        if (calibrating) {
            /* AND THE BUTTON STILL WORKS: one press abandons the run and the previous calibration,
               if any, is untouched. The hold still powers the panel down, because a way out that
               depends on the touchscreen is no use in the one mode where it is suspect. */
            boot_button_poll();
            if (boot_button_take() || boot_button_sleep_requested()) {
                s_cal_active = false;
                s_cal_note_ms = 0;
                calib_sample_reset(&s_cal_s);
                s_cal_last_tap = 0;
                ESP_LOGW(TAG, "calibration abandoned by the button — keeping the old");
                if (sound) stream_cue_play(st, STREAM_SLOT_PRESS, CUE_OOPS);
                continue;
            }
            /* AND IT GIVES UP ON ITS OWN. A crosshair ignored for half a minute was left like that
               by somebody who did not mean to start it. */
            if (s_cal_active) {
                if (s_cal_last_tap == 0) s_cal_last_tap = now;
                if (tapped) s_cal_last_tap = now;
                if (now - s_cal_last_tap >= CAL_IDLE_MS) {
                    s_cal_active = false;
                    s_cal_note_ms = 0;
                    calib_sample_reset(&s_cal_s);
                    s_cal_last_tap = 0;
                    ESP_LOGW(TAG, "calibration timed out — keeping the old");
                    continue;
                }
            }
            if (s_cal_note_ms > 0) {
                s_cal_note_ms -= dt_ms;
                cal_note(fb, s_cal_ok);
            } else {
                const int i = s_cal_i % CAL_KNOTS, j = s_cal_i / CAL_KNOTS;
                if (tapped) {
                    int rx = -1, ry = -1;
                    touch_point(&rx, &ry);
                    /* RAW, not corrected: a calibration measured through the previous
                       calibration would fit the correction on top of itself. */
                    calib_sample_add(&s_cal_s, rx, ry);
                    if (sound) stream_cue_play(st, STREAM_SLOT_PRESS, CUE_TICK);
                }
                /* Advance when the taps AGREE, or when this target has had its cap — a target
                   that will not settle must not trap the owner on it. */
                const bool settled = calib_sample_settled(&s_cal_s);
                const bool capped = s_cal_s.n >= CAL_SAMPLES_MAX;
                if (settled || capped) {
                    calib_sample_result(&s_cal_s, &s_cal_mx[j][i], &s_cal_my[j][i]);
                    if (!settled) {
                        ESP_LOGW(TAG, "target %d never settled (spread %d) — using the median",
                                 s_cal_i, calib_sample_spread(&s_cal_s));
                    }
                    calib_sample_reset(&s_cal_s);
                    s_cal_last_tap = now;
                    s_cal_i++;
                    if (s_cal_i >= CAL_TARGETS) {
                        s_cal_active = false;
                        s_cal_ok = calib_build(s_cal_mx, s_cal_my, &s_touch_cal);
                        if (s_cal_ok) {
                            uint8_t blob[CAL_BLOB_BYTES];
                            const int n = calib_save(&s_touch_cal, blob, sizeof(blob));
                            if (n > 0) cfg_calibration_save(blob, n);
                            ESP_LOGI(TAG, "calibration accepted: x %d %d %d %d, y %d %d %d %d",
                                     s_touch_cal.raw_x[0], s_touch_cal.raw_x[1], s_touch_cal.raw_x[2],
                                     s_touch_cal.raw_x[3], s_touch_cal.raw_y[0], s_touch_cal.raw_y[1],
                                     s_touch_cal.raw_y[2], s_touch_cal.raw_y[3]);
                        } else {
                            /* Refused rather than stored: a non-monotone map folds the panel
                               onto itself. The previous calibration, if any, is untouched. */
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

        s_open = blink_open(dt_ms);
        s_flinch *= FLINCH_DECAY;
        if (s_flinch < 0.02f) s_flinch = 0.0f;
        /* Animating means every poll is a frame: a blink at the 200 ms idle floor would be one
           frame long and read as a glitch, and a wave drawn five times a second is a twitch. */
        if (s_flinch > 0.0f || s_open < 1.0f) dirty = true;
        if (action != ACT_NONE) dirty = true;
        /* A moved figure is a new frame — once it has moved enough to see. */
        if (s_lean - s_drawn_lean > 2 || s_drawn_lean - s_lean > 2) dirty = true;
        if (fabsf(s_upright - s_drawn_upright) > 1.0f) dirty = true;
        /* And while he is still putting his feet down (`rig.h`). */
        if (walk.amp > 0.0f) dirty = true;
        /* What the machine says to draw changed, or it animates: the dot pulses, the dots cycle,
           the run's numeral moves. */
        if (memcmp(view, &s_drawn, sizeof(*view)) != 0) dirty = true;
        if (view->mode != SM_PET || view->think || view->sending) dirty = true;
        if (s_result_word != NULL && jp_due(now, s_result_until)) {
            s_result_word = NULL;
            dirty = true;
        }
        const bool rebooting = act == GESTURE_REBOOT;
        if (act == GESTURE_CALIBRATE) cal_begin();
        if (act == GESTURE_FORM) {
            fst.form = fst.form == FORM_OSTRICH ? FORM_ROBOT : FORM_OSTRICH;
            dirty = true;
            ESP_LOGI(TAG, "form -> %s", fst.form == FORM_OSTRICH ? "ostrich" : "robot");
        }
        if (cue != prev_cue || gest.taps != prev_taps) dirty = true;

        /* ── AWAKE OR NOT, DECIDED ONCE, AFTER EVERYTHING THAT COULD COUNT AS ALIVE ──
         *
         * Idle is not "nobody touched it": a reply playing, a turn, a message being played or
         * sent, a running animation and a finger mid-gesture are all the panel being USED. The
         * machine's part of that is `sm_busy`; the rest is this file's own. */
        {
            const bool was_dark = s_sleep == SCREEN_DARK || woke_by_touch;
            if (active_ms == 0) active_ms = now == 0 ? 1 : now;
            /* A MESSAGE ARRIVING WAKES THE SCREEN; A MESSAGE WAITING DOES NOT HOLD IT — counting
               the queue would leave one unacknowledged good-night lighting the room till
               morning. VOICE DOES NOT HOLD IT EITHER (*"you should only be on longer from a poke
               or accelerometer data"*); the pet answering does. */
            const bool used = tapped || down || woke_by_touch || s_moved || gest.taps > 0 ||
                              cue > 0.0f || action != ACT_NONE || facts.speaker_busy ||
                              s_out.arrived || sm_busy(&s_sm, &s_snap);
            if (s_moved) {
                if (s_sleep != SCREEN_AWAKE) {
                    ESP_LOGI(TAG, "screen: movement %d counts (threshold %d)", s_move_mag,
                             SCREEN_MOVE_COUNTS);
                }
                s_moved = false;
            }
            if (used) {
                active_ms = now == 0 ? 1 : now;
                /* STANDBY ENDS ON A PERSON OR A MESSAGE, AND ON NOTHING ELSE: the owner asked for
                   two ways back — the button, and a message arriving — plus a finger on the
                   glass, which is a person saying the same thing. */
                const bool wanted_back = s_out.arrived || tapped || down || woke_by_touch;
                if (s_sleep != SCREEN_AWAKE && (!s_sm.standby || wanted_back)) {
                    if (s_sm.standby) s_wake_next = true;
                    if (was_dark) s_dark_wake_next = true;
                    sleep_wake(s_out.arrived ? "message" : "activity");
                    dirty = true;
                }
            } else if (s_sm.standby) {
                /* PINNED, NOT COUNTED DOWN TO: the child asked for off, and off does not expire. */
                s_sleep = SCREEN_DARK;
            } else {
                const uint32_t idle = now - active_ms;
                const screen_stage_t want = screen_stage(idle);
                if (want != s_sleep) {
                    /* The same black frame the standby press paints: on an AMOLED "dark" was only
                       ever "stop blitting", which leaves the last frame there. */
                    if (want == SCREEN_DARK) s_blank_pending = true;
                    s_sleep = want;
                    s_brightness_pending = true;
                    ESP_LOGI(TAG, "screen: %s after %u min idle",
                             want == SCREEN_DARK ? "dark" : want == SCREEN_DIM ? "dim" : "awake",
                             (unsigned)(idle / 60000u));
                }
            }
            if (s_sleep == SCREEN_DARK) since_draw = FACE_FLOOR_MS;
            poll_ms = s_sleep == SCREEN_DARK ? SCREEN_POLL_MS : TOUCH_POLL_MS;
        }

        /* BEFORE THE GATE BELOW, because that gate is exactly what stops a dark screen being
           redrawn — so a blank queued by going dark would never be painted from inside it. */
        if (s_blank_pending) {
            s_blank_pending = false;
            memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
            const esp_err_t berr = blit_frame(fb);
            if (berr != ESP_OK) ESP_LOGW(TAG, "blank: %s", esp_err_to_name(berr));
            dirty = true;
        }
        /* A body chosen from the PWA, taken on the render task (the only writer of `fst`) and
           only when the box's answer changed, so it never fights a child's live choice. BEFORE
           the gate (0.4.3): after it, its `dirty` was never read, and the new body waited for the
           200 ms floor. */
        if (s_form_pending >= 0) {
            fst.form = (face_form_t)s_form_pending;
            s_form_pending = -1;
            dirty = true;
            ESP_LOGI(TAG, "form <- box: %s", fst.form == FORM_OSTRICH ? "ostrich" : "robot");
        }
        /* THE MOUTH MOVES WHILE IT SPEAKS, so every frame of a reply is a new frame. This was set
           inside the draw below, after the gate had already read `dirty`, so a reply's mouth was
           drawn at the 200 ms floor: five frames a second (0.4.3). */
        if (facts.speaker_busy) dirty = true;
        if (tickle_armed(&tickle, now)) dirty = true; /* the waiting shiver moves */
        /* WHAT GOES IN (`poop.h`): owed after three meals in a minute, and done at the first
           moment he is free, on the pet screen. It lands at POOP_AT (below); from then it
           slides on the tilt for its twenty seconds. A frame whenever it moves, plops in or
           shrinks away; the stink lines make do with the floor rate. */
        /* THE GAME'S FRAME (`game.h`): the tilt from where it began, a step, its sounds. Off the
           pet screen it ends; while it is on, every frame is drawn. */
        if (game_on(&s_game)) {
            if (view->mode != SM_PET) game_stop(&s_game, now);
            float hw, hh;
            face_game_field(&hw, &hh);
            const float tx = s_tilt_g - s_game_tx0;
            /* The change in angle, not in `az`, which fades as the panel lies back (review). */
            const float ty = game_tilt_y(game_pitch(s_az, s_grav_down), s_game_pitch0);
            static uint32_t game_last;
            const uint32_t gdt = game_last != 0 && now - game_last < 200u ? now - game_last : 40u;
            game_last = now;
            const unsigned ev = game_step(&s_game, now, gdt, tx, ty, hw, hh);
            if (sound && (ev & GAME_EV_GO)) stream_cue_play(st, STREAM_SLOT_PRESS, CUE_GAME_GO);
            if (sound && (ev & GAME_EV_DODGE)) stream_cue_play(st, STREAM_SLOT_PRESS, CUE_DODGE);
            if (sound && (ev & GAME_EV_HIT)) stream_cue_play(st, STREAM_SLOT_PRESS, CUE_GAME_HIT);
            if (ev & GAME_EV_HIT) {
                ESP_LOGI(TAG, "game: hit, score %d (best %d)", s_game.score, s_game.best);
            }
            if (ev & GAME_EV_DONE) ESP_LOGI(TAG, "game: over");
            dirty = true;
        }
        if (poop.owed && action == ACT_NONE && view->mode == SM_PET && !game_on(&s_game)) {
            action = ACT_POOP;
            action_mag = 1.0f;
            action_start = now;
            dirty = true;
        }
        {
            const uint32_t dt = poop_last != 0 ? now - poop_last : 0u;
            poop_last = now;
            const float x0 = poop.x;
            const bool was = poop.on;
            poop_step(&poop, now, s_tilt_g, dt > 100u ? 100u : dt, POOP_HALF);
            const uint32_t age = now - poop.born;
            if (poop.on != was || poop.x != x0 ||
                (poop.on && (age < POOP_POP_MS + 80u || age > POOP_SHOW_MS - POOP_SHRINK_MS - 80u))) {
                dirty = true;
            }
        }
        if (s_sleep != SCREEN_DARK && (dirty || since_draw >= FACE_FLOOR_MS)) {
            s_drawn_lean = s_lean;
            s_drawn_upright = s_upright;
            s_drawn = *view;
            float p = 0.0f;
            if (action != ACT_NONE) {
                const int dur = rig_spec(action)->dur_ms;
                const uint32_t el = now - action_start;
                if (dur <= 0 || (int)el >= dur) {
                    /* TICKLED OUT (`rig_run_next`): the fit leads into a fart, the fart into
                       sitting dazed; anything else simply ends. */
                    action = rig_run_next(&run, action, action_start, now);
                    if (action != ACT_NONE) {
                        action_mag = 1.0f;
                        action_start = now;
                        if (sound && view->mode == SM_PET && !facts.speaker_busy &&
                            rig_cue_on_start(action)) {
                            play_action_cue(st, action, 0.0f);
                        }
                    }
                }
                if (action != ACT_NONE) {
                    p = (float)(now - action_start) / (float)rig_spec(action)->dur_ms;
                }
            }
            /* WAITING TO BE TICKLED (`tickle_wait_p`): warned and doing nothing else, he holds
               the brace, hunched and shivering, so the game is visibly on. Drawn only — it is
               not an action, so nothing re-cues and a poke is still read as a tickle. */
            action_t shown = action;
            float shown_p = p;
            /* At full shiver, not the last tickle's lighter magnitude (review). */
            float shown_mag = action_mag;
            if (action == ACT_NONE && tickle_armed(&tickle, now)) {
                shown = ACT_BRACE;
                shown_p = tickle_wait_p(now);
                shown_mag = 1.0f;
            }
            /* AN ACTION THAT OUTLASTS ITS SOUND SAYS IT AGAIN (`rig_recue`): the tickle laughs
               three times. Measured from the last DRAWN frame's progress, so a skipped frame
               cannot skip a laugh; and only into a speaker nobody else is using. */
            if (action_start != recue_from) {
                recue_from = action_start;
                recue_p = 0.0f;
            }
            /* A song's next phrase cuts its last (`cue_may_recue`). */
            if (action != ACT_NONE &&
                (rig_recue(action, recue_p, p) || rig_recue_continuous(action, p)) && sound &&
                view->mode == SM_PET &&
                cue_may_recue(cue_for_action_at((int)action, p), facts.speaker_busy,
                              facts.pet_only)) {
                play_action_cue(st, action, p);
            }
            /* The plop: behind him, where a bird's tail is. */
            if (action == ACT_POOP && recue_p < POOP_AT && p >= POOP_AT) {
                poop_drop(&poop, now, (float)s_lean - POOP_BEHIND, POOP_HALF, esp_random());
            }
            recue_p = p;
            const bool conversing = view->mode == SM_LISTEN || view->mode == SM_RECORD ||
                                    view->think;
            const bool speaking = facts.speaker_busy;
            /* DIM MEANS ASLEEP, NOT MERELY DARKER (*"the robot should go to sleep and should have
               a zzz animation with eyes closed"*), deferring to a live conversation. */
            const bool dozing = screen_dozing(s_sleep, conversing, speaking);
            face_params_t target;
            emotion_resolve(dozing                      ? FACE_SLEEPY
                            : view->mode == SM_LISTEN ? FACE_CURIOUS
                            : view->dash              ? FACE_BEWILDERED
                                                      : rig_spec(shown)->face,
                            &target);
            /* TWICE PER FRAME: `emotion_approach` halves per frame and was written against 50 fps;
               this panel renders at 25. */
            emotion_approach_face(&fst.eyes, &target, target.rate);
            emotion_approach_face(&fst.eyes, &target, target.rate);
            rig_for(shown, shown_p, shown_mag, now, &fst.rig);
            /* PLAYING, the legs go with how far the game moved him, in his own size's steps:
               a fifth of the size, five times the strides for the same distance. */
            static float game_px, game_py;
            {
                /* Followed between games too, so the first step of the next is not a leap. */
                float gx, gy, gs;
                game_figure(&s_game, now, &gx, &gy, &gs);
                if (game_on(&s_game)) {
                    rig_walk(&walk, hypotf(gx - game_px, gy - game_py) / gs, &fst.rig);
                } else {
                    rig_walk(&walk, (float)(s_lean - walked_from), &fst.rig);
                }
                game_px = gx;
                game_py = gy;
            }
            walked_from = s_lean;
            rig_figure(shown, shown_p, shown_mag, now, fst.eyes.face_ang, &fst.fig);
            fst.game = game_on(&s_game) ? &s_game : NULL;
            fst.game_ms = now;
            if (s_game.phase == GAME_OVER) {
                /* HIT: dazed, the stars going round, until the tap. */
                fst.fig.extra = EXTRA_STARS;
                fst.fig.extra_p = 0.1f + fmodf((float)(now - s_game.since) * 0.0001f, 0.8f);
            }
            fst.bob = bob_step(frame++);
            fst.lean = s_lean;
            fst.upright = s_upright;
            fst.poop_x = poop.x;
            fst.poop_size = poop_scale(&poop, now);
            fst.poop_t = (float)(now - poop.born) * 0.001f;
            fst.poop_kind = poop.kind;
            fst.open = dozing ? emotion_approach(emotion_approach(fst.open, 0.0f, 0.12f), 0.0f, 0.12f)
                              : s_open;
            fst.startle = s_flinch;
            /* THE MOUTH, WHILE THERE IS SOUND COMING OUT OF IT: two frequencies, so it reads as
               speech rather than a hinge. Not driven by the audio, which the panel deafens itself
               to while it speaks. */
            if (speaking) {
                const float t = (float)now * 0.001f;
                const float syll = sinf(t * 6.3f), phrase = sinf(t * 2.7f + 1.1f);
                float open = 0.55f + 0.30f * syll + 0.15f * phrase;
                if (open < 0.25f) open = 0.25f;
                if (open > 1.0f) open = 1.0f;
                fst.talk = open;
            } else {
                fst.talk = 0.0f;
            }
            fst.fig.oy += FLINCH_DIP * s_flinch;
            PHASE(6);
            face_draw(fb, colour, &fst);
            PHASE(7);
            /* Not over the game's score, which has that corner. */
            if (!game_on(&s_game)) {
                font_draw(fb, FACE_W, FACE_H, LABEL_X, over_y0 + LABEL_Y, LABEL_SCALE,
                          s_show_version ? ota_running_version() : s_name_up, LABEL_COLOUR);
            }
            if (dozing) {
                const uint32_t phase = (now / 700u) % 4u; /* 0..3: one Z, two, three, rest */
                char z[4] = {0};
                for (uint32_t i = 0; i < phase && i < 3; i++) z[i] = 'Z';
                if (z[0] != '\0') {
                    const int zx = FACE_W / 2 + 26 + (int)phase * 4;
                    const int zy = over_y0 + 40 - (int)phase * 6;
                    font_draw(fb, FACE_W, FACE_H, zx, zy, 2, z, LABEL_COLOUR);
                }
            }
            draw_meter(fb, level);
            caption_draw(&cap, fb, FACE_W, over_h, CAPTION_COLOUR);
            const overlay_ctx_t oc = {
                .over_y0 = over_y0,
                .over_h = over_h,
                .now = now,
                .sibling = s_snap.waiting.sibling,
                .result_word = s_result_word,
                .result_colour = s_result_colour,
            };
            overlay_draw(fb, view, &oc);
            PHASE(8);
            if (s_upside_down) flip_frame(fb);
            /* The tap marker, in FRAME coordinates and after the flip; it fades with the recoil. */
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
                /* The maintenance hold's bar, growing across the top of the square. */
                int w = (int)(FACE_W * cue);
                if (w > FACE_W) w = FACE_W;
                for (int y = over_y0; y < over_y0 + 4; y++) {
                    for (int x = 0; x < w; x++) fb[y * FACE_W + x] = CUE_COLOUR;
                }
            } else if (gest.taps > 0) {
                /* One pip per counted tap, so the sequence is visible while it is entered. */
                for (int i = 0; i < gest.taps && i < GESTURE_TAPS_MAX; i++) {
                    const int x0 = i * (PIP_W + PIP_GAP);
                    for (int y = over_y0; y < over_y0 + 4; y++) {
                        for (int x = x0; x < x0 + PIP_W && x < FACE_W; x++) {
                            fb[y * FACE_W + x] = CUE_COLOUR;
                        }
                    }
                }
            }
            PHASE(9);
            s_fr_blit_at = (uint32_t)(esp_timer_get_time() / 1000);
            const esp_err_t err = blit_frame(fb);
            if (err != ESP_OK) {
                /* ONCE, THEN EVERY HUNDREDTH, AND ALWAYS WITH THE HEAP: the number that explains a
                   failed blit is the largest free internal block, which `mem_log` prints. */
                s_blit_fail_total++;
                if (s_blit_fails++ % 100 == 0) {
                    ESP_LOGE(TAG, "blit: %s (failure %d)", esp_err_to_name(err), s_blit_fails);
                    mem_log("blit-fail");
                }
                s_blit_ok = 0;
                /* SELF-HEAL, BECAUSE THE OWNER CANNOT REACH THIS PANEL: a DMA underflow leaves the
                   SPI driver poisoned until a power cycle. The uptime guard keeps a fault present
                   from boot from becoming a reboot loop. */
                if (now > BLIT_HEAL_AFTER_MS && s_blit_fails >= BLIT_HEAL_FAILS) {
                    ESP_LOGE(TAG, "%d consecutive blit failures — restarting", s_blit_fails);
                    mem_log("blit-heal");
                    display_note_restart(DISPLAY_RESTART_BLIT_HEAL);
                    vTaskDelay(pdMS_TO_TICKS(150));
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
            frame_note(now);
            since_draw = 0;
        }
        /* OFF, AND IT LEAVES FROM HERE FOR THE REASON THE REBOOTS DO: a frame has just finished
           and nothing is in flight on the QSPI bus. A unit in deep sleep answers nothing but its
           own button, so leaving mid-transfer is a panel somebody has to find. */
        if (s_power_off) {
            ESP_LOGW(TAG, "power: deep sleep — the button is the only way back");
            s_sleep = SCREEN_DARK;
            memset(fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
            (void)blit_frame(fb); /* painted, not switched off — see `s_blank_pending` */
            apply_brightness();
            stream_stop(st, false);
            stream_silence(st);
            /* WAIT FOR THE FINGER TO ACTUALLY LEAVE: EXT0 is a LEVEL trigger, so arming it while
               the button is still down wakes the chip at once — the owner's *"long press seems to
               just reset the device"*. Bounded, so a stuck button cannot strand the panel. */
            const uint32_t wait_from = (uint32_t)(esp_timer_get_time() / 1000);
            while (gpio_get_level(BOOT_BTN) == 0) {
                if ((uint32_t)(esp_timer_get_time() / 1000) - wait_from > POWER_RELEASE_MAX_MS) {
                    ESP_LOGW(TAG, "power: button still down after %u ms — sleeping anyway",
                             (unsigned)POWER_RELEASE_MAX_MS);
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(20));
            }
            vTaskDelay(pdMS_TO_TICKS(BOOT_DEBOUNCE_MS));
            esp_sleep_enable_ext0_wakeup(BOOT_BTN, 0);
            esp_deep_sleep_start(); /* never returns */
        }
        /* BOTH REBOOTS LEAVE FROM HERE: the one place a frame has just finished and nothing is
           in flight on the QSPI bus (`display.h`). */
        if (rebooting || s_restart_pending) {
            const display_restart_t why = rebooting ? DISPLAY_RESTART_GESTURE : s_restart_why_req;
            ESP_LOGW(TAG, "%s — parking the renderer and restarting",
                     rebooting ? "reboot gesture completed" : "restart requested");
            display_note_restart(why);
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
        /* THE PACING IS A DEADLINE, NOT A DELAY (0.4.3). It slept `poll_ms` after the work, so
           while animating a pass took draw + blit + 40 ms: about twelve frames a second against
           the 25 every per-frame tween here was tuned for. Now the pass is topped up to
           `poll_ms`. At least one tick always, so the idle tasks, and the watchdog that feeds on
           them, still run after a pass that overran. */
        PHASE(10);
        {
            const int spent = (int)((uint32_t)(esp_timer_get_time() / 1000) - now);
            int wait = poll_ms - spent;
            if (wait < (int)portTICK_PERIOD_MS) wait = (int)portTICK_PERIOD_MS;
            vTaskDelay(pdMS_TO_TICKS(wait));
        }
        if (sound) {
            level = audio_level();
            if (level > s_mic_peak) s_mic_peak = level;
            PHASE(11);
            blit_meter(level);
        }
        /* A RENDER HEARTBEAT, BECAUSE A STOPPED RENDER TASK LOOKED EXACTLY LIKE A QUIET ONE. The
           stage is on the line because a sleeping screen stops blitting on purpose. */
        since_beat += dt_ms;
        if (since_beat >= BEAT_MS) {
            since_beat = 0;
            /* Every beat, not every pass: it walks the whole stack to find the untouched words. */
            s_stack_free = (int)uxTaskGetStackHighWaterMark(NULL);
            ESP_LOGI(TAG, "render: %d frames ok, %d failed | screen %s | internal largest %u",
                     s_blit_ok, s_blit_fails, display_screen(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                                MALLOC_CAP_DMA));
        }
        since_draw += dt_ms;
        since_reassert += dt_ms;
        since_sample += dt_ms;
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
