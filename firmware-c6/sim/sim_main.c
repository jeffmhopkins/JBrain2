/* The panel on the host, headless. Each named state is reached the way the box would reach it
 * on a live panel — fake services set the world, and DEBUG COMMANDS drive the UI (open,
 * inject) — and the picture is taken by the debug screenshot command through the same stripe
 * codec, then decoded to PNG. So every render is also a test of the debug surface.
 *
 *   jpanel-sim --state NAME --png out.png [--snapshot out.json]
 *   jpanel-sim --list
 *   jpanel-sim --lifecycle      every app opens and closes back to the same widget/timer count
 *   jpanel-sim --touch          a scripted finger through the shell's real pointer path
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "apps.h"
#include "dbg_core.h"
#include "lvgl.h"
#include "shell.h"
#include "sim_png.h"
#include "alarm_port_fake.h"
#include "svc_alarms_drv.h"
#include "svc_audio_fake.h"
#include "svc_box_fake.h"
#include "svc_convo_drv.h"
#include "svc_display.h"
#include "svc_display_fake.h"
#include "svc_messages_fake.h"
#include "svc_net_fake.h"
#include "svc_notice_fake.h"
#include "svc_post_drv.h"
#include "svc_power_fake.h"
#include "svc_rtc.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"
#include "svc_siblings_drv.h"
#include "ui_kit.h"

#define W UI_W
#define H UI_H
/* The device's strip height: 40 rows of RGB565 is 38 KB a buffer, two of them for DMA. */
#define STRIP_ROWS 40

static uint16_t s_strip[2][W * STRIP_ROWS];
static uint32_t s_ms;

static struct {
    bool on;
    dbg_shot_t shot;
    uint8_t *buf;
    size_t n, cap;
} s_cap;

static uint32_t sim_ms(void)
{
    return s_ms;
}

static void flush(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    /* A capture must see whole rows top to bottom. A strip that is not full width means LVGL
     * merged areas differently than assumed, and the codec fails the capture rather than
     * shipping a torn picture. */
    if (s_cap.on) {
        if (a->x1 != 0 || a->x2 != W - 1)
            s_cap.shot.failed = true;
        else
            dbg_shot_stripe(&s_cap.shot, (uint16_t)a->y1, (uint16_t)(a->y2 - a->y1 + 1), (const uint16_t *)px);
    }
    lv_display_flush_ready(d);
}

static void cap_sink(void *ctx, const uint8_t *b, size_t n)
{
    (void)ctx;
    if (s_cap.n + n > s_cap.cap) {
        s_cap.cap = (s_cap.n + n) * 2;
        s_cap.buf = realloc(s_cap.buf, s_cap.cap);
    }
    memcpy(s_cap.buf + s_cap.n, b, n);
    s_cap.n += n;
}

/* ---- the box's conversations (0.0.15), answered by the sim the way `api/panel_convo.py` does,
 * from the mock's sessions (Main.dc.html `sessionSeed`) ---- */

typedef struct {
    const char *id, *title, *origin, *snippet;
    int days, h, m; /* last active: `days` back at local h:m, or (days < 0) -days minutes ago */
} sim_session_t;

static const sim_session_t JERV_SESSIONS[] = {
    {"6a1c0001-0000-4000-8000-000000000001", "Shopping list", "panel", "Added milk to your shopping list.", -2, 0, 0},
    {"6a1c0002-0000-4000-8000-000000000002", "Tides at Port St John", "pwa",
     "Yes. Go after 14:00, back up the beach by 16:30.", -12, 0, 0},
    {"6a1c0003-0000-4000-8000-000000000003", "T-Rex fossils", "panel",
     "Sue, in Chicago. About 90% of the skeleton was found.", 1, 19, 40},
    {"6a1c0004-0000-4000-8000-000000000004", "Desk panel", "panel",
     "Done. The porch light goes off at 11:00 tonight.", 1, 7, 58},
    {"6a1c0005-0000-4000-8000-000000000005", "Car insurance renewal", "pwa",
     "On the 28th. I added a reminder for the 21st.", 2, 16, 10},
    {"6a1c0006-0000-4000-8000-000000000006", "Desk panel", "panel",
     "Done. I'll remind you at 09:00 tomorrow.", 3, 8, 30},
    {"6a1c0007-0000-4000-8000-000000000007", "Pancake recipe", "pwa",
     "Two cups of flour, two eggs, two cups of milk, a pinch of salt.", 4, 18, 2},
    {"6a1c0008-0000-4000-8000-000000000008", "Server backup", "pwa",
     "Last night's backup finished at 02:14. Nothing failed.", 5, 22, 15},
    {"6a1c0009-0000-4000-8000-000000000009", "Desk panel", "panel",
     "Added: bins out every Tuesday evening.", 7, 9, 0},
    {"6a1c0010-0000-4000-8000-000000000010", "Birthday ideas", "pwa",
     "You: Birthday ideas for an eight-year-old?", 9, 12, 0},
};
static const sim_session_t DAVE_SESSIONS[] = {
    {"6a1d0001-0000-4000-8000-000000000001", "Desk panel", "panel",
     "North of spawn, past the river. I can point you there.", -5, 0, 0},
    {"6a1d0002-0000-4000-8000-000000000002", "Wheat farm", "pwa",
     "Water within four blocks of every crop, and light so it grows at night.", 1, 18, 20},
    {"6a1d0003-0000-4000-8000-000000000003", "Desk panel", "panel", "It is. Two players are on right now.", 2, 10, 5},
    {"6a1d0004-0000-4000-8000-000000000004", "Nether portal", "pwa",
     "Ten for the frame without corners, fourteen with them.", 4, 19, 30},
    {"6a1d0005-0000-4000-8000-000000000005", "Desk panel", "panel",
     "At the bed in your base. It is your spawn point now.", 10, 20, 0},
};

/* Which session each agent's panel thread points at, as the box keeps it. */
static int s_box_cur[2];
/* Recent's loading board: its list is held unanswered. The empty board: the box has none. */
static bool s_box_hold, s_box_none;

static int64_t box_at(const sim_session_t *s)
{
    int64_t now = 0;
    svc_rtc_epoch(&now);
    if (s->days < 0) return now + s->days * 60;
    rtc_time_t t;
    svc_rtc_now(&t);
    const int64_t midnight = now - (t.h * 3600 + t.m * 60 + t.s);
    return midnight - (int64_t)s->days * 86400 + s->h * 3600 + s->m * 60;
}

static void box_sessions(const char *agent, const sim_session_t **list, int *n, int **cur)
{
    const bool jerv = !strcmp(agent, "jerv");
    *list = jerv ? JERV_SESSIONS : DAVE_SESSIONS;
    *n = jerv ? (int)(sizeof JERV_SESSIONS / sizeof *JERV_SESSIONS)
              : (int)(sizeof DAVE_SESSIONS / sizeof *DAVE_SESSIONS);
    *cur = &s_box_cur[jerv ? 0 : 1];
}

static char *box_body(const char *text)
{
    char *b = malloc(strlen(text) + 1);
    strcpy(b, text);
    return b;
}

/* The tides chat's last two exchanges, as `GET …/messages?limit=3` sends them. */
static const char TIDES_THREAD[] =
    "{\"id\":\"6a1c0002-0000-4000-8000-000000000002\",\"title\":\"Tides at Port St John\","
    "\"messages\":[{\"mine\":true,\"text\":\"When's low tide today?\"},"
    "{\"mine\":false,\"text\":\"Low 14:52, high 21:07.\"},"
    "{\"mine\":true,\"text\":\"Good for rock pools later?\"},"
    "{\"mine\":false,\"text\":\"Yes. Go after 14:00, back up the beach by 16:30.\"}],\"more\":true}";

static void box_answer(const convo_req_t *r)
{
    static char body[4096];
    const sim_session_t *list;
    int n, *cur;
    box_sessions(r->agent, &list, &n, &cur);
    if (r->kind == CONVO_RQ_RECENT) {
        int o = snprintf(body, sizeof body, "{\"items\":[");
        for (int k = 0; k < n && !s_box_none; k++) {
            /* The current one first, then newest first: the box's order. */
            const int i = k == 0 ? *cur : (k <= *cur ? k - 1 : k);
            o += snprintf(body + o, sizeof body - (size_t)o,
                          "%s{\"id\":\"%s\",\"title\":\"%s\",\"at\":%lld,\"origin\":\"%s\","
                          "\"snippet\":\"%s\",\"current\":%s}",
                          k ? "," : "", list[i].id, list[i].title, (long long)box_at(&list[i]),
                          list[i].origin, list[i].snippet, i == *cur ? "true" : "false");
        }
        snprintf(body + o, sizeof body - (size_t)o, "]}");
    } else if (r->kind == CONVO_RQ_SWITCH) {
        int found = -1;
        for (int i = 0; i < n; i++)
            if (!strcmp(list[i].id, r->id)) found = i;
        if (found < 0) {
            svc_convo_done(r, 404, NULL, 0);
            return;
        }
        *cur = found;
        if (!strcmp(r->id, JERV_SESSIONS[1].id))
            snprintf(body, sizeof body, "%s", TIDES_THREAD);
        else
            snprintf(body, sizeof body,
                     "{\"id\":\"%s\",\"title\":\"%s\",\"messages\":[{\"mine\":false,\"text\":\"%s\"}],"
                     "\"more\":true}",
                     list[found].id, list[found].title, list[found].snippet);
    } else {
        snprintf(body, sizeof body, "{\"id\":\"%s\",\"title\":\"%s\",\"messages\":[],\"more\":true}",
                 list[*cur].id, list[*cur].title);
    }
    svc_convo_done(r, 200, box_body(body), strlen(body));
}

