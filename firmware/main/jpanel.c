/* Voice post's task and real io. The engine is `jpanel_core.c`; see jpanel.h for the split. */

#include "jpanel.h"

#include <stdatomic.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "jpanel_core.h"
#include "link.h"
#include "mbedtls/sha256.h"

static const char *TAG = "jpanel";

/* The plan's depth (§7.1): render posts at most one command per press. */
#define JPANEL_QUEUE_DEPTH 4
/* Engine stack (§7.2): every large buffer in the core is static, so 6 KB is the same as 0.3.39. */
#define JPANEL_STACK 6144
#define POLL_EVERY_MS 30000

static QueueHandle_t s_q;
static TaskHandle_t s_task;
static mbedtls_sha256_context s_sha;
static bool s_sha_ok;
static _Atomic uint32_t s_post_failed;

static link_res_t io_request(void *ctx, const link_req_t *req)
{
    (void)ctx;
    return link_request(req);
}

static bool io_take(void *ctx, jp_cmd_t *out, uint32_t wait_ms)
{
    (void)ctx;
    return xQueueReceive(s_q, out, pdMS_TO_TICKS(wait_ms)) == pdTRUE;
}

static bool io_queue_empty(void *ctx)
{
    (void)ctx;
    return uxQueueMessagesWaiting(s_q) == 0;
}

static uint32_t io_now(void *ctx)
{
    (void)ctx;
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void io_sleep(void *ctx, uint32_t ms)
{
    (void)ctx;
    const TickType_t t = pdMS_TO_TICKS(ms);
    vTaskDelay(t > 0 ? t : 1);
}

static void copy_str(char *dst, size_t cap, const cJSON *s)
{
    const char *v = cJSON_IsString(s) && s->valuestring != NULL ? s->valuestring : "";
    strlcpy(dst, v, cap);
}

/* `queue` IS THE ANSWER AND THE TWO SCALARS ARE ITS FALLBACK: a box too old to send a list still
 * names its head, which leaves the panel exactly as capable as it was. Ids are believed only when
 * every described entry has one (a W3 box); otherwise the panel works by position, as 0.3.39 did
 * (§6). `from_owner` is a kind, not a name: the name is the owner's to change (migration 0211). */
static bool io_parse(void *ctx, const uint8_t *body, size_t n, jp_waiting_t *out)
{
    (void)ctx;
    cJSON *root = cJSON_ParseWithLength((const char *)body, n);
    if (root == NULL) return false;
    memset(out, 0, sizeof(*out));
    const cJSON *count = cJSON_GetObjectItemCaseSensitive(root, "count");
    out->count = cJSON_IsNumber(count) ? count->valueint : 0;
    copy_str(out->sibling, sizeof(out->sibling), cJSON_GetObjectItemCaseSensitive(root, "sibling"));
    const cJSON *q = cJSON_GetObjectItemCaseSensitive(root, "queue");
    bool ids = true;
    if (cJSON_IsArray(q)) {
        const cJSON *one = NULL;
        cJSON_ArrayForEach(one, q) {
            if (out->n >= JPANEL_QUEUE_MAX) break;
            jp_who_t *w = &out->q[out->n++];
            copy_str(w->id, sizeof(w->id), cJSON_GetObjectItemCaseSensitive(one, "id"));
            copy_str(w->from, sizeof(w->from), cJSON_GetObjectItemCaseSensitive(one, "from_name"));
            w->from_dad = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(one, "from_owner"));
            if (w->id[0] == '\0') ids = false;
        }
    }
    if (out->n == 0 && out->count > 0) {
        copy_str(out->q[0].from, sizeof(out->q[0].from),
                 cJSON_GetObjectItemCaseSensitive(root, "from_name"));
        out->q[0].from_dad = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "from_owner"));
        out->n = 1;
        ids = false;
    }
    out->has_ids = ids && out->n > 0;
    if (!out->has_ids) {
        for (int i = 0; i < out->n; i++) out->q[i].id[0] = '\0';
    }
    cJSON_Delete(root);
    return true;
}

/* SHA-256 because the S3 has it in hardware and mbedTLS is already linked for the CA bundle. A
   digest that could not be computed reads as a mismatch: an unverifiable message is not acked. */
static void io_sha_begin(void *ctx)
{
    (void)ctx;
    mbedtls_sha256_init(&s_sha);
    s_sha_ok = mbedtls_sha256_starts(&s_sha, 0) == 0;
}

static void io_sha_update(void *ctx, const uint8_t *data, size_t n)
{
    (void)ctx;
    if (s_sha_ok && mbedtls_sha256_update(&s_sha, data, n) != 0) s_sha_ok = false;
}

static void io_sha_finish(void *ctx, uint8_t out[JP_SHA256_LEN])
{
    (void)ctx;
    if (!s_sha_ok || mbedtls_sha256_finish(&s_sha, out) != 0) memset(out, 0, JP_SHA256_LEN);
    mbedtls_sha256_free(&s_sha);
}

static void jpanel_task(void *arg)
{
    (void)arg;
    while (true) jpanel_core_step();
}

bool jpanel_start(const cfg_t *cfg, stream_t *stream)
{
    if (cfg == NULL || stream == NULL) return false;
    s_q = xQueueCreate(JPANEL_QUEUE_DEPTH, sizeof(jp_cmd_t));
    if (s_q == NULL) {
        ESP_LOGE(TAG, "no memory for voice post");
        return false;
    }
    const jpanel_io_t io = {
        .ctx = NULL,
        .request = io_request,
        .take = io_take,
        .queue_empty = io_queue_empty,
        .now_ms = io_now,
        .sleep_ms = io_sleep,
        .parse_waiting = io_parse,
        .sha_begin = io_sha_begin,
        .sha_update = io_sha_update,
        .sha_finish = io_sha_finish,
        .yield = NULL,
        .stream = stream,
        /* Staggered, so two panels on the same Wi-Fi do not ask in lockstep forever. */
        .poll_first_ms = esp_random() % POLL_EVERY_MS,
    };
    jpanel_core_init(&io);
    /* Off the render core, like `talk.c`: this task blocks on a socket for seconds. */
    if (xTaskCreatePinnedToCore(jpanel_task, "jpanel", JPANEL_STACK, NULL, 4, &s_task, 0) != pdPASS) {
        ESP_LOGE(TAG, "task failed");
        vQueueDelete(s_q);
        s_q = NULL;
        return false;
    }
    ESP_LOGI(TAG, "ready");
    return true;
}

bool jpanel_post(const jp_cmd_t *cmd)
{
    if (s_q != NULL && cmd != NULL && cmd->seq != JP_SEQ_NONE && xQueueSend(s_q, cmd, 0) == pdTRUE) {
        return true;
    }
    atomic_fetch_add(&s_post_failed, 1);
    return false;
}

bool jpanel_snapshot(jp_view_t *out)
{
    if (s_task == NULL) return false;
    return jpanel_core_snapshot(out);
}

bool jpanel_health(jp_health_t *out)
{
    if (s_task == NULL) return false;
    return jpanel_core_health(out);
}

void jpanel_poll_soon(void)
{
    jpanel_core_poll_soon();
}

void jpanel_stats(jp_stats_t *out)
{
    memset(out, 0, sizeof(*out));
    if (s_task == NULL) return;
    jpanel_core_stats(out);
    out->post_failed = atomic_load(&s_post_failed);
    out->jpanel_stack_free = (uint32_t)uxTaskGetStackHighWaterMark(s_task);
}

const char *jpanel_msg_err(void)
{
    return jpanel_core_err();
}
