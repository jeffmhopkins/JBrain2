#include "shell.h"

#include <stdio.h>
#include <string.h>

#include "dbg_core.h"
#include "shell_evq.h"
#include "svc_alarms.h"
#include "svc_display.h"
#include "svc_notice.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "ui_kit.h"

/* Buttons share the event queue with the services' events so their order is kept; PWR is
 * the shell's alone and never reaches an app. */
#define SH_EV_PWR ((app_event_type_t)APP_EV__COUNT)
#define APPS_MAX 8
#define RECENT 8

static app_t *s_apps[APPS_MAX];
static unsigned s_napps;
static const char *s_alarm_app = "clock";
static uint8_t s_pages = 1;

static sh_evq_t s_q;
static sh_state_t s_st;
static uint8_t s_page;
static app_t *s_open;
static lv_obj_t *s_home, *s_app_root, *s_notice, *s_waker;
static uint32_t s_notice_id;
static uint8_t s_notice_n;
static bool s_started;

static struct {
    app_event_type_t type;
    uint32_t at;
} s_recent[RECENT];
static unsigned s_recent_n;

static const char *const EV_NAMES[] = {"key_down", "key_up", "boot", "net_up", "net_down",
                                       "msg_arrived", "alarm_due", "notice_arrived",
                                       "tick_minute", "wake", "pwr"};

static void host_home(void);
static void host_toast(const char *t);
static const app_host_t HOST = {host_home, host_toast};

/* ---- registry ---- */

bool shell_register(app_t *app)
{
    if (s_napps >= APPS_MAX || s_started) return false;
    app->host = &HOST;
    s_apps[s_napps++] = app;
    return true;
}

void shell_set_alarm_app(const char *id)
{
    s_alarm_app = id;
}

static app_t *find(const char *id)
{
    for (unsigned i = 0; i < s_napps; i++)
        if (!strcmp(s_apps[i]->id, id)) return s_apps[i];
    return NULL;
}

const app_t *shell_open_app(void)
{
    return s_open;
}

/* ---- facts: gathered fresh for every decision, never cached ---- */

static sh_facts_t facts(void)
{
    sh_facts_t f = {0};
    f.alarm_ringing = svc_alarms_ringing(NULL);
    f.notices = svc_notice_count();
    f.app_open = s_open != NULL;
    f.page = s_page;
    f.dark = svc_display_dark();
    return f;
}

/* ---- home ---- */

static bool s_swiped;
static lv_point_t s_down;

static void tile_clicked(lv_event_t *e)
{
    if (s_swiped) return;
    app_t *a = lv_event_get_user_data(e);
    shell_open(a->id);
}

static void build_home(void);

static void home_input(lv_event_t *e)
{
    lv_indev_t *in = lv_indev_active();
    if (!in) return;
    lv_point_t p;
    lv_indev_get_point(in, &p);
    if (lv_event_get_code(e) == LV_EVENT_PRESSED) {
        s_down = p;
        s_swiped = false;
        return;
    }
    uint8_t next = sh_swipe(s_page, p.x - s_down.x, s_pages);
    if (next != s_page) {
        s_swiped = true;
        s_page = next;
        build_home();
    }
}

static void drop_home(void)
{
    if (s_home) lv_obj_delete(s_home);
    s_home = NULL;
}

