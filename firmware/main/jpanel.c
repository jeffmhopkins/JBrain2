/* Voice post. See jpanel.h for why this is a task of its own. */

#include "jpanel.h"

#include <string.h>
#include <strings.h>

#include "audio.h"
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "jpanel";

/* Shorter than `talk.c`'s 30 s. A turn waits on whisper AND a language model AND a voice; a
   send waits on whisper alone and a fetch on a blob read, so a request still running after
   this is not slow, it is broken. */
#define JPANEL_HTTP_TIMEOUT_MS 20000

/* WHAT THE BOX WILL HAND OVER AT MOST, and it is NOT the reply cap.
 *
 * `MAX_MESSAGE_MS` in `backend/src/jbrain/api/jpanel.py` is twenty seconds, and a message
 * from Dad is text put through a voice — `SendText` allows 600 characters, which is far more
 * speech than the ten seconds `audio.c` used to truncate at without a word to anyone. The two
 * numbers must move together; `audio.c`'s buffer is sized from this one. */
#define JPANEL_MAX_BYTES (16000 * 2 * 20)

/* ~30 s, as the plan's contract says: fast enough that "my sister just sent me something" is
   answered while she is still in the room, small enough that two panels asking forever costs
   the box nothing — the route returns a count and a name and touches one index. */
#define POLL_EVERY_MS 30000

typedef enum { CMD_SEND = 0, CMD_FETCH, CMD_POLL } cmd_kind_t;

typedef struct {
    cmd_kind_t kind;
    jpanel_to_t to;  /* CMD_SEND only */
    bool asked;      /* CMD_FETCH: a finger is waiting for this, so play it and report */
} cmd_t;

static const cfg_t *s_cfg;
static QueueHandle_t s_q;
static volatile jpanel_state_t s_state;

/* The outgoing recording, borrowed from `audio.c` and valid until the next capture. */
static const int16_t *s_pcm;
static volatile size_t s_bytes;

/* The incoming message: PSRAM, claimed once at start-up, because a heap request in the middle
   of a child waiting for their sister's voice is a failure with no good outcome. */
static uint8_t *s_in;
static volatile int s_in_len;
/* A FETCHED MESSAGE, WAITING FOR A FINGER — the difference between a pop-up that plays and
   one that makes a child wait.
 *
 * The owner: *"the message should start playing faster. There's a couple seconds between me
 * acknowledging the message and it's starting to play."* That gap was the whole round trip —
 * a TLS handshake, a blob read on the box, a rate conversion and up to 640 KB down the wire —
 * and it ran AFTER the tap because the tap is what used to start it.
 *
 * Nothing required that order. `GET /next` deliberately does not mark a message played, so
 * fetching one early costs nothing and risks nothing: a panel that loses power holding an
 * unplayed message still has it on the box. So the poll that discovers a message now also
 * collects it, and the tap is a memcpy into the speaker's buffer. */
static volatile bool s_held;
/* A MESSAGE HAS BEEN HANDED TO THE SPEAKER AND THE BOX HAS NOT BEEN TOLD YET.
 *
 * SET WHERE THE PLAY HAPPENS, NOT BY WATCHING FOR A STATE. It was armed by polling for
 * `JPANEL_PLAYING` every 250 ms, and the renderer clears that state from another task — in the
 * SAME frame as the tap, because its `speaking` flag is sampled at the top of the frame and the
 * tap that starts the audio comes later in it. So the task never once saw PLAYING, `POST
 * /played` never fired, the box kept the message unplayed, and every poll delivered it again:
 * a pop-up on a child's wall repeating the same message every thirty seconds, forever.
 *
 * A flag set synchronously at the moment the audio starts cannot be missed by a reader that
 * runs later, which is the property the polled version did not have. */
