/* Host suite for the panel's pure modules. One behaviour per test, named as a sentence; every
 * CHECK message says what is true. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_event.h"
#include "clock_model.h"
#include "dbg_core.h"
#include "dbg_json.h"
#include "jerv_model.h"
#include "minutes.h"
#include "notice_policy.h"
#include "settings_model.h"
#include "shell_core.h"
#include "shell_evq.h"
#include "svc_box_fake.h"
#include "svc_net_fake.h"
#include "svc_notice_fake.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"
#include "thread_model.h"
#include "ui_pin.h"
#include "ui_theme.h"

static int s_checks, s_fails;

#define CHECK(cond, msg)                                                          \
    do {                                                                          \
        s_checks++;                                                               \
        if (!(cond)) {                                                            \
            s_fails++;                                                            \
            fprintf(stderr, "FAIL %s:%d %s: %s\n", __FILE__, __LINE__, __func__, msg); \
        }                                                                         \
    } while (0)

/* ---- shell ---- */

static sh_facts_t home(void)
{
    return (sh_facts_t){0};
}

static sh_facts_t in_app(void)
{
    sh_facts_t f = {0};
    f.app_open = true;
    return f;
}

static void test_an_alarm_outranks_a_notice_which_outranks_the_app(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NONE, "an app alone has no overlay");
    f.notices = 2;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NOTICE, "a notice covers the app");
    f.alarm_ringing = true;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_ALARM, "an alarm covers the notice");
}

static void test_a_notice_waits_for_a_hold_to_end(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_TO_APP, "KEY goes to the open app");
    f.notices = 1;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NONE, "a notice arriving mid-hold stays under the talk ring");
    r = sh_route_button(&st, &f, SH_BTN_KEY_UP);
    CHECK(r.first == SH_ACT_TO_APP, "the release still reaches the app that took the press");
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NOTICE, "and the notice shows the moment KEY is up");
}

static void test_key_up_follows_its_key_down_whatever_arrives_between(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    f.dark = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_TO_APP,
          "a release after the screen went dark still ends the recording");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_NONE,
          "a second release with no press behind it goes nowhere");
    st.key_owner = SH_KEY_FREE;
    f = in_app();
    sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    sh_app_closed(&st);
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_NONE,
          "an app that closed mid-hold is not handed the release of a press it lost");
}

static void test_boot_dismisses_a_notice_and_key_does_not_reach_under_it(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.notices = 1;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_DISMISS_NOTICE, "BOOT dismisses");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_NONE,
          "KEY does not start a recording under a notice");
    CHECK(st.key_owner == SH_KEY_FREE, "and so owns no release");
}

static void test_the_alarm_takes_both_buttons(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.alarm_ringing = true;
    f.notices = 1;
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_TO_APP && r.fallback == SH_ACT_SNOOZE_ALARM, "KEY snoozes");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_NONE,
          "the snooze press's release is not a second action");
    r = sh_route_button(&st, &f, SH_BTN_BOOT);
    CHECK(r.first == SH_ACT_TO_APP && r.fallback == SH_ACT_STOP_ALARM, "BOOT stops, not dismisses");
}

static void test_boot_is_back_then_home(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_BOOT);
    CHECK(r.first == SH_ACT_TO_APP && r.fallback == SH_ACT_GO_HOME, "the app may use BOOT as back");
    f = home();
    f.page = 1;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_PAGE_FIRST, "BOOT on page 2 goes to page 1");
    f.page = 0;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_NONE, "BOOT on page 1 does nothing");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_HINT_TALK,
          "KEY on home says to open a conversation");
}

static void test_a_dark_screen_only_wakes(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.dark = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_WAKE, "KEY wakes and records nothing");
    CHECK(st.key_owner == SH_KEY_FREE, "so its release goes nowhere");
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_WAKE, "BOOT wakes and does not go home");
    CHECK(sh_route_button(&st, &f, SH_BTN_PWR).first == SH_ACT_WAKE, "PWR wakes");
    f.dark = false;
    CHECK(sh_route_button(&st, &f, SH_BTN_PWR).first == SH_ACT_DARK, "PWR darkens");
}

