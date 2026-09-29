/* Voice post. See jpanel.h for why this is a task of its own. */

#include "jpanel.h"

#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "audio.h"
#include "nudge.h"
#include "reach.h"
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "mbedtls/sha256.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "jpanel";

/* Local for `ota.c`'s reason: `reach.c` is on the host suite and takes its clock as an argument. */
static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Shorter than `talk.c`'s 30 s. A turn waits on whisper AND a language model AND a voice; a
   send waits on whisper alone and a fetch on a blob read, so a request still running after
   this is not slow, it is broken. */
#define JPANEL_HTTP_TIMEOUT_MS 20000

/* THERE IS NO INBOUND CEILING ANY MORE, and its absence is the feature.
 *
 * This used to be the size of the buffer a whole message was read into — twenty seconds, then
 * thirty — and it had to be kept in step with `MAX_MESSAGE_MS` on the box and with `audio.c`'s
 * play buffer, or a message was cut at whichever was smallest, silently. The audio streams
 * through a ring now (`audio_stream_*`), so the only limit left on a message's length is what
 * the box is willing to store, and the panel does not need to know that number at all. */

/* ~30 s, as the plan's contract says: fast enough that "my sister just sent me something" is
   answered while she is still in the room, small enough that two panels asking forever costs
   the box nothing — the route returns a count and a name and touches one index. */
#define POLL_EVERY_MS 30000

typedef enum { CMD_SEND = 0, CMD_FETCH, CMD_POLL, CMD_REPLAY } cmd_kind_t;

typedef struct {
    cmd_kind_t kind;
    jpanel_to_t to;  /* CMD_SEND only */
    bool asked;      /* CMD_FETCH: a finger is waiting for this, so play it and report */
    int at;          /* CMD_FETCH: which queued message — 0 is the oldest */
} cmd_t;

static const cfg_t *s_cfg;
static QueueHandle_t s_q;
static volatile jpanel_state_t s_state;

/* The outgoing recording, borrowed from `audio.c` and valid until the next capture. */
static const int16_t *s_pcm;
static volatile size_t s_bytes;

/* The incoming message: PSRAM, claimed once at start-up, because a heap request in the middle
   of a child waiting for their sister's voice is a failure with no good outcome. */
/* ONE HTTP READ'S WORTH, NOT ONE MESSAGE'S. The 960 KB that used to sit here held a whole
   message so the repeat icon could replay it from memory; it is a ring in `audio.c` now, and
   "again" asks the box a second time (`GET /message/{id}/pcm`). What is left is the staging
   buffer between the socket and that ring. */
#define JPANEL_READ_CHUNK 4096
static uint8_t s_chunk[JPANEL_READ_CHUNK];
/* WHY THERE IS NO LONGER A HELD MESSAGE, because the reason there WAS one still matters.
 *
 * The owner: *"the message should start playing faster. There's a couple seconds between me
 * acknowledging the message and it's starting to play."* That gap was the whole round trip —
 * a TLS handshake, a blob read on the box, a rate conversion and up to 640 KB down the wire —
 * and it ran AFTER the tap, because the tap is what started it. The answer then was to fetch
 * early and hold the bytes, so the tap became a memcpy.
 *
 * Streaming removes the gap at its source instead. The first sound needs only the preroll —
 * about 48 KB rather than the whole message — so the tap is answered sooner than the prefetch
 * ever managed, and nothing is held. The 960 KB that held it, and the 960 KB play buffer it
 * was copied into, are both gone; what replaces them is a four-second ring in `audio.c`. */

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

/* ── WHETHER A MESSAGE WAS ACTUALLY HEARD, WHICH NOTHING HAS EVER REPORTED ────────────────────
 *
 * The owner, on 0.3.32: *"the notification shows up and I click the notification and then playback
 * menu pulls up. But then [it] only stay[s] for about a half second before going back to the big
 * blue notification and it doesn't play."*
 *
 * THE BOX'S LOG SAID THE OPPOSITE AND WAS NOT WRONG: `GET /next` 200, then `POST /played` 204.
 * The bytes were served, the digest matched — `verified()` would have refused otherwise — and the
 * panel acknowledged. Every server-side record of that message says it was delivered and heard.
 *
 * Because the acknowledgement watches `audio_playing()`, not the SPEAKER. That is the right
 * design — a panel that loses power mid-message must keep the message — but it makes "the ring
 * drained" and "a child heard it" the same event, and they are not. A ring that drains in forty
 * milliseconds has played nothing, and the box cannot tell that from three seconds of a father's
 * voice.
 *
 * SO MEASURE THE WALL CLOCK, which is the one number that separates them: how long from
 * `audio_stream_begin` to the ring going quiet, against how long the bytes should have taken.
 * 98 KB of 16-bit mono at 16 kHz is 3.06 seconds; if `msg_ms` comes back as 40 then the bytes went
 * somewhere that was not a speaker, and that is a different fault from the ones this file already
 * reports. Cheap, cumulative, and it survives to the next telemetry post like everything in
 * `reach.c` — for the same reason, which is that nobody is holding a serial cable. */
static volatile int s_msg_bytes;   /* of the last message streamed */
static volatile int s_msg_ms;      /* how long its ring actually sounded */
static volatile unsigned s_msg_ok; /* messages that streamed, verified and drained */
static volatile unsigned s_msg_bad;
static const char *s_msg_err = "";
static uint32_t s_msg_began_ms;
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
/* A MESSAGE IS ON ITS WAY TO THE SPEAKER — which `JPANEL_BUSY` does not mean. That state
   covers SENDING too, and the renderer used it to decide when to put the playback controls up.
   So hitting the green tick to send Dad a message raised BUSY, and the panel drew a pause
   button and a sender's face over a message that did not exist and would never play: the tap
   handler correctly refused to honour any of it, so the controls sat there inert. The owner:
   *"it immediately goes to this second screen with the little girl on the top left with a
   little play pause. But there's nothing playing and nothing responds to it."*
   Set where a FETCH begins and cleared where it ends, so it answers the question the renderer
   is actually asking rather than one that happens to overlap it most of the time. */