static volatile bool s_owed;
/* A RUN: press once, hear everything waiting, oldest first.
 *
 * The owner: *"when multiple messages stack up it doesn't have a good way to show them."* One
 * pop-up per message meant five messages were five pop-ups and five taps — tedious, and
 * indistinguishable from the repeat bug even when it was working correctly.
 *
 * "Press once, hear everything" is how a four-year-old thinks, and the escape is the gesture
 * this panel already has in two other places: A FINGER CANCELS. Touch during a run and it
 * stops. Nothing new to teach, and the third use of the same rule.
 *
 * WHAT IS PLAYED IS ACKNOWLEDGED AS IT GOES, one message at a time, so stopping halfway leaves
 * the rest genuinely unheard rather than silently consumed — the pop-up comes back for them. */
static volatile bool s_run;
/* The id the box gave it, held so `POST /played` can name it after the speaker finishes, and
   who it came from, which is what the repeat icon's caption says. Both are filled by the
   header handler below. */
static char s_in_id[48];
static char s_in_from[32];

/* What the poll last saw. `s_wait_from` is written BEFORE `s_wait_count` is raised and
   cleared AFTER it is lowered, so a renderer that sees a non-zero count always reads a name
   that belongs to it — the one ordering rule this lock-free pair needs. */
static char s_wait_from[32];
static volatile int s_wait_count;

/* THE OTHER PANEL'S NAME, which this device has no other way to learn — it is not in NVS,
   because the box mints it at the SIBLING'S flash, and a panel that had to be re-flashed every
   time its twin was named would be a cable for a fact. It rides the poll that was already
   happening. Empty until the first successful poll, and empty forever where the box says there
   is not exactly one other panel: with two siblings "the other one" is a question, not a name,
   and a guess would put the wrong child on the glass. */
static char s_sibling[32];

static void trust(esp_http_client_config_t *hc)
{
    if (s_cfg->ca != NULL && s_cfg->ca[0] != '\0') {
        hc->cert_pem = s_cfg->ca;
        return;
    }
    hc->crt_bundle_attach = esp_crt_bundle_attach;
}

/* CHECKED, BECAUSE snprintf TRUNCATES SILENTLY AND A TRUNCATED CREDENTIAL IS A 401 — the
   same trap `talk.c` documents, and the same answer. Both strings come out of NVS with no
   length bound. */
/* `/api/jpanel/…`, NOT `/api/endpoint/jpanel/…`, AND THE DIFFERENCE WAS A 404 ON EVERY CALL.
 *
 * `JPANEL_PLAN.md` §3b wrote the panel's routes under the device surface, beside
 * `/endpoint/converse`, and W2 mounted the whole thing — panel routes and owner routes alike —
 * under one `/jpanel` router instead. The plan was not re-read when the firmware was written,
 * so this built, linked, passed every test there is, and would have failed silently on a
 * panel: a 404 is indistinguishable from "nobody sent me anything" through `GET /waiting`, so
 * the feature would have looked merely quiet.
 *
 * Caught by asking the live box rather than by reading either file — `/api/jpanel/waiting`
 * answered 401 and `/api/endpoint/jpanel/waiting` answered 404. The paths are pinned from the
 * other end now by `test_the_panel_facing_routes_are_where_the_firmware_looks`, because
 * nothing on the host can check a URL this file builds. */
static bool endpoint(char *url, size_t url_cap, char *auth, size_t auth_cap, const char *path)
{
    const int un = snprintf(url, url_cap, "%s/jpanel%s", s_cfg->api, path);
    const int an = snprintf(auth, auth_cap, "Bearer %s", s_cfg->token);
    if (un < 0 || un >= (int)url_cap || an < 0 || an >= (int)auth_cap) {
        ESP_LOGE(TAG, "api url or token too long (%d, %d) — not sending", un, an);
        return false;
    }
    return true;
}


/* THE MESSAGE ID ARRIVES IN A RESPONSE HEADER, AND THERE IS ONLY ONE WAY TO READ ONE.
 *
 * `esp_http_client_get_header()` reads the REQUEST headers — it returns what this panel sent,
 * not what the box answered, which is a trap worth naming because the call compiles, runs and
 * hands back NULL forever. Response headers reach a caller through the event handler alone,
 * dispatched while `esp_http_client_fetch_headers()` parses them.
 *
 * `X-Jpanel-Id` is what `POST /played` names, so losing it means a message that plays every
 * time the panel asks. `X-Jpanel-From` is who the child hears it is from. */