static void test_events_route_as_the_plan_says(void)
{
    sh_facts_t f = home();
    unsigned a = sh_route_event(&f, APP_EV_ALARM_DUE);
    CHECK((a & SH_DO_WAKE) && (a & SH_DO_OPEN_ALARM_APP) && (a & SH_DO_DELIVER),
          "an alarm wakes, opens Clock and tells it");
    a = sh_route_event(&f, APP_EV_NOTICE_ARRIVED);
    CHECK((a & SH_DO_WAKE) && (a & SH_DO_REFRESH_OVERLAY) && !(a & SH_DO_DELIVER),
          "a notice wakes and is the shell's, not the app's");
    CHECK(sh_route_event(&f, APP_EV_MSG_ARRIVED) & SH_DO_REFRESH_TILES, "a message refreshes badges");
    CHECK(sh_route_event(&f, APP_EV_KEY_DOWN) == 0, "a button is never routed as an event");
}

static void test_tiles_fill_the_glass_without_touching(void)
{
    int x[4], y[4];
    for (uint8_t s = 0; s < 4; s++) sh_tile_pos(s, &x[s], &y[s]);
    CHECK(x[0] == 36 && y[0] == 28, "the first tile sits where the mock puts it");
    CHECK(x[1] - x[0] == UI_TILE_W + UI_TILE_GAP && y[2] - y[0] == UI_TILE_H + UI_TILE_GAP,
          "tiles step by size plus the 16 px gap");
    CHECK(x[3] + UI_TILE_W <= UI_W && y[3] + UI_TILE_H <= UI_H - 24, "the last tile clears the page dots");
}

static void test_a_swipe_needs_sixty_pixels_and_stops_at_the_ends(void)
{
    CHECK(sh_swipe(0, -59, 2) == 0, "59 px is a tap that wobbled");
    CHECK(sh_swipe(0, -60, 2) == 1, "60 px left is the next page");
    CHECK(sh_swipe(1, -200, 2) == 1, "there is no page after the last");
    CHECK(sh_swipe(1, 80, 2) == 0, "right goes back");
    CHECK(sh_swipe(0, 80, 2) == 0, "and stops at the first");
}

static void test_the_registry_refuses_clashes(void)
{
    char err[80];
    sh_place_t ok[] = {{"jerv", 0, 0}, {"minecraft", 0, 1}, {"clock", 1, 0}};
    CHECK(sh_registry_check(ok, 3, err, sizeof err), "the shipped layout is valid");
    CHECK(sh_pages_used(ok, 3) == 2, "two pages are in use");
    sh_place_t slot[] = {{"a", 0, 0}, {"b", 0, 0}};
    CHECK(!sh_registry_check(slot, 2, err, sizeof err) && strstr(err, "share"), "two apps in one slot");
    sh_place_t id[] = {{"a", 0, 0}, {"a", 0, 1}};
    CHECK(!sh_registry_check(id, 2, err, sizeof err) && strstr(err, "two apps"), "two apps with one id");
    sh_place_t off[] = {{"a", 0, 4}};
    CHECK(!sh_registry_check(off, 1, err, sizeof err), "a slot off the 2x2 grid");
}

static void test_the_event_queue_is_fifo_and_counts_what_it_drops(void)
{
    sh_evq_t q;
    sh_evq_init(&q);
    app_event_t e = {0}, out;
    for (int i = 0; i < SH_EVQ_DEPTH + 3; i++) {
        e.at_ms = (uint32_t)i;
        sh_evq_push(&q, &e);
    }
    CHECK(q.dropped == 3, "three over depth are counted, not lost silently");
    sh_evq_pop(&q, &out);
    CHECK(out.at_ms == 0, "oldest first");
    e.at_ms = 99;
    CHECK(sh_evq_push(&q, &e), "room again after a pop");
    unsigned n = 1;
    while (sh_evq_pop(&q, &out)) n++;
    CHECK(n == SH_EVQ_DEPTH + 1 && out.at_ms == 99, "every kept event comes out, in order, across the wrap");
}

/* ---- clock ---- */

static alarm_t al(uint8_t id, uint8_t h, uint8_t m, uint8_t days, bool on)
{
    alarm_t a = {.id = id, .h = h, .m = m, .days = days, .on = on};
    return a;
}