static volatile bool s_fetching;
/* A finger ended the last stream, rather than the network. See `jpanel_stop`. */
static volatile bool s_stopped;
/* The id the box gave it, held so `POST /played` can name it after the speaker finishes, and
   who it came from, which is what the repeat icon's caption says. Both are filled by the
   header handler below. */
static char s_in_id[48];
static char s_in_from[32];
/* Whether the message being streamed came from the owner rather than the other panel. See
   `X-Jpanel-From-Kind` in `on_header`. */
static bool s_in_from_dad;
/* WHAT THE BOX SAYS IT SENT, hex, or "" from a box too old to say. The panel hashes the
   message as it streams it into the speaker and compares at the end — not to re-play it, which
   it cannot, but to decide whether to ACKNOWLEDGE it. A download that ends early plays a
   message that stops mid-sentence, and acknowledging that would retire it: the box would never
   offer it again and nobody would know the rest existed. Unverified, it is simply not
   acknowledged, so it stays unplayed and the pop-up comes back. */
static char s_in_sha[72];

/* What the poll last saw. `s_wait` is written BEFORE `s_wait_count` is raised and cleared AFTER
   it is lowered, so a renderer that sees a non-zero count always reads entries that belong to it
   — the one ordering rule this lock-free pair needs.

   A LIST NOW, NOT A SINGLE NAME, because the queue stopped being a number the moment a child
   could swipe through it: the face in the corner is per-message, and one `from_name` could only
   ever describe the head. The head is `s_wait[0]`, which is what `jpanel_waiting_from_dad` and the
   `from` out-parameter of `jpanel_waiting` still report — they did not change meaning, they
   acquired seven neighbours. */
typedef struct {
    char from[32];
    bool from_dad;
} wait_one_t;

static wait_one_t s_wait[JPANEL_QUEUE_MAX];
/* How many of `s_wait` the last poll actually filled — never more than `s_wait_count`, and less
   whenever the box has more queued than it will describe one by one. */