static void on_header(const esp_http_client_event_t *e)
{
    if (e->header_key == NULL || e->header_value == NULL) return;
    if (strcasecmp(e->header_key, "X-Jpanel-Id") == 0) {
        strlcpy(s_in_id, e->header_value, sizeof(s_in_id));
    } else if (strcasecmp(e->header_key, "X-Jpanel-From") == 0) {
        strlcpy(s_in_from, e->header_value, sizeof(s_in_from));
    }
}

static esp_err_t http_event(esp_http_client_event_t *e)
{
    if (e->event_id == HTTP_EVENT_ON_HEADER) on_header(e);
    return ESP_OK;
}

static esp_http_client_handle_t open_client(const char *path, esp_http_client_method_t method,
                                            char *url, size_t url_cap)
{
    char auth[256];
    if (!endpoint(url, url_cap, auth, sizeof(auth), path)) return NULL;
    esp_http_client_config_t hc = {.url = url,
                                   .timeout_ms = JPANEL_HTTP_TIMEOUT_MS,
                                   .method = method,
                                   .event_handler = http_event};
    trust(&hc);
    esp_http_client_handle_t c = esp_http_client_init(&hc);
    if (c != NULL) esp_http_client_set_header(c, "Authorization", auth);
    return c;
}

/* --- POST /send?to=panel|dad ------------------------------------------------------------- */

static void do_send(jpanel_to_t to)
{
    char url[288];
    char path[32];
    snprintf(path, sizeof(path), "/send?to=%s", to == JPANEL_TO_DAD ? "dad" : "panel");
    esp_http_client_handle_t c = open_client(path, HTTP_METHOD_POST, url, sizeof(url));
    if (c == NULL) {
        s_state = JPANEL_FAILED;
        return;
    }
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");

    const int64_t t0 = esp_timer_get_time();
    jpanel_state_t out = JPANEL_FAILED;

    if (esp_http_client_open(c, (int)s_bytes) != ESP_OK) {
        ESP_LOGW(TAG, "connect failed");
        goto done;
    }
    const uint8_t *p = (const uint8_t *)s_pcm;
    size_t left = s_bytes;
    while (left > 0) {
        const int n = esp_http_client_write(c, (const char *)p, left > 4096 ? 4096 : (int)left);
        if (n <= 0) {
            ESP_LOGW(TAG, "upload stalled with %u bytes left", (unsigned)left);
            goto done;
        }
        p += n;
        left -= (size_t)n;
    }
    if (esp_http_client_fetch_headers(c) < 0) goto done;
    const int status = esp_http_client_get_status_code(c);
    if (status == 409) {
        /* THE REFUSAL THE PLAN REFUSED TO GUESS AT. "The other panel" is only obvious with
           exactly two, so with none or several the box says no rather than picking — and the
           panel says it out loud, because a message a child believes they sent is worse than
           one they were told did not go. */
        ESP_LOGW(TAG, "no single other panel to send to");
        out = JPANEL_NOBODY;
        goto done;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "box said %d", status);
        goto done;
    }
    out = JPANEL_SENT;

done:
    ESP_LOGI(TAG, "send: %u B to %s in %d ms -> %d", (unsigned)s_bytes,
             to == JPANEL_TO_DAD ? "dad" : "panel",
             (int)((esp_timer_get_time() - t0) / 1000), (int)out);
    esp_http_client_cleanup(c);
    s_state = out;
}

/* --- GET /waiting ------------------------------------------------------------------------ */