/* Every request the glass made, answered at once — except a list held for the loading board. */
static void fake_box(void)
{
    static convo_req_t held;
    static bool holding;
    convo_req_t r;
    if (holding && !s_box_hold) {
        holding = false;
        box_answer(&held);
    }
    while (svc_convo_take(&r)) {
        if (r.kind == CONVO_RQ_RECENT && s_box_hold) {
            held = r;
            holding = true;
            continue;
        }
        box_answer(&r);
    }
}

/* ---- the owner's voice post with the twins' panels (0.0.16), answered by the sim the way
 * `api/panel_messages.py` does — placeholder names and words, as every fixture uses ---- */

#define SIM_Q "0b8e5f7a-1111-4c2d-9e3f-5a6b7c8d9e01"
#define SIM_T "0b8e5f7a-2222-4c2d-9e3f-5a6b7c8d9e02"
static const char POST_Q[] =
    "{\"device_id\":\"" SIM_Q "\",\"name\":\"Quorra\",\"tag\":\"5e1d0c2a\",\"messages\":["
    "{\"id\":\"6b000000-0000-4000-8000-000000000001\",\"mine\":true,"
    "\"text\":\"Have a good day at school!\",\"ms\":4200,\"played\":true,\"voice\":true},"
    "{\"id\":\"6b000000-0000-4000-8000-000000000002\",\"mine\":false,"
    "\"text\":\"Daddy, when are you coming home?\",\"ms\":5100,\"played\":false,\"voice\":true}%s]}";
/* What a second message from her adds: the tag on the pickup moves and the list grows. */
static const char POST_Q_NEW[] =
    ",{\"id\":\"6b000000-0000-4000-8000-000000000005\",\"mine\":false,"
    "\"text\":\"I drew you a dragon. It's purple.\",\"ms\":3400,\"played\":false,\"voice\":true}";
static const char POST_T[] =
    "{\"device_id\":\"" SIM_T "\",\"name\":\"Tamsin\",\"tag\":\"5e1d0c2a\",\"messages\":["
    "{\"id\":\"6b000000-0000-4000-8000-000000000003\",\"mine\":false,"
    "\"text\":\"I made you a drawing.\",\"ms\":3000,\"played\":true,\"voice\":true},"
    "{\"id\":\"6b000000-0000-4000-8000-000000000004\",\"mine\":true,"
    "\"text\":\"I can't wait to see it!\",\"ms\":2200,\"played\":true,\"voice\":true}]}";
static bool s_post_new;
/* Dad's voice message from the desk, once the box has filed it: in her thread from then on. */
#define POSTED_TEXT "Coming home at six. Love you!"
static const char POST_Q_SENT[] =
    ",{\"id\":\"6b000000-0000-4000-8000-000000000006\",\"mine\":true,"
    "\"text\":\"" POSTED_TEXT "\",\"ms\":2600,\"played\":false,\"voice\":true}";
static bool s_post_sent;
/* Her four longest, more than the glass holds: the thread scrolls (0.0.17's stability check). */
static const char POST_Q_LONG[] =
    "{\"device_id\":\"" SIM_Q "\",\"name\":\"Quorra\",\"tag\":\"5e1d0c2a\",\"messages\":["
    "{\"id\":\"6b000000-0000-4000-8000-000000000011\",\"mine\":false,\"text\":\"Daddy, we did the "
    "volcano at school and it fizzed all over the table and Ms Reyes laughed so hard.\",\"ms\":9200,"
    "\"played\":false,\"voice\":true},"
    "{\"id\":\"6b000000-0000-4000-8000-000000000012\",\"mine\":false,\"text\":\"And then we had "
    "pizza for lunch, the square kind, and I traded my apple for a cookie.\",\"ms\":8000,"
    "\"played\":false,\"voice\":true},"
    "{\"id\":\"6b000000-0000-4000-8000-000000000013\",\"mine\":true,\"text\":\"That sounds like "
    "the best day. Save me a cookie next time!\",\"ms\":4100,\"played\":true,\"voice\":true},"
    "{\"id\":\"6b000000-0000-4000-8000-000000000014\",\"mine\":false,\"text\":\"No way, cookies "
    "are for kids. When are you coming home? We want to show you the drawing.\",\"ms\":7400,"
    "\"played\":false,\"voice\":true}]}";
static bool s_post_long;

static void fake_post_box(void)
{
    post_req_t r;
    while (svc_post_take(&r)) {
        if (r.kind != POST_RQ_THREAD) {
            svc_post_done(&r, 204, NULL, 0);
            continue;
        }
        char body[2048];
        if (strstr(r.path, SIM_Q) && s_post_long)
            snprintf(body, sizeof body, "%s", POST_Q_LONG);
        else if (strstr(r.path, SIM_Q))
            snprintf(body, sizeof body, POST_Q, s_post_new ? POST_Q_NEW : s_post_sent ? POST_Q_SENT : "");
        else
            snprintf(body, sizeof body, "%s", POST_T);
        svc_post_done(&r, 200, box_body(body), strlen(body));
    }
}

static void step(uint32_t ms)
{
    for (uint32_t t = 0; t < ms; t += 10) {
        s_ms += 10;
        svc_rtc_fake_advance_ms(10);
        svc_audio_service();
        svc_siblings_service();
        fake_box();
        svc_convo_service();
        fake_post_box();
        svc_post_service();
        if (svc_post_take_chime(svc_audio_talking() || svc_audio_replying() || svc_audio_playing() ||
                                svc_audio_ringing()))
            svc_audio_chime();
        svc_alarms_service(svc_rtc_uptime_ms());
        shell_pump();
        lv_timer_handler();
    }
}

/* ---- the commands the device binds in its own main (C2), bound here for the sim ---- */

static bool cmd_screenshot(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r;
    s_cap.n = 0;
    dbg_shot_begin(&s_cap.shot, W, H, cap_sink, NULL);
    s_cap.on = true;
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(NULL);
    s_cap.on = false;
    if (!dbg_shot_end(&s_cap.shot)) {
        snprintf(err, n, "capture torn");
        return false;
    }
    dbg_json_int(d, "bytes", s_cap.shot.bytes);
    dbg_json_int(d, "stripes", s_cap.shot.stripes);
    dbg_json_int(d, "strip_rows", STRIP_ROWS);
    return true;
}

static bool cmd_set(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    for (int k = 0; k < SET__COUNT; k++)
        if (!strcmp(r->name, svc_settings_name((setting_t)k))) {
            svc_settings_set((setting_t)k, r->value);
            dbg_json_int(d, r->name, svc_settings_get((setting_t)k));
            return true;
        }
    snprintf(err, n, "no setting %s", r->name);
    return false;
}

static bool cmd_ota(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r;
    if (!svc_box_update_now()) {
        snprintf(err, n, "offline or busy");
        return false;
    }
    dbg_json_str(d, "ota", "checking");
    return true;
}

static bool cmd_report(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r, (void)err, (void)n;
    svc_box_report_now();
    dbg_json_int(d, "reports", svc_box_fake_reports());
    return true;
}

static bool cmd_log_level(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    if (r->level < 0 || r->level > 5 || !r->tag[0]) {
        snprintf(err, n, "tag and level 0-5");
        return false;
    }
    dbg_json_str(d, "tag", r->tag);
    dbg_json_int(d, "level", r->level);
    return true;
}

static bool cmd_reboot(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r, (void)err, (void)n;
    dbg_json_str(d, "reboot", "the sim does not reboot");
    return true;
}

static char s_result[16384];

static const char *run(const char *json)
{
    dbg_json_t w;
    dbg_json_init(&w, s_result, sizeof s_result);
    dbg_cmd_run(json, &w);
    step(50);
    return dbg_json_text(&w);
}

/* ---- the world ---- */