static void test_the_clock_is_twenty_four_hour(void)
{
    char s[6];
    clock_fmt_hm(6, 5, s);
    CHECK(!strcmp(s, "06:05"), "leading zeros");
    clock_fmt_hm(21, 30, s);
    CHECK(!strcmp(s, "21:30"), "no am/pm");
}

static void test_next_alarm_honours_days_and_wraps(void)
{
    uint8_t idx;
    uint32_t wait;
    alarm_t a[] = {al(1, 6, 45, 0x1F, true), al(2, 8, 30, 0x60, false), al(3, 21, 0, 0x7F, true)};
    CHECK(clock_next_alarm(a, 3, 22 * 60, 2, &idx, &wait) && idx == 0 && wait == 8 * 60 + 45,
          "Wednesday 22:00: the workday alarm, past midnight");
    CHECK(clock_next_alarm(a, 3, 22 * 60, 4, &idx, &wait) && idx == 2 && wait == 23 * 60,
          "Friday 22:00: no workday tomorrow, so 21:00 Saturday");
    CHECK(clock_next_alarm(a, 3, 21 * 60, 2, &idx, &wait) && idx == 0,
          "exactly 21:00 is ringing now, not next");
    alarm_t once[] = {al(4, 7, 0, 0, true)};
    CHECK(clock_next_alarm(once, 1, 8 * 60, 6, &idx, &wait) && wait == 23 * 60,
          "a one-off already past rings tomorrow");
    alarm_t sunday[] = {al(5, 9, 0, 1 << 6, true)};
    CHECK(clock_next_alarm(sunday, 1, 10 * 60, 6, &idx, &wait) && wait == 7 * 1440 - 60,
          "Sunday-only, missed this Sunday: a week less an hour");
    alarm_t off[] = {al(6, 9, 0, 0x7F, false)};
    CHECK(!clock_next_alarm(off, 1, 0, 0, &idx, &wait), "an alarm switched off is never next");
}

static void test_alarm_text_reads_like_the_mock(void)
{
    char s[64];
    alarm_t a = al(1, 6, 45, 0x1F, true);
    clock_fmt_next(&a, 9 * 60 + 4, s, sizeof s);
    CHECK(!strcmp(s, "Next 06:45 - in 9 h 4 min"), "hours and minutes");
    clock_fmt_next(&a, 12, s, sizeof s);
    CHECK(!strcmp(s, "Next 06:45 - in 12 min"), "minutes alone under an hour");
    clock_fmt_days(0x1F, "Work", s, sizeof s);
    CHECK(!strcmp(s, "Weekdays - Work"), "weekdays with a label");
    clock_fmt_days(0x60, "", s, sizeof s);
    CHECK(!strcmp(s, "Sat, Sun"), "a weekend lists its days");
    clock_fmt_days(0x7F, NULL, s, sizeof s);
    CHECK(!strcmp(s, "Every day"), "every day");
    clock_fmt_days(0, NULL, s, sizeof s);
    CHECK(!strcmp(s, "Once"), "no days is once");
}

static void test_snooze_is_nine_minutes_across_midnight(void)
{
    CHECK(clock_snooze(6, 45, 9) == 6 * 60 + 54, "06:45 snoozes to 06:54");
    CHECK(clock_snooze(23, 55, 9) == 4, "23:55 snoozes to 00:04");
}

static void test_the_night_face_wraps_past_midnight(void)
{
    uint16_t from = 21 * 60 + 30, to = 6 * 60 + 30;
    CHECK(clock_face(true, from, to, 0, 23 * 60, false).night, "23:00 is night");
    CHECK(clock_face(true, from, to, 0, 3 * 60, false).night, "03:00 is night");
    CHECK(!clock_face(true, from, to, 0, 6 * 60 + 30, false).night, "06:30 is morning: the end is open");
    CHECK(!clock_face(true, from, to, 0, 21 * 60 + 29, false).night, "21:29 is still day");
    CHECK(!clock_face(false, from, to, 0, 23 * 60, false).night, "switched off, it is never night");
    CHECK(clock_face(false, from, to, 1, 12 * 60, true).color == 0xA47A3E, "forced amber is amber");
    CHECK(clock_face(true, from, to, 9, 23 * 60, false).color == 0xB23A3A, "a bad colour falls back to red");
    CHECK(clock_face(true, 600, 600, 0, 600, false).night == false, "an empty window is never night");
    CHECK(min_window_contains(600, 540, 720) && !min_window_contains(720, 540, 720),
          "a same-day window is [from, to)");
}