static volatile int s_wait_known;
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
    } else if (strcasecmp(e->header_key, "X-Jpanel-From-Kind") == 0) {
        /* WHERE A REPLY GOES, which is not the same question as who the child hears it is
           from. `X-Jpanel-From` is a NAME the owner can change; this is the kind, and it has
           only two answers. Matching the name against "Dad" would work until somebody was
           renamed — the coupling migration 0211 exists to undo. Absent on a box too old to
           send it, and the default below is the safer of the two: replying to the other panel
           is a message to a four-year-old, where replying to the owner is not what was asked
           for but is at least delivered to somebody who can work out what happened. */
        s_in_from_dad = strcasecmp(e->header_value, "owner") == 0;
    } else if (strcasecmp(e->header_key, "X-Jpanel-Sha256") == 0) {
        strlcpy(s_in_sha, e->header_value, sizeof(s_in_sha));
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

/* --- GET /events: THE PUSH CHANNEL ------------------------------------------------------- *
 *
 * A held-open HTTPS stream the box writes into. This is what makes a message arrive rather
 * than be discovered, and it is an ordinary authenticated request rather than a new protocol
 * precisely so that it inherits the device key, the TLS and the pinned certificate the panel
 * already uses instead of growing a second scheme to get right.
 *
 * WHAT IT CARRIES IS NOT DATA. Every event means "come and ask", and the panel answers by
 * making the poll it would have made anyway. So this stream can never become a second, weaker
 * path by which state reaches a child's panel, and nothing arriving on it is trusted.
 *
 * IT REPLACES POLLING RATHER THAN ADDING TO IT. While the stream is up, `main.c` stretches the
 * settings cadence from three seconds to `PUSH_SETTLED_MS`, because the box can now say when
 * something changed and no longer needs to be asked. That matters for more than tidiness: it
 * keeps the number of TLS sessions this panel holds at once the SAME as before rather than one
 * higher, which is the only real cost of a persistent connection on this board.
 *
 * THE READ TIMEOUT IS THE LIVENESS CHECK. The box writes a heartbeat comment every 20 s
 * (`_HEARTBEAT_S`), so a read that returns nothing for `PUSH_IDLE_MS` means the link is gone —
 * a router rebooted, the box restarted, the Wi-Fi dropped — rather than that nobody has sent a
 * message. A TCP connection nobody writes to is indistinguishable from a dead one, and this is
 * a channel whose whole job is to be idle. */
#define PUSH_IDLE_MS 45000
#define PUSH_RETRY_MIN_MS 2000
#define PUSH_RETRY_MAX_MS 60000

static volatile bool s_push_live;
static volatile uint16_t s_push_events;
static volatile uint16_t s_push_drops;

bool jpanel_push_live(void)
{
    return s_push_live;
}

unsigned jpanel_push_events(void)
{
    return s_push_events;
}

unsigned jpanel_push_drops(void)
{
    return s_push_drops;
}

static void push_task(void *arg)
{
    (void)arg;
    uint32_t backoff = PUSH_RETRY_MIN_MS;
    while (true) {
        char url[288];
        char auth[256];
        if (!endpoint(url, sizeof(url), auth, sizeof(auth), "/events")) {
            vTaskDelay(pdMS_TO_TICKS(backoff));
            continue;
        }
        esp_http_client_config_t hc = {.url = url,
                                       /* Long, not absent: this is the liveness check above. */
                                       .timeout_ms = PUSH_IDLE_MS,
                                       .method = HTTP_METHOD_GET,
                                       .event_handler = http_event};
        trust(&hc);
        esp_http_client_handle_t c = esp_http_client_init(&hc);
        if (c == NULL) {
            vTaskDelay(pdMS_TO_TICKS(backoff));
            continue;
        }
        esp_http_client_set_header(c, "Authorization", auth);
        /* Says what this is to anything in between, and makes a proxy that buffers by default
           stop doing so. The box sets `X-Accel-Buffering: no` for the same reason. */
        esp_http_client_set_header(c, "Accept", "text/event-stream");

        bool opened = esp_http_client_open(c, 0) == ESP_OK &&
                      esp_http_client_fetch_headers(c) >= 0 &&
                      esp_http_client_get_status_code(c) == 200;
        if (opened) {
            ESP_LOGI(TAG, "push: stream open");
            s_push_live = true;
            backoff = PUSH_RETRY_MIN_MS; /* a connection that worked resets the patience */
            char buf[128];
            while (true) {
                const int n = esp_http_client_read(c, buf, sizeof(buf) - 1);
                if (n <= 0) break; /* timeout, close, or error — all mean reconnect */
                buf[n] = '\0';
                /* A HEARTBEAT IS NOT AN EVENT. The box writes ": \n\n" to keep the link
                   provably alive; only a `data:` line means something changed. Counting
                   heartbeats as events would make the panel poll three times a minute forever
                   and quietly undo the cadence this stream exists to relax. */
                if (strstr(buf, "data:") == NULL) continue;
                if (s_push_events < 65535) s_push_events++;
                /* THE SAME TWO HALVES THE DATAGRAM WAKES, for the same reason: the event says
                   only that something changed, so the panel asks about everything it would
                   have asked about anyway. */
                jpanel_poll_soon();
                nudge_wake_settings();
            }
            ESP_LOGW(TAG, "push: stream closed");
            s_push_live = false;
            if (s_push_drops < 65535) s_push_drops++;
        }
        esp_http_client_cleanup(c);
        vTaskDelay(pdMS_TO_TICKS(backoff));
        /* Doubling, capped. A box that is down stays down for minutes, and a panel retrying
           every two seconds against it is a TLS handshake every two seconds on the one board
           whose handshakes are expensive. */
        backoff = backoff * 2 > PUSH_RETRY_MAX_MS ? PUSH_RETRY_MAX_MS : backoff * 2;
    }
}

/* --- POST /send?to=panel|dad ------------------------------------------------------------- */

/* One retry, and only for the one failure a retry can fix.
 *
 * A hash mismatch means the bytes on the box are not the bytes in this buffer — a truncated
 * upload or a flipped bit on the wire — and sending the same buffer again is exactly the right
 * response, because the buffer is the good copy. Everything else (no sibling, a 500, a dead
 * socket) is either permanent or already reported, and hammering it would only delay the
 * child's answer. */
#define SEND_ATTEMPTS 2

static jpanel_state_t do_send_once(jpanel_to_t to, bool *corrupt);

static void do_send(jpanel_to_t to)
{
    for (int attempt = 1; attempt <= SEND_ATTEMPTS; attempt++) {
        bool corrupt = false;
        const jpanel_state_t out = do_send_once(to, &corrupt);
        if (!corrupt || attempt == SEND_ATTEMPTS) {
            if (corrupt) {
                /* LOUD, because this is a child's message that did not go and the panel is
                   about to say so on the glass. Twice in a row is not a bad packet. */
                ESP_LOGE(TAG, "upload failed verification twice — not sent");
            }
            s_state = out;
            return;
        }
        ESP_LOGW(TAG, "box did not get what we sent — trying once more");
    }
}

static jpanel_state_t do_send_once(jpanel_to_t to, bool *corrupt)
{
    char url[288];
    char path[32];
    snprintf(path, sizeof(path), "/send?to=%s", to == JPANEL_TO_DAD ? "dad" : "panel");
    esp_http_client_handle_t c = open_client(path, HTTP_METHOD_POST, url, sizeof(url));
    if (c == NULL) return JPANEL_FAILED;
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");

    /* WHAT THE BOX SHOULD END UP WITH, SAID BEFORE THE BYTES GO.
     *
     * The upload is a chunked write over a radio in a bedroom, and until now nothing on either
     * end could tell a message that arrived whole from one that arrived short. A stalled write
     * is caught here, but a connection that ends cleanly after two thirds of a sentence is not
     * — the box would store what it got, whisper would transcribe it, and a child would be
     * told her message went. Hashing what we are about to send turns that into a refusal the
     * panel can act on.
     *
     * Computed over the capture buffer before the first byte, because a header cannot follow a
     * body. The buffer is still ours until the box says yes, which is what makes a retry
     * possible — and is the reason the recording is not streamed straight off the microphone.
     *
     * SHA-256 because the ESP32-S3 has it in hardware and `mbedtls` is already linked for the
     * CA bundle; a cheaper checksum would catch a truncation but not a corruption, and the box
     * is content-addressing these bytes with the same function anyway. */
    unsigned char digest[32];
    char hex[65];
    if (mbedtls_sha256((const unsigned char *)s_pcm, s_bytes, digest, 0) == 0) {
        for (int i = 0; i < 32; i++) snprintf(&hex[i * 2], 3, "%02x", digest[i]);
        hex[64] = '\0';
        esp_http_client_set_header(c, "X-Jpanel-Sha256", hex);
    } else {
        /* Not fatal: an older box ignores the header and a newer one treats its absence as
           "this panel cannot prove it", which is exactly what has been true all along. */
        ESP_LOGW(TAG, "could not hash the recording — sending it unverified");
        hex[0] = '\0';
    }

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
    if (status == 422) {
        /* The box hashed what arrived and got something else. Recoverable, and the only
           status this panel retries. */
        *corrupt = true;
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
    return out;
}

/* --- GET /waiting ------------------------------------------------------------------------ */

static void do_poll(void)
{
    char url[288];
    esp_http_client_handle_t c = open_client("/waiting", HTTP_METHOD_GET, url, sizeof(url));
    if (c == NULL) {
        reach_fail(REACH_POLL, "no-client", now_ms());
        return;
    }
    const char *why = "unknown";

    /* STATIC, AND BIGGER THAN IT WAS, because the response grew a list.
     *
       192 bytes held the four scalar fields with room to spare and would silently TRUNCATE a queue
       of eight. THE FAILURE MODE IS WHY THIS IS SIZED RATHER THAN GUESSED: a partial JSON body
       parses as nothing, so the panel would read every poll as "no messages" — a quiet panel, which
       looks like the box having nothing to say and not like a buffer at all.
     *
       MEASURED AGAINST THE BOX'S OWN CAP, not against a name anybody has: eight entries of
       `{"from_name":"<14>","from_owner":false},` plus the four scalars is 557 bytes at the 14
       characters `MAX_PANEL_NAME` allows, and 667 even if every name filled the 31 this panel can
       hold. `test_the_panel_can_hold_the_longest_poll_the_box_will_send` recomputes that from both
       sides, so a raised name cap or a longer queue fails there rather than here.
     *
       Static rather than automatic because only `jpanel_task` ever calls this, and a kilobyte is a
       meaningful fraction of a 6 KB task stack on a board where stacks are what crash-looped
       0.3.22. */
    static char body[1024];
    int got = 0;
    /* NAMED, LIKE THE SETTINGS FETCH AND THE CONVERSATION, and this path is the reason the other
       two were diagnosable at all on 2026-09-29: it kept succeeding for an hour after the settings
       poll stopped, and "one task can reach the box and the other cannot" is what ruled out the
       network and pointed at the panel. A path that can only be inferred from the box's access log
       is a path that says nothing while the panel is silent, which is precisely when it is asked. */
    if (esp_http_client_open(c, 0) != ESP_OK) {
        why = "connect";
        goto done;
    }
    if (esp_http_client_fetch_headers(c) < 0) {
        why = "no-headers";
        goto done;
    }
    const int status = esp_http_client_get_status_code(c);
    if (status != 200) {
        static char code[12];
        snprintf(code, sizeof(code), "http-%d", status);
        why = code;
        goto done;
    }
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
            /* WHOSE FACE THE BADGE DRAWS. A kind, not a name: `from_name` is the owner's to
               change, and matching it against "Dad" here is the coupling migration 0211 undid.
               Absent on an older box, where false means the badge falls back to the sister —
               the more likely sender on a panel, and the wrong guess costs a picture rather
               than a misdelivered message. */
            const cJSON *fo = cJSON_GetObjectItemCaseSensitive(root, "from_owner");
            const cJSON *sib = cJSON_GetObjectItemCaseSensitive(root, "sibling");
            const int count = cJSON_IsNumber(n) ? n->valueint : 0;
            /* THE ENTRIES FIRST, THEN THE COUNT: see the declaration. A reader that sees a count
               must already be able to see what it counts.

               `queue` IS THE ANSWER AND THE TWO SCALARS ARE ITS FALLBACK, not the other way
               round. The box derives `from_name`/`from_owner` from `queue[0]`, so on a current
               box the two agree by construction; on one too old to send a list they are the only
               thing there is, and seeding the head from them leaves the panel exactly as capable
               as it was — one message describable, the rest merely counted. */
            int known = 0;
            const cJSON *q = cJSON_GetObjectItemCaseSensitive(root, "queue");
            if (cJSON_IsArray(q)) {
                const cJSON *one = NULL;
                cJSON_ArrayForEach(one, q) {
                    if (known >= JPANEL_QUEUE_MAX) break;
                    const cJSON *qn = cJSON_GetObjectItemCaseSensitive(one, "from_name");
                    const cJSON *qo = cJSON_GetObjectItemCaseSensitive(one, "from_owner");
                    strlcpy(s_wait[known].from,
                            cJSON_IsString(qn) && qn->valuestring != NULL ? qn->valuestring : "",
                            sizeof(s_wait[known].from));
                    s_wait[known].from_dad = cJSON_IsTrue(qo);
                    known++;
                }
            }
            if (known == 0 && count > 0) {
                strlcpy(s_wait[0].from,
                        cJSON_IsString(f) && f->valuestring != NULL ? f->valuestring : "",
                        sizeof(s_wait[0].from));
                s_wait[0].from_dad = cJSON_IsTrue(fo);
                known = 1;
            }
            s_wait_known = known;
            /* Whatever the box says, including "" — a panel renamed out of the pair must
               stop claiming a sibling, and an older box that does not send the field leaves
               the word MESSAGE in place rather than a stale name. */
            strlcpy(s_sibling, cJSON_IsString(sib) && sib->valuestring != NULL ? sib->valuestring
                                                                              : "",
                    sizeof(s_sibling));
            if (count != s_wait_count) {
                ESP_LOGI(TAG, "waiting: %d (%d named) from %s", count, known, s_wait[0].from);
            }
            s_wait_count = count;
            if (count == 0) {
                s_wait_known = 0;
                s_wait[0].from[0] = '\0';
            }
            cJSON_Delete(root);
            why = NULL;
        } else {
            why = "bad-json";
        }
    } else {
        why = "empty-body";
    }

done:
    esp_http_client_cleanup(c);
    if (why != NULL) {
        reach_fail(REACH_POLL, why, now_ms());
    } else {
        reach_ok(REACH_POLL, now_ms());
    }
}

