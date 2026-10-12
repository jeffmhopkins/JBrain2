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
#include "svc_alarms_fake.h"
#include "svc_audio_fake.h"
#include "svc_box_fake.h"
#include "svc_display.h"
#include "svc_display_fake.h"
#include "svc_messages_fake.h"
#include "svc_net_fake.h"
#include "svc_notice_fake.h"
#include "svc_power_fake.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"

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

static void step(uint32_t ms)
{
    for (uint32_t t = 0; t < ms; t += 10) {
        s_ms += 10;
        svc_rtc_fake_advance_ms(10);
        svc_audio_service();
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
    svc_audio_init();
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

static void ring(void)
{
    svc_rtc_fake_set(6, 45);
    svc_alarms_fake_ring(1);
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

static void session(void)
{
    msg_t m = {.id = 50, .voice = true, .dur_s = 4, .text = "Can we have pancakes?"};
    svc_messages_fake_add("tamsin", &m);
}

typedef struct {
    const char *name;
    void (*setup)(void);
    const char *cmds[6];
} state_t;

#define OPEN(a) "{\"cmd\":\"open\",\"app\":\"" a "\"}"
#define TAP(x, y) "{\"cmd\":\"inject\",\"event\":\"touch\",\"x\":" #x ",\"y\":" #y "}"
/* Not a debug command: the sim scrolls the open page to its end, as a finger would. */
#define SCROLL_END "scroll end"
#define SCROLL(px) "scroll " #px
#define KEY_DOWN "{\"cmd\":\"inject\",\"event\":\"key_down\"}"
#define KEY_UP "{\"cmd\":\"inject\",\"event\":\"key_up\"}"
#define WAIT(ms) "wait " #ms
/* Not a debug command: the fake box answers the hold in flight. */
#define REPLY "reply"

/* Mock boards 1-17 (docs/mocks/jpanel-c6/canvas.json), by the live panel's `start` names. */
static const state_t STATES[] = {
    {"home", NULL, {0}},
    {"home-p2", NULL, {"{\"cmd\":\"inject\",\"event\":\"swipe\",\"x\":400,\"y\":240,\"value\":-200}", WAIT(400)}},
    {"jerv", NULL, {OPEN("jerv")}},
    {"rec-jerv", NULL, {OPEN("jerv"), KEY_DOWN, WAIT(3400)}},
    {"quorra", NULL, {OPEN("quorra")}},
    {"rec-quorra", NULL, {OPEN("quorra"), KEY_DOWN, WAIT(4100)}},
    {"tamsin", NULL, {OPEN("tamsin")}},
    {"minecraft", NULL, {OPEN("minecraft")}},
    {"settings", NULL, {OPEN("settings")}},
    {"settings-end", NULL, {OPEN("settings"), SCROLL_END}},
    {"settings-usb", no_battery, {OPEN("settings")}},
    /* The first tap on Disconnect: rose, "Tap again to disconnect". */
    {"settings-armed", NULL, {OPEN("settings"), SCROLL_END, TAP(240, 418), WAIT(300)}},
    {"updating", downloading, {OPEN("settings"), SCROLL(680)}},
    {"clock", NULL, {OPEN("clock")}},
    {"clock-night", night, {OPEN("clock")}},
    {"clock-badge", night_badge, {OPEN("clock")}},
    {"clock-alarms", NULL, {OPEN("clock"), TAP(240, 240), WAIT(300)}},
    {"clock-alarms-end", NULL, {OPEN("clock"), TAP(240, 240), WAIT(800), SCROLL_END}},
    {"ringing", ring, {WAIT(100)}},
    {"notify-info", push_info, {WAIT(100)}},
    {"notify-question", push_question, {WAIT(100)}},
    /* KEY from Settings: the shell's talk to Jerv, over whatever is open. */
    {"rec-anywhere", NULL, {OPEN("settings"), KEY_DOWN, WAIT(3400)}},
    /* Its release: the Jerv thread, the hold's bubble waiting for its transcript. */
    {"jerv-after-key", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300)}},
    /* The box answers: the transcript, Jerv's words, and "Speaking…" while its voice plays. */
    {"jerv-reply", NULL, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), REPLY}},
    /* The same with "Read replies aloud" off: words only. */
    {"jerv-reply-text", text_only, {OPEN("clock"), KEY_DOWN, WAIT(2600), KEY_UP, WAIT(300), REPLY}},
    /* BOOT held from inside an app: the clock face (owner, 2026-10-12). */
    {"boot-long", NULL, {OPEN("settings"), "{\"cmd\":\"inject\",\"event\":\"boot_long\"}", WAIT(300)}},
    /* Not a mock board: a short session whose snapshot shows the debug surface. */
    {"session", session, {OPEN("quorra"), KEY_DOWN, WAIT(2000), KEY_UP, OPEN("tamsin"), WAIT(100)}},
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
    step(100);
    for (int i = 0; i < 6 && st->cmds[i]; i++) {
        if (!strncmp(st->cmds[i], "wait ", 5)) {
            step((uint32_t)atoi(st->cmds[i] + 5));
            continue;
        }
        if (!strcmp(st->cmds[i], REPLY)) {
            svc_audio_fake_reply(HEARD, REPLIED, svc_settings_get(SET_TTS) != 0);
            step(300);
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
    static const char *const IDS[] = {"jerv", "quorra", "tamsin", "minecraft", "clock", "settings"};
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
    unsigned before = svc_messages_count("jerv");
    run(KEY_DOWN);
    step(1500);
    run(KEY_UP);
    step(100);
    expect(shell_open_app() && !strcmp(shell_open_app()->id, "jerv"),
           "a real hold opens the Jerv thread on release");
    msg_t last = {0};
    svc_messages_get("jerv", (uint16_t)(svc_messages_count("jerv") - 1), &last);
    expect(svc_messages_count("jerv") == before + 1 && last.pending,
           "where the hold waits for its transcript, never a made-up one");
    svc_audio_fake_reply(HEARD, REPLIED, true);
    step(100);
    expect(strstr(run("{\"cmd\":\"snapshot\"}"), "\"replying\":true") != NULL,
           "the reply arrives and Jerv speaks it");
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
            return lifecycle();
        }
        if (!strcmp(argv[i], "--touch")) {
            boot(NULL);
            return touch_checks();
        }
        if (i + 1 >= argc) break;
        if (!strcmp(argv[i], "--state")) state = argv[++i];
        else if (!strcmp(argv[i], "--png")) png = argv[++i];
        else if (!strcmp(argv[i], "--snapshot")) snap = argv[++i];
        else if (!strcmp(argv[i], "--only")) only = argv[++i];
    }
    if (!state) {
        fprintf(stderr, "usage: jpanel-sim --state NAME [--png F] [--snapshot F] [--only APP] | --list | --lifecycle | --touch\n");
        return 2;
    }
    boot(only);
    return render(state, png, snap);
}