static void boot(const char *only)
{
    lv_init();
    lv_tick_set_cb(sim_ms);
    lv_display_t *d = lv_display_create(W, H);
    lv_display_set_color_format(d, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(d, s_strip[0], s_strip[1], sizeof s_strip[0], LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(d, flush);
    dbg_log_set_clock(sim_ms);
    svc_settings_init();
    svc_rtc_init();
    svc_display_init();
    svc_power_init();
    svc_net_init();
    svc_box_init();
    svc_messages_init();
    svc_siblings_init();
    svc_convo_init();
    svc_post_init();
    svc_audio_init();
    alarm_port_fake_wipe();
    svc_alarms_init();
    svc_notice_init();
    dbg_sys_init();
    dbg_cmd_bind("screenshot", cmd_screenshot);
    dbg_cmd_bind("set", cmd_set);
    dbg_cmd_bind("ota_check", cmd_ota);
    dbg_cmd_bind("report_now", cmd_report);
    dbg_cmd_bind("log_level", cmd_log_level);
    dbg_cmd_bind("reboot", cmd_reboot);
    apps_register(only);
    char err[96];
    if (!shell_start(err, sizeof err)) {
        fprintf(stderr, "shell: %s\n", err);
        exit(2);
    }
    step(100);
}

/* The box's own answer to `GET /endpoint/siblings`, through the panel's own path (svc_siblings_
 * offer, then the UI task's service) — placeholder names, as every fixture uses. */
static const char SIBLINGS[] =
    "{\"siblings\":[{\"device_id\":\"0b8e5f7a-1111-4c2d-9e3f-5a6b7c8d9e01\",\"name\":\"Quorra\"},"
    "{\"device_id\":\"0b8e5f7a-2222-4c2d-9e3f-5a6b7c8d9e02\",\"name\":\"Tamsin\"}]}";
static bool s_no_names;

/* The box's own answer to `GET /endpoint/alarms`: the PWA mock's three alarms, through the panel's
 * own path (svc_alarms_offer, then the UI task's service), as the device takes them. */
static const char ALARMS[] =
    "{\"etag\":\"5d1c0e2a9b7f3c11\",\"alarms\":["
    "{\"id\":1,\"at\":405,\"days\":31,\"on\":true,\"label\":\"Work\",\"sound\":\"chime\"},"
    "{\"id\":2,\"at\":510,\"days\":96,\"on\":false,\"label\":\"\",\"sound\":\"rise\"},"
    "{\"id\":3,\"at\":1260,\"days\":127,\"on\":true,\"label\":\"Girls to bed\",\"sound\":\"beeps\"}],"
    "\"max\":8}";

/* The mock's sample conversations. The image ships none: they are the sim's, added the way the
 * services add any message — the agents' directly, the people's from the box's own JSON through
 * svc_post (0.0.16), answered by fake_post_box once the siblings are bound. */
static void fixtures(void)
{
    svc_alarms_offer(ALARMS, sizeof ALARMS - 1);
    if (!s_no_names) svc_siblings_offer(SIBLINGS, sizeof SIBLINGS - 1);
    step(20);
    static const struct {
        const char *thread;
        msg_t m;
    } SAMPLES[] = {
        {"jerv", {1, true, true, true, false, 3, "Add milk to the shopping list.", "", 0}},
        {"jerv", {2, false, false, true, false, 0, "Added milk to your shopping list.", "", 0}},
        {"dave", {11, true, true, true, false, 2, "Where did I leave my base?", "", 0}},
        {"dave", {12, false, false, true, false, 0, "North of spawn, past the river. I can point you there.", "", 0}},
    };
    for (size_t i = 0; i < sizeof SAMPLES / sizeof *SAMPLES; i++)
        svc_messages_fake_add(SAMPLES[i].thread, &SAMPLES[i].m);
    step(20);
}

static void no_names(void)
{
    s_no_names = true;
}

/* Recent's loading board: the box has not answered the list yet. */
static void hold_list(void)
{
    s_box_hold = true;
}

/* Recent's empty board: nothing on the box for Jerv yet. */
static void no_convos(void)
{
    s_box_none = true;
}

/* The tapped point of whatever on the top layer or screen says `text`: the sim taps a menu's
 * button by its words, as a person would, rather than by a coordinate that drifts with layout. */
static lv_obj_t *labelled(lv_obj_t *o, const char *text)
{
    if (lv_obj_check_type(o, &lv_label_class) && !strcmp(lv_label_get_text(o), text)) return o;
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *hit = labelled(lv_obj_get_child(o, (int32_t)i), text);
        if (hit) return hit;
    }
    return NULL;
}

static bool tap_label(const char *text)
{
    lv_obj_t *l = labelled(lv_layer_top(), text);
    if (!l) l = labelled(lv_screen_active(), text);
    if (!l) return false;
    lv_obj_update_layout(lv_screen_active());
    lv_area_t a;
    lv_obj_get_coords(lv_obj_get_parent(l), &a);
    char cmd[96];
    snprintf(cmd, sizeof cmd, "{\"cmd\":\"inject\",\"event\":\"touch\",\"x\":%d,\"y\":%d}",
             (int)(a.x1 + a.x2) / 2, (int)(a.y1 + a.y2) / 2);
    dbg_json_t w;
    static char out[256];
    dbg_json_init(&w, out, sizeof out);
    dbg_cmd_run(cmd, &w);
    step(500);
    return true;
}

static notice_t notice(uint32_t id, const char *src, const char *title, const char *body, int nch,
                       const char *c0, const char *c1, const char *c2)
{
    notice_t n = {.id = id, .n_choices = (uint8_t)nch};
    snprintf(n.source, sizeof n.source, "%s", src);
    snprintf(n.title, sizeof n.title, "%s", title);
    snprintf(n.body, sizeof n.body, "%s", body);
    const char *c[] = {c0, c1, c2};
    for (int i = 0; i < nch && i < 3; i++) snprintf(n.choices[i], sizeof n.choices[i], "%s", c[i]);
    return n;
}

static void push_info(void)
{
    notice_t n = notice(1, "Ops", "Disk 91% full", "The box is running low on space. Ops → Disk shows what is using it.", 0,
                        NULL, NULL, NULL);
    svc_notice_fake_push(&n);
}

static void push_question(void)
{
    notice_t n = notice(2, "Jerv", "Run the cleanup now?", "Frees about 38 GB of build cache. Nothing you keep is touched.", 2,
                        "Yes", "No", NULL);
    svc_notice_fake_push(&n);
    push_info();
}

static void night(void)
{
    svc_rtc_fake_set(23, 12);
}

static void night_badge(void)
{
    night();
    push_question();
}

/* 06:45 on a Monday: the box's "Work" alarm comes due on the panel's own schedule. */
static void ring(void)
{
    svc_rtc_fake_set(6, 45);
}

/* A minute before it, so the state can put the glass out first and let the alarm wake it. */
static void before_ring(void)
{
    svc_rtc_fake_set(6, 44);
}

static void ring_with_notice(void)
{
    ring();
    push_question();
}

static void downloading(void)
{
    fw_info_t fw = {"0.0.8", "0.0.9", FW_DOWNLOADING, 64, ""};
    svc_box_fake_set(&fw);
}

static void no_battery(void)
{
    power_info_t p = {.fitted = false, .usb = true, .usb_mv = 5020};
    svc_power_fake_set(&p);
}

static void text_only(void)
{
    svc_settings_set(SET_TTS, 0);
}

/* What the box answers to the sim's talks: a question the owner might ask his desk. */
#define HEARD "What's the weather like tomorrow?"
#define REPLIED "Sunny and 18 degrees in the morning, with rain after six in the evening."
#define TIDE_HEARD "Find the high tide near Port St John, Florida"
#define TIDE_STATUS "Searching the web: high tide Port St John FL…"
/* The steps a tool-using answer takes (JT2 STEP records, 0.0.14), as the box labels them. */
#define TIDE_STEP_0 "web_search · high tide Port St John FL"
#define TIDE_STEP_1 "web_fetch · tide-forecast.com"
#define TIDE_STEP_2 "web_fetch · tidesandcurrents.noaa.gov"
#define TIDE_STEP_3 "weather · Port St John"
#define TIDE_STEP_4 "web_search · Port St John sunset"
#define TIDE_STEP_5 "web_fetch · timeanddate.com"
#define TIDE_LONG                                                                                  \
    "High tide at Port St John is at 4:12 pm today, at 3.1 feet, and the next one is at 4:38 am "  \
    "tomorrow. Low tide falls between them, at 10:27 pm, at 0.4 feet. The Indian River there is "  \
    "a lagoon, so the swing is small and the times run about two hours behind the ocean beaches " \
    "at Cocoa. If you are launching a boat, the hour either side of high water is the easiest. "  \
    "The station I read is Port Canaveral's, adjusted for the river; a strong east wind can "      \
    "hold water in the lagoon and push the high a little later and a little higher than this."

/* A new message from her, as the box signals it: the pickup's tag moves and the list grows. */
static void post_arrives(void)
{
    s_post_new = true;
    svc_post_tag("7a3f9b10");
}

static void session(void)
{
    post_arrives();
}

#define CMDS 8
typedef struct {
    const char *name;
    void (*setup)(void);
    const char *cmds[CMDS];
} state_t;

#define OPEN(a) "{\"cmd\":\"open\",\"app\":\"" a "\"}"
#define TAP(x, y) "{\"cmd\":\"inject\",\"event\":\"touch\",\"x\":" #x ",\"y\":" #y "}"
/* Not a debug command: the sim scrolls the open page to its end, as a finger would. */
#define SCROLL_END "scroll end"
#define SCROLL(px) "scroll " #px
#define KEY_DOWN "{\"cmd\":\"inject\",\"event\":\"key_down\"}"
#define KEY_UP "{\"cmd\":\"inject\",\"event\":\"key_up\"}"
#define PWR "{\"cmd\":\"inject\",\"event\":\"pwr\"}"
#define WAIT(ms) "wait " #ms
/* A finger held still on the conversation: the long press that opens its menu. */
#define HOLD(x, y) "{\"cmd\":\"inject\",\"event\":\"hold\",\"x\":" #x ",\"y\":" #y ",\"value\":800}"
/* ... for `ms`: the Clock face's two-second hold (owner, 2026-10-12). */
#define HOLD_MS(x, y, ms) "{\"cmd\":\"inject\",\"event\":\"hold\",\"x\":" #x ",\"y\":" #y ",\"value\":" #ms "}"
/* Not a debug command: the sim taps whatever says this, as a finger would. */
#define TAP_LABEL(t) "tap " t
/* Not a debug command: the fake box answers the hold in flight. */
#define REPLY "reply"
/* Not a debug command: the fake box streams the transcript and a status line (JT2), no answer
 * yet. */
#define PROGRESS "progress"
/* Not a debug command: the fake box answers with a long answer, more than a bubble's 255 bytes. */
#define REPLY_LONG "reply long"
/* Not debug commands (0.0.16): her message arrives (the pickup's tag moves); the box answers the
 * play with its audio, still streaming; the box files the voice message to her. */
#define POST_NEW "post new"
#define LISTEN "listen"
#define POSTED "posted"
/* Not a debug command (0.0.17): half of the message playing has reached the speaker. */
#define PLAYED_HALF "played half"
/* Not debug commands (0.0.14): the fake box streams the transcript, "Thinking…" and then six
 * steps, the last still running and one failed; and, after them, the answer. */
#define STEPS "steps"
#define REPLY_TIDE "reply tide"

/* Mock boards 1-17 (docs/mocks/jpanel-c6/canvas.json), by the live panel's `start` names. */
static const state_t STATES[] = {
    {"home", NULL, {0}},
    {"home-p2", NULL, {"{\"cmd\":\"inject\",\"event\":\"swipe\",\"x\":400,\"y\":240,\"value\":-200}", WAIT(400)}},
    {"jerv", NULL, {OPEN("jerv")}},
    {"rec-jerv", NULL, {OPEN("jerv"), KEY_DOWN, WAIT(3400)}},
    {"quorra", NULL, {OPEN(SVC_MESSAGES_THREAD_1)}},
    {"rec-quorra", NULL, {OPEN(SVC_MESSAGES_THREAD_1), KEY_DOWN, WAIT(4100)}},
    {"tamsin", NULL, {OPEN(SVC_MESSAGES_THREAD_2)}},
    /* 0.0.16: her message playing (the pause, the dot gone), a voice message to her on its way
       and filed, a new one's badge on her tile and on the clock face, and live in her thread. */
    {"quorra-playing", NULL, {OPEN(SVC_MESSAGES_THREAD_1), TAP_LABEL(LV_SYMBOL_PLAY), LISTEN}},
    /* 0.0.17: the bar half full at half the message, and a message heard once its audio started —
       stopped at once, it has no dot and its badge is gone. */
    {"quorra-mid", NULL, {OPEN(SVC_MESSAGES_THREAD_1), TAP_LABEL(LV_SYMBOL_PLAY), LISTEN, PLAYED_HALF}},
    {"quorra-heard", NULL, {OPEN(SVC_MESSAGES_THREAD_1), TAP_LABEL(LV_SYMBOL_PLAY), LISTEN,
                            TAP_LABEL(LV_SYMBOL_PAUSE)}},
    {"quorra-sending", NULL, {OPEN(SVC_MESSAGES_THREAD_1), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300)}},
    {"quorra-sent", NULL, {OPEN(SVC_MESSAGES_THREAD_1), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), POSTED}},
    {"home-new", NULL, {POST_NEW, WAIT(300)}},
    {"clock-new", NULL, {OPEN("clock"), POST_NEW, WAIT(1300)}},
    {"quorra-new", NULL, {OPEN(SVC_MESSAGES_THREAD_1), POST_NEW, WAIT(300)}},
    /* Before the box has ever answered (no cache either): neutral names, never compiled-in ones. */
    {"home-unnamed", no_names, {0}},
    {"thread-unnamed", no_names, {OPEN(SVC_MESSAGES_THREAD_1)}},
    /* Minecraft's second level (owner, 2026-10-12): Dave, and Compass. */
    {"minecraft", NULL, {OPEN("minecraft")}},
    {"dave", NULL, {OPEN("minecraft"), TAP_LABEL("Dave")}},
    {"rec-dave", NULL, {OPEN("minecraft"), TAP_LABEL("Dave"), KEY_DOWN, WAIT(2600)}},
    {"dave-reply", NULL, {OPEN("minecraft"), TAP_LABEL("Dave"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), REPLY}},
    {"compass", NULL, {OPEN("minecraft"), TAP_LABEL("Compass")}},
    /* A long press in a person's thread (owner, 2026-10-12): the menu, then Clear armed. */
    {"quorra-menu", NULL, {OPEN(SVC_MESSAGES_THREAD_1), HOLD(240, 240), WAIT(1000)}},
    {"quorra-menu-armed", NULL, {OPEN(SVC_MESSAGES_THREAD_1), HOLD(240, 240), WAIT(1000), TAP_LABEL("Clear chat")}},
    {"quorra-cleared", NULL, {OPEN(SVC_MESSAGES_THREAD_1), HOLD(240, 240), WAIT(1000), TAP_LABEL("Clear chat"),
                              TAP_LABEL("Tap again to clear")}},
    /* 0.0.15 (owner, 2026-10-12, the mock's Convo boards): in an agent's thread the long press is
       New conversation and Recent conversations. */
    {"jerv-menu", NULL, {OPEN("jerv"), HOLD(240, 240), WAIT(1000)}},
    {"jerv-recent-loading", hold_list, {OPEN("jerv"), HOLD(240, 240), WAIT(1000), TAP_LABEL("Recent conversations")}},
    {"jerv-recent", NULL, {OPEN("jerv"), HOLD(240, 240), WAIT(1000), TAP_LABEL("Recent conversations")}},
    {"jerv-recent-end", NULL, {OPEN("jerv"), HOLD(240, 240), WAIT(1000), TAP_LABEL("Recent conversations"), SCROLL_END}},
    {"jerv-recent-empty", no_convos, {OPEN("jerv"), HOLD(240, 240), WAIT(1000), TAP_LABEL("New conversation"),
                                      WAIT(2400), HOLD(240, 240), WAIT(1000), TAP_LABEL("Recent conversations")}},
    {"jerv-switched", NULL, {OPEN("jerv"), HOLD(240, 240), WAIT(1000), TAP_LABEL("Recent conversations"),
                             TAP_LABEL("Tides at Port St John"), WAIT(500)}},
    {"jerv-new", NULL, {OPEN("jerv"), HOLD(240, 240), WAIT(1000), TAP_LABEL("New conversation"), WAIT(2400)}},
    {"dave-menu", NULL, {OPEN("minecraft"), TAP_LABEL("Dave"), HOLD(240, 240), WAIT(1000)}},
    {"dave-recent", NULL, {OPEN("minecraft"), TAP_LABEL("Dave"), HOLD(240, 240), WAIT(1000),
                           TAP_LABEL("Recent conversations")}},
    {"settings", NULL, {OPEN("settings")}},
    {"settings-end", NULL, {OPEN("settings"), SCROLL_END}},
    {"settings-usb", no_battery, {OPEN("settings")}},
    /* The first tap on Disconnect: rose, "Tap again to disconnect". */
    {"settings-armed", NULL, {OPEN("settings"), SCROLL_END, TAP(240, 418), WAIT(300)}},
    {"updating", downloading, {OPEN("settings"), SCROLL(680)}},
    {"clock", NULL, {OPEN("clock")}},
    {"clock-night", night, {OPEN("clock")}},
    {"clock-badge", night_badge, {OPEN("clock")}},
    /* The face opens its alarms on a two-second hold, not a tap (owner, 2026-10-12). */
    {"clock-alarms", NULL, {OPEN("clock"), HOLD_MS(240, 240, 2100), WAIT(2400)}},
    {"clock-alarms-end", NULL, {OPEN("clock"), HOLD_MS(240, 240, 2100), WAIT(2900), SCROLL_END}},
    /* Mid-hold: the ring filling, steel by day and the night colour at night. */
    {"clock-hold", NULL, {OPEN("clock"), HOLD_MS(240, 240, 3000), WAIT(1200)}},
    {"clock-hold-night", night, {OPEN("clock"), HOLD_MS(240, 240, 3000), WAIT(1200)}},
    /* A tap on the face: nothing. */
    {"clock-tap", NULL, {OPEN("clock"), TAP(240, 240), WAIT(300)}},
    {"ringing", ring, {WAIT(100)}},
    /* 0.0.13: the alarm wakes a dark screen and takes it over at a readable brightness. */
    {"ringing-dark", before_ring, {PWR, WAIT(61000)}},
    /* Over a notice: the alarm is on top. */
    {"ringing-over-notice", ring_with_notice, {WAIT(100)}},
    /* KEY snoozes; held two seconds, the face opens the list saying until when. */
    {"alarm-snoozed", ring, {WAIT(100), KEY_DOWN, KEY_UP, WAIT(400), HOLD_MS(240, 240, 2100), WAIT(2400)}},
    /* The debug relay's `inject event=alarm_test`: the whole ring path, with no alarm set. */
    {"alarm-test", NULL, {"{\"cmd\":\"inject\",\"event\":\"alarm_test\",\"name\":\"beeps\"}", WAIT(300)}},
    {"notify-info", push_info, {WAIT(100)}},
    {"notify-question", push_question, {WAIT(100)}},
    /* KEY from Settings: the shell's talk to Jerv, over whatever is open. */
    {"rec-anywhere", NULL, {OPEN("settings"), KEY_DOWN, WAIT(3400)}},
    /* Its release: the Jerv thread, the hold's bubble waiting for its transcript. */
    {"jerv-after-key", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300)}},
    /* The box answers: the transcript, Jerv's words, and "Speaking…" while its voice plays. */
    {"jerv-reply", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), REPLY}},
    /* JT2 (0.0.12): the transcript the moment the box has it, and the live status line under it. */
    {"jerv-working", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), PROGRESS}},
    {"dave-working", NULL, {OPEN("minecraft"), TAP_LABEL("Dave"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), PROGRESS}},
    /* 0.0.14: thinking with its clock, the steps mid-turn ("+2 more", ✓, a dim ✗, the spinner on
       the running one), and the answer with its steps folded into one dim line. */
    {"jerv-thinking", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), PROGRESS, WAIT(41500)}},
    {"jerv-steps", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), STEPS, WAIT(20000)}},
    {"jerv-steps-done", text_only, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), STEPS, WAIT(57000), REPLY_TIDE}},
    /* A long answer kept whole on the glass, past a bubble's 255 bytes. */
    {"jerv-long", text_only, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), REPLY_LONG, SCROLL_END}},
    /* The same with "Read replies aloud" off: words only. */
    {"jerv-reply-text", text_only, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), REPLY}},
    /* BOOT held from inside an app: the clock face (owner, 2026-10-12). */
    {"boot-long", NULL, {OPEN("settings"), "{\"cmd\":\"inject\",\"event\":\"boot_long\"}", WAIT(300)}},
    /* Not a mock board: a short session whose snapshot shows the debug surface. */
    {"session", session, {OPEN(SVC_MESSAGES_THREAD_1), KEY_DOWN, WAIT(2000), KEY_UP, OPEN(SVC_MESSAGES_THREAD_2), WAIT(100)}},
};
#define NSTATES (sizeof STATES / sizeof *STATES)