/* --- GET /next: collect a message, do NOT play it ------------------------------------------ */

/* Socket to ring, at the speed of the speaker.
 *
 * `audio_stream_write` takes what fits and returns how much it took, so a full ring is not an
 * error — it is the speaker saying "not yet". Waiting here is what bounds the memory: without
 * it a fast network would need somewhere to put a whole message again, which is the thing this
 * path exists to stop. Twenty milliseconds is half a chunk, so the codec never starves waiting
 * for this loop to come back.
 *
 * Returns the bytes handed over, which is how the caller tells a real message from an empty
 * one. */
static int pump(esp_http_client_handle_t c, char *got_hex, size_t hex_cap)
{
    /* FIRST, NOT LAST. Every path out of this function — including the early one when a finger
       stops the stream — must leave a readable string behind, or the caller compares the box's
       digest against whatever was on the stack. */
    if (got_hex != NULL && hex_cap > 0) got_hex[0] = '\0';

    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    bool hashing = mbedtls_sha256_starts(&sha, 0) == 0;
    int total = 0;
    /* AN ODD BYTE IS CARRIED, NOT OFFERED, and that is not tidiness — it is a hang.
       `esp_http_client_read` returns whatever the transport has, which on a timeout or a FIN
       mid-body is routinely an odd count; the ring deals in samples and refuses anything under
       two bytes, so a lone trailing byte would be offered forever at 20 ms a go on the task
       that also polls, sends and acknowledges. Held over and prepended to the next read
       instead, which is also the only way the samples stay aligned. */
    uint8_t odd = 0;
    bool have_odd = false;
    while (audio_stream_live()) {
        const int n = esp_http_client_read(c, (char *)s_chunk + (have_odd ? 1 : 0),
                                           (int)sizeof(s_chunk) - (have_odd ? 1 : 0));
        if (n <= 0) break;
        if (have_odd) s_chunk[0] = odd;
        int avail = n + (have_odd ? 1 : 0);
        have_odd = false;
        if (avail % 2 == 1) {
            odd = s_chunk[avail - 1];
            have_odd = true;
            avail -= 1;
        }
        if (hashing && mbedtls_sha256_update(&sha, s_chunk, (size_t)avail) != 0) hashing = false;
        int off = 0;
        while (off < avail) {
            /* CHECKED EVERY TIME ROUND, because a refusal has two meanings. A full ring says
               "not yet"; a stopped stream says "never" — and waiting out the second one would
               hang this task forever on a speaker that is no longer listening. */
            if (!audio_stream_live()) {
                mbedtls_sha256_free(&sha);
                return total;
            }
            const size_t took = audio_stream_write(s_chunk + off, (size_t)(avail - off));
            if (took == 0) {
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            off += (int)took;
        }
        total += avail;
    }
    /* A body that ended on an odd byte is a body that was cut: the digest will not match, and
       the last half-sample is not worth playing. Hashed as received so the mismatch is honest
       about what arrived. */
    if (have_odd && hashing && mbedtls_sha256_update(&sha, &odd, 1) == 0) total += 1;
    unsigned char digest[32];
    if (got_hex != NULL && hex_cap >= 65 && hashing && mbedtls_sha256_finish(&sha, digest) == 0) {
        for (int i = 0; i < 32; i++) snprintf(&got_hex[i * 2], 3, "%02x", digest[i]);
        got_hex[64] = '\0';
    }
    mbedtls_sha256_free(&sha);
    return total;
}

/* Did we get what the box said it was sending.
 *
 * TRUE WHEN THE BOX DID NOT SAY, and that is deliberate rather than lax: a panel talking to an
 * older box has exactly the assurance it always had, and refusing to acknowledge messages it
 * cannot verify would make every one of them play forever. What changes is only that a box
 * which DOES say is believed. */
static bool verified(const char *claimed, const char *got)
{
    if (claimed[0] == '\0') return true;
    if (got[0] == '\0') return false;
    return strcasecmp(claimed, got) == 0;
}

/* "AGAIN", WHICH USED TO BE A MEMCPY. The bytes are gone once they have played, so the repeat
   icon re-asks the box for that id. It is a different route from `/next` on purpose: replaying
   must not spend one of the five delivery attempts that exist to stop the box trying forever
   (`JPANEL_MAX_DELIVERIES`), or listening twice would be a way to lose a message. */
static void do_replay(void)
{
    /* BOTH EARLY RETURNS CLEAR IT. `jpanel_replay()` raises `s_fetching` on the RENDER task and
       checks the id there, while `do_fetch` clears the id on THIS task — so the empty-id return
       is genuinely reachable, not defensive, and either leak pins a pause button over a silent
       panel forever. */
    if (s_in_id[0] == '\0') {
        s_fetching = false;
        return;
    }
    bool ok = false;
    char path[96];
    snprintf(path, sizeof(path), "/message/%s/pcm", s_in_id);
    char url[288];
    esp_http_client_handle_t c = open_client(path, HTTP_METHOD_GET, url, sizeof(url));
    if (c == NULL) {
        s_fetching = false;
        s_state = JPANEL_FAILED;
        return;
    }
    /* CLEARED AFTER `path` IS BUILT, because that used `s_in_id` — and cleared at all because
       a box too old to send the header would otherwise leave the PREVIOUS message's digest
       standing, and this replay would be judged against it. */
    s_in_sha[0] = '\0';
    /* EVERY WAY OUT OF HERE SAYS SO. A replay that fails silently is the control a child
       presses when she missed something answering with nothing at all — which is exactly what
       the touch cue was added to stop, and the link being down is when she is most likely to
       be pressing it. */
    if (esp_http_client_open(c, 0) != ESP_OK) {
        ESP_LOGW(TAG, "replay: could not reach the box");
        goto done;
    }
    if (esp_http_client_fetch_headers(c) < 0) {
        ESP_LOGW(TAG, "replay: no answer from the box");
        goto done;
    }
    if (esp_http_client_get_status_code(c) != 200) {
        ESP_LOGW(TAG, "replay: box said %d", esp_http_client_get_status_code(c));
        goto done;
    }
    if (!audio_stream_begin()) {
        ESP_LOGW(TAG, "replay: speaker busy");
        goto done;
    }
    ok = true;
    /* NO `s_owed` AND NO `s_run`. This message was already acknowledged the first time it
       played; telling the box again would be a second `POST /played` for one listen, and
       joining the run would make "again" walk on into the next unheard message. */
    char heard[72];
    const int got = pump(c, heard, sizeof(heard));
    audio_stream_end();
    if (got < 2) {
        audio_stream_abort();
    } else if (!verified(s_in_sha, heard)) {
        /* Nothing to un-acknowledge — this message was acknowledged the first time it played.
           Worth a line, because a replay that arrives short is the same network fault that
           would cut a first play, and this is where it shows up without costing anything. */
        ESP_LOGW(TAG, "replay arrived incomplete (%d B), id %s", got, s_in_id);
    } else {
        ESP_LOGI(TAG, "replayed %d B, id %s", got, s_in_id);
    }

done:
    esp_http_client_cleanup(c);
    s_fetching = false;
    if (!ok) s_state = JPANEL_FAILED;
}

/* SEED THE SENDER FROM THE QUEUE THE BOX ALREADY DESCRIBED, and do it wherever a fetch is
 * ARMED — on the render task at the press, and again on this task when the request goes out.
 *
 * THIS IS THE WHOLE OF "the little girl icon while playing a message from Dad". `s_in_from_dad`
 * used to be CLEARED at the top of every fetch, and false means the sister. The renderer treats a
 * live run as authoritative about its own sender (which it must — the queue's head is by then the
 * NEXT message), so for the whole window between a fetch starting and its response headers
 * landing, the corner showed a little girl for a message from Dad. Two messages queued makes that
 * window visible every time: the panel finishes one and chains straight into the next, `s_run` is
 * still true, and the reset lands in plain sight.
 *
 * A clear was never the right shape. The box has already said who each queued message is from, and
 * `/next?at=` resolves the same index off the same ordered list — so the seed is not a guess about
 * a different message, it is the same message's own answer, arriving one poll early. The header
 * still overwrites it, so a box that disagrees still wins; a box too old to send the header leaves
 * the panel with the poll's answer instead of with "sister".
 *
 * Idempotent, which is why calling it from two tasks is safe: both read the same snapshot and
 * write the same bytes, and the header handler is the only other writer. */
static void seed_sender(int at)
{
    const int i = at >= 0 && at < JPANEL_QUEUE_MAX && at < s_wait_known ? at : 0;
    if (s_wait_known <= 0) {
        /* Nothing described. Leave whatever the last message left: a stale name is a better guess
           than the sister, and the header is moments away. */
        return;
    }
    strlcpy(s_in_from, s_wait[i].from, sizeof(s_in_from));
    s_in_from_dad = s_wait[i].from_dad;
}

static void do_fetch(bool asked, int at)
{
    char path[32];
    /* THE INDEX RIDES THE PATH RATHER THAN A HEADER, because `open_client` signs and builds one
       string and a query is part of it. 0 is spelled out rather than omitted: a request that says
       what it means is one fewer thing to reason about when reading a box access log. */
    snprintf(path, sizeof(path), "/next?at=%d", at < 0 ? 0 : at);
    char url[288];
    esp_http_client_handle_t c = open_client(path, HTTP_METHOD_GET, url, sizeof(url));
    if (c == NULL) {
        /* CLEARED ON THE EARLY RETURN TOO, and the comment at `done:` promising "whatever the
           outcome" was written one release before this path existed to contradict it. A leaked
           `s_fetching` pins `starting` true forever, which draws a pause button and a sender's
           face over a panel with nothing playing — the exact fault the flag was added to fix.
           `open_client` returns NULL on no Wi-Fi and no key, so this is the ordinary case. */
        s_fetching = false;
        s_state = JPANEL_FAILED;
        return;
    }

    jpanel_state_t out = JPANEL_FAILED;
    int got = 0;
    /* THE ID AND THE DIGEST ARE CLEARED; THE SENDER IS SEEDED. Both are about a box too old to
       send the header — but a stale ID would acknowledge the WRONG MESSAGE and a stale digest
       would judge this one against the last, where a stale sender costs a picture. So the two that
       can do damage are cleared and the one that cannot is given the best answer available. */
    s_in_id[0] = '\0';
    s_in_sha[0] = '\0';
    seed_sender(at);
    if (esp_http_client_open(c, 0) != ESP_OK) goto done;
    if (esp_http_client_fetch_headers(c) < 0) goto done;
    const int status = esp_http_client_get_status_code(c);
    if (status == 204) {
        /* Someone else played it, the poll was stale, or the index is past the end of a queue that
           shrank under the finger. Not a failure — just nothing here. */
        s_wait_count = 0;
        s_wait_known = 0;
        s_wait[0].from[0] = '\0';
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
    if (!audio_stream_begin()) {
        ESP_LOGW(TAG, "speaker busy — not starting this message");
        s_msg_err = "speaker-busy";
        s_msg_bad++;
        goto done;
    }
    s_stopped = false;
    /* CLAIMED BEFORE THE FIRST BYTE, and that ordering is the whole safety of this path.
       `audio_playing()` is true from here until the ring drains, so the renderer, the pop-up
       and `POST /played` all see one message in flight — including during the seconds before
       the preroll has landed, when nothing is audible yet. */
    s_owed = true;
    s_run = true;
    s_state = JPANEL_PLAYING;
    s_msg_began_ms = now_ms();
    /* THE LOCAL VIEW LOSES THE ENTRY THAT WAS JUST TAKEN, not merely a number off the total.
       The count has been decremented here since the queue existed — a poll is up to thirty
       seconds away and a notice for a message already playing would be wrong for all of it — and
       that was enough while the panel only ever played the head. It is not enough now that a
       finger can point at index 2: dropping the count without dropping the ENTRY leaves the list
       and the count describing different queues, and the next selection resolves against a stale
       name.

       COUNT DOWN FIRST, THEN SHIFT, which is the removal half of the ordering rule at the
       declaration: a reader that sees a count must see that many valid entries, so the moment
       where the count is low and the entries are the old ones is safe and the reverse is not. */
    if (s_wait_count > 0) s_wait_count--;
    const int taken = at >= 0 && at < s_wait_known ? at : 0;
    if (s_wait_known > 0) {
        const int n = s_wait_known - 1;
        s_wait_known = n;
        for (int i = taken; i < n; i++) s_wait[i] = s_wait[i + 1];
    }
    if (s_wait_count == 0) {
        s_wait_known = 0;
        s_wait[0].from[0] = '\0';
    }
    char heard[72];
    got = pump(c, heard, sizeof(heard));
    audio_stream_end();
    if (got < 2) {
        ESP_LOGW(TAG, "empty message");
        s_msg_err = "empty";
        s_msg_bad++;
        audio_stream_abort();
        s_owed = false;
        s_run = false;
        goto done;
    }
    if (s_stopped) {
        /* Her choice, not a fault. `s_owed` stays set, so `POST /played` fires when the ring
           finishes draining and the message retires as it always did. */
        ESP_LOGI(TAG, "stopped by a finger after %d B, id %s", got,
                 s_in_id[0] ? s_in_id : "(none)");
        out = JPANEL_PLAYING;
        goto done;
    }
    if (!verified(s_in_sha, heard)) {
        /* NOT ACKNOWLEDGED, WHICH IS THE WHOLE POINT. What played was short — the child heard
           her father stop mid-sentence — and telling the box it was played would retire it:
           `/next` would never offer it again and nobody would know the rest existed. Leaving
           `s_owed` clear keeps the row unplayed, so the pop-up comes back and the next tap
           fetches it whole. `deliveries` counts this attempt, and five of them is the box
           giving up loudly rather than a message quietly lost. */
        ESP_LOGE(TAG, "message arrived incomplete (%d B) — not acknowledging, id %s", got,
                 s_in_id[0] ? s_in_id : "(none)");
        s_msg_err = "short";
        s_msg_bad++;
        s_owed = false;
        s_run = false;
        out = JPANEL_PLAYING;
        goto done;
    }
    out = JPANEL_PLAYING;
    s_msg_bytes = got;
    ESP_LOGI(TAG, "streamed %d B from %s, id %s", got, s_in_from[0] ? s_in_from : "?",
             s_in_id[0] ? s_in_id : "(none)");

done:
    esp_http_client_cleanup(c);
    /* CLEARED WHERE THE FETCH ENDS, whatever the outcome. The renderer holds the playback
       controls up on this, so a path that returned without clearing it would leave a pause
       button over a panel with nothing playing — the exact fault this flag was added to fix,
       reintroduced by an early return. */
    s_fetching = false;
    /* THE BACKGROUND FETCH REPORTS NOTHING, and that is not tidiness. It runs off the poll,
       which fires on this task's own clock — so writing `s_state` here would overwrite a
       `JPANEL_SENT` or `JPANEL_NOBODY` the renderer had not shown yet, and the child would
       lose the sound that told them their message went. Only a fetch a finger asked for has
       an outcome worth reporting.
     *
       And never PLAYING: nothing was played. `jpanel_play_next` owns that transition. */
    /* `out` is already PLAYING when a stream started — the sound IS the outcome, and it began
       inside the loop above rather than after it. */
    if (asked) s_state = out;
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
            case CMD_FETCH: do_fetch(cmd.asked, cmd.at); break;
            case CMD_REPLAY: do_replay(); break;
            case CMD_POLL: next_poll = 0; break;
            }
        }
        /* THE ACKNOWLEDGEMENT WAITS FOR THE SPEAKER, which is the whole reason `GET /next`
           does not mark it played: a panel that loses power mid-message must still have the
           message. `s_owed` is set where the audio starts — see its declaration for the race
           that taught us not to watch for a state instead. */
        if (s_owed && !audio_playing() && s_state != JPANEL_BUSY) {
            s_owed = false;
            /* HOW LONG IT ACTUALLY SOUNDED, measured here because this is the moment the ring is
               observed to be quiet — the same moment the message is retired, so the two numbers
               describe one event and cannot drift apart. Against `msg_bytes` it says whether a
               child heard anything: 16-bit mono at 16 kHz is 32 bytes a millisecond, so a report
               whose `msg_ms` is a small fraction of `msg_bytes / 32` is a message the box believes
               was delivered and nobody heard. */
            s_msg_ms = (int)(now_ms() - s_msg_began_ms);
            s_msg_ok++;
            do_played();
            next_poll = 0;
            /* STRAIGHT ON TO THE NEXT, if the child has not stopped the run. The one just
               finished is acknowledged first — the order matters, because fetching before
               acknowledging would hand back the same message again. */
            if (s_run && s_wait_count > 0) {
                /* THE HEAD, ALWAYS. A run walks the queue oldest-first; the index exists for a
                   finger pointing at one, and a chain has no finger. */
                do_fetch(true, 0);
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
            /* NO PREFETCH ANY MORE, and that is the point of streaming rather than a loss.
               The panel used to collect a whole message in the background so a tap would not
               wait two seconds for it; a stream needs only the preroll before the first sound,
               so the tap is answered sooner with nothing held in PSRAM at all. */
        }
    }
}