/* ---- notice policy (plan §2's table) ---- */

static void test_the_notice_table(void)
{
    notice_report_t r = {true, true, 21 * 60 + 30, 6 * 60 + 30};
    uint16_t until = 0;
    CHECK(notice_policy(&r, 12 * 60, 2, &until) == NOTICE_ACCEPT, "noon, online, accepting: queued");
    CHECK(notice_http_status(NOTICE_ACCEPT) == 202, "as 202");
    CHECK(notice_policy(&r, 23 * 60, 2, &until) == NOTICE_REFUSE_QUIET_HOURS && until == 6 * 60 + 30,
          "23:00 is quiet hours, until 06:30");
    CHECK(notice_http_status(NOTICE_REFUSE_QUIET_HOURS) == 409 &&
              !strcmp(notice_reason(NOTICE_REFUSE_QUIET_HOURS), "quiet_hours"),
          "as 409 quiet_hours");
    r.quiet = false;
    CHECK(notice_policy(&r, 23 * 60, 2, &until) == NOTICE_ACCEPT, "quiet switched off: night is fine");
    r.accept = false;
    CHECK(notice_policy(&r, 12 * 60, 2, &until) == NOTICE_REFUSE_NOT_ACCEPTING, "switched off: not accepting");
    CHECK(notice_policy(&r, 12 * 60, 30, &until) == NOTICE_REFUSE_OFFLINE,
          "30 s without a poll is offline, before anything the stale report says");
    CHECK(notice_http_status(NOTICE_REFUSE_OFFLINE) == 503, "as 503");
    r.accept = true;
    CHECK(notice_policy(&r, 12 * 60, 29, &until) == NOTICE_ACCEPT, "29 s is still online");
}

static void test_urgent_always_chimes(void)
{
    CHECK(notice_chime(true, false, false), "chime on, daytime: chimes");
    CHECK(!notice_chime(true, false, true), "night hours: silent");
    CHECK(!notice_chime(false, false, false), "chime off: silent");
    CHECK(notice_chime(false, true, true), "urgent: chimes even off, even at night");
}

static void test_the_panel_reports_what_the_box_decides_from(void)
{
    svc_settings_set(SET_NOTIF_ACCEPT, 0);
    notice_report_t r;
    svc_notice_report(&r);
    CHECK(!r.accept && r.from == svc_settings_get(SET_NIGHT_FROM), "the report carries the switch and the window");
    svc_settings_set(SET_NOTIF_ACCEPT, 1);
}

/* ---- settings ---- */

static void test_brightness_never_goes_below_ten(void)
{
    CHECK(svc_settings_clamp(SET_BRIGHTNESS, 0) == 10, "zero would look like the blanking fault");
    CHECK(svc_settings_clamp(SET_BRIGHTNESS, -40) == 10, "negative is the floor");
    CHECK(svc_settings_clamp(SET_BRIGHTNESS, 140) == 100, "and 100 the ceiling");
    svc_settings_set(SET_BRIGHTNESS, 3);
    CHECK(svc_settings_get(SET_BRIGHTNESS) == 10, "a write below the floor is clamped by the service itself");
    CHECK(settings_brightness_from_slider(12) == 10 && settings_brightness_from_slider(13) == 15,
          "the slider snaps to 5 %");
    CHECK(settings_brightness_from_slider(7) == 10, "and a snap below the floor is lifted to it");
    CHECK(svc_settings_clamp(SET_NIGHT_COLOR, 7) == 2 && svc_settings_clamp(SET_TTS, 5) == 1,
          "every key is clamped, not just brightness");
}