static void build_home(void)
{
    drop_home();
    s_home = lv_obj_create(lv_screen_active());
    lv_obj_remove_style_all(s_home);
    lv_obj_set_size(s_home, UI_W, UI_H);
    lv_obj_remove_flag(s_home, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_home, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_home, home_input, LV_EVENT_PRESSED, NULL);
    lv_obj_add_event_cb(s_home, home_input, LV_EVENT_RELEASED, NULL);
    bool used[SH_SLOTS_PER_PAGE] = {false};
    for (unsigned i = 0; i < s_napps; i++) {
        app_t *a = s_apps[i];
        if (a->page != s_page) continue;
        ui_tile_state_t st = {0};
        if (a->tile_state) a->tile_state(a, &st);
        int x, y;
        sh_tile_pos(a->slot, &x, &y);
        ui_tile(s_home, x, y, a->title, a->icon, a->accent, &st, tile_clicked, a);
        used[a->slot] = true;
    }
    for (uint8_t s = 0; s < SH_SLOTS_PER_PAGE && s_page > 0; s++) {
        if (used[s]) continue;
        int x, y;
        sh_tile_pos(s, &x, &y);
        ui_tile_empty(s_home, x, y);
    }
    if (s_pages > 1) {
        lv_obj_t *dots = ui_box(s_home, LV_FLEX_FLOW_ROW, 12);
        lv_obj_align(dots, LV_ALIGN_BOTTOM_MID, 0, -14);
        for (uint8_t p = 0; p < s_pages; p++) {
            lv_obj_t *d = lv_obj_create(dots);
            lv_obj_remove_style_all(d);
            lv_obj_remove_flag(d, LV_OBJ_FLAG_CLICKABLE);
            lv_obj_set_size(d, 10, 10);
            lv_obj_set_style_radius(d, LV_RADIUS_CIRCLE, 0);
            lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
            lv_obj_set_style_bg_color(d, lv_color_hex(p == s_page ? UI_C_TEXT : UI_C_LINE_STRONG), 0);
        }
    }
}

/* ---- the one open app ---- */

static void close_current(void)
{
    if (!s_open) return;
    s_open->close(s_open);
    /* The shell deletes the root whatever the app did, so a widget an app forgot to free
     * cannot outlive it. Its timers are its own to delete; describe counts them. */
    lv_obj_delete(s_app_root);
    s_app_root = NULL;
    s_open = NULL;
    sh_app_closed(&s_st);
}