bool jpanel_start(const cfg_t *cfg)
{
    if (cfg == NULL) return false;
    s_cfg = cfg;
    s_q = xQueueCreate(2, sizeof(cmd_t));
    if (s_q == NULL) {
        ESP_LOGE(TAG, "no memory for voice post");
        return false;
    }
    /* Off the render core, like `talk.c`: this task blocks on a socket for seconds. */
    if (xTaskCreatePinnedToCore(jpanel_task, "jpanel", 6144, NULL, 4, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "task failed");
        return false;
    }
    /* NOT STARTED, AND THIS IS A RETREAT FROM A CRASH LOOP RATHER THAN A DESIGN.
     *
     * MEASURED 2026-09-27: 0.3.22 put Lydian into `reset=panic(4)` every ~13 seconds, in a
     * child's bedroom, within minutes of the OTA. The task is the only thing 0.3.22 added that
     * runs code, and the fault is almost certainly the number in the line below this comment:
     * I gave it 4096 bytes of stack, while EVERY other task in this firmware that opens a TLS
     * connection — `jpanel` and `talk` — is given 6144, and an mbedTLS handshake alone wants
     * about 3.6 KB before `esp_http_client` has taken any. The comment that shipped with it
     * claimed 4096 was "ample" on the grounds that the task reads into a 128-byte buffer,
     * which measures the wrong thing entirely: the buffer is not what sits on the stack, the
     * handshake is.
     *
     * "ALMOST CERTAINLY" IS WHY THIS IS A GUARD AND NOT A BIGGER NUMBER. Twice today a
     * confident diagnosis was shipped straight to these panels and was wrong — the QSPI
     * framing that hung both renderers, and the memory argument this whole feature was built
     * around. A panel that crashes every thirteen seconds is not a thing to iterate on
     * remotely. The stack goes to 8192 and the task comes back when it can be watched on a
     * bench with a cable, and the fix is one word on the line below. Everything else about the
     * push channel is kept and untouched, including the box's half, which is harmless with
     * nobody connected. */
#define PUSH_TASK_ENABLED 0
    if (PUSH_TASK_ENABLED &&
        xTaskCreatePinnedToCore(push_task, "push", 8192, NULL, 4, NULL, 0) != pdPASS) {
        ESP_LOGW(TAG, "no push task — messages will arrive on the poll instead");
    }
    if (!PUSH_TASK_ENABLED) ESP_LOGW(TAG, "push: disabled (0.3.22 crash loop) — polling only");
    ESP_LOGI(TAG, "ready");
    return true;
}