static void test_settings_cards_say_what_the_mock_says(void)
{
    net_info_t n = {NET_CONNECTED, "x", -58, "", 0};
    settings_wifi_t w;
    settings_wifi(&n, &w);
    CHECK(!strcmp(w.button, "Disconnect") && w.button_live && w.bars == 3, "connected, three bars");
    n.state = NET_CONNECTING;
    settings_wifi(&n, &w);
    CHECK(!w.button_live, "nothing to press while connecting");
    fw_info_t fw = {"0.0.1", "0.0.2", FW_DOWNLOADING, 64};
    char note[96];
    settings_fw_t f;
    settings_fw(&fw, true, &f, note, sizeof note);
    CHECK(!f.button_live && f.show_bar && strstr(note, "64%"), "downloading: bar up, button dead");
    fw.phase = FW_IDLE;
    settings_fw(&fw, false, &f, note, sizeof note);
    CHECK(!f.button_live && strstr(note, "Connect Wi-Fi"), "offline: Update now is dead and says why");
    power_info_t p = {.fitted = true, .pct = 12, .mv = 3520};
    settings_power_t pv;
    settings_power(&p, &pv);
    CHECK(pv.low && !strcmp(pv.big, "12%") && strstr(pv.line, "plug in"), "low battery turns amber");
    p.fitted = false;
    p.usb_mv = 5020;
    settings_power(&p, &pv);
    CHECK(!pv.bar && !strcmp(pv.title, "Power") && strstr(pv.line, "5.02 V"), "no battery: USB");
    char s[200];
    settings_notif_note(true, true, 21 * 60 + 30, 6 * 60 + 30, s, sizeof s);
    CHECK(strstr(s, "21:30-06:30") != NULL, "the quiet note names the window");
    CHECK(!settings_notif_row_live(1, false) && settings_notif_row_live(0, false),
          "with messages refused only the accept switch is live");
}

/* ---- conversations ---- */

static void test_conversation_rules(void)
{
    msg_t theirs = {.id = 4, .voice = true, .dur_s = 65};
    thread_bubble_t b;
    thread_bubble(&theirs, 0, 0, &b);
    CHECK(b.unplayed && b.prog == 0 && !strcmp(b.dur, "1:05"), "unheard: dot, empty bar");
    thread_bubble(&theirs, 4, 40, &b);
    CHECK(!b.unplayed && b.playing && b.prog == 40, "the dot goes the moment it starts");
    msg_t reply = {.id = 9};
    CHECK(jerv_shows_listen(&reply, false, 0), "text only: a reply has Listen");
    CHECK(!jerv_shows_listen(&reply, true, 0), "read aloud: no Listen");
    CHECK(!jerv_shows_listen(&reply, false, 9), "not while it is being spoken");
    CHECK(talk_policy_end(599, true) == TALK_TOO_SHORT && talk_policy_end(600, true) == TALK_SENT,
          "under 0.6 s is a knock, not a message");
    CHECK(talk_policy_end(5000, false) == TALK_CANCELLED, "a tap cancels whatever the length");
    CHECK(talk_policy_end(60000, true) == TALK_CAPPED, "the minute cap sends");
    ui_pin_t pin = {0};
    CHECK(ui_pin_should(&pin, 5), "opening pins to the newest");
    CHECK(!ui_pin_should(&pin, 5), "a redraw does not yank the reader down");
    CHECK(ui_pin_should(&pin, 6), "a new message does");
}

/* ---- debug ---- */

static void test_the_writer_escapes_and_refuses_a_truncated_document(void)
{
    char buf[128];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_json_str(&w, "q", "a\"b\\c\n");
    dbg_json_arr_begin(&w, "n");
    dbg_json_int(&w, NULL, -3);
    dbg_json_bool(&w, NULL, true);
    dbg_json_arr_end(&w);
    dbg_json_obj_end(&w);
    CHECK(!strcmp(dbg_json_text(&w), "{\"q\":\"a\\\"b\\\\c\\u000a\",\"n\":[-3,true]}"), "valid JSON out");
    char small[16];
    dbg_json_init(&w, small, sizeof small);
    dbg_json_obj_begin(&w, NULL);
    dbg_json_str(&w, "long", "more than sixteen bytes");
    dbg_json_obj_end(&w);
    CHECK(w.overflow && !strcmp(dbg_json_text(&w), ""), "an overflow yields nothing, never half a document");
}