bool shell_open(const char *id)
{
    app_t *a = find(id);
    if (!a) return false;
    if (a == s_open) return true;
    close_current();
    drop_home();
    s_app_root = lv_obj_create(lv_screen_active());
    lv_obj_remove_style_all(s_app_root);
    lv_obj_set_size(s_app_root, UI_W, UI_H);
    lv_obj_remove_flag(s_app_root, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    s_open = a;
    svc_debug_log("shell", "open %s", id);
    a->open(a, s_app_root);
    return true;
}

void shell_home(void)
{
    close_current();
    build_home();
}

static void host_home(void)
{
    shell_home();
}

static void host_toast(const char *t)
{
    ui_toast(t, 2200);
}

/* ---- overlays: the notice, and the waker over a dark screen ---- */

static void notice_choice(lv_event_t *e)
{
    uint8_t choice = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    notice_t n;
    char t[64] = "Dismissed";
    if (svc_notice_head(&n) && n.n_choices)
        snprintf(t, sizeof t, "Sent to %s: %s", n.source, n.choices[choice]);
    svc_notice_answer(choice);
    ui_toast(t, 2200);
}

static void drop_notice(void)
{
    if (s_notice) lv_obj_delete(s_notice);
    s_notice = NULL;
    s_notice_id = 0;
}

static void build_notice(const notice_t *n, uint8_t count)
{
    drop_notice();
    s_notice = ui_overlay(lv_layer_top());
    lv_obj_set_flex_align(s_notice, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_top(s_notice, 40, 0);
    lv_obj_set_style_pad_hor(s_notice, 32, 0);
    lv_obj_set_style_pad_bottom(s_notice, 30, 0);
    lv_obj_set_style_pad_row(s_notice, 14, 0);
    uint32_t accent = n->urgent ? UI_C_WARN : UI_C_STEEL;
    lv_obj_t *head = ui_box(s_notice, LV_FLEX_FLOW_ROW, 10);
    lv_obj_set_width(head, LV_PCT(100));
    char from[40];
    snprintf(from, sizeof from, LV_SYMBOL_BELL "  %s%s", n->source, n->urgent ? " - urgent" : "");
    lv_obj_t *src = ui_label(head, from, 18, accent);
    lv_obj_set_flex_grow(src, 1);
    if (count > 1) {
        char c[12];
        snprintf(c, sizeof c, "1 of %u", count);
        ui_label(head, c, 16, UI_C_MUTED);
    }
    ui_label_wrap(s_notice, n->title, 30, UI_C_TEXT);
    lv_obj_t *body = ui_label_wrap(s_notice, n->body, 22, UI_C_TEXT_SOFT);
    lv_obj_set_flex_grow(body, 1);
    lv_obj_t *row = ui_box(s_notice, LV_FLEX_FLOW_ROW, 10);
    lv_obj_set_width(row, LV_PCT(100));
    uint8_t k = n->n_choices ? n->n_choices : 1;
    for (uint8_t i = 0; i < k; i++) {
        lv_obj_t *b = ui_button(row, n->n_choices ? n->choices[i] : "OK",
                                i == 0 ? UI_BTN_PRIMARY : UI_BTN_SECONDARY, true, notice_choice,
                                (void *)(uintptr_t)i);
        lv_obj_set_height(b, UI_CHOICE_H);
        lv_obj_set_width(b, LV_SIZE_CONTENT);
        lv_obj_set_flex_grow(b, 1);
        lv_obj_set_style_radius(b, UI_R_CHOICE, 0);
    }
    lv_obj_t *hint = ui_label(s_notice,
                              n->n_choices ? "Your answer goes back to the sender - BOOT answers nothing"
                                           : "BOOT dismisses",
                              16, UI_C_MUTED);
    lv_obj_set_width(hint, LV_PCT(100));
    lv_obj_set_style_text_align(hint, LV_TEXT_ALIGN_CENTER, 0);
    s_notice_id = n->id;
    s_notice_n = count;
}

static void waker_pressed(lv_event_t *e)
{
    (void)e;
    shell_button(SH_BTN_PWR);
}

/* One decision draws the overlay and arms its hit area together: what is on the glass is what
 * a press reaches (the twins' notice was once painted in one rectangle and armed in another). */
static void sync_overlays(void)
{
    sh_facts_t f = facts();
    notice_t n;
    if (sh_top_overlay(&s_st, &f) == SH_OV_NOTICE && svc_notice_head(&n)) {
        if (n.id != s_notice_id || f.notices != s_notice_n) build_notice(&n, f.notices);
    } else {
        drop_notice();
    }
    if (f.dark && !s_waker) {
        s_waker = lv_obj_create(lv_layer_sys());
        lv_obj_remove_style_all(s_waker);
        lv_obj_set_size(s_waker, UI_W, UI_H);
        lv_obj_add_flag(s_waker, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(s_waker, waker_pressed, LV_EVENT_PRESSED, NULL);
    } else if (!f.dark && s_waker) {
        lv_obj_delete(s_waker);
        s_waker = NULL;
    }
}

/* ---- input ---- */

static void set_dark(bool dark)
{
    svc_display_set_dark(dark);
    if (!dark) svc_display_set_brightness((uint8_t)svc_settings_get(SET_BRIGHTNESS));
}

static bool offer(app_event_type_t type)
{
    if (!s_open || !s_open->on_event) return false;
    app_event_t ev = {.type = type, .at_ms = svc_rtc_uptime_ms()};
    return s_open->on_event(s_open, &ev);
}

static void act(sh_act_t a)
{
    char hm[32];
    switch (a) {
    case SH_ACT_DISMISS_NOTICE:
        svc_notice_dismiss();
        ui_toast("Dismissed - no answer sent", 2200);
        break;
    case SH_ACT_SNOOZE_ALARM: {
        uint16_t at = svc_alarms_snooze();
        snprintf(hm, sizeof hm, "Snoozed until %02u:%02u", at / 60, at % 60);
        ui_toast(hm, 2200);
        break;
    }
    case SH_ACT_STOP_ALARM:
        svc_alarms_stop();
        ui_toast("Alarm stopped", 2200);
        break;
    case SH_ACT_GO_HOME: s_page = 0; shell_home(); break;
    case SH_ACT_PAGE_FIRST: s_page = 0; build_home(); break;
    case SH_ACT_HINT_TALK: ui_toast("Open a conversation, then hold KEY", 2200); break;
    case SH_ACT_WAKE: set_dark(false); break;
    case SH_ACT_DARK: set_dark(true); break;
    default: break;
    }
}

static void handle_button(sh_button_t b, app_event_type_t as)
{
    sh_facts_t f = facts();
    sh_route_t r = sh_route_button(&s_st, &f, b);
    if (r.first == SH_ACT_TO_APP) {
        if (!offer(as)) act(r.fallback);
    } else {
        act(r.first);
    }
}

static void handle_event(const app_event_t *ev)
{
    sh_facts_t f = facts();
    unsigned todo = sh_route_event(&f, ev->type);
    if (todo & SH_DO_WAKE) set_dark(false);
    if (todo & SH_DO_OPEN_ALARM_APP) shell_open(s_alarm_app);
    if ((todo & SH_DO_DELIVER) && s_open && s_open->on_event) s_open->on_event(s_open, ev);
    if ((todo & SH_DO_REFRESH_TILES) && s_home) build_home();
}

static void remember(app_event_type_t type)
{
    s_recent[s_recent_n % RECENT].type = type;
    s_recent[s_recent_n % RECENT].at = svc_rtc_uptime_ms();
    s_recent_n++;
}

void shell_pump(void)
{
    app_event_t ev;
    while (sh_evq_pop(&s_q, &ev)) {
        remember(ev.type);
        switch ((int)ev.type) {
        case APP_EV_KEY_DOWN: handle_button(SH_BTN_KEY_DOWN, ev.type); break;
        case APP_EV_KEY_UP: handle_button(SH_BTN_KEY_UP, ev.type); break;
        case APP_EV_BOOT: handle_button(SH_BTN_BOOT, ev.type); break;
        case SH_EV_PWR: handle_button(SH_BTN_PWR, ev.type); break;
        default: handle_event(&ev); break;
        }
        sync_overlays();
    }
    sync_overlays();
}

void shell_post(const app_event_t *ev)
{
    sh_evq_push(&s_q, ev);
}

void shell_button(sh_button_t b)
{
    static const app_event_type_t MAP[] = {APP_EV_KEY_DOWN, APP_EV_KEY_UP, APP_EV_BOOT, SH_EV_PWR};
    app_event_t ev = {.type = MAP[b], .at_ms = svc_rtc_uptime_ms()};
    sh_evq_push(&s_q, &ev);
}

/* ---- the virtual pointer ---- */

static struct {
    int x, y, dx;
    int step; /* -1 idle; then press, drag, release */
} s_touch = {0, 0, 0, -1};

#define DRAG_STEPS 6

static void touch_read(lv_indev_t *in, lv_indev_data_t *d)
{
    (void)in;
    if (s_touch.step < 0) {
        d->state = LV_INDEV_STATE_RELEASED;
        return;
    }
    int s = s_touch.step++;
    int moved = s < 2 ? 0 : s_touch.dx * (s - 1 > DRAG_STEPS ? DRAG_STEPS : s - 1) / DRAG_STEPS;
    d->point.x = s_touch.x + moved;
    d->point.y = s_touch.y;
    d->state = s < DRAG_STEPS + 3 ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
    if (d->state == LV_INDEV_STATE_RELEASED) s_touch.step = -1;
}

void shell_touch(int x, int y, int dx)
{
    s_touch.x = x;
    s_touch.y = y;
    s_touch.dx = dx;
    s_touch.step = 0;
}

/* ---- debug ---- */

static unsigned count_tree(lv_obj_t *o)
{
    unsigned n = 1;
    for (uint32_t i = 0; i < lv_obj_get_child_count(o); i++) n += count_tree(lv_obj_get_child(o, (int32_t)i));
    return n;
}

unsigned shell_obj_count(void)
{
    return count_tree(lv_screen_active()) + count_tree(lv_layer_top()) + count_tree(lv_layer_sys());
}

unsigned shell_timer_count(void)
{
    unsigned n = 0;
    for (lv_timer_t *t = lv_timer_get_next(NULL); t; t = lv_timer_get_next(t)) n++;
    return n;
}

static const char *const OVERLAY[] = {"none", "notice", "alarm"};

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    sh_facts_t f = facts();
    dbg_json_str(w, "open", s_open ? s_open->id : "home");
    dbg_json_int(w, "page", s_page);
    dbg_json_str(w, "overlay", OVERLAY[sh_top_overlay(&s_st, &f)]);
    dbg_json_bool(w, "dark", f.dark);
    dbg_json_str(w, "key_owner", s_st.key_owner == SH_KEY_APP ? "app"
                                 : s_st.key_owner == SH_KEY_ALARM ? "alarm" : "free");
    dbg_json_int(w, "queue_depth", s_q.count);
    dbg_json_int(w, "queue_dropped", s_q.dropped);
    dbg_json_int(w, "objects", shell_obj_count());
    dbg_json_int(w, "timers", shell_timer_count());
    dbg_json_arr_begin(w, "recent");
    unsigned from = s_recent_n > RECENT ? s_recent_n - RECENT : 0;
    for (unsigned i = from; i < s_recent_n; i++) {
        dbg_json_obj_begin(w, NULL);
        dbg_json_str(w, "ev", EV_NAMES[s_recent[i % RECENT].type]);
        dbg_json_int(w, "at", s_recent[i % RECENT].at);
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
    if (s_open && s_open->describe) {
        dbg_json_obj_begin(w, "app");
        s_open->describe(s_open, w);
        dbg_json_obj_end(w);
    }
}

static bool cmd_open(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    if (!shell_open(r->app)) {
        snprintf(err, n, "no app %s", r->app);
        return false;
    }
    dbg_json_str(d, "open", r->app);
    return true;
}

static bool cmd_home(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    (void)r, (void)err, (void)n;
    s_page = 0;
    shell_home();
    dbg_json_str(d, "open", "home");
    return true;
}

static bool cmd_inject(const dbg_req_t *r, dbg_json_t *d, char *err, size_t n)
{
    static const struct {
        const char *name;
        sh_button_t b;
    } BTN[] = {{"key_down", SH_BTN_KEY_DOWN}, {"key_up", SH_BTN_KEY_UP}, {"boot", SH_BTN_BOOT},
               {"pwr", SH_BTN_PWR}};
    for (size_t i = 0; i < sizeof BTN / sizeof *BTN; i++)
        if (!strcmp(r->event, BTN[i].name)) {
            shell_button(BTN[i].b);
            dbg_json_str(d, "queued", r->event);
            return true;
        }
    if (!strcmp(r->event, "touch") || !strcmp(r->event, "swipe")) {
        if (r->x < 0 || r->x >= UI_W || r->y < 0 || r->y >= UI_H) {
            snprintf(err, n, "off the glass");
            return false;
        }
        shell_touch(r->x, r->y, !strcmp(r->event, "swipe") ? r->value : 0);
        dbg_json_str(d, "queued", r->event);
        return true;
    }
    snprintf(err, n, "unknown event");
    return false;
}

static void sink(const app_event_t *ev)
{
    shell_post(ev);
}

bool shell_start(char *err, size_t err_len)
{
    sh_place_t places[APPS_MAX];
    for (unsigned i = 0; i < s_napps; i++)
        places[i] = (sh_place_t){s_apps[i]->id, s_apps[i]->page, s_apps[i]->slot};
    if (!sh_registry_check(places, s_napps, err, (unsigned)err_len)) return false;
    s_pages = sh_pages_used(places, s_napps);
    sh_evq_init(&s_q);
    app_events_set_sink(sink);
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(UI_C_BG), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_indev_t *in = lv_indev_create();
    lv_indev_set_type(in, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(in, touch_read);
    svc_debug_register("shell", describe, NULL);
    dbg_cmd_bind("open", cmd_open);
    dbg_cmd_bind("home", cmd_home);
    dbg_cmd_bind("inject", cmd_inject);
    s_started = true;
    build_home();
    return true;
}