static void do_poll(void)
{
    char url[288];
    esp_http_client_handle_t c = open_client("/waiting", HTTP_METHOD_GET, url, sizeof(url));
    if (c == NULL) return;

    char body[192];
    int got = 0;
    if (esp_http_client_open(c, 0) != ESP_OK) goto done;
    if (esp_http_client_fetch_headers(c) < 0) goto done;
    if (esp_http_client_get_status_code(c) != 200) goto done;
    while (got < (int)sizeof(body) - 1) {
        const int n = esp_http_client_read(c, body + got, (int)sizeof(body) - 1 - got);
        if (n <= 0) break;
        got += n;
    }
    body[got] = '\0';
    if (got > 0) {
        cJSON *root = cJSON_Parse(body);
        if (root != NULL) {
            const cJSON *n = cJSON_GetObjectItemCaseSensitive(root, "count");
            const cJSON *f = cJSON_GetObjectItemCaseSensitive(root, "from_name");
            const cJSON *sib = cJSON_GetObjectItemCaseSensitive(root, "sibling");
            const int count = cJSON_IsNumber(n) ? n->valueint : 0;
            /* Name first, then the count: see the declaration. */
            if (count > 0 && cJSON_IsString(f) && f->valuestring != NULL) {
                strlcpy(s_wait_from, f->valuestring, sizeof(s_wait_from));
            }
            /* Whatever the box says, including "" — a panel renamed out of the pair must
               stop claiming a sibling, and an older box that does not send the field leaves
               the word MESSAGE in place rather than a stale name. */
            strlcpy(s_sibling, cJSON_IsString(sib) && sib->valuestring != NULL ? sib->valuestring
                                                                              : "",
                    sizeof(s_sibling));
            if (count != s_wait_count) ESP_LOGI(TAG, "waiting: %d from %s", count, s_wait_from);
            s_wait_count = count;
            if (count == 0) s_wait_from[0] = '\0';
            cJSON_Delete(root);
        }
    }

done:
    esp_http_client_cleanup(c);
}

/* --- GET /next: collect a message, do NOT play it ------------------------------------------ */

static void do_fetch(bool asked)
{
    char url[288];
    esp_http_client_handle_t c = open_client("/next", HTTP_METHOD_GET, url, sizeof(url));
    if (c == NULL) {
        s_state = JPANEL_FAILED;
        return;
    }

    jpanel_state_t out = JPANEL_FAILED;
    int got = 0;
    s_in_id[0] = '\0';
    s_in_from[0] = '\0';
    if (esp_http_client_open(c, 0) != ESP_OK) goto done;
    if (esp_http_client_fetch_headers(c) < 0) goto done;
    const int status = esp_http_client_get_status_code(c);
    if (status == 204) {
        /* Someone else played it, or the poll was stale. Not a failure — just nothing here. */
        s_wait_count = 0;
        s_wait_from[0] = '\0';
        out = JPANEL_IDLE;
        goto done;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "box said %d", status);
        goto done;
    }
    /* `s_in_id` and `s_in_from` were filled by `on_header` while `fetch_headers` ran, and
       both were cleared before the request so a box that sends neither cannot leave the last
       message's id standing. */
    while (got < JPANEL_MAX_BYTES) {
        const int n = esp_http_client_read(c, (char *)s_in + got, JPANEL_MAX_BYTES - got);
        if (n <= 0) break;
        got += n;
    }
    if (got < 2) {
        ESP_LOGW(TAG, "empty message");
        goto done;
    }
    s_in_len = got;
    s_held = true;
    out = JPANEL_IDLE;
    ESP_LOGI(TAG, "holding %d B from %s, id %s", got, s_in_from[0] ? s_in_from : "?",
             s_in_id[0] ? s_in_id : "(none)");

done:
    esp_http_client_cleanup(c);
    /* THE BACKGROUND FETCH REPORTS NOTHING, and that is not tidiness. It runs off the poll,
       which fires on this task's own clock — so writing `s_state` here would overwrite a
       `JPANEL_SENT` or `JPANEL_NOBODY` the renderer had not shown yet, and the child would
       lose the sound that told them their message went. Only a fetch a finger asked for has
       an outcome worth reporting.
     *
       And never PLAYING: nothing was played. `jpanel_play_next` owns that transition. */
    if (asked) s_state = out;
    if (asked && s_held) {
        /* Tapped before the poll had collected it. Play it now rather than making the child
           tap a second time — the pop-up is already gone from their screen. */
        if (audio_play((const int16_t *)s_in, (size_t)s_in_len)) {
            s_held = false;
            s_owed = true;
            s_run = true;
            s_state = JPANEL_PLAYING;
            if (s_wait_count > 0) s_wait_count--;
            if (s_wait_count == 0) s_wait_from[0] = '\0';
            ESP_LOGI(TAG, "playing %d B from %s (fetched on the tap)", s_in_len,
                     s_in_from[0] ? s_in_from : "?");
        }
    }
}