static void test_secret_keys_are_never_written(void)
{
    const char *deny[] = {"pass", "PASS", "token", "ca", "key", "device_key", "wifi_pass",
                          "box_token", "Authorization", "secret", "psk"};
    for (size_t i = 0; i < sizeof deny / sizeof *deny; i++)
        CHECK(dbg_json_key_is_secret(deny[i]), deny[i]);
    const char *allow[] = {"ssid", "api", "name", "role", "brightness", "cache", "keyboard"};
    for (size_t i = 0; i < sizeof allow / sizeof *allow; i++)
        CHECK(!dbg_json_key_is_secret(allow[i]), allow[i]);
    char buf[128];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_json_str(&w, "pass", "hunter22");
    dbg_json_int(&w, "token", 1234);
    dbg_json_obj_end(&w);
    CHECK(!strstr(buf, "hunter22") && !strstr(buf, "1234") && strstr(buf, DBG_REDACTED),
          "a secret key's value, string or number, is replaced");
}

static int contains_any_secret(const char *s)
{
    return strstr(s, SETTINGS_FAKE_PASS) || strstr(s, SETTINGS_FAKE_TOKEN) || strstr(s, SETTINGS_FAKE_CA) ||
           strstr(s, "fakeboxroot") || strstr(s, "0123456789");
}

static void test_a_snapshot_never_contains_a_secret(void)
{
    dbg_registry_reset();
    dbg_log_reset();
    dbg_secret_clear();
    svc_settings_init();
    svc_rtc_init();
    svc_net_init();
    svc_box_init();
    svc_notice_init();
    dbg_sys_init();
    /* A careless log line, the way a token actually leaks. */
    svc_debug_log("net", "auth header Bearer %s", SETTINGS_FAKE_TOKEN);
    svc_debug_log("net", "joined %s", "[Your network]");
    static char buf[8192];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_snapshot(&w);
    const char *snap = dbg_json_text(&w);
    CHECK(snap[0] == '{', "the snapshot is a whole document");
    CHECK(strstr(snap, "\"system\":{\"platform\":\"host\"") != NULL, "the system describer is in it");
    CHECK(!contains_any_secret(snap), "no password, device key or CA in the snapshot");
    CHECK(strstr(snap, "\"ssid\":\"[Your network]\"") && strstr(snap, "\"pass\":\"" DBG_REDACTED "\""),
          "while the network name is there and the password is visibly redacted");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"r1\",\"cmd\":\"logs\"}", &w);
    const char *logs = dbg_json_text(&w);
    CHECK(!contains_any_secret(logs), "a token written into a log line is scrubbed from the dump");
    CHECK(strstr(logs, "joined [Your network]") != NULL, "the line beside it survives");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"r2\",\"cmd\":\"snapshot\"}", &w);
    CHECK(!contains_any_secret(dbg_json_text(&w)) && strstr(buf, "\"settings\":{"),
          "the snapshot command is the same snapshot, just as clean");
}

static void test_requests_parse_flat_and_nothing_else(void)
{
    dbg_req_t r;
    CHECK(dbg_req_parse("{\"id\":\"a1\",\"cmd\":\"inject\",\"event\":\"swipe\",\"x\":400,\"y\":-2,"
                        "\"value\":-120,\"confirm\":true,\"later\":\"x\",\"n\":5}",
                        &r) == DBG_PARSE_OK,
          "a flat object parses, unknown keys skipped");
    CHECK(!strcmp(r.cmd, "inject") && r.x == 400 && r.y == -2 && r.value == -120 && r.confirm,
          "and every field lands");
    CHECK(dbg_req_parse("{\"cmd\":{\"a\":1}}", &r) == DBG_PARSE_NESTED, "nesting is refused");
    CHECK(dbg_req_parse("{\"cmd\":\"aaaaaaaaaaaaaaaaaaaaaaa\"}", &r) == DBG_PARSE_TOO_LONG,
          "an over-long field is refused, not truncated into another command");
    CHECK(dbg_req_parse("{\"cmd\":\"x\"", &r) == DBG_PARSE_BAD, "an unterminated object is refused");
    CHECK(dbg_req_parse("{\"cmd\":\"x\"} trailing", &r) == DBG_PARSE_BAD, "trailing bytes are refused");
}

static int s_reboots;
static bool fake_reboot(const dbg_req_t *r, dbg_json_t *d, char *e, size_t n)
{
    (void)r, (void)d, (void)e, (void)n;
    s_reboots++;
    return true;
}

