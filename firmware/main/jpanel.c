/* Voice post. See jpanel.h for why this is a task of its own. */

#include "jpanel.h"

#include <string.h>
#include <strings.h>

#include "audio.h"
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
/* A finger ended the last stream, rather than the network. See `jpanel_stop`. */
static volatile bool s_stopped;
/* The id the box gave it, held so `POST /played` can name it after the speaker finishes, and
   who it came from, which is what the repeat icon's caption says. Both are filled by the
   header handler below. */
static char s_in_id[48];
static char s_in_from[32];
/* WHAT THE BOX SAYS IT SENT, hex, or "" from a box too old to say. The panel hashes the
   message as it streams it into the speaker and compares at the end — not to re-play it, which
   it cannot, but to decide whether to ACKNOWLEDGE it. A download that ends early plays a
   message that stops mid-sentence, and acknowledging that would retire it: the box would never
   offer it again and nobody would know the rest existed. Unverified, it is simply not
   acknowledged, so it stays unplayed and the pop-up comes back. */
static char s_in_sha[72];

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
    if (s_in_id[0] == '\0') return;
    bool ok = false;
    char path[96];
    snprintf(path, sizeof(path), "/message/%s/pcm", s_in_id);
    char url[288];
    esp_http_client_handle_t c = open_client(path, HTTP_METHOD_GET, url, sizeof(url));
    if (c == NULL) {
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
    if (!ok) s_state = JPANEL_FAILED;
}

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
    s_in_sha[0] = '\0';
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
    if (!audio_stream_begin()) {
        ESP_LOGW(TAG, "speaker busy — not starting this message");
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
    if (s_wait_count > 0) s_wait_count--;
    if (s_wait_count == 0) s_wait_from[0] = '\0';
    char heard[72];
    got = pump(c, heard, sizeof(heard));
    audio_stream_end();
    if (got < 2) {
        ESP_LOGW(TAG, "empty message");
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
        s_owed = false;
        s_run = false;
        out = JPANEL_PLAYING;
        goto done;
    }
    out = JPANEL_PLAYING;
    ESP_LOGI(TAG, "streamed %d B from %s, id %s", got, s_in_from[0] ? s_in_from : "?",
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
            case CMD_FETCH: do_fetch(cmd.asked); break;
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
    /* ONE PATH NOW. The fetch and the playing are the same act: the jpanel task opens the
       message, claims the speaker before the first byte and feeds the ring as it arrives, so
       there is nothing here to do but ask for it. The count and the state move on that task,
       where the stream actually begins, rather than optimistically here. */
    if (s_state == JPANEL_BUSY) return false;
    s_state = JPANEL_BUSY;
    if (post(CMD_FETCH, JPANEL_TO_PANEL, true)) return true;
    s_state = JPANEL_IDLE;
    return false;
}

bool jpanel_replay(void)
{
    if (s_in_id[0] == '\0') return false;
    if (audio_playing()) return false;
    return post(CMD_REPLAY, JPANEL_TO_PANEL, false);
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