/* The open page: of everything that scrolls, the one with the most below the glass (a label can
 * carry the scrollable flag and a pixel of overhang). */
static void scroller(lv_obj_t *o, lv_obj_t **best, int32_t *most)
{
    int32_t below = lv_obj_get_scroll_bottom(o);
    if (lv_obj_has_flag(o, LV_OBJ_FLAG_SCROLLABLE) && below > *most) {
        *best = o;
        *most = below;
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++)
        scroller(lv_obj_get_child(o, (int32_t)i), best, most);
}

static void scroll_to(int32_t y)
{
    lv_obj_t *s = NULL;
    int32_t most = 0;
    lv_obj_update_layout(lv_screen_active());
    scroller(lv_screen_active(), &s, &most);
    if (s) lv_obj_scroll_to_y(s, y, LV_ANIM_OFF);
    step(100);
}

static int render(const char *name, const char *png, const char *snap)
{
    const state_t *st = NULL;
    for (size_t i = 0; i < NSTATES; i++)
        if (!strcmp(STATES[i].name, name)) st = &STATES[i];
    if (!st) {
        fprintf(stderr, "no state %s (--list)\n", name);
        return 2;
    }
    if (st->setup) st->setup();
    fixtures();
    step(100);
    for (int i = 0; i < CMDS && st->cmds[i]; i++) {
        if (!strncmp(st->cmds[i], "wait ", 5)) {
            step((uint32_t)atoi(st->cmds[i] + 5));
            continue;
        }
        if (!strcmp(st->cmds[i], POST_NEW)) {
            post_arrives();
            step(100);
            continue;
        }
        if (!strcmp(st->cmds[i], LISTEN)) {
            svc_audio_fake_listen(true, false, false);
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], PLAYED_HALF)) {
            msg_t m;
            if (svc_messages_find(SVC_MESSAGES_THREAD_1, svc_audio_playing(), &m))
                svc_audio_fake_played(m.dur_ms / 2u);
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], POSTED)) {
            s_post_sent = true;
            svc_audio_fake_posted(201, POSTED_TEXT);
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], REPLY)) {
            svc_audio_fake_reply(HEARD, REPLIED, svc_settings_get(SET_TTS) != 0);
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], REPLY_LONG)) {
            svc_audio_fake_reply(TIDE_HEARD, TIDE_LONG, svc_settings_get(SET_TTS) != 0);
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], PROGRESS)) {
            svc_audio_fake_progress(TIDE_HEARD, "Thinking…");
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], STEPS)) {
            static const char *const LABELS[] = {TIDE_STEP_0, TIDE_STEP_1, TIDE_STEP_2,
                                                 TIDE_STEP_3, TIDE_STEP_4, TIDE_STEP_5};
            svc_audio_fake_progress(TIDE_HEARD, "Thinking…");
            step(100);
            for (uint8_t k = 0; k < 6; k++) {
                svc_audio_fake_step(k, LABELS[k]);
                step(100);
                if (k < 5) svc_audio_fake_step_done(k, k != 2);
            }
            step(300);
            continue;
        }
        if (!strcmp(st->cmds[i], REPLY_TIDE)) {
            svc_audio_fake_step_done(5, true);
            svc_audio_fake_reply(TIDE_HEARD, "High tide at Port St John is at 4:12 pm, 3.1 feet.",
                                 svc_settings_get(SET_TTS) != 0);
            step(300);
            continue;
        }
        if (!strncmp(st->cmds[i], "tap ", 4)) {
            if (!tap_label(st->cmds[i] + 4)) {
                fprintf(stderr, "%s: nothing says \"%s\"\n", name, st->cmds[i] + 4);
                return 1;
            }
            continue;
        }
        if (!strncmp(st->cmds[i], "scroll ", 7)) {
            scroll_to(!strcmp(st->cmds[i], SCROLL_END) ? LV_COORD_MAX : atoi(st->cmds[i] + 7));
            continue;
        }
        const char *res = run(st->cmds[i]);
        if (!strstr(res, "\"ok\":true")) {
            fprintf(stderr, "%s: %s -> %s\n", name, st->cmds[i], res);
            return 1;
        }
    }
    step(300);
    const char *res = run("{\"id\":\"shot\",\"cmd\":\"screenshot\"}");
    if (!strstr(res, "\"ok\":true")) {
        fprintf(stderr, "%s: screenshot -> %s\n", name, res);
        return 1;
    }
    static uint16_t frame[W * H];
    if (!dbg_shot_decode(s_cap.buf, s_cap.n, frame, W, H)) {
        fprintf(stderr, "%s: the stripe stream did not decode\n", name);
        return 1;
    }
    if (png && !png_write_rgb565(png, frame, W, H)) {
        fprintf(stderr, "%s: cannot write %s\n", name, png);
        return 1;
    }
    if (snap) {
        res = run("{\"id\":\"snap\",\"cmd\":\"snapshot\"}");
        FILE *f = fopen(snap, "w");
        if (!f || fprintf(f, "%s\n", res) < 0 || fclose(f)) return 1;
    }
    /* LVGL's pool peak, with this host's 64-bit pointers: the device's is a static 64 KB pool
     * with 32-bit ones, so a state near 64 KB here is the one to measure there. */
    lv_mem_monitor_t mon;
    lv_mem_monitor(&mon);
    printf("%s: %u stripes, %u bytes on the wire, lvgl peak %u B\n", name, (unsigned)s_cap.shot.stripes,
           (unsigned)s_cap.shot.bytes, (unsigned)mon.max_used);
    return 0;
}