static void test_commands_are_whitelisted_and_destructive_ones_need_confirm(void)
{
    char buf[2048];
    dbg_json_t w;
    dbg_cmd_reset();
    CHECK(!dbg_cmd_bind("format_flash", fake_reboot), "a command outside the whitelist cannot be bound");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"1\",\"cmd\":\"reboot\"}", &w);
    CHECK(strstr(buf, "\"err\":\"needs_confirm\"") && strstr(buf, "\"destructive\":true"),
          "reboot without confirm is refused and says why");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"2\",\"cmd\":\"reboot\",\"confirm\":true}", &w);
    CHECK(strstr(buf, "\"err\":\"unavailable\"") != NULL, "confirmed but unbound is unavailable, not a crash");
    dbg_cmd_bind("reboot", fake_reboot);
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"3\",\"cmd\":\"reboot\",\"confirm\":true}", &w);
    CHECK(s_reboots == 1 && strstr(buf, "\"ok\":true") && strstr(buf, "\"id\":\"3\""),
          "confirmed and bound, it runs once and answers its own id");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"4\",\"cmd\":\"rm\"}", &w);
    CHECK(strstr(buf, "unknown_command") != NULL, "an unknown command is named as such");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("{\"id\":\"5\",\"cmd\":\"help\"}", &w);
    CHECK(strstr(buf, "{\"name\":\"reboot\",\"destructive\":true,\"idempotent\":false,\"available\":true}") &&
              strstr(buf, "{\"name\":\"snapshot\",\"destructive\":false,\"idempotent\":true"),
          "help tells the box which commands need a confirm and which may be retried");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_cmd_run("not json", &w);
    CHECK(strstr(buf, "\"err\":\"parse\"") && dbg_json_text(&w)[0] == '{', "garbage still gets a JSON answer");
    dbg_cmd_reset();
}

static void test_the_log_ring_keeps_whole_lines_newest_last(void)
{
    dbg_log_reset();
    dbg_secret_clear();
    char line[DBG_LOG_LINE_MAX + 40];
    for (int i = 0; i < 200; i++) {
        snprintf(line, sizeof line, "line %03d ................................................", i);
        dbg_log_line(line);
    }
    static char buf[8192];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_log_write(&w, "lines");
    dbg_json_obj_end(&w);
    CHECK(dbg_log_evicted() > 0, "old lines were evicted");
    CHECK(strstr(buf, "\"line 199") && !strstr(buf, "\"line 000"), "the newest stays, the oldest went");
    CHECK(strstr(buf, "[\"line ") != NULL, "the first kept line is whole, not a tail");
    memset(line, 'x', sizeof line - 1);
    line[sizeof line - 1] = '\0';
    dbg_log_line(line);
    CHECK(strlen(line) > DBG_LOG_LINE_MAX, "a long line went in");
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_log_write(&w, "lines");
    dbg_json_obj_end(&w);
    char want[DBG_LOG_LINE_MAX + 3];
    memset(want, 'x', DBG_LOG_LINE_MAX);
    want[DBG_LOG_LINE_MAX] = '"';
    want[DBG_LOG_LINE_MAX + 1] = ']';
    want[DBG_LOG_LINE_MAX + 2] = '\0';
    CHECK(strstr(buf, want) != NULL, "and came out clipped to the line limit");
}

static int s_prev_calls;
static int prev_vprintf(const char *fmt, va_list ap)
{
    (void)fmt, (void)ap;
    return ++s_prev_calls;
}

static int log_via_hook(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int r = dbg_log_vprintf(fmt, ap);
    va_end(ap);
    return r;
}

static void test_the_esp_log_hook_keeps_the_line_and_still_prints_it(void)
{
    dbg_log_reset();
    dbg_log_chain(prev_vprintf);
    log_via_hook("I (%d) wifi: got ip %s\n", 1234, "192.168.1.50");
    dbg_log_chain(NULL);
    static char buf[1024];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_log_write(&w, "lines");
    dbg_json_obj_end(&w);
    CHECK(strstr(buf, "\"I (1234) wifi: got ip 192.168.1.50\"") != NULL,
          "the formatted line is in the ring, newline trimmed");
    CHECK(s_prev_calls == 1, "and the console it used to go to still gets it");
}