static bool post(cmd_kind_t kind, jpanel_to_t to, bool asked, int at)
{
    if (s_q == NULL) return false;
    const cmd_t cmd = {.kind = kind, .to = to, .asked = asked, .at = at};
    return xQueueSend(s_q, &cmd, 0) == pdTRUE;
}

bool jpanel_send(const int16_t *pcm, size_t bytes, jpanel_to_t to)
{
    if (s_q == NULL || pcm == NULL || bytes < 2) return false;
    if (s_state == JPANEL_BUSY) return false;
    s_pcm = pcm;
    s_bytes = bytes;
    s_state = JPANEL_BUSY;
    if (post(CMD_SEND, to, false, 0)) return true;
    s_state = JPANEL_IDLE;
    return false;
}

bool jpanel_fetching(void)
{
    return s_fetching;
}

bool jpanel_play_at(int at)
{
    if (s_q == NULL) return false;
    /* ONE PATH NOW. The fetch and the playing are the same act: the jpanel task opens the
       message, claims the speaker before the first byte and feeds the ring as it arrives, so
       there is nothing here to do but ask for it. The count and the state move on that task,
       where the stream actually begins, rather than optimistically here. */
    if (s_state == JPANEL_BUSY) return false;
    s_state = JPANEL_BUSY;
    s_fetching = true;
    /* SEEDED HERE TOO, ON THE RENDER TASK, AND THAT IS NOT BELT-AND-BRACES. `s_fetching` goes
       true on this line, so the renderer draws the playback controls — including the sender's face
       — from the very next frame, which is several frames before the jpanel task has picked the
       command up. Seeding only inside `do_fetch` would leave that gap showing the PREVIOUS
       message's sender, which is the same bug one layer in. */
    seed_sender(at);
    if (post(CMD_FETCH, JPANEL_TO_PANEL, true, at)) return true;
    s_fetching = false;
    s_state = JPANEL_IDLE;
    return false;
}