/* Opening and closing every app must leave exactly what was there: the plan's "only the open
 * app's screen exists", checked rather than asserted. */
static int lifecycle(void)
{
    static const char *const IDS[] = {"jerv", SVC_MESSAGES_THREAD_1, SVC_MESSAGES_THREAD_2, "minecraft", "clock", "settings"};
    int bad = 0;
    step(3000);
    unsigned objs = shell_obj_count(), timers = shell_timer_count();
    for (size_t i = 0; i < sizeof IDS / sizeof *IDS; i++) {
        char cmd[64];
        snprintf(cmd, sizeof cmd, "{\"cmd\":\"open\",\"app\":\"%s\"}", IDS[i]);
        run(cmd);
        step(200);
        /* Closed mid-hold, the hardest case: the talk ring and its timer are up when the
         * app goes, and the release arrives after it has gone. */
        run(KEY_DOWN);
        step(800);
        unsigned open_objs = shell_obj_count();
        run("{\"cmd\":\"home\"}");
        run(KEY_UP);
        step(3000);
        unsigned o = shell_obj_count(), t = shell_timer_count();
        printf("%-10s open %4u widgets; closed back to %u widgets (home %u), %u timers (home %u)\n", IDS[i],
               open_objs, o, objs, t, timers);
        if (o != objs || t != timers) bad = 1;
    }
    /* Dave's conversation, a level down, closed mid-hold; and a conversation closed with its
       menu armed (the menu's lapse timer must go with it). */
    static const char *const DEEP[][4] = {
        {OPEN("minecraft"), "tap Dave", KEY_DOWN, NULL},
        {OPEN(SVC_MESSAGES_THREAD_1), HOLD(240, 240), "wait 1000", "tap Clear chat"},
        {OPEN("minecraft"), "tap Compass", NULL, NULL},
        {OPEN("jerv"), HOLD(240, 240), "wait 1000", "tap Recent conversations"},
    };
    static const char *const DEEP_NAME[] = {"dave", "quorra-menu", "compass", "jerv-recent"};
    for (size_t i = 0; i < sizeof DEEP / sizeof *DEEP; i++) {
        for (int k = 0; k < 4 && DEEP[i][k]; k++) {
            if (!strncmp(DEEP[i][k], "tap ", 4)) tap_label(DEEP[i][k] + 4);
            else if (!strncmp(DEEP[i][k], "wait ", 5)) step((uint32_t)atoi(DEEP[i][k] + 5));
            else run(DEEP[i][k]);
            step(200);
        }
        step(800);
        unsigned open_objs = shell_obj_count();
        run("{\"cmd\":\"home\"}");
        run(KEY_UP);
        step(3000);
        unsigned o = shell_obj_count(), t = shell_timer_count();
        printf("%-10s open %4u widgets; closed back to %u widgets (home %u), %u timers (home %u)\n",
               DEEP_NAME[i], open_objs, o, objs, t, timers);
        if (o != objs || t != timers) bad = 1;
    }
    return bad;
}

