#include "nudge.h"

#include <lwip/sockets.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "jpanel.h"

static const char *TAG = "nudge";

static unsigned s_count;
static unsigned s_dropped;
/* Whose sleep to cut short for the SETTINGS half. `jpanel_poll_soon()` covers messages by
   posting to a queue the poll task is already waiting on; the main task has no queue, it is
   sitting in a timed wait, so the wake is a direct notification to that task. */
static TaskHandle_t s_settings_task;

void nudge_wake_settings_from(TaskHandle_t t)
{
    s_settings_task = t;
}

static void nudge_task(void *arg)
{
    (void)arg;
    const int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) {
        /* Not fatal, and deliberately not retried: the panel still polls, so a socket that
           could not be opened costs latency rather than function. Loud, because "push quietly
           stopped existing" is indistinguishable from "nobody sent anything". */
        ESP_LOGE(TAG, "no socket (%d) — falling back to the poll alone", errno);
        vTaskDelete(NULL);
        return;
    }
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(NUDGE_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "bind %d failed (%d) — falling back to the poll alone", NUDGE_PORT, errno);
        close(fd);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "listening on udp/%d", NUDGE_PORT);

    uint32_t last_ms = 0;
    while (true) {
        char buf[16];
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        /* BLOCKS, which is the point: this task exists so that nothing else has to poll a
           socket. It costs a task and no TLS, which is the trade the whole file is built on. */
        const int n = recvfrom(fd, buf, sizeof(buf), 0, (struct sockaddr *)&from, &from_len);
        if (n < NUDGE_MAGIC_LEN || memcmp(buf, NUDGE_MAGIC, NUDGE_MAGIC_LEN) != 0) {
            /* Not ours. Not counted as dropped either — a dropped nudge means one we refused
               to act on, and this was never a nudge. */
            continue;
        }
        const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        if (last_ms != 0 && now - last_ms < NUDGE_MIN_GAP_MS) {
            s_dropped++;
            continue;
        }
        last_ms = now;
        s_count++;
        /* BOTH HALVES, because a nudge does not say which kind of thing changed and must not:
           the datagram carries no data, so the panel's answer is to ask about everything it
           would have asked about anyway. Messages go through the queue the poll task already
           waits on; settings cut short the main task's timed sleep. */
        jpanel_poll_soon();
        if (s_settings_task != NULL) xTaskNotifyGive(s_settings_task);
    }
}

void nudge_start(void)
{
    /* Small: this task opens no TLS, parses nothing and holds no buffers — it receives at most
       sixteen bytes and calls two functions. */
    if (xTaskCreate(nudge_task, "nudge", 3072, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "no task — falling back to the poll alone");
    }
}

unsigned nudge_count(void)
{
    return s_count;
}

unsigned nudge_dropped(void)
{
    return s_dropped;
}