/* --- POST /played ------------------------------------------------------------------------ */

static void do_played(void)
{
    if (s_in_id[0] == '\0') return;
    char url[288];
    esp_http_client_handle_t c = open_client("/played", HTTP_METHOD_POST, url, sizeof(url));
    if (c == NULL) return;
    char body[80];
    const int bn = snprintf(body, sizeof(body), "{\"id\":\"%s\"}", s_in_id);
    if (bn > 0 && bn < (int)sizeof(body)) {
        esp_http_client_set_header(c, "Content-Type", "application/json");
        esp_http_client_set_post_field(c, body, bn);
        if (esp_http_client_perform(c) == ESP_OK) {
            const int status = esp_http_client_get_status_code(c);
            if (status != 204) ESP_LOGW(TAG, "played returned HTTP %d", status);
        } else {
            /* NOT FATAL, AND NOT RETRIED HERE. An unacknowledged message stays unplayed on
               the box, so the worst case is hearing it twice — which is strictly better than
               a message that vanishes because the acknowledgement raced a flaky link. */
            ESP_LOGW(TAG, "could not acknowledge %s", s_in_id);
        }
    }
    esp_http_client_cleanup(c);
}

static void jpanel_task(void *arg)
{
    (void)arg;
    /* Staggered, so two panels on the same Wi-Fi do not ask in lockstep forever. */
    uint32_t next_poll = (uint32_t)(esp_timer_get_time() / 1000) + (esp_random() % POLL_EVERY_MS);
    while (true) {
        cmd_t cmd;
        /* A short wait rather than a block: the poll is this task's own heartbeat, and the
           acknowledgement below has to fire when the SPEAKER finishes, which no command
           announces. */
        const bool have = xQueueReceive(s_q, &cmd, pdMS_TO_TICKS(250)) == pdTRUE;
        if (have) {
            switch (cmd.kind) {
            case CMD_SEND: do_send(cmd.to); break;
            case CMD_FETCH: do_fetch(cmd.asked); break;
            case CMD_POLL: next_poll = 0; break;
            }
        }
        /* THE ACKNOWLEDGEMENT WAITS FOR THE SPEAKER, which is the whole reason `GET /next`
           does not mark it played: a panel that loses power mid-message must still have the
           message. `s_owed` is set where the audio starts — see its declaration for the race
           that taught us not to watch for a state instead. */
        if (s_owed && !audio_playing() && s_state != JPANEL_BUSY) {
            s_owed = false;
            do_played();
            next_poll = 0;
            /* STRAIGHT ON TO THE NEXT, if the child has not stopped the run. The one just
               finished is acknowledged first — the order matters, because fetching before
               acknowledging would hand back the same message again. */
            if (s_run && s_wait_count > 0) {
                do_fetch(true);
            } else {
                s_run = false;
            }
        }
        const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
        /* Never while the speaker is running: a poll is a TLS handshake, and the audio task
           is feeding I2S from the same core's spare cycles. */
        if (now >= next_poll && !audio_playing() && s_state != JPANEL_BUSY) {
            do_poll();
            next_poll = now + POLL_EVERY_MS;
            /* COLLECT IT NOW, NOT WHEN THE CHILD TAPS. The whole round trip moves off the tap
               path and into the wait nobody is watching, which is what turns two seconds of a
               child staring at a pop-up into a memcpy. Safe precisely because `GET /next` does
               not mark a message played. */
            if (s_wait_count > 0 && !s_held && s_state != JPANEL_BUSY) do_fetch(false);
        }
    }
}