/* ---- a finger: samples queued the way svc_touch queues them, read by the shell's pointer ---- */

static struct {
    int16_t x, y;
    bool pressed;
} s_fq[32];
static unsigned s_fq_head, s_fq_n;

static bool finger_feed(int16_t *x, int16_t *y, bool *pressed)
{
    if (s_fq_head == s_fq_n) return false;
    *x = s_fq[s_fq_head].x;
    *y = s_fq[s_fq_head].y;
    *pressed = s_fq[s_fq_head].pressed;
    s_fq_head++;
    return true;
}

static void finger(int x, int y, bool pressed)
{
    if (s_fq_head == s_fq_n) s_fq_head = s_fq_n = 0;
    s_fq[s_fq_n].x = (int16_t)x;
    s_fq[s_fq_n].y = (int16_t)y;
    s_fq[s_fq_n].pressed = pressed;
    s_fq_n++;
}

static int s_touch_bad;

static void expect(bool ok, const char *what)
{
    printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) s_touch_bad = 1;
}

static bool on_page(int page)
{
    char want[16];
    snprintf(want, sizeof want, "\"page\":%d", page);
    return strstr(run("{\"cmd\":\"snapshot\"}"), want) != NULL;
}

static lv_obj_t *find_spinner(lv_obj_t *o)
{
    if (ui_is_spinner(o)) return o;
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *hit = find_spinner(lv_obj_get_child(o, (int32_t)i));
        if (hit) return hit;
    }
    return NULL;
}

static unsigned spinners(lv_obj_t *o)
{
    unsigned n = ui_is_spinner(o);
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++)
        n += spinners(lv_obj_get_child(o, (int32_t)i));
    return n;
}

/* The spinner bug (owner, 2026-10-12: "it's kind of like resetting"): each status line rebuilt
 * the conversation and with it the spinner, whose sweep began again. A status change, a step and
 * the clock's tick must now leave the spinner and the question's widgets exactly where they are,
 * the spinner turning on the tick's phase. Jerv's thread is open, the box has heard the question
 * and the agent is thinking. */
static void spinner_checks(void)
{
    lv_obj_t *spin = find_spinner(lv_screen_active());
    lv_obj_t *question = labelled(lv_screen_active(), HEARD);
    expect(spin && question && spinners(lv_screen_active()) == 1, "thinking: one spinner, under the question");
    svc_audio_fake_progress(NULL, "Writing the answer…");
    step(100);
    svc_audio_fake_step(0, "weather · tomorrow");
    step(100);
    expect(find_spinner(lv_screen_active()) == spin && spinners(lv_screen_active()) == 1,
           "a status change and a step move the same spinner: it is not remade");
    expect(labelled(lv_screen_active(), HEARD) == question, "nor is the question above it");
    expect(labelled(lv_screen_active(), "weather · tomorrow") != NULL, "the step is listed");
    step(2400);
    svc_audio_fake_step_done(0, true);
    svc_audio_fake_progress(NULL, "Thinking…");
    step(100);
    expect(find_spinner(lv_screen_active()) == spin && labelled(lv_screen_active(), HEARD) == question,
           "two seconds of the clock and the step ending change nothing above the working line");
    /* The animation runs on LVGL's refresh period, so it may be a frame behind the tick. */
    const int32_t lag = ((int32_t)(lv_tick_get() % 1000 * 360 / 1000) - lv_arc_get_rotation(spin) + 360) % 360;
    expect(lag <= 15, "and it turns on the tick's phase, so even a new one would carry on where this one is");
    expect(lv_arc_get_angle_end(spin) - lv_arc_get_angle_start(spin) == 90,
           "a fixed quarter arc, not a sweep that grows and shrinks");
}

/* The open thread's "rebuilds" count, from its snapshot. */
static long thread_rebuilds(void)
{
    const char *at = strstr(run("{\"cmd\":\"snapshot\"}"), "\"rebuilds\":");
    return at ? atol(at + 11) : -1;
}

/* The page scrolled down from its top: a thread pinned to its newest message. */
static lv_obj_t *scrolled(lv_obj_t *o)
{
    if (lv_obj_has_flag(o, LV_OBJ_FLAG_SCROLLABLE) && lv_obj_get_scroll_top(o) > 0) return o;
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *hit = scrolled(lv_obj_get_child(o, (int32_t)i));
        if (hit) return hit;
    }
    return NULL;
}

/* A play glyph whose button is on the glass inside the page: the one a finger can reach. */
static lv_obj_t *visible_play(lv_obj_t *o, const lv_area_t *in)
{
    if (lv_obj_check_type(o, &lv_label_class) && !strcmp(lv_label_get_text(o), LV_SYMBOL_PLAY)) {
        lv_area_t a;
        lv_obj_get_coords(lv_obj_get_parent(o), &a);
        /* Clear of the header over the page's top and the hint under its end. */
        if (a.y1 >= in->y1 + UI_HEADER_H && a.y2 <= in->y2 - 60) return o;
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *hit = visible_play(lv_obj_get_child(o, (int32_t)i), in);
        if (hit) return hit;
    }
    return NULL;
}

static void tap_obj(lv_obj_t *o)
{
    lv_obj_update_layout(lv_screen_active());
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    char cmd[96];
    snprintf(cmd, sizeof cmd, "{\"cmd\":\"inject\",\"event\":\"touch\",\"x\":%d,\"y\":%d}",
             (int)(a.x1 + a.x2) / 2, (int)(a.y1 + a.y2) / 2);
    run(cmd);
    step(500);
}

/* 0.0.17 (owner, 2026-10-13: "the screen kind of jumps around depending on when I play"): each
 * play's start and stop rebuilt her whole thread, and an emptied page clamps its scroll, so the
 * reader was thrown about. Now a play's start, its progress and its stop change her bubble where
 * it stands: the list is not rebuilt, its widgets are the same ones, and the scroll stays where
 * the reader left it. And the bar fills left to right with what has played. */
static int thread_checks(void)
{
    s_post_long = true;
    run(OPEN(SVC_MESSAGES_THREAD_1));
    step(500);
    lv_obj_update_layout(lv_screen_active());
    lv_obj_t *page = scrolled(lv_screen_active());
    expect(page != NULL, "her four long messages run past the glass, pinned to the newest");
    if (!page) return 1;
    /* The reader scrolls up from the newest until one of her earlier messages is in view, and
       stops short of the top, so a page clamped by a rebuild would show. */
    lv_area_t in;
    lv_obj_get_coords(page, &in);
    lv_obj_t *glyph = NULL;
    for (int32_t at = lv_obj_get_scroll_y(page) - 40; at > 0 && !glyph; at -= 20) {
        lv_obj_scroll_to_y(page, at, LV_ANIM_OFF);
        lv_obj_update_layout(page);
        glyph = visible_play(page, &in);
    }
    step(200);
    const int32_t y = lv_obj_get_scroll_y(page);
    expect(glyph != NULL && y > 0, "scrolled part way up, with one of her messages to play in view");
    if (!glyph) return 1;
    lv_obj_t *row = lv_obj_get_parent(lv_obj_get_parent(glyph));
    lv_obj_t *track = lv_obj_get_child(row, 1), *fill = lv_obj_get_child(track, 0);
    lv_obj_t *dot = lv_obj_get_child(row, 3);
    const long built = thread_rebuilds();
    const unsigned objs = shell_obj_count();
    const uint16_t badge = svc_messages_unplayed(SVC_MESSAGES_THREAD_1);
    lv_area_t at0;
    lv_obj_get_coords(lv_obj_get_parent(glyph), &at0);
    expect(dot && lv_obj_get_style_opa(dot, 0) == LV_OPA_COVER && lv_obj_get_width(fill) == 0,
           "unheard: its dot, and an empty bar");

    tap_obj(lv_obj_get_parent(glyph));
    msg_t m = {0};
    svc_messages_find(SVC_MESSAGES_THREAD_1, svc_audio_playing(), &m);
    expect(m.id && !strcmp(lv_label_get_text(glyph), LV_SYMBOL_PAUSE),
           "the press plays it: the same button now says pause");
    expect(lv_obj_get_style_opa(dot, 0) == LV_OPA_TRANSP &&
               svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == badge,
           "playing, its dot goes; the badge waits for the box's audio");
    svc_audio_fake_listen(true, false, false);
    step(300);
    expect(svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == badge - 1,
           "its audio starts: heard, one off the badge at once");
    const int32_t full = lv_obj_get_width(track);
    expect(lv_obj_get_width(fill) == 0, "the box's audio arrives: the bar starts empty, at the left");
    svc_audio_fake_played(m.dur_ms / 4u);
    step(200);
    const int32_t quarter = lv_obj_get_width(fill);
    svc_audio_fake_played(m.dur_ms / 4u);
    step(200);
    const int32_t half = lv_obj_get_width(fill);
    expect(quarter > 0 && half > quarter && half >= full / 2 - 2 && half <= full / 2 + 2,
           "it fills left to right with what has played: a quarter, then half");
    step(1000);
    expect(lv_obj_get_width(fill) == half, "an underrun (nothing more played) holds it where it is");
    expect(lv_obj_get_scroll_y(page) == y, "and the page has not moved through the play");
    tap_obj(lv_obj_get_parent(glyph));
    expect(!svc_audio_playing() && !strcmp(lv_label_get_text(glyph), LV_SYMBOL_PLAY),
           "stopped part way: the same button says play again");
    expect(lv_obj_get_width(fill) == full && lv_obj_get_style_opa(dot, 0) == LV_OPA_TRANSP,
           "heard: a full bar and no dot, as every heard message shows");
    lv_area_t at1;
    lv_obj_get_coords(lv_obj_get_parent(glyph), &at1);
    expect(lv_obj_get_scroll_y(page) == y && at1.x1 == at0.x1 && at1.y1 == at0.y1,
           "the scroll is still where the reader left it, and the button where it was");
    expect(thread_rebuilds() == built && shell_obj_count() == objs &&
               lv_obj_get_child(row, 1) == track && lv_obj_get_child(track, 0) == fill,
           "start, progress and stop rebuilt nothing: the same widgets, the same count");
    /* Played again to its end: still nothing rebuilt, still nothing moved. */
    tap_obj(lv_obj_get_parent(glyph));
    svc_audio_fake_listen(true, true, true);
    step(300);
    expect(!svc_audio_playing() && thread_rebuilds() == built && lv_obj_get_scroll_y(page) == y,
           "a play to its end: the same");
    s_post_long = false;
    return s_touch_bad;
}