bool jpanel_play_next(void)
{
    return jpanel_play_at(0);
}

bool jpanel_replay(void)
{
    if (s_in_id[0] == '\0') return false;
    if (audio_playing()) return false;
    /* A replay is a message on its way to the speaker too, so the controls belong up for it
       from the press rather than from the first byte. `do_replay` clears it at its own exit. */
    s_fetching = true;
    if (post(CMD_REPLAY, JPANEL_TO_PANEL, false, 0)) return true;
    s_fetching = false;
    return false;
}

/* Stop a run. The message sounding is cut and nothing more is fetched; whatever has not been
   played is still unplayed on the box, so the pop-up returns for it. */
void jpanel_stop(void)
{
    /* RECORDED, BECAUSE A STOP AND A CUT LOOK IDENTICAL FROM THE HASH.
     *
       Both end the stream early, so both fail verification — but they mean opposite things. A
       truncated download must NOT be acknowledged, or the box retires a message nobody heard
       the end of. A child putting her finger on the screen must BE acknowledged, exactly as it
       was before streaming: she heard it and chose to stop. Without this the stop path burns a
       delivery attempt every time, and after five `GET /next` filters the message out while
       `GET /waiting` still counts it — a pop-up that returns every thirty seconds and that no
       tap can ever satisfy. */
    s_stopped = true;
    s_run = false;
    audio_stop();
}