bool jpanel_start(const cfg_t *cfg)
{
    if (cfg == NULL) return false;
    s_cfg = cfg;
    s_in = heap_caps_malloc(JPANEL_MAX_BYTES, MALLOC_CAP_SPIRAM);
    s_q = xQueueCreate(2, sizeof(cmd_t));
    if (s_in == NULL || s_q == NULL) {
        ESP_LOGE(TAG, "no memory for voice post");
        return false;
    }
    /* Off the render core, like `talk.c`: this task blocks on a socket for seconds. */
    if (xTaskCreatePinnedToCore(jpanel_task, "jpanel", 6144, NULL, 4, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "task failed");
        return false;
    }
    ESP_LOGI(TAG, "ready");
    return true;
}

static bool post(cmd_kind_t kind, jpanel_to_t to, bool asked)
{
    if (s_q == NULL) return false;
    const cmd_t cmd = {.kind = kind, .to = to, .asked = asked};
    return xQueueSend(s_q, &cmd, 0) == pdTRUE;
}

bool jpanel_send(const int16_t *pcm, size_t bytes, jpanel_to_t to)
{
    if (s_q == NULL || pcm == NULL || bytes < 2) return false;
    if (s_state == JPANEL_BUSY) return false;
    s_pcm = pcm;
    s_bytes = bytes;
    s_state = JPANEL_BUSY;
    if (post(CMD_SEND, to, false)) return true;
    s_state = JPANEL_IDLE;
    return false;
}

bool jpanel_play_next(void)
{
    if (s_q == NULL) return false;
    /* THE FAST PATH, AND IT IS THE ONLY ONE A CHILD SHOULD EVER MEET. A message collected by
       the poll is already in PSRAM, so this is a memcpy into the speaker's buffer and the
       sound starts on the same frame as the finger. */
    if (s_held && s_in_len >= 2) {
        if (!audio_play((const int16_t *)s_in, (size_t)s_in_len)) return false;
        s_held = false;
        s_owed = true;
        s_run = true;
        s_state = JPANEL_PLAYING;
        /* Optimistic, and deliberately so: the count is what draws the pop-up, and leaving it
           up while the message plays would tell a child there is still one waiting. The next
           poll corrects it either way. */
        if (s_wait_count > 0) s_wait_count--;
        if (s_wait_count == 0) s_wait_from[0] = '\0';
        ESP_LOGI(TAG, "playing %d B from %s", s_in_len, s_in_from[0] ? s_in_from : "?");
        return true;
    }
    /* The slow path survives for the case the fast one cannot cover: a pop-up tapped before
       the poll that announced it had time to collect the audio. It still works, it is simply
       the two seconds this change exists to remove. */
    if (s_state == JPANEL_BUSY) return false;
    s_state = JPANEL_BUSY;
    if (post(CMD_FETCH, JPANEL_TO_PANEL, true)) return true;
    s_state = JPANEL_IDLE;
    return false;
}

bool jpanel_replay(void)
{
    if (s_in == NULL || s_in_len < 2) return false;
    return audio_play((const int16_t *)s_in, (size_t)s_in_len);
}

/* Stop a run. The message sounding is cut and nothing more is fetched; whatever has not been
   played is still unplayed on the box, so the pop-up returns for it. */
void jpanel_stop(void)
{
    s_run = false;
    audio_stop();
}

bool jpanel_running(void)
{
    return s_run;
}

void jpanel_poll_soon(void)
{
    (void)post(CMD_POLL, JPANEL_TO_PANEL, false);
}

/* Who the message now in the buffer came from — the caption beside the repeat icon. Empty
   when nothing has been played, which is what the renderer tests. */
const char *jpanel_last_from(void)
{
    return s_in_from;
}

int jpanel_waiting(char *from, size_t cap)
{
    const int n = s_wait_count;
    if (from != NULL && cap > 0) strlcpy(from, s_wait_from, cap);
    return n;
}

int jpanel_sibling(char *out, size_t cap)
{
    if (out == NULL || cap == 0) return 0;
    strlcpy(out, s_sibling, cap);
    return (int)strlen(out);
}

jpanel_state_t jpanel_state(void)
{
    return s_state;
}

void jpanel_clear(void)
{
    if (s_state != JPANEL_BUSY) s_state = JPANEL_IDLE;
}
