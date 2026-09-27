/* Touch, as one whole-screen button.
 *
 * NOT a touch driver, deliberately. The design round settled this by measurement: a 20 mm
 * child touch target is 69% x 57% of this panel, so two of them do not fit in either axis
 * and the whole screen is the only target there is (mocks/room-endpoint/README.md). A
 * coordinate is therefore something this surface has no use for — and reading the finger
 * count is one register, where a coordinate would be a component dependency and a rotation
 * convention to get wrong.
 *
 * Long-press is not a gesture at this age either: 4-5 year olds produce ordinary taps
 * lasting up to 4.2 seconds. So this reports the EDGE, never the hold — a finger resting on
 * the screen must not fire again, or the robot changes colour eleven times while he looks
 * at it.
 *
 * The controller is CST820 on the V2 board (CST816-compatible) at 0x15, on the same I2C bus
 * as the PMU, the RTC and the IMU.
 */

#include "touch.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "i2c_bus.h"

static const char *TAG = "touch";

static void touch_task(void *arg);

#define CST_ADDR 0x15
#define REG_FINGERS 0x02

static i2c_master_dev_handle_t s_dev;
static volatile bool s_down;
static volatile int s_x = -1;
static volatile int s_y = -1;

/* THE PRESSES THE RENDERER HAS NOT COLLECTED YET.
 *
 * A REAL QUEUE, NOT A HAND-ROLLED RING. Two tasks touch this — the sampler pushes, the
 * renderer drains — and a `count++` against a `count--` is not atomic on this core however
 * volatile the variable is. FreeRTOS already owns a correct answer to exactly this, and
 * writing a lock-free ring instead would be inventing a concurrency bug to save an allocation.
 *
 * Four deep, and the depth is a judgement about children rather than about buffers: a jab is
 * one press, and the realistic worst case is a small person hitting the glass repeatedly while
 * a slow frame is in flight. A fifth press inside one frame is hammering, where dropping it is
 * the right answer rather than a loss — they all mean the same thing, and acting on every one
 * would fire a control several times from what the child experienced as one impatient burst.
 * The queue drops the NEWEST in that case, because the oldest is the one they aimed. */
#define TAP_QUEUE 4

typedef struct {
    int x;
    int y;
} tap_t;

static QueueHandle_t s_taps;

bool touch_start(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) return false;
    const i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = CST_ADDR,
        .scl_speed_hz = 400000,
    };
    const esp_err_t err = i2c_master_bus_add_device(bus, &dev_cfg, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "add device: %s", esp_err_to_name(err));
        return false;
    }
    s_taps = xQueueCreate(TAP_QUEUE, sizeof(tap_t));
    if (s_taps == NULL) {
        ESP_LOGE(TAG, "no tap queue — presses will be missed");
        return false;
    }
    /* ITS OWN TASK, so that how fast this panel notices a finger stops depending on how much
       it happens to be drawing. Small on purpose and genuinely small in truth: one I2C
       transaction and a four-entry queue, no TLS, no buffers — 3072 is ample for that, and
       the number is stated rather than guessed because under-provisioning a task stack is
       exactly what crash-looped a panel in 0.3.22. Priority above the renderer, so a finger
       is recorded while a frame is being composed rather than after it. */
    if (xTaskCreate(touch_task, "touch", 3072, NULL, 6, NULL) != pdPASS) {
        ESP_LOGE(TAG, "no sampling task — presses will be missed");
        return false;
    }
    ESP_LOGI(TAG, "sampling every %d ms", TOUCH_SAMPLE_MS);
    return true;
}

/* One read of the controller. Returns whether this read saw a NEW finger. */
static bool sample(void)
{
    if (s_dev == NULL) return false;
    /* Five bytes in one transaction: the finger count at 0x02 and the coordinate that
       follows it. The count alone was all the whole-screen target needed; zones need where.
       Both high bytes carry flags in their top nibble, so only the low four bits are
       position. */
    const uint8_t reg = REG_FINGERS;
    uint8_t buf[5] = {0};
    if (i2c_master_transmit_receive(s_dev, &reg, 1, buf, sizeof(buf), 50) != ESP_OK) {
        /* A read that fails is not a tap. Saying so out loud every poll would bury the log,
           so this is silent by design — `touch_start` already reported whether it opened. */
        return false;
    }
    const bool down = buf[0] > 0;
    const bool edge = down && !s_down;
    s_down = down;
    if (edge) {
        s_x = ((buf[1] & 0x0F) << 8) | buf[2];
        s_y = ((buf[3] & 0x0F) << 8) | buf[4];
    }
    return edge;
}

static void touch_task(void *arg)
{
    (void)arg;
    while (true) {
        if (sample()) {
            /* LATCHED WITH ITS OWN POINT. Two presses queued behind one slow frame must not
               both report the second one's coordinates — that is how a press aimed at a
               notification gets acted on as a press somewhere else entirely. */
            const tap_t t = {.x = s_x, .y = s_y};
            (void)xQueueSend(s_taps, &t, 0); /* full means hammering; see TAP_QUEUE */
        }
        vTaskDelay(pdMS_TO_TICKS(TOUCH_SAMPLE_MS));
    }
}

bool touch_take(int *x, int *y)
{
    tap_t t;
    if (s_taps == NULL || xQueueReceive(s_taps, &t, 0) != pdTRUE) return false;
    if (x != NULL) *x = t.x;
    if (y != NULL) *y = t.y;
    return true;
}

bool touch_is_down(void)
{
    return s_down;
}

void touch_point(int *x, int *y)
{
    if (x != NULL) *x = s_x;
    if (y != NULL) *y = s_y;
}