/* Every rule a real finger meets that the debug pointer's fixed stroke never did. The first
 * tile's centre is (134, 126). */
static int touch_checks(void)
{
    shell_set_pointer(finger_feed);
    step(200);

    finger(134, 126, true);
    finger(134, 126, false);
    step(100);
    expect(shell_open_app() && !strcmp(shell_open_app()->id, "jerv"),
           "a press and its release inside one read period still open the tile");
    run("{\"cmd\":\"home\"}");

    run("{\"cmd\":\"inject\",\"event\":\"pwr\"}");
    expect(svc_display_dark(), "PWR darkens");
    finger(134, 126, true);
    step(600);
    expect(!svc_display_dark(), "a finger on the dark screen wakes it while still down");
    finger(134, 126, false);
    step(100);
    expect(!shell_open_app(), "and opens nothing, however long it stayed");

    finger(134, 126, true);
    step(60);
    finger(174, 126, true);
    step(60);
    finger(174, 126, false);
    step(100);
    expect(!shell_open_app() && on_page(0), "a 40 px drag from a tile is neither a tap nor a swipe");

    finger(134, 126, true);
    step(60);
    finger(134, 300, true);
    finger(214, 300, false);
    step(100);
    expect(!shell_open_app() && on_page(0), "a mostly-vertical drag turns no page");

    finger(400, 240, true);
    step(60);
    finger(200, 240, true);
    finger(200, 240, false);
    step(100);
    expect(!shell_open_app() && on_page(1), "a swipe left from a tile turns the page and opens nothing");
    run("{\"cmd\":\"home\"}");

    finger(134, 126, true);
    step(100);
    push_info();
    step(100);
    finger(134, 126, false);
    step(100);
    expect(!shell_open_app(), "a notice that appears under a finger takes its release");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"overlay\":\"none\"") != NULL, "BOOT dismissed it");

    /* The clock face is never covered: a notice waits as the corner badge, opens on its tap. */
    run(OPEN("clock"));
    step(100);
    push_info();
    step(300);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"overlay\":\"none\"") != NULL,
           "a notice arriving on the clock face does not cover it");
    expect(shell_open_app() && !strcmp(shell_open_app()->id, "clock"), "and the face stays up");
    finger(426, 48, true);
    finger(426, 48, false);
    step(1200);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"overlay\":\"notice\"") != NULL,
           "a tap on the badge opens it");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);

    /* The face opens its alarms on a two-second hold, never a tap (owner, 2026-10-12). */
    run(OPEN("clock"));
    step(100);
    finger(240, 240, true);
    finger(240, 240, false);
    step(400);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"face\"") != NULL, "a tap on the face does nothing");
    finger(240, 240, true);
    step(1900);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"face\"") != NULL &&
               strstr(run("{\"cmd\":\"snapshot\"}"), "\"hold_fill\":0") == NULL,
           "1.9 s held: still the face, with the ring filling");
    finger(240, 240, false);
    step(400);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"face\"") != NULL &&
               strstr(run("{\"cmd\":\"snapshot\"}"), "\"hold_fill\":0") != NULL,
           "let go early, it cancels quietly");
    finger(240, 240, true);
    step(60);
    finger(300, 240, true);
    step(2400);
    finger(300, 240, false);
    step(400);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"face\"") != NULL, "a drag never opens it");
    finger(240, 240, true);
    step(2200);
    finger(240, 240, false);
    step(400);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"alarms\"") != NULL,
           "two seconds held still opens the alarms list");
    run("{\"cmd\":\"inject\",\"event\":\"pwr\"}");
    finger(240, 240, true);
    finger(240, 240, false);
    step(300);
    expect(!svc_display_dark(), "a tap still wakes a dark screen");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);

    /* KEY outside a conversation is Jerv's, wherever the panel is. */
    run(OPEN("settings"));
    run(KEY_DOWN);
    step(300);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"talking\":true") != NULL,
           "KEY in Settings starts the shell's talk to Jerv");
    run(KEY_UP);
    step(100);
    expect(shell_open_app() && !strcmp(shell_open_app()->id, "settings"),
           "released under 0.6 s it is a knock: nothing opens");
    expect(svc_messages_count("jerv") > 0, "Jerv's thread has an old conversation in it");
    run(KEY_DOWN);
    step(1500);
    run(KEY_UP);
    step(100);
    expect(shell_open_app() && !strcmp(shell_open_app()->id, "jerv"),
           "a real hold opens the Jerv thread on release");
    msg_t last = {0};
    svc_messages_get("jerv", (uint16_t)(svc_messages_count("jerv") - 1), &last);
    expect(svc_messages_count("jerv") == 1 && last.pending,
           "a new conversation, holding just the hold waiting for its transcript");
    expect(strstr(svc_audio_fake_last_path(), "&new_thread=true") != NULL,
           "KEY outside the conversation asks the box for a new one");
    svc_audio_fake_progress(HEARD, "Thinking…");
    step(100);
    spinner_checks();
    unsigned waiting = svc_messages_count("jerv");
    run(KEY_DOWN);
    step(300);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"talking\":false") != NULL && svc_audio_replying(),
           "KEY while Jerv is still working starts no new talk and leaves his turn alone");
    run(KEY_UP);
    step(100);
    expect(svc_messages_count("jerv") == waiting, "and its release sends nothing");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);
    expect(!shell_open_app() && svc_audio_replying(), "BOOT leaves the thread; the turn goes on");
    svc_audio_fake_reply(HEARD, REPLIED, true);
    step(100);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"replying\":true") != NULL,
           "the reply arrives and Jerv speaks it");
    run(OPEN("jerv"));
    step(100);
    unsigned answered = svc_messages_count("jerv");
    run(KEY_DOWN);
    step(300);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"talking\":false") != NULL &&
               !svc_audio_replying(),
           "KEY while Jerv speaks stops it and records nothing");
    run(KEY_UP);
    step(100);
    expect(svc_messages_count("jerv") == answered, "and its release sends nothing");
    run("{\"cmd\":\"home\"}");
    run(KEY_DOWN);
    step(300);
    finger(240, 240, true);
    finger(240, 240, false);
    step(100);
    run(KEY_UP);
    step(100);
    expect(!shell_open_app() && svc_messages_count("jerv") == answered,
           "a tap during the hold cancels it, and the release sends nothing");

    /* The conversation menu, by a real finger. */
    run(OPEN("jerv"));
    step(100);
    finger(240, 240, true);
    step(400);
    expect(!shell_menu_open(), "a finger resting under the threshold opens nothing yet");
    step(400);
    expect(shell_menu_open(), "held still past 600 ms, the menu opens while the finger is down");
    finger(240, 240, false);
    step(200);
    expect(shell_menu_open(), "and lifting it presses nothing under it");
    run(KEY_DOWN);
    step(200);
    expect(!shell_menu_open() && strstr(run("{\"cmd\":\"snapshot\"}"), "\"talking\":false"),
           "KEY closes the menu and records nothing");
    run(KEY_UP);
    step(100);
    expect(svc_messages_count("jerv") == answered, "and its release sends nothing");

    finger(240, 240, true);
    step(60);
    finger(240, 320, true);
    step(900);
    finger(240, 320, false);
    step(200);
    expect(!shell_menu_open(), "a scroll held at its end is not a long press");

    unsigned daves = svc_messages_count("dave");
    /* 0.0.15: an agent's menu is New conversation and Recent conversations. */
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"subtitle\":\"Shopping list\"") != NULL,
           "the thread's header names the conversation the box says it is");
    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    expect(labelled(lv_layer_top(), "Recent conversations") && !labelled(lv_layer_top(), "Clear chat"),
           "Jerv's menu has New and Recent, and no Clear chat");
    tap_label("Recent conversations");
    expect(!shell_menu_open() && strstr(run("{\"cmd\":\"snapshot\"}"), "\"recent\":\"list\"") != NULL,
           "Recent opens over the thread with the box's list");
    finger(240, 240, true);
    step(900);
    finger(240, 240, false);
    step(200);
    expect(!shell_menu_open(), "a long press on Recent opens no menu");
    run(KEY_DOWN);
    step(200);
    const char *rs = run("{\"cmd\":\"snapshot\"}");
    expect(strstr(rs, "\"recent\":\"closed\"") != NULL && strstr(rs, "\"talking\":true") != NULL,
           "KEY in Recent closes it and talks in the current conversation");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    run(KEY_UP);
    step(100);
    expect(svc_messages_count("jerv") == answered, "(that hold cancelled, nothing sent)");

    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    tap_label("Recent conversations");
    tap_label("Tides at Port St John");
    step(300);
    rs = run("{\"cmd\":\"snapshot\"}");
    expect(svc_messages_count("jerv") == 4 && strstr(rs, "\"subtitle\":\"Tides at Port St John\"") &&
               strstr(rs, "\"recent\":\"closed\""),
           "a row switches the thread to that conversation's last exchanges, under its title");
    expect(svc_messages_count("dave") == daves, "and Dave's thread is untouched");
    run(KEY_DOWN);
    step(1500);
    run(KEY_UP);
    step(100);
    expect(strstr(svc_audio_fake_last_path(), "persona=jerv") &&
               !strstr(svc_audio_fake_last_path(), "new_thread"),
           "the next talk in it continues that conversation");
    svc_audio_fake_reply("and tomorrow?", "Low at 15:40.", false);
    step(200);
    const unsigned switched = svc_messages_count("jerv");

    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    tap_label("New conversation");
    expect(!shell_menu_open() && svc_messages_count("jerv") == 0 &&
               strstr(run("{\"cmd\":\"snapshot\"}"), "\"subtitle\":\"New conversation\"") != NULL,
           "New conversation is one tap: the thread starts over (the old one is in Recent)");
    expect(switched > 0 && svc_messages_count("dave") == daves, "and Dave's is untouched");
    expect(svc_audio_fresh_pending("jerv") && !svc_audio_fresh_pending("dave"),
           "Jerv's next talk starts a new conversation on the box; Dave's continues");
    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    tap_label("Recent conversations");
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"recent\":\"list\"") != NULL,
           "Recent after New lists the last one, no longer current");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"recent\":\"closed\"") != NULL && shell_open_app(),
           "BOOT leaves Recent for the thread");
    const char *before_new = strstr(run("{\"cmd\":\"snapshot\"}"), "\"new_conversations\":");
    const int news = before_new ? atoi(before_new + 20) : -1;
    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    tap_label("New conversation");
    expect(svc_messages_count("jerv") == 0 && svc_audio_fresh_pending("jerv") &&
               strstr(run("{\"cmd\":\"snapshot\"}"), "\"new_conversations\":") != NULL && news >= 1,
           "a second New on a new, empty conversation does nothing (it says it already is)");

    /* A person's thread keeps 0.0.11's Clear chat, two taps. */
    run(OPEN(SVC_MESSAGES_THREAD_1));
    step(100);
    const unsigned hers = svc_messages_count(SVC_MESSAGES_THREAD_1);
    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    tap_label("Clear chat");
    expect(shell_menu_open() && hers > 0 && svc_messages_count(SVC_MESSAGES_THREAD_1) == hers,
           "the first tap on Clear chat only arms it");
    tap_label("Tap again to clear");
    expect(!shell_menu_open() && svc_messages_count(SVC_MESSAGES_THREAD_1) == 0,
           "the second clears her thread");
    run(OPEN("jerv"));
    step(100);

    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);
    expect(!shell_menu_open() && shell_open_app() && !strcmp(shell_open_app()->id, "jerv"),
           "BOOT closes the menu and stays in the conversation");
    finger(240, 240, true);
    step(800);
    finger(240, 240, false);
    step(200);
    finger(20, 20, true);
    finger(20, 20, false);
    step(200);
    expect(!shell_menu_open(), "a tap outside the menu closes it");

    run(OPEN("settings"));
    step(100);
    finger(240, 300, true);
    step(900);
    finger(240, 300, false);
    step(200);
    expect(!shell_menu_open(), "a long press outside a conversation opens nothing");

    run(OPEN("minecraft"));
    step(100);
    finger(240, 240, true);
    step(900);
    finger(240, 240, false);
    step(200);
    expect(!shell_menu_open(), "nor on the Minecraft menu");
    tap_label("Dave");
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"dave\"") != NULL, "Dave opens a level down");
    run(KEY_DOWN);
    step(1500);
    run(KEY_UP);
    step(100);
    expect(strstr(svc_audio_fake_last_path(), "persona=dave") != NULL,
           "KEY in Dave's thread asks the box for Dave");
    svc_audio_fake_reply("where is spawn", "Two hundred blocks east.", false);
    step(100);
    expect(svc_messages_count("jerv") == 0 && svc_messages_count("dave") == daves + 2,
           "his answer lands in his thread and not Jerv's");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"view\":\"menu\"") != NULL,
           "BOOT climbs back to the Minecraft menu");
    run(KEY_DOWN);
    step(300);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"talking\":true") != NULL,
           "KEY on the menu is the shell's talk to Jerv");
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    run(KEY_UP);
    step(100);
    run("{\"cmd\":\"inject\",\"event\":\"boot\"}");
    step(100);
    expect(!shell_open_app(), "and BOOT on the menu goes home");

    /* Alarms (0.0.13): the box's list, the switch on the glass, and a ring that beats a talk. */
    svc_alarms_offer(ALARMS, sizeof ALARMS - 1);
    step(100);
    run(OPEN("clock"));
    step(100);
    finger(240, 240, true);
    step(2200);
    finger(240, 240, false);
    step(400);
    finger(420, 177, true);
    finger(420, 177, false);
    step(400);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"queue\":[{\"id\":1,\"on\":false}]") != NULL,
           "the 06:45 switch on the glass queues its write for the box");
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"next\":{\"id\":3,") != NULL,
           "and the next alarm is the 21:00 one at once");
    run("{\"cmd\":\"home\"}");
    const unsigned talked = svc_messages_count("jerv");
    run(KEY_DOWN);
    step(300);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"talking\":true") != NULL, "a talk to Jerv is under way");
    run("{\"cmd\":\"inject\",\"event\":\"alarm_test\"}");
    step(100);
    const char *snap = run("{\"cmd\":\"snapshot\"}");
    expect(strstr(snap, "\"talking\":false") != NULL && strstr(snap, "\"overlay\":\"alarm\"") != NULL &&
               strstr(snap, "\"view\":\"ringing\"") != NULL,
           "an alarm ends the talk and takes the glass");
    run(KEY_UP);
    step(100);
    expect(svc_messages_count("jerv") == talked, "the held KEY's release sends nothing");
    run("{\"cmd\":\"inject\",\"event\":\"pwr\"}");
    step(100);
    expect(!svc_display_dark(), "PWR cannot put out a ringing alarm");
    run(KEY_DOWN);
    step(100);
    run(KEY_UP);
    step(100);
    snap = run("{\"cmd\":\"snapshot\"}");
    expect(strstr(snap, "\"last_end\":\"snoozed\"") != NULL && strstr(snap, "\"view\":\"face\"") != NULL,
           "KEY snoozes it and the face comes back");
    run("{\"cmd\":\"inject\",\"event\":\"alarm_test\"}");
    step(100);
    run("{\"cmd\":\"inject\",\"event\":\"boot_long\"}");
    step(100);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"last_end\":\"stopped\"") != NULL,
           "BOOT, even held, stops it");

    /* 0.0.16: a twin's new message on the clock face is its corner badge and one chime — never a
       pop-up, never a sound over the alarm. */
    run(OPEN("clock"));
    step(1200);
    const unsigned chimes = svc_audio_fake_chimes();
    const uint16_t unheard = svc_messages_unplayed_total();
    post_arrives();
    step(1200);
    snap = run("{\"cmd\":\"snapshot\"}");
    expect(svc_messages_unplayed_total() > unheard && strstr(snap, "\"overlay\":\"none\"") &&
               shell_open_app() && !strcmp(shell_open_app()->id, "clock"),
           "her new message waits on the face as its badge");
    expect(svc_audio_fake_chimes() == chimes + 1, "and chimes once");
    run("{\"cmd\":\"set\",\"name\":\"notif_chime\",\"value\":0}");
    s_post_new = false;
    svc_post_tag("0c0c0c0c");
    step(600);
    post_arrives();
    svc_post_tag("1d1d1d1d");
    step(600);
    expect(svc_audio_fake_chimes() == chimes + 1, "with the chime switched off it is silent");
    run("{\"cmd\":\"set\",\"name\":\"notif_chime\",\"value\":1}");
    return s_touch_bad;
}

int main(int argc, char **argv)
{
    const char *state = NULL, *png = NULL, *snap = NULL, *only = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--list")) {
            for (size_t k = 0; k < NSTATES; k++) printf("%s\n", STATES[k].name);
            return 0;
        }
        if (!strcmp(argv[i], "--lifecycle")) {
            boot(NULL);
            fixtures();
            return lifecycle();
        }
        if (!strcmp(argv[i], "--thread")) {
            boot(NULL);
            fixtures();
            return thread_checks();
        }
        if (!strcmp(argv[i], "--touch")) {
            boot(NULL);
            fixtures();
            return touch_checks();
        }
        if (i + 1 >= argc) break;
        if (!strcmp(argv[i], "--state")) state = argv[++i];
        else if (!strcmp(argv[i], "--png")) png = argv[++i];
        else if (!strcmp(argv[i], "--snapshot")) snap = argv[++i];
        else if (!strcmp(argv[i], "--only")) only = argv[++i];
    }
    if (!state) {
        fprintf(stderr, "usage: jpanel-sim --state NAME [--png F] [--snapshot F] [--only APP] | --list | --lifecycle | --touch | --thread\n");
        return 2;
    }
    boot(only);
    return render(state, png, snap);
}