bool jpanel_running(void)
{
    return s_run;
}

void jpanel_poll_soon(void)
{
    (void)post(CMD_POLL, JPANEL_TO_PANEL, false, 0);
}

/* Who the message now in the buffer came from — the caption beside the repeat icon. Empty
   when nothing has been played, which is what the renderer tests. */
const char *jpanel_last_from(void)
{
    return s_in_from;
}

jpanel_to_t jpanel_in_from(void)
{
    return s_in_from_dad ? JPANEL_TO_DAD : JPANEL_TO_PANEL;
}

int jpanel_waiting(char *from, size_t cap)
{
    const int n = s_wait_count;
    if (from != NULL && cap > 0) strlcpy(from, s_wait[0].from, cap);
    return n;
}

bool jpanel_waiting_from_dad(void)
{
    return s_wait[0].from_dad;
}

bool jpanel_waiting_at(int at, char *from, size_t cap, bool *from_dad)
{
    /* THE COUNT IS READ FIRST AND IS THE GUARD. `s_wait_known` can only ever be lowered before
       the entries move (see `do_fetch`), so an index inside it is an index whose entry is still
       whole — which is the whole of what this lock-free pair promises. */
    const int known = s_wait_known;
    if (at < 0 || at >= known || at >= JPANEL_QUEUE_MAX) return false;
    if (from != NULL && cap > 0) strlcpy(from, s_wait[at].from, cap);
    if (from_dad != NULL) *from_dad = s_wait[at].from_dad;
    return true;
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

void jpanel_message_stats(int *bytes, int *ms, unsigned *ok, unsigned *bad, const char **err)
{
    if (bytes != NULL) *bytes = s_msg_bytes;
    if (ms != NULL) *ms = s_msg_ms;
    if (ok != NULL) *ok = s_msg_ok;
    if (bad != NULL) *bad = s_msg_bad;
    if (err != NULL) *err = s_msg_err;
}