typedef struct {
    uint8_t *buf;
    size_t n, cap;
    unsigned calls;
} mem_t;

static void mem_sink(void *ctx, const uint8_t *b, size_t n)
{
    mem_t *m = ctx;
    if (m->n + n <= m->cap) memcpy(m->buf + m->n, b, n);
    m->n += n;
    m->calls++;
}

static void test_a_screenshot_streams_in_stripes_and_round_trips(void)
{
    enum { W = 480, H = 480, ROWS = 40 };
    static uint16_t frame[W * H], back[W * H];
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) frame[y * W + x] = (x > 100 && x < 300 && y > 50) ? (uint16_t)(x * 31 + y) : 0;
    static uint8_t out[W * H * 3 + 4096];
    mem_t m = {out, 0, sizeof out, 0};
    dbg_shot_t s;
    dbg_shot_begin(&s, W, H, mem_sink, &m);
    bool ok = true;
    for (int y = 0; y < H; y += ROWS) ok &= dbg_shot_stripe(&s, (uint16_t)y, ROWS, frame + y * W);
    ok &= dbg_shot_end(&s);
    CHECK(ok && s.stripes == H / ROWS, "twelve stripes of forty rows");
    CHECK(m.n == s.bytes && m.calls > 1, "streamed through the small buffer, not held whole");
    CHECK(dbg_shot_decode(out, m.n, back, W, H) && !memcmp(frame, back, sizeof frame), "it decodes to the same frame");

    static uint16_t black[W * ROWS];
    memset(black, 0, sizeof black);
    m.n = 0;
    dbg_shot_begin(&s, W, H, mem_sink, &m);
    for (int y = 0; y < H; y += ROWS) dbg_shot_stripe(&s, (uint16_t)y, ROWS, black);
    dbg_shot_end(&s);
    CHECK(m.n < 8000, "an all-black screen is under 8 KB, not 460");

    m.n = 0;
    dbg_shot_begin(&s, W, H, mem_sink, &m);
    dbg_shot_stripe(&s, 0, ROWS, black);
    CHECK(!dbg_shot_stripe(&s, 80, ROWS, black), "a skipped stripe fails the capture");
    CHECK(!dbg_shot_end(&s), "and the capture says so at the end");
    CHECK(!dbg_shot_decode(out, 6, back, W, H), "a truncated stream does not decode");
}

int main(void)
{
    test_an_alarm_outranks_a_notice_which_outranks_the_app();
    test_a_notice_waits_for_a_hold_to_end();
    test_key_up_follows_its_key_down_whatever_arrives_between();
    test_boot_dismisses_a_notice_and_key_does_not_reach_under_it();
    test_the_alarm_takes_both_buttons();
    test_boot_is_back_then_home();
    test_a_dark_screen_only_wakes();
    test_events_route_as_the_plan_says();
    test_tiles_fill_the_glass_without_touching();
    test_a_swipe_needs_sixty_pixels_and_stops_at_the_ends();
    test_the_registry_refuses_clashes();
    test_the_event_queue_is_fifo_and_counts_what_it_drops();
    test_the_clock_is_twenty_four_hour();
    test_next_alarm_honours_days_and_wraps();
    test_alarm_text_reads_like_the_mock();
    test_snooze_is_nine_minutes_across_midnight();
    test_the_night_face_wraps_past_midnight();
    test_the_notice_table();
    test_urgent_always_chimes();
    test_the_panel_reports_what_the_box_decides_from();
    test_brightness_never_goes_below_ten();
    test_settings_cards_say_what_the_mock_says();
    test_conversation_rules();
    test_the_writer_escapes_and_refuses_a_truncated_document();
    test_secret_keys_are_never_written();
    test_a_snapshot_never_contains_a_secret();
    test_requests_parse_flat_and_nothing_else();
    test_commands_are_whitelisted_and_destructive_ones_need_confirm();
    test_the_log_ring_keeps_whole_lines_newest_last();
    test_the_esp_log_hook_keeps_the_line_and_still_prints_it();
    test_a_screenshot_streams_in_stripes_and_round_trips();
    printf("%d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
