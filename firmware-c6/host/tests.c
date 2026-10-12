/* Host suite for the panel's pure modules. One behaviour per test, named as a sentence; every
 * CHECK message says what is true. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <time.h>

#include "alarm_logic.h"
#include "alarm_port_fake.h"
#include "alarm_sound.h"
#include "app_event.h"
#include "axp_logic.h"
#include "box_logic.h"
#include "btn_logic.h"
#include "clock_model.h"
#include "dbg_core.h"
#include "dbg_json.h"
#include "disp_guard.h"
#include "disp_logic.h"
#include "convo_model.h"
#include "minutes.h"
#include "notice_policy.h"
#include "settings_model.h"
#include "shell_core.h"
#include "shell_evq.h"
#include "settings_store.h"
#include "sib_logic.h"
#include "sib_port_fake.h"
#include "svc_audio_fake.h"
#include "talk_core.h"
#include "svc_box_fake.h"
#include "svc_messages_fake.h"
#include "svc_net_fake.h"
#include "svc_notice_fake.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"
#include "svc_alarms_drv.h"
#include "svc_siblings_drv.h"
#include "thread_model.h"
#include "time_logic.h"
#include "tp_logic.h"
#include "ui_hold.h"
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
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_TALK_JERV,
          "KEY on home talks to Jerv");
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
    CHECK((a & SH_DO_WAKE) && (a & SH_DO_REFRESH_OVERLAY) && (a & SH_DO_DELIVER),
          "a notice wakes, is the shell's to draw, and is told to the app so a face can badge it");
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

static void test_alarm_text_reads_like_the_mock(void)
{
    char s[64];
    alarm_next_t n = {.a = al(1, 6, 45, 0x1F, true), .at_h = 6, .at_m = 45, .wait_min = 9 * 60 + 4};
    clock_fmt_next(&n, s, sizeof s);
    CHECK(!strcmp(s, "Next 06:45 · in 9 h 4 min"), "hours and minutes");
    n.wait_min = 12;
    clock_fmt_next(&n, s, sizeof s);
    CHECK(!strcmp(s, "Next 06:45 · in 12 min"), "minutes alone under an hour");
    n = (alarm_next_t){.a = al(1, 6, 45, 0x1F, true), .snoozed = true, .at_h = 6, .at_m = 54, .wait_min = 9};
    clock_fmt_next(&n, s, sizeof s);
    CHECK(!strcmp(s, "Snoozed until 06:54 · in 9 min"), "a snooze says so");
    clock_fmt_days(0x1F, "Work", s, sizeof s);
    CHECK(!strcmp(s, "Weekdays · Work"), "weekdays with a label");
    clock_fmt_days(0x60, "", s, sizeof s);
    CHECK(!strcmp(s, "Sat, Sun"), "a weekend lists its days");
    clock_fmt_days(0x7F, NULL, s, sizeof s);
    CHECK(!strcmp(s, "Every day"), "every day");
    clock_fmt_days(0, NULL, s, sizeof s);
    CHECK(!strcmp(s, "Once"), "no days is once");
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
    settings_wifi(&n, false, &w);
    CHECK(!strcmp(w.button, "Disconnect") && w.button_live && w.bars == 3 && w.kind == WIFI_BTN_SECONDARY,
          "connected, three bars, a quiet Disconnect");
    n.state = NET_CONNECTING;
    settings_wifi(&n, false, &w);
    CHECK(!w.button_live && !strcmp(w.button, "Connecting…"), "nothing to press while connecting");
    n.state = NET_OFF;
    settings_wifi(&n, false, &w);
    CHECK(w.button_live && w.kind == WIFI_BTN_PRIMARY && !strcmp(w.button, "Connect"), "off: Connect");
    fw_info_t fw = {"0.0.8", "0.0.9", FW_DOWNLOADING, 64, ""};
    char note[96];
    settings_fw_t f;
    settings_fw(&fw, true, &f, note, sizeof note);
    CHECK(!f.button_live && f.show_bar && !strcmp(note, "Downloading 0.0.9 · 64%"),
          "downloading: bar up, button dead");
    settings_fw(&fw, false, &f, note, sizeof note);
    CHECK(f.show_bar && strstr(note, "64%"), "a download under way is still reported with the radio down");
    fw.phase = FW_IDLE;
    settings_fw(&fw, false, &f, note, sizeof note);
    CHECK(!f.button_live && strstr(note, "Connect Wi-Fi"), "offline: Update now is dead and says why");
    fw.phase = FW_FAILED;
    snprintf(fw.err, sizeof fw.err, "wrong-board");
    settings_fw(&fw, true, &f, note, sizeof note);
    CHECK(f.button_live && !strcmp(note, "Update failed (wrong-board). The panel stays on 0.0.8."),
          "a failed update says why and that nothing changed");
    fw.phase = FW_CHECKING;
    settings_fw(&fw, true, &f, note, sizeof note);
    CHECK(!strcmp(f.button, "Checking…") && !strcmp(note, "Asking the box…"), "checking");
    power_info_t p = {.fitted = true, .pct = 12, .mv = 3520};
    settings_power_t pv;
    settings_power(&p, &pv);
    CHECK(pv.low && !strcmp(pv.big, "12%") && strstr(pv.line, "plug in"), "low battery turns amber");
    p.fitted = false;
    p.usb_mv = 5020;
    settings_power(&p, &pv);
    CHECK(!pv.bar && !strcmp(pv.title, "Power") && !strcmp(pv.line, "No battery fitted · USB 5.02 V"),
          "no battery: USB, as the mock words it");
    char s[200];
    settings_notif_note(true, true, 21 * 60 + 30, 6 * 60 + 30, s, sizeof s);
    CHECK(strstr(s, "Refused 21:30–06:30 — ") == s, "the quiet note names the window");
    CHECK(strstr(settings_rotate_note(true), "later update") && !strcmp(settings_rotate_note(false), "Locked upright"),
          "auto-rotate says it is not turning anything yet");
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
    CHECK(convo_shows_listen(&reply, false, 0), "text only: a reply has Listen");
    CHECK(!convo_shows_listen(&reply, true, 0), "read aloud: no Listen");
    CHECK(!convo_shows_listen(&reply, false, 9), "not while it is being spoken");
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
    svc_messages_init();
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


/* ---- drivers: the pure halves of svc_display, svc_power and svc_buttons ---- */

static void test_every_flush_window_is_even_aligned_and_covers_the_dirty_area(void)
{
    for (int32_t x1 = 0; x1 < 480; x1 += 7)
        for (int32_t w = 1; x1 + w <= 480; w += 13) {
            int32_t a = x1, b = 5, c = x1 + w - 1, d = 5 + (w % 9);
            disp_area_even(&a, &b, &c, &d, 480, 480);
            if (a % 2 || b % 2 || (c - a + 1) % 2 || (d - b + 1) % 2) {
                CHECK(false, "start and size are even in both axes");
                return;
            }
            if (a > x1 || c < x1 + w - 1 || b > 5 || d < 5 + (w % 9)) {
                CHECK(false, "the rounded window still covers what was dirty");
                return;
            }
        }
    int32_t a = 479, b = 479, c = 479, d = 479;
    disp_area_even(&a, &b, &c, &d, 480, 480);
    CHECK(a == 478 && c == 479 && b == 478 && d == 479, "the last pixel rounds inward, never off the glass");
    a = -3, b = -1, c = 600, d = 900;
    disp_area_even(&a, &b, &c, &d, 480, 480);
    CHECK(a == 0 && b == 0 && c == 479 && d == 479, "an area past the edges is clipped to the screen");
    a = 0, b = 0, c = 479, d = 39;
    disp_area_even(&a, &b, &c, &d, 480, 480);
    CHECK(a == 0 && c == 479 && b == 0 && d == 39, "an aligned strip is left exactly as it was");
}

static void test_brightness_maps_percent_to_the_0x51_byte_and_dark_is_zero(void)
{
    CHECK(disp_level(100, false) == 255 && disp_level(0, false) == 0, "0-100 spans 0x00-0xFF");
    CHECK(disp_level(50, false) == 128, "half is 0x80");
    CHECK(disp_level(200, false) == 255, "out of range is clamped, not wrapped");
    CHECK(disp_level(80, true) == 0, "dark is level 0 whatever the setting");
}

static void test_the_display_is_skipped_only_on_the_boot_after_a_fault(void)
{
    const int faults[] = {DG_RST_PANIC, DG_RST_INT_WDT, DG_RST_TASK_WDT, DG_RST_WDT,
                          DG_RST_BROWNOUT, DG_RST_PWR_GLITCH, DG_RST_CPU_LOCKUP};
    for (size_t i = 0; i < sizeof faults / sizeof *faults; i++)
        CHECK(!dg_may_start(faults[i]), "a panic, a watchdog or a brownout leaves the display off");
    CHECK(dg_may_start(DG_RST_POWERON), "a power-on starts it");
    CHECK(dg_may_start(DG_RST_SW), "the reset after an OTA starts it, so a fixed image is tried");
    CHECK(dg_may_start(DG_RST_EXT) && dg_may_start(DG_RST_UNKNOWN), "anything else starts it");
}

static void test_a_button_needs_three_steady_samples(void)
{
    btn_db_t b = {0};
    CHECK(btn_db_step(&b, true) == 0 && btn_db_step(&b, true) == 0, "two samples are not a press");
    CHECK(btn_db_step(&b, true) == 1, "the third is");
    CHECK(btn_db_step(&b, true) == 0, "holding is not another press");
    CHECK(btn_db_step(&b, false) == 0 && btn_db_step(&b, true) == 0, "a one-sample bounce is ignored");
    CHECK(btn_db_step(&b, false) == 0 && btn_db_step(&b, false) == 0, "release needs three too");
    CHECK(btn_db_step(&b, false) == -1, "and is reported once");
}

static void test_one_pwr_press_seen_two_ways_toggles_once(void)
{
    pwr_merge_t m = {0};
    CHECK(!strcmp(pwr_seen_name(m.seen), "none"), "nothing seen yet");
    CHECK(pwr_merge_accept(&m, PWR_SRC_GPIO18, 1000), "the first report is a press");
    CHECK(!pwr_merge_accept(&m, PWR_SRC_PEK, 1050), "the PMIC's report of the same press is not");
    CHECK(!strcmp(pwr_seen_name(m.seen), "pek+gpio18"), "but both routes are recorded as working");
    CHECK(pwr_merge_accept(&m, PWR_SRC_PEK, 1000 + PWR_MERGE_MS), "a press after the window counts");
    pwr_merge_t z = {0};
    CHECK(pwr_merge_accept(&z, PWR_SRC_PEK, 0), "a press at uptime zero counts");
    CHECK(!strcmp(pwr_seen_name(z.seen), "pek"), "and names its route");
    pwr_merge_t w = {.last_ms = 0xFFFFFF00u, .any = true};
    CHECK(!pwr_merge_accept(&w, PWR_SRC_GPIO18, 0x10), "the window holds across the 49-day wrap");
}

static void test_the_pmic_registers_decode_as_the_probe_read_them(void)
{
    /* 0.0.3 on the board: 0x00=0x20, 0x01=0x15 — on USB, no battery, not charging. */
    axp_status_t st = axp_status_decode(0x20, 0x15);
    CHECK(st.vbus_good && !st.battery && !st.charging, "on USB, no cell, not charging");
    st = axp_status_decode(0x28, 0x20 | 0x05);
    CHECK(st.battery && st.charging, "a cell on charge reads as one");
    CHECK(axp_adc14(0xD3, 0x88) == 0x1388, "VBUS keeps 14 bits: 5000 mV");
    CHECK(axp_adc13(0xF0, 0x68) == 0x1068, "the battery keeps 13 bits: 4200 mV");
    CHECK(axp_pek_pressed(AXP_PEK_DOWN), "the falling edge is a press");
    CHECK(!axp_pek_pressed(AXP_PEK_SHORT | AXP_PEK_UP), "a release latched later is not a second one");
    CHECK(!axp_pek_pressed(0xF0), "battery and VBUS events are not the key");
    CHECK(axp_field(0xFF, 0x04, 0x00) == 0xFB, "clearing ALDO3 leaves every other rail on");
    CHECK(axp_field(0x15, 0x1F, 9) == 0x09 && axp_field(0xE5, 0x1F, 9) == 0xE9, "a field write keeps the bits outside its mask");
}

/* ---- touch ---- */

/* A report as the CST9220 sends one: point 0 at raw axes (a, b). */
static void report(uint8_t d[TP_REPORT_LEN], uint8_t status, uint8_t points, uint16_t a, uint16_t b)
{
    memset(d, 0, TP_REPORT_LEN);
    d[0] = (uint8_t)(0x10 | status);
    d[1] = (uint8_t)(a >> 4);
    d[2] = (uint8_t)(b >> 4);
    d[3] = (uint8_t)(((a & 0x0F) << 4) | (b & 0x0F));
    d[5] = points;
    d[6] = TP_ACK;
}

static void test_a_touch_report_needs_the_ack_byte_and_a_pressed_point(void)
{
    uint8_t d[TP_REPORT_LEN];
    report(d, 0x06, 1, 0x123, 0x0AB);
    tp_report_t r = tp_decode(d);
    CHECK(r.kind == TP_DOWN, "status 6 with one point is a finger");
    CHECK(r.raw_a == 0x123 && r.raw_b == 0x0AB, "each axis is 8 high bits and a nibble of byte 3");
    d[5] = 0x81;
    CHECK(tp_decode(d).kind == TP_DOWN, "the point count's top bit is not part of it");
    d[6] = 0xAA;
    CHECK(tp_decode(d).kind == TP_NOACK, "without 0xAB in byte 6 it is not a report at all");
    report(d, 0x06, 0, 10, 10);
    CHECK(tp_decode(d).kind == TP_UP, "no points is a lifted finger");
    report(d, 0x07, 1, 10, 10);
    CHECK(tp_decode(d).kind == TP_UP, "a point not in status 6 is not pressed");
    memset(d, 0, sizeof d);
    CHECK(tp_decode(d).kind == TP_NOACK, "an all-zero read is not a release");
}

static void test_raw_axes_map_to_the_glass_swapped_and_mirrored(void)
{
    int16_t x, y;
    tp_map(0, 0, &x, &y);
    CHECK(x == 479 && y == 0, "raw (0,0) is the top-right corner");
    tp_map(479, 479, &x, &y);
    CHECK(x == 0 && y == 479, "raw (479,479) is the bottom-left corner");
    tp_map(0, 479, &x, &y);
    CHECK(x == 0 && y == 0, "rawB is the inverted x axis");
    tp_map(479, 0, &x, &y);
    CHECK(x == 479 && y == 479, "rawA is y");
    tp_map(100, 300, &x, &y);
    CHECK(x == 179 && y == 100, "display x = 479 - rawB, y = rawA");
    tp_map(4095, 480, &x, &y);
    CHECK(x == 0 && y == 479, "a reading past the edge lands on the edge, not the far side");
    /* The owner's four corner taps on the real panel (0.0.6 telemetry, raw a,b), each of which
       0.0.6 placed in the opposite corner. */
    tp_map(67, 401, &x, &y);
    CHECK(x < 120 && y < 120, "his top-left tap lands top-left");
    tp_map(63, 63, &x, &y);
    CHECK(x > 360 && y < 120, "his top-right tap lands top-right");
    tp_map(438, 47, &x, &y);
    CHECK(x > 360 && y > 360, "his bottom-right tap lands bottom-right");
    tp_map(418, 433, &x, &y);
    CHECK(x < 120 && y > 360, "his bottom-left tap lands bottom-left");
}

static void test_a_stroke_survives_a_quiet_poll_but_not_three(void)
{
    tp_track_t t = {0};
    const tp_report_t down = {TP_DOWN, 100, 300}, moved = {TP_DOWN, 100, 310},
                      up = {TP_UP, 0, 0}, quiet = {TP_NOACK, 0, 0};
    CHECK(tp_track(&t, &quiet) == TP_EV_NONE, "silence with no finger is nothing");
    CHECK(tp_track(&t, &down) == TP_EV_DOWN && t.x == 179 && t.y == 100, "a press, mapped");
    CHECK(tp_track(&t, &down) == TP_EV_NONE, "a finger that did not move is not a move");
    CHECK(tp_track(&t, &moved) == TP_EV_MOVE && t.x == 169, "a moved one is");
    CHECK(tp_track(&t, &quiet) == TP_EV_NONE && tp_track(&t, NULL) == TP_EV_NONE,
          "a missing ack and a failed read each hold the stroke");
    CHECK(tp_track(&t, &down) == TP_EV_MOVE, "and the finger carries on where it is");
    for (int i = 1; i < TP_HOLD_POLLS; i++) CHECK(tp_track(&t, NULL) == TP_EV_NONE, "held");
    CHECK(tp_track(&t, NULL) == TP_EV_UP && !t.down, "but enough quiet polls lift it");
    CHECK(tp_track(&t, &down) == TP_EV_DOWN, "the next touch is a new press");
    CHECK(tp_track(&t, &up) == TP_EV_UP && t.x == 179, "a release report lifts it at once, where it was");
    CHECK(tp_track(&t, &up) == TP_EV_NONE, "and is reported once");
}

static void test_the_touch_ring_keeps_every_edge_and_coalesces_moves(void)
{
    tp_ring_t q = {0};
    tp_sample_t s = {1, 1, TP_EV_DOWN, 10}, out;
    tp_ring_push(&q, &s);
    for (int i = 2; i < 30; i++) {
        s = (tp_sample_t){(int16_t)i, 1, TP_EV_MOVE, (uint32_t)(10 + i)};
        tp_ring_push(&q, &s);
    }
    s = (tp_sample_t){29, 1, TP_EV_UP, 50};
    tp_ring_push(&q, &s);
    CHECK(q.count == 3 && q.dropped == 0, "a long drag is a press, one move and a release");
    CHECK(tp_ring_pop(&q, &out) && out.ev == TP_EV_DOWN && out.x == 1, "the press first");
    CHECK(tp_ring_pop(&q, &out) && out.ev == TP_EV_MOVE && out.x == 29, "then the latest position");
    CHECK(tp_ring_pop(&q, &out) && out.ev == TP_EV_UP, "then the release");
    CHECK(!tp_ring_pop(&q, &out), "then nothing");
    for (int i = 0; i < TP_RING + 2; i++) {
        s = (tp_sample_t){(int16_t)i, 0, i % 2 ? TP_EV_UP : TP_EV_DOWN, (uint32_t)i};
        tp_ring_push(&q, &s);
    }
    CHECK(q.count == TP_RING && q.dropped == 2, "a full ring drops the oldest, and counts it");
    CHECK(tp_ring_pop(&q, &out) && out.x == 2, "keeping the newest");
}

static void test_a_tap_a_swipe_and_a_drag_are_told_apart(void)
{
    CHECK(sh_stroke(0, 0) == SH_STROKE_TAP, "a still finger taps");
    CHECK(sh_stroke(SH_TAP_SLOP - 1, -(SH_TAP_SLOP - 1)) == SH_STROKE_TAP, "a wobble inside the slop taps");
    CHECK(sh_stroke(SH_TAP_SLOP, 0) == SH_STROKE_DRAG, "past it, it no longer taps");
    CHECK(sh_stroke(-SH_SWIPE_PX, 10) == SH_STROKE_SWIPE, "sixty px sideways swipes");
    CHECK(sh_stroke(SH_SWIPE_PX - 1, 0) == SH_STROKE_DRAG, "a short swipe opens nothing");
    CHECK(sh_stroke(80, 120) == SH_STROKE_DRAG, "a mostly-vertical drag turns no page");
    CHECK(sh_stroke(0, 200) == SH_STROKE_DRAG, "a scroll is a drag");
}

static void test_a_finger_on_a_dark_screen_buys_the_screen_and_nothing_else(void)
{
    sh_gate_t g = {0};
    bool wake;
    CHECK(!sh_gate(&g, true, true, &wake) && wake, "the press wakes and reaches nothing");
    CHECK(!sh_gate(&g, true, false, &wake) && !wake, "lit now, the same stroke's moves still reach nothing");
    CHECK(!sh_gate(&g, false, false, &wake) && !wake, "nor its release");
    CHECK(sh_gate(&g, true, false, &wake) && !wake, "the next touch on the lit screen is a touch");
    CHECK(sh_gate(&g, true, true, &wake) && !wake, "a stroke that started lit is not cut by going dark");
    CHECK(!sh_gate(&g, false, true, &wake) && !wake, "and its release goes where its press went");
    sh_state_t st = {0};
    sh_facts_t f = home();
    f.dark = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_TOUCH).first == SH_ACT_WAKE, "the queued wake wakes");
    f.dark = false;
    CHECK(sh_route_button(&st, &f, SH_BTN_TOUCH).first == SH_ACT_NONE,
          "and does not darken a screen PWR already lit");
    f = in_app();
    st.key_owner = SH_KEY_APP;
    sh_route_button(&st, &f, SH_BTN_TOUCH);
    CHECK(st.key_owner == SH_KEY_APP, "it leaves a KEY hold where it was");
}


/* ---- KEY outside a conversation (owner, 2026-10-12) ---- */

static void test_key_outside_a_conversation_talks_to_jerv(void)
{
    sh_state_t st = {0};
    sh_facts_t f = home();
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_TALK_JERV && st.key_owner == SH_KEY_SHELL,
          "KEY on home starts the shell's own talk to Jerv, with every app closed");
    f.talking = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_SEND_JERV,
          "its release sends to Jerv");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_NONE,
          "and a second release has nothing behind it");

    st = (sh_state_t){0};
    f = in_app();
    r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_TO_APP && r.fallback == SH_ACT_TALK_JERV,
          "in an app KEY is offered to it first, Jerv if it declines");
    CHECK(sh_declined(&st, r) == SH_ACT_TALK_JERV && st.key_owner == SH_KEY_SHELL,
          "Settings, the clock face or Minecraft decline it, and the shell takes the hold");
    sh_app_closed(&st);
    f = home();
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_SEND_JERV,
          "an app closing under the shell's hold does not lose its release");

    st = (sh_state_t){0};
    f = in_app();
    sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(st.key_owner == SH_KEY_APP, "a thread that consumes KEY keeps it for its own recipient");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_TO_APP, "and gets its release");

    st = (sh_state_t){0};
    f = in_app();
    f.alarm_ringing = true;
    r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(sh_declined(&st, r) == SH_ACT_SNOOZE_ALARM && st.key_owner == SH_KEY_ALARM,
          "a ringing alarm takes KEY as snooze, never as talk");
}

static void test_the_talk_ring_takes_boot_and_holds_notices_back(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.talking = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_CANCEL_TALK, "BOOT cancels the talk");
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_NONE,
          "a second press cannot start a second recording");
    st.key_owner = SH_KEY_SHELL;
    f.notices = 1;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NONE, "a notice waits for the shell's hold to end too");
    f = home();
    f.dark = true;
    st = (sh_state_t){0};
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_WAKE && st.key_owner == SH_KEY_FREE,
          "on a dark screen KEY only wakes: no recording started blind");
}

static void test_the_clock_face_keeps_notices_as_a_badge(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.notices = 1;
    f.notices_held = true;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NONE, "the face is never covered by a notice");
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_TO_APP,
          "BOOT on the face is the face's, not a dismissal of what it holds");
    f.alarm_ringing = true;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_ALARM, "an alarm still takes the whole screen");
    CHECK(clock_holds_notices(true, 2, 7, 0), "on the face, notices wait");
    CHECK(!clock_holds_notices(true, 2, 7, 7), "the one whose badge was tapped opens");
    CHECK(clock_holds_notices(true, 1, 8, 7), "and the next one waits again");
    CHECK(!clock_holds_notices(false, 2, 7, 0), "off the face (the alarm list) they pop up as anywhere");
    CHECK(clock_badge_count(1, 2) == 3 && clock_badge_count(0, 0) == 0,
          "the badge counts notices and unheard messages, and hides at zero");
    CHECK(clock_badge_opa(true) == 255 && clock_badge_opa(false) == 204, "urgent at full strength");
    /* The image ships no sample messages; the suite brings its own, as the sim does. */
    svc_messages_fake_clear();
    svc_messages_fake_add(SVC_MESSAGES_THREAD_1,
                          &(msg_t){.id = 4, .voice = true, .dur_s = 5, .text = "When are you home?"});
    svc_messages_fake_add(SVC_MESSAGES_THREAD_2, &(msg_t){.id = 5, .voice = true, .played = true,
                                                          .dur_s = 3, .text = "I made a drawing."});
    svc_messages_fake_add("jerv", &(msg_t){.id = 2, .text = "Added milk to your shopping list."});
    CHECK(svc_messages_unplayed_total() == 1, "Quorra's unheard message is counted, Jerv's text is not");
    char who[16] = "";
    CHECK(svc_messages_first_unplayed(who) && !strcmp(who, SVC_MESSAGES_THREAD_1),
          "and a tap opens her thread");
    svc_messages_fake_clear();
}

static void test_day_and_date_read_like_the_mock(void)
{
    char s[24];
    rtc_time_t t = {.valid = true, .h = 9, .m = 41, .wday = 0, .mday = 12, .mon = 10, .year = 2026};
    clock_fmt_daydate(&t, s, sizeof s);
    CHECK(!strcmp(s, "Mon 12 Oct"), "en-GB short: weekday, day, month");
    t.wday = 6, t.mday = 1, t.mon = 3;
    clock_fmt_daydate(&t, s, sizeof s);
    CHECK(!strcmp(s, "Sun 1 Mar"), "no leading zero on the day");
    char hm[6];
    clock_fmt_face(&t, hm);
    CHECK(!strcmp(hm, "09:41"), "the face is the 24-hour time");
    t.valid = false;
    clock_fmt_daydate(&t, s, sizeof s);
    clock_fmt_face(&t, hm);
    CHECK(s[0] == '\0' && !strcmp(hm, "--:--"), "with no time yet: dashes and no date, never 00:00");
    clock_fmt_window(21 * 60 + 30, 6 * 60 + 30, s, sizeof s);
    CHECK(!strcmp(s, "21:30 – 06:30"), "the night window, with the mock's en dash");
    clock_face_t day = clock_face(true, 21 * 60 + 30, 6 * 60 + 30, 0, 12 * 60, false);
    clock_face_t night = clock_face(true, 21 * 60 + 30, 6 * 60 + 30, 0, 23 * 60, false);
    CHECK(day.date_opa == 140 && night.date_opa == 98 && night.date_opa < night.opa,
          "the date is fainter than the digits, and dims with the night clock");
}

/* ---- time ---- */

static void test_calendar_dates_round_trip_through_unix_time(void)
{
    civil_t c = {.year = 2026, .mon = 10, .mday = 12, .h = 13, .m = 41, .s = 7};
    const int64_t e = civil_to_epoch(&c);
    CHECK(e == 1791812467, "2026-10-12 13:41:07 UTC");
    civil_t back;
    epoch_to_civil(e, &back);
    CHECK(back.year == 2026 && back.mon == 10 && back.mday == 12 && back.h == 13 && back.m == 41 &&
              back.s == 7 && back.wday == 0,
          "and back, a Monday");
    civil_t leap = {.year = 2028, .mon = 2, .mday = 29};
    epoch_to_civil(civil_to_epoch(&leap) + 86400, &back);
    CHECK(back.mon == 3 && back.mday == 1, "the day after 29 February 2028 is 1 March");
    epoch_to_civil(0, &back);
    CHECK(back.year == 1970 && back.wday == 3, "1970 began on a Thursday");
}

static void test_the_clock_chip_registers(void)
{
    CHECK(bcd_to_bin(0x59) == 59 && bin_to_bcd(59) == 0x59, "BCD both ways");
    civil_t c = {.year = 2026, .mon = 10, .mday = 12, .h = 23, .m = 5, .s = 9}, out;
    epoch_to_civil(civil_to_epoch(&c), &c);
    uint8_t r[7];
    pcf_encode(&c, r);
    CHECK(r[0] == 0x09 && r[2] == 0x23 && r[4] == 1 && r[6] == 0x26,
          "seconds with OS clear, 24-hour hours, Monday as the chip's 1, two-digit year");
    CHECK(pcf_decode(r, &out) && out.h == 23 && out.mday == 12 && out.wday == 0, "and read back");
    r[0] |= 0x80;
    CHECK(!pcf_decode(r, &out), "an oscillator that stopped holds no time to show");
    pcf_encode(&c, r);
    r[6] = 0x00;
    CHECK(!pcf_decode(r, &out), "2000 is a chip nobody set");
    pcf_encode(&c, r);
    r[5] = 0x13;
    CHECK(!pcf_decode(r, &out), "month 13 is garbage, not a date");
}

static void test_the_box_date_header(void)
{
    int64_t e = 0;
    CHECK(http_date_parse("Mon, 12 Oct 2026 13:41:07 GMT", &e) && e == 1791812467,
          "IMF-fixdate, as every current server sends it");
    CHECK(!http_date_parse("Monday, 12-Oct-26 13:41:07 GMT", &e), "the obsolete RFC 850 form is refused");
    CHECK(!http_date_parse("Mon Oct 12 13:41:07 2026", &e), "so is asctime's");
    CHECK(!http_date_parse("Mon, 12 Oct 2026 13:41:07 UTC", &e), "and anything not GMT");
    CHECK(!http_date_parse("Mon, 12 Foo 2026 13:41:07 GMT", &e), "and a month that is not one");
    CHECK(!http_date_parse(NULL, &e) && !http_date_parse("", &e), "and nothing at all");
}

static void test_when_the_box_time_replaces_the_panels(void)
{
    const int64_t box = 1791812467;
    CHECK(time_should_step(false, 0, box), "no time yet: take the box's");
    CHECK(!time_should_step(true, box + 1, box), "a second off is the header's own resolution: leave it");
    CHECK(time_should_step(true, box - 2, box) && time_should_step(true, box + 90, box),
          "two seconds or more either way is drift: step");
    CHECK(!time_should_step(false, 0, 1000), "a box with no clock of its own is never believed");
    CHECK(time_should_write_rtc(false, false, 10), "a chip that held no time is written");
    CHECK(time_should_write_rtc(true, true, 10), "so is one the panel had to correct");
    CHECK(time_should_write_rtc(true, false, -1), "and one never written this boot");
    CHECK(!time_should_write_rtc(true, false, 3600), "a good chip is left alone");
    CHECK(time_should_write_rtc(true, false, TIME_RTC_REWRITE_S), "until a day has passed");
}

static void test_the_zone_table_keeps_daylight_saving(void)
{
    CHECK(!strcmp(svc_rtc_tz_label(TZ_DEFAULT), "New York"), "New York by default");
    CHECK(!strcmp(svc_rtc_tz_label(99), "New York") && !strcmp(tz_posix(-1), tz_posix(TZ_DEFAULT)),
          "an index from nowhere falls back to it");
    struct tm tm;
    const time_t july = 1783000000, january = 1767600000; /* 2026-07-02, 2026-01-05 */
    setenv("TZ", tz_posix(TZ_DEFAULT), 1);
    tzset();
    localtime_r(&july, &tm);
    const int summer = tm.tm_hour;
    struct tm utc;
    gmtime_r(&july, &utc);
    CHECK((utc.tm_hour - summer + 24) % 24 == 4, "New York in July is UTC-4");
    localtime_r(&january, &tm);
    gmtime_r(&january, &utc);
    CHECK((utc.tm_hour - tm.tm_hour + 24) % 24 == 5, "and UTC-5 in January");
    setenv("TZ", tz_posix(8), 1);
    tzset();
    localtime_r(&july, &tm);
    gmtime_r(&july, &utc);
    CHECK(!strcmp(svc_rtc_tz_label(8), "London") && (tm.tm_hour - utc.tm_hour + 24) % 24 == 1,
          "London is an hour ahead in summer");
    setenv("TZ", "UTC0", 1);
    tzset();
}

/* ---- settings persistence ---- */

static void test_settings_are_written_once_the_panel_is_left_alone(void)
{
    settings_store_t s;
    settings_store_defaults(&s);
    CHECK(settings_store_due(&s, 100000) == 0, "defaults owe flash nothing");
    CHECK(settings_store_set(&s, SET_BRIGHTNESS, 70, 1000), "a change is a change");
    CHECK(settings_store_set(&s, SET_BRIGHTNESS, 75, 1500), "and the slider's next tick");
    CHECK(settings_store_due(&s, 3000) == 0, "nothing is written while the slider still moves");
    CHECK(settings_store_due(&s, 3500) == 1u << SET_BRIGHTNESS, "two quiet seconds: just brightness");
    settings_store_mark_saved(&s, SET_BRIGHTNESS);
    CHECK(settings_store_due(&s, 9000) == 0, "and once written, nothing more");
    CHECK(!settings_store_set(&s, SET_BRIGHTNESS, 75, 9000), "the same value is no change at all");
    settings_store_set(&s, SET_TTS, 0, 10000);
    settings_store_set(&s, SET_TTS, 1, 10100);
    CHECK(settings_store_due(&s, 20000) == 0, "a switch flipped and flipped back owes nothing");
    settings_store_set(&s, SET_BRIGHTNESS, 2, 20000);
    CHECK(s.val[SET_BRIGHTNESS] == BRIGHTNESS_FLOOR, "the store clamps like the service");
    settings_store_defaults(&s);
    settings_store_loaded(&s, SET_BRIGHTNESS, 400);
    CHECK(s.val[SET_BRIGHTNESS] == 100 && settings_store_due(&s, 5000) == 1u << SET_BRIGHTNESS,
          "an out-of-range saved value loads clamped and is written back corrected");
    settings_store_loaded(&s, SET_TZ, 40);
    CHECK(s.val[SET_TZ] == TZ_DEFAULT, "an unknown zone loads as the default");
    CHECK(svc_settings_default(SET_SHOW_DATE) == 1 && svc_settings_default(SET_AUTO_ROTATE) == 1 &&
              svc_settings_default(SET_NIGHT_COLOR) == 0 && svc_settings_default(SET_BRIGHTNESS) == 50,
          "date on, auto-rotate on, red, and the brightness the glass has run at since 0.0.4");
    for (int k = 0; k < SET__COUNT; k++)
        CHECK(strlen(svc_settings_name((setting_t)k)) <= 15 && svc_settings_name((setting_t)k)[0],
              "every key is a valid NVS key");
}

static void test_disconnect_takes_two_taps_within_three_seconds(void)
{
    CHECK(settings_wifi_tap(NET_CONNECTED, false, 0, 5000) == WIFI_TAP_ARM, "the first tap only arms");
    CHECK(settings_wifi_tap(NET_CONNECTED, true, 5000, 7999) == WIFI_TAP_DISCONNECT,
          "a second inside three seconds disconnects");
    CHECK(settings_wifi_tap(NET_CONNECTED, true, 5000, 8000) == WIFI_TAP_ARM,
          "at three seconds the first has lapsed and this one arms again");
    CHECK(!settings_wifi_armed(NET_CONNECTED, true, 5000, 8000) &&
              settings_wifi_armed(NET_CONNECTED, true, 5000, 6000),
          "the rose button lasts three seconds");
    CHECK(settings_wifi_tap(NET_OFF, true, 5000, 6000) == WIFI_TAP_CONNECT, "off, one tap connects");
    CHECK(settings_wifi_tap(NET_CONNECTING, false, 0, 6000) == WIFI_TAP_NONE, "connecting, nothing");
    net_info_t n = {NET_CONNECTED, "x", -58, "", 0};
    settings_wifi_t w;
    settings_wifi(&n, true, &w);
    CHECK(!strcmp(w.button, "Tap again to disconnect") && w.kind == WIFI_BTN_ARMED,
          "armed, the button says so in rose");
}

static void test_update_now_is_live_only_online_and_idle(void)
{
    CHECK(box_may_update(NET_CONNECTED, FW_IDLE) && box_may_update(NET_CONNECTED, FW_UPTODATE) &&
              box_may_update(NET_CONNECTED, FW_FAILED),
          "online and idle, a check can start");
    CHECK(!box_may_update(NET_OFF, FW_IDLE), "offline there is no box to ask");
    CHECK(!box_may_update(NET_CONNECTED, FW_CHECKING) && !box_may_update(NET_CONNECTED, FW_DOWNLOADING),
          "a check or download under way is already the answer");
}

static void test_a_hold_that_cannot_be_sent_says_so(void)
{
    msg_t m;
    CHECK(talk_placeholder(TALK_SENT, 3400, 1000, &m) && m.mine && m.voice && m.dur_s == 3 &&
              !strcmp(m.status, "Not sent") && strstr(m.text, "later update"),
          "a finished hold leaves an honest bubble, not a made-up transcript");
    CHECK(talk_placeholder(TALK_CAPPED, 61000, 1001, &m) && m.dur_s == 60, "the cap is a minute");
    CHECK(!talk_placeholder(TALK_TOO_SHORT, 300, 1, &m) && !talk_placeholder(TALK_CANCELLED, 5000, 1, &m),
          "a knock or a cancel leaves nothing");
}

/* ---- 0.0.9: the debug relay's pickup, and BOOT held ---- */

/* The buttons task in miniature: one raw sample every 10 ms through the debounce and the hold. */
static void boot_samples(btn_db_t *db, btn_hold_t *h, uint32_t *t, bool pressed, int n, int *shorts, int *longs)
{
    for (int i = 0; i < n; i++, *t += 10) {
        const btn_hold_ev_t e = btn_hold_step(h, btn_db_step(db, pressed), *t);
        *shorts += e == BTN_HOLD_SHORT;
        *longs += e == BTN_HOLD_LONG;
    }
}

static void test_boot_short_is_back_on_release_and_long_is_the_clock_once(void)
{
    btn_db_t db = {0};
    btn_hold_t h = {0};
    uint32_t t = 1000;
    int s = 0, l = 0;
    boot_samples(&db, &h, &t, true, 30, &s, &l);
    CHECK(s == 0 && l == 0, "a press held 0.3 s has done nothing yet: it might still be long");
    boot_samples(&db, &h, &t, false, 5, &s, &l);
    CHECK(s == 1 && l == 0, "its release is one home/back");

    s = l = 0;
    boot_samples(&db, &h, &t, true, BTN_LONG_MS / 10 + 3, &s, &l);
    CHECK(l == 1 && s == 0, "held past the threshold, the clock fires while still held");
    boot_samples(&db, &h, &t, false, 5, &s, &l);
    CHECK(l == 1 && s == 0, "and its release is not also a home/back");

    s = l = 0;
    boot_samples(&db, &h, &t, true, 6000, &s, &l);
    CHECK(l == 1 && s == 0, "held for a minute it fires once, not on repeat");
    boot_samples(&db, &h, &t, false, 5, &s, &l);

    s = l = 0;
    for (int i = 0; i < 50; i++) {
        boot_samples(&db, &h, &t, true, BTN_DB_SAMPLES - 1, &s, &l);
        boot_samples(&db, &h, &t, false, 1, &s, &l);
    }
    CHECK(s == 0 && l == 0, "a chattering contact below the debounce is nothing at all");

    btn_hold_t w = {0};
    CHECK(btn_hold_step(&w, 1, 0xFFFFFF00u) == BTN_HOLD_NONE &&
              btn_hold_step(&w, 0, 0xFFFFFF00u + BTN_LONG_MS) == BTN_HOLD_LONG,
          "the threshold holds across the 49-day wrap");
    btn_hold_t z = {0};
    CHECK(btn_hold_step(&z, -1, 5) == BTN_HOLD_NONE, "a release with no press before it is nothing");
}

static void test_boot_held_opens_the_clock_from_anywhere_but_the_clock(void)
{
    sh_state_t st = {0};
    sh_facts_t f = {0};
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).first == SH_ACT_OPEN_CLOCK, "from home");
    f.app_open = true;
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_BOOT_LONG);
    CHECK(r.first == SH_ACT_OPEN_CLOCK && r.fallback == SH_ACT_NONE, "from an app, which is not asked");
    f.clock_open = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).first == SH_ACT_NONE, "on the clock it does nothing");
    f.clock_open = false;
    f.notices = 1;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).first == SH_ACT_OPEN_CLOCK,
          "over a notice too: the face keeps it as its badge");
    f.notices = 0;
    f.talking = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).first == SH_ACT_CANCEL_TALK,
          "over the talk ring it is BOOT: cancel");
    f.talking = false;
    f.alarm_ringing = true;
    r = sh_route_button(&st, &f, SH_BTN_BOOT_LONG);
    CHECK(r.first == SH_ACT_TO_APP && r.fallback == SH_ACT_STOP_ALARM, "on a ringing alarm it is BOOT: stop");
    f.alarm_ringing = false;
    f.dark = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).first == SH_ACT_WAKE, "on a dark screen it only wakes");
}

static void test_the_pickup_waits_three_seconds_backs_off_and_never_jumps_the_manifest(void)
{
    dbg_sched_t s;
    uint32_t sleep = 0;
    dbg_sched_init(&s, 1000);
    CHECK(dbg_sched_next(&s, 1000, 61000, true, &sleep) == DBG_NEXT_POLL, "the first pickup is at once");
    dbg_sched_done(&s, 1000, DBG_POLL_EMPTY);
    CHECK(dbg_sched_next(&s, 1000, 61000, true, &sleep) == DBG_NEXT_SLEEP && sleep == DBG_POLL_MS,
          "nothing waiting: three seconds");
    dbg_sched_done(&s, 4000, DBG_POLL_CMD);
    CHECK(dbg_sched_next(&s, 4000, 61000, true, &sleep) == DBG_NEXT_SLEEP && sleep == DBG_POLL_AFTER_CMD_MS,
          "after a command, sooner: the session is likely to ask again");
    uint32_t gaps[7];
    for (int i = 0; i < 7; i++) {
        dbg_sched_done(&s, 0, DBG_POLL_FAIL);
        gaps[i] = s.due_ms;
    }
    CHECK(gaps[0] == 6000 && gaps[1] == 12000 && gaps[2] == 24000 && gaps[3] == 48000 &&
              gaps[4] == DBG_POLL_MAX_MS && gaps[6] == DBG_POLL_MAX_MS,
          "a dead box is asked at 6, 12, 24, 48, then every 60 s");
    dbg_sched_done(&s, 0, DBG_POLL_EMPTY);
    CHECK(s.due_ms == DBG_POLL_MS && s.fails == 0, "one answer and it is back to three seconds");

    dbg_sched_init(&s, 0);
    CHECK(dbg_sched_next(&s, 5000, 5000, true, &sleep) == DBG_NEXT_CHECK,
          "a due manifest goes before a due pickup");
    CHECK(dbg_sched_next(&s, 5000, 6000, false, &sleep) == DBG_NEXT_SLEEP && sleep == 1000,
          "offline, nothing but the manifest check is scheduled");
    dbg_sched_init(&s, 0xFFFFF000u);
    dbg_sched_done(&s, 0xFFFFF000u, DBG_POLL_EMPTY);
    CHECK(dbg_sched_next(&s, 0xFFFFF000u, 0x00010000u, true, &sleep) == DBG_NEXT_SLEEP && sleep == DBG_POLL_MS,
          "across the 49-day wrap the pickup is still three seconds out");
    CHECK(dbg_sched_next(&s, 0x00001000u, 0x00010000u, true, &sleep) == DBG_NEXT_POLL, "and due after it");
}

static void test_the_relay_reports_itself_in_one_token(void)
{
    dbg_stats_t s = {.polls_ok = 1200, .polls_err = 3, .cmds = 4, .last_cmd = "screenshot",
                     .last_status = 204, .shot_bytes = 31012, .shot_ms = 240};
    char out[96];
    dbg_stats_note(&s, out, sizeof out);
    CHECK(!strcmp(out, "dbg=ok:1200,err:3,cmds:4,last:screenshot/204,shot:31012/240"), "dbg= as the README reads it");
    dbg_stats_t z = {.last_status = -1};
    dbg_stats_note(&z, out, sizeof out);
    CHECK(!strcmp(out, "dbg=ok:0,err:0,cmds:0,last:-/-1,shot:0/0"), "and before any command");
}

static void test_a_request_id_is_uuid_text_or_it_is_dropped(void)
{
    CHECK(dbg_req_id_ok("0b6c2a7e-41d3-4f0a-9a51-3d2e8c1f7b90"), "the box's ids pass");
    CHECK(!dbg_req_id_ok(""), "an empty id does not");
    CHECK(!dbg_req_id_ok("../telemetry"), "a path does not");
    CHECK(!dbg_req_id_ok("abc?x=1"), "a query does not");
    CHECK(!dbg_req_id_ok("0b6c2a7e-41d3-4f0a-9a51-3d2e8c1f7b90-0000"), "nor one longer than the request holds");
}

static void test_a_registered_secret_is_scrubbed_from_a_log_line_even_cut_short(void)
{
    static const char token[] = "dk_live_9f8e7d6c5b4a39281706f5e4";
    static const char pass[] = "correct horse";
    dbg_secret_clear();
    dbg_secret_add(token);
    dbg_secret_add(pass);
    CHECK(dbg_secret_registered(token) && dbg_secret_registered(pass) && !dbg_secret_registered("dk_live"),
          "registration is by exact value");
    dbg_log_reset();
    dbg_log_line("auth Bearer dk_live_9f8e7d6c5b4a39281706f5e4 ok");
    char line[200];
    snprintf(line, sizeof line, "I (5) wifi: joining with '%s' then '%s'\n", pass, pass);
    dbg_log_line(line);
    /* The line limit lands inside the token: what is left is still most of it. */
    char fmt[DBG_LOG_LINE_MAX + 64];
    memset(fmt, '.', DBG_LOG_LINE_MAX - 12);
    snprintf(fmt + DBG_LOG_LINE_MAX - 12, sizeof fmt - (DBG_LOG_LINE_MAX - 12), "%s\n", token);
    log_via_hook("%s", fmt);
    static char buf[4096];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_json_obj_begin(&w, NULL);
    dbg_log_write(&w, "lines");
    dbg_json_obj_end(&w);
    CHECK(!strstr(buf, "dk_live_9f8e") && !strstr(buf, "horse"), "neither secret, whole or cut, is in the dump");
    CHECK(strstr(buf, "\"auth Bearer " DBG_REDACTED " ok\""), "a whole one is replaced and the line kept");
    CHECK(strstr(buf, "joining with '" DBG_REDACTED "' then '" DBG_REDACTED "'"), "every occurrence of it");
    CHECK(strstr(buf, "......" DBG_REDACTED "\""), "a clipped one is cut at its start and marked");
    char plain[] = "a line that merely ends in dk_l";
    CHECK(!dbg_secret_scrub(plain, sizeof plain, false), "an unclipped line ending like a secret is left alone");
    char tiny[12] = "x correct h";
    dbg_secret_scrub(tiny, sizeof tiny, true);
    CHECK(strlen(tiny) < sizeof tiny && !strstr(tiny, "corr"), "the marker never overruns the buffer");
    dbg_secret_clear();
}

static void test_the_stream_is_byte_for_byte_what_the_box_decodes(void)
{
    /* The box's own vectors (backend/tests/unit/test_panel_debug_relay.py, `jps1()`), as bytes. */
    static uint8_t out[256];
    mem_t m = {out, 0, sizeof out, 0};
    dbg_shot_t s;
    const uint16_t two[] = {0xFFFF, 0x8410};
    dbg_shot_begin(&s, 2, 1, mem_sink, &m);
    dbg_shot_stripe(&s, 0, 1, two);
    CHECK(dbg_shot_end(&s), "a 2x1 frame encodes");
    static const uint8_t WANT2[] = {'J', 'P', 'S', '1', 2, 0, 1, 0, 0, 0, 1, 0, 6, 0, 0, 0,
                                    0, 0xFF, 0xFF, 0, 0x10, 0x84, 0xFF, 0xFF};
    CHECK(m.n == sizeof WANT2 && !memcmp(out, WANT2, sizeof WANT2),
          "white and mid-grey: little-endian RGB565, not byte-swapped for the glass");

    m.n = 0;
    const uint16_t px[] = {1, 1, 2, 2, 3, 3, 4, 4};
    dbg_shot_begin(&s, 4, 2, mem_sink, &m);
    dbg_shot_stripe(&s, 0, 1, px);
    dbg_shot_stripe(&s, 1, 1, px + 4);
    dbg_shot_end(&s);
    static const uint8_t WANT4[] = {'J', 'P', 'S', '1', 4, 0, 2, 0, 0, 0, 1, 0, 6, 0, 0, 0, 1, 1, 0, 1,
                                    2, 0, 1, 0, 1, 0, 6, 0, 0, 0, 1, 3, 0, 1, 4, 0, 0xFF, 0xFF};
    CHECK(m.n == sizeof WANT4 && !memcmp(out, WANT4, sizeof WANT4), "the box's one-row-stripe vector, exactly");

    static uint16_t row[300];
    static uint8_t big[2048];
    mem_t b = {big, 0, sizeof big, 0};
    for (int i = 0; i < 300; i++) row[i] = 0x1234;
    dbg_shot_begin(&s, 128, 1, mem_sink, &b);
    dbg_shot_stripe(&s, 0, 1, row);
    dbg_shot_end(&s);
    CHECK(big[16] == 127 && b.n == 8 + 8 + 3 + 2, "128 alike is one run of the most a byte says");
    b.n = 0;
    dbg_shot_begin(&s, 129, 1, mem_sink, &b);
    dbg_shot_stripe(&s, 0, 1, row);
    dbg_shot_end(&s);
    CHECK(big[16] == 127 && big[19] == 0 && b.n == 8 + 8 + 6 + 2, "129 is that and a run of one");
    b.n = 0;
    dbg_shot_begin(&s, 100, 3, mem_sink, &b);
    dbg_shot_stripe(&s, 0, 3, row);
    dbg_shot_end(&s);
    CHECK(big[16] == 127 && big[19] == 127 && big[22] == 43 && b.n == 8 + 8 + 9 + 2,
          "a run carries on across rows inside a stripe");
    b.n = 0;
    dbg_shot_begin(&s, 4, 2, mem_sink, &b);
    dbg_shot_stripe(&s, 0, 1, row);
    dbg_shot_stripe(&s, 1, 1, row);
    dbg_shot_end(&s);
    CHECK(b.n == 8 + 2 * (8 + 3) + 2 && big[16] == 3 && big[27] == 3, "but never across a stripe");
    for (int i = 0; i < 300; i++) row[i] = (uint16_t)(i & 1 ? 0xF800 : 0x001F);
    b.n = 0;
    dbg_shot_begin(&s, 64, 1, mem_sink, &b);
    dbg_shot_stripe(&s, 0, 1, row);
    dbg_shot_end(&s);
    CHECK(b.n == 8 + 8 + 64 * 3 + 2 && big[12] == 192, "alternating pixels cost 3 bytes each, as the length says");
    uint16_t back[64];
    CHECK(dbg_shot_decode(big, b.n, back, 64, 1) && !memcmp(back, row, sizeof back), "and decode back");
    dbg_shot_begin(&s, 64, 1, mem_sink, &b);
    CHECK(!dbg_shot_stripe(&s, 0, 0, row), "a stripe of no rows fails the capture rather than send one");
}

/* ---- 0.0.10: sound, and the talk to Jerv ---- */

static void test_the_ring_never_overwrites_and_wraps(void)
{
    uint8_t mem[8], out[8];
    tk_ring_t r;
    tk_ring_init(&r, mem, sizeof mem);
    CHECK(tk_ring_write(&r, "abcdef", 6) == 6 && tk_ring_used(&r) == 6, "it takes what fits");
    CHECK(tk_ring_write(&r, "ghij", 4) == 2 && tk_ring_free(&r) == 0,
          "a full ring takes no more: the producer never overwrites unread bytes");
    CHECK(tk_ring_read(&r, out, 5) == 5 && !memcmp(out, "abcde", 5), "the oldest come out first");
    CHECK(tk_ring_write(&r, "KLMN", 4) == 4, "freed room is reused across the end");
    CHECK(tk_ring_read(&r, out, 8) == 7 && !memcmp(out, "fghKLMN", 7),
          "and reads back in order across the wrap");
    CHECK(tk_ring_read(&r, out, 4) == 0, "the consumer never reads past the producer");
}

static void test_a_hold_records_sends_and_hears_back(void)
{
    tk_t t;
    tk_init(&t);
    CHECK(tk_begin(&t, 1000, true, 7) == TK_BEGIN_OK && tk_audio_want(&t) == TK_AUDIO_MIC,
          "KEY down opens the microphone");
    CHECK(tk_net_claim(&t) && !tk_net_claim(&t), "the main task takes the exchange once");
    CHECK(tk_net_next(&t) == TK_NET_UPLOAD, "while KEY is held the recording goes up");
    t.mic_on = true;
    CHECK(tk_end(&t, 4500, true) == TALK_SENT && t.held_ms == 3500, "the release sends");
    CHECK(tk_net_next(&t) == TK_NET_FINISH && tk_audio_want(&t) == TK_AUDIO_OFF,
          "the upload finishes and the microphone closes");
    CHECK(tk_replying(&t) && tk_begin(&t, 4600, true, 9) == TK_BEGIN_BUSY,
          "a press while Jerv thinks starts nothing");
    t.mic_on = false;
    tk_net_reply(&t, true);
    CHECK(t.phase == TK_PLAY && tk_audio_want(&t) == TK_AUDIO_SPK && tk_net_wants_audio(&t),
          "the words come back and the speech plays");
    char why[40];
    CHECK(tk_take(&t, why, sizeof why) == TK_RES_REPLY && tk_take(&t, why, sizeof why) == TK_RES_NONE,
          "the glass hears of the reply once");
    tk_audio_drained(&t);
    CHECK(t.phase == TK_PLAY, "the speaker is not done while the network may still send");
    tk_net_end(&t);
    tk_audio_drained(&t);
    CHECK(t.phase == TK_IDLE && tk_take(&t, why, sizeof why) == TK_RES_SPOKEN,
          "once all of it has played, the reply is spoken");
    CHECK(tk_quiet(&t), "and nothing touches the ring any more");
}

static void test_a_knock_or_a_cancel_lets_go_of_everything(void)
{
    tk_t t;
    tk_init(&t);
    tk_begin(&t, 0, true, 1);
    tk_net_claim(&t);
    CHECK(tk_end(&t, 300, true) == TALK_TOO_SHORT && t.abort && tk_net_next(&t) == TK_NET_ABORT,
          "a knock drops the upload, so the box hears no turn");
    tk_net_end(&t);
    CHECK(tk_begin(&t, 1000, true, 3) == TK_BEGIN_OK && !t.abort, "and the next hold starts clean");
    CHECK(tk_end(&t, 9000, false) == TALK_CANCELLED && tk_audio_want(&t) == TK_AUDIO_OFF,
          "a tap on the glass cancels however long the hold");
    t.mic_on = true;
    CHECK(tk_begin(&t, 9100, true, 5) == TK_BEGIN_BUSY,
          "a hold waits for the microphone a dropped one still has open");
}

static void test_key_or_boot_over_a_reply_stops_it(void)
{
    tk_t t;
    tk_init(&t);
    tk_begin(&t, 0, true, 1);
    tk_net_claim(&t);
    tk_end(&t, 2000, true);
    tk_net_reply(&t, true);
    CHECK(tk_begin(&t, 3000, true, 3) == TK_BEGIN_STOPPED && t.phase == TK_IDLE && t.abort,
          "KEY while Jerv speaks stops the speech and records nothing");
    CHECK(!tk_net_wants_audio(&t) && tk_audio_want(&t) == TK_AUDIO_OFF,
          "the network stops sending it and the speaker closes");
    tk_net_end(&t);
    tk_init(&t);
    tk_begin(&t, 0, true, 1);
    tk_net_claim(&t);
    tk_end(&t, 2000, true);
    CHECK(tk_stop(&t) && !tk_replying(&t), "BOOT while Jerv thinks drops the wait");
    tk_net_reply(&t, true);
    CHECK(t.phase == TK_IDLE && t.res == TK_RES_NONE, "and an answer arriving after is not shown");
    CHECK(!tk_stop(&t), "with nothing to stop, BOOT is BOOT again");
}

static void test_a_box_that_drops_mid_hold_is_told_at_the_release(void)
{
    tk_t t;
    tk_init(&t);
    tk_begin(&t, 0, true, 1);
    tk_net_claim(&t);
    tk_net_fail(&t, "the upload failed");
    CHECK(tk_audio_want(&t) == TK_AUDIO_OFF, "the microphone stops when nothing is listening");
    CHECK(tk_end(&t, 5000, true) == TALK_FAILED && t.res == TK_RES_FAILED && !strcmp(t.why, "the upload failed"),
          "the release learns it was not sent, and why");
}

static void test_text_only_never_opens_the_speaker(void)
{
    tk_t t;
    tk_init(&t);
    tk_begin(&t, 0, false, 1);
    tk_net_claim(&t);
    tk_end(&t, 2000, true);
    tk_net_reply(&t, true);
    CHECK(t.phase == TK_IDLE && t.res == TK_RES_REPLY && tk_audio_want(&t) == TK_AUDIO_OFF,
          "with read-aloud off the words come and the speaker stays shut");
    tk_init(&t);
    tk_begin(&t, 0, true, 1);
    tk_net_claim(&t);
    tk_end(&t, 2000, true);
    tk_net_nothing(&t);
    CHECK(t.phase == TK_IDLE && t.res == TK_RES_NOTHING, "silence is its own answer, not a failure");
}

/* A JT2 record as the box writes it (backend `panel_stream.record`). */
static size_t rec(uint8_t *b, uint8_t type, const void *payload, size_t n)
{
    b[0] = type;
    b[1] = (uint8_t)n;
    b[2] = (uint8_t)(n >> 8);
    memcpy(b + 3, payload, n);
    return 3 + n;
}

/* A turn's whole stream: transcript, two statuses, a keep-alive, an unknown record, the answer
 * with its speech in two records, END. Returns its length; `pcm` gets the speech. */
static size_t jt2_turn(uint8_t *b, uint8_t *pcm, size_t *pcm_n)
{
    size_t k = 0;
    memcpy(b, "JT2", 3);
    k = 3;
    k += rec(b + k, TS_HEARD, "high tide at Port St John", 25);
    k += rec(b + k, TS_STATUS, "Thinking\xE2\x80\xA6", 11);
    k += rec(b + k, TS_ALIVE, "", 0);
    k += rec(b + k, 0x7F, "a record from a newer box", 25);
    k += rec(b + k, TS_STATUS, "Searching the web: tides\xE2\x80\xA6", 27);
    uint8_t answer[64] = {TS_ANSWER_SPEECH};
    memcpy(answer + 1, "High tide is at 4:12 pm.", 24);
    k += rec(b + k, TS_ANSWER, answer, 25);
    for (size_t i = 0; i < 1000; i++) pcm[i] = (uint8_t)(i * 7);
    k += rec(b + k, TS_PCM, pcm, 600);
    k += rec(b + k, TS_PCM, pcm + 600, 400);
    *pcm_n = 1000;
    const uint8_t end[] = {TS_END_OK};
    k += rec(b + k, TS_END, end, 1);
    return k;
}

typedef struct {
    int heard, status, alive, answer, end, bad;
    uint8_t flags, code;
    char last_status[64], answer_text[64];
    uint8_t pcm[1200];
    size_t pcm_n;
} jt2_seen_t;

/* Feeds `b` in slices of `step` bytes, as reads off the radio would land, and tallies. */
static void jt2_read(const uint8_t *b, size_t n, size_t step, jt2_seen_t *out)
{
    tk_stream_t *s = malloc(sizeof *s);
    tk_stream_init(s);
    memset(out, 0, sizeof *out);
    for (size_t at = 0; at < n && !tk_stream_bad(s);) {
        const size_t len = n - at < step ? n - at : step;
        size_t off = 0;
        while (off < len) {
            tk_stream_ev_t ev;
            const uint8_t *pcm;
            size_t pn;
            const size_t used = tk_stream_feed(s, b + at + off, len - off, &ev, &pcm, &pn);
            off += used;
            switch (ev) {
            case TS_EV_HEARD: out->heard++; break;
            case TS_EV_STATUS:
                out->status++;
                tk_fit(out->last_status, sizeof out->last_status, s->text);
                break;
            case TS_EV_ALIVE: out->alive++; break;
            case TS_EV_ANSWER:
                out->answer++;
                out->flags = s->flags;
                tk_fit(out->answer_text, sizeof out->answer_text, s->text);
                break;
            case TS_EV_PCM:
                memcpy(out->pcm + out->pcm_n, pcm, pn);
                out->pcm_n += pn;
                break;
            case TS_EV_END:
                out->end++;
                out->code = s->flags;
                break;
            case TS_EV_BAD: out->bad++; break;
            default: break;
            }
            if (!used || ev == TS_EV_BAD) break;
        }
        at += len;
    }
    free(s);
}

static void test_the_reply_stream_reads_in_any_split(void)
{
    uint8_t b[1400], pcm[1000];
    size_t pcm_n;
    const size_t n = jt2_turn(b, pcm, &pcm_n);
    static const size_t STEPS[] = {4096, 1, 2, 3, 7, 250, 1024};
    for (size_t i = 0; i < sizeof STEPS / sizeof *STEPS; i++) {
        jt2_seen_t got;
        jt2_read(b, n, STEPS[i], &got);
        CHECK(got.heard == 1 && got.status == 2 && got.alive == 1 && got.answer == 1 && got.end == 1 &&
                  !got.bad,
              "every record lands once, however the reads split it");
        CHECK(!strcmp(got.last_status, "Searching the web: tides\xE2\x80\xA6") &&
                  !strcmp(got.answer_text, "High tide is at 4:12 pm.") &&
                  (got.flags & TS_ANSWER_SPEECH) && got.code == TS_END_OK,
              "the words, the flags and the end code come out whole");
        CHECK(got.pcm_n == pcm_n && !memcmp(got.pcm, pcm, pcm_n),
              "the speech comes out byte for byte, in the slices it arrived in");
    }
    tk_stream_t *s = malloc(sizeof *s);
    tk_stream_init(s);
    tk_stream_ev_t ev;
    const uint8_t *p;
    size_t pn;
    size_t off = 0, events = 0;
    while (off < n) {
        off += tk_stream_feed(s, b + off, n - off, &ev, &p, &pn);
        events += ev != TS_EV_NONE;
    }
    CHECK(s->skipped == 1 && s->records == 9 && events == 8,
          "a record type this panel does not know is skipped by its length");
    CHECK(tk_stream_done(s) && tk_stream_feed(s, (const uint8_t *)"JUNK", 4, &ev, &p, &pn) == 4 &&
              ev == TS_EV_NONE,
          "after END the rest of the body is consumed and ignored");
    free(s);
}

static void test_a_stream_that_is_not_jt2_or_too_big_is_refused_cleanly(void)
{
    jt2_seen_t got;
    jt2_read((const uint8_t *)"<html>", 6, 64, &got);
    CHECK(got.bad == 1 && !got.heard, "an error page is refused at its first byte");
    jt2_read((const uint8_t *)"JT1\x01\x02\x00hi", 8, 64, &got);
    CHECK(got.bad == 1, "a JT1 frame is not read as JT2");
    uint8_t big[16] = {'J', 'T', '2', TS_PCM, (TS_MAX_RECORD + 1) & 0xFF, (TS_MAX_RECORD + 1) >> 8};
    jt2_read(big, 6, 64, &got);
    CHECK(got.bad == 1 && !got.pcm_n, "a record longer than any the box sends is refused, not buffered");
    tk_stream_t *s = malloc(sizeof *s);
    tk_stream_init(s);
    tk_stream_ev_t ev;
    const uint8_t *p;
    size_t pn;
    tk_stream_feed(s, big, 6, &ev, &p, &pn);
    CHECK(tk_stream_bad(s) && tk_stream_feed(s, big, 6, &ev, &p, &pn) == 0,
          "and nothing more is read once it is");
    free(s);
}

static void test_an_answer_longer_than_the_panel_keeps_is_cut_on_a_character(void)
{
    /* 700 two-byte letters: 1,400 bytes, more than the 1 KB the panel keeps. */
    static uint8_t b[1600];
    uint8_t answer[1401] = {0};
    for (int i = 0; i < 700; i++) memcpy(answer + 1 + 2 * i, "\xC3\xA9", 2);
    memcpy(b, "JT2", 3);
    const size_t n = 3 + rec(b + 3, TS_ANSWER, answer, sizeof answer);
    tk_stream_t *s = malloc(sizeof *s);
    tk_stream_init(s);
    tk_stream_ev_t ev = TS_EV_NONE;
    const uint8_t *p;
    size_t pn, off = 0;
    while (off < n && ev != TS_EV_ANSWER) off += tk_stream_feed(s, b + off, n - off, &ev, &p, &pn);
    const size_t len = strlen(s->text);
    CHECK(ev == TS_EV_ANSWER && len < TS_TEXT_CAP && !strcmp(s->text + len - 3, "\xE2\x80\xA6") &&
              (len - 3) % 2 == 0,
          "an answer longer than the panel keeps is cut on a character, with an ellipsis");
    free(s);
    char fit[8];
    tk_fit(fit, sizeof fit, "\xC3\xA9\xC3\xA9\xC3\xA9\xC3\xA9");
    CHECK(!strcmp(fit, "\xC3\xA9\xC3\xA9\xE2\x80\xA6"), "a bubble's copy is cut the same way");
    tk_fit(fit, sizeof fit, "short");
    CHECK(!strcmp(fit, "short"), "and what fits is copied whole");
}

static void test_the_voice_starts_only_once_the_amp_has_settled_and_the_ring_has_filled(void)
{
    CHECK(!tk_spk_may_start(0, 16384, true) && !tk_spk_may_start(TK_SPK_SETTLE_MS - 1, 16384, false),
          "never before the amp has had its settle time, however full the ring");
    CHECK(!tk_spk_may_start(TK_SPK_SETTLE_MS, TK_SPK_START_BYTES - 1, true),
          "nor while less than 150 ms is queued and more is coming");
    CHECK(tk_spk_may_start(TK_SPK_SETTLE_MS, TK_SPK_START_BYTES, true),
          "settled and filled: the first word plays whole");
    CHECK(tk_spk_may_start(TK_SPK_SETTLE_MS + 500, 640, false),
          "a short reply whose stream has ended plays what it has");
    CHECK(TK_SPK_START_BYTES == 150 * TALK_RATE * 2 / 1000 && TK_SPK_SETTLE_MS >= 100 &&
              TK_SPK_SETTLE_MS <= 250 && TK_SPK_TAIL_MS >= 80,
          "150 ms queued, a settle of 100-250 ms, a tail past the DMA's 80 ms");
}

static void test_two_microphones_average_and_each_peak_is_kept(void)
{
    const int16_t lr[] = {1000, 3000, -32768, 0, 10, -20};
    int16_t mono[3];
    uint16_t pl, pr;
    talk_mono(lr, 3, mono, &pl, &pr);
    CHECK(mono[0] == 2000 && mono[1] == -16384 && mono[2] == -5, "the box gets the two averaged");
    CHECK(pl == 32767 && pr == 3000, "and each microphone's peak is kept, so a dead one shows");
    CHECK(talk_level(0) == 0 && talk_level(31) == 0 && talk_level(32767) == 100 &&
              talk_level(4000) > talk_level(500),
          "the ring pulses with the voice, quiet at -60 dB and full at full scale");
}

static void test_the_audio_token(void)
{
    tk_stats_t s = {.talks = 3, .sent = 2, .replies = 1, .failed = 1, .up_bytes = 65536,
                    .down_bytes = 2048, .reply_ms = 2400, .reply_ms_max = 5100, .underruns = 4};
    char out[200];
    tk_stats_note(&s, "ok", out, sizeof out);
    CHECK(!strcmp(out, "audio=ok,talks:3,sent:2,heard:1,none:0,fail:1,stop:0,up:64K,down:2K,"
                       "ms:2400/5100,under:4,over:0"),
          "telemetry says what the codecs did and what each talk cost");
}

static msg_t last_jerv(void)
{
    msg_t m = {0};
    svc_messages_get("jerv", (uint16_t)(svc_messages_count("jerv") - 1), &m);
    return m;
}

static void test_a_talk_on_the_glass_end_to_end(void)
{
    svc_messages_fake_clear();
    svc_audio_init();
    svc_settings_set(SET_TTS, 1);
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv") && svc_audio_talking(), "KEY down records");
    svc_rtc_fake_advance_ms(3200);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "a three-second hold is sent");
    msg_t m = last_jerv();
    CHECK(m.mine && m.voice && m.pending && m.dur_s == 3 && !strcmp(m.text, TALK_HEARING),
          "the bubble says it is being heard, never a made-up transcript");
    CHECK(svc_audio_replying(), "and Jerv is thinking");
    char status[64];
    CHECK(!svc_audio_status("jerv", status, sizeof status),
          "no status line until the box has heard the question");
    svc_audio_fake_progress("what time is sunset", NULL);
    svc_audio_service();
    m = last_jerv();
    CHECK(!m.pending && !strcmp(m.text, "what time is sunset") && svc_messages_count("jerv") == 1,
          "the transcript replaces \"Transcribing…\" the moment the box has it");
    CHECK(svc_audio_status("jerv", status, sizeof status) && !strcmp(status, "Thinking…"),
          "and the status line says Jerv is thinking");
    svc_audio_fake_progress(NULL, "Searching the web: sunset…");
    svc_audio_service();
    CHECK(svc_audio_status("jerv", status, sizeof status) &&
              !strcmp(status, "Searching the web: sunset…") && !svc_audio_status("dave", status, sizeof status),
          "each step the agent takes replaces the line, in its own thread only");
    CHECK(svc_audio_replying() && !svc_audio_speaking(), "thinking, not speaking");
    svc_audio_fake_reply("what time is sunset", "About seven tonight.", true);
    svc_audio_service();
    CHECK(svc_messages_count("jerv") == 2, "the reply arrives as a second bubble");
    msg_t heard;
    svc_messages_get("jerv", 0, &heard);
    m = last_jerv();
    CHECK(!heard.pending && !strcmp(heard.text, "what time is sunset"),
          "the voice bubble now holds what the box heard");
    CHECK(!m.mine && !strcmp(m.text, "About seven tonight.") && svc_audio_playing() == m.id,
          "Jerv's words are shown while its voice plays");
    CHECK(!svc_audio_status("jerv", status, sizeof status) && svc_audio_speaking(),
          "the answer replaces the status line");
    CHECK(svc_audio_stop_reply() && svc_audio_playing() == 0 && !svc_audio_replying(),
          "BOOT stops the voice; the words stay");
    svc_audio_service();
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv"), "the next hold may start");
    svc_rtc_fake_advance_ms(2000);
    svc_audio_talk_end(true);
    CHECK(svc_audio_stop_reply(), "BOOT while Jerv thinks");
    m = last_jerv();
    CHECK(!m.pending && !strcmp(m.status, "Stopped"), "leaves the bubble saying it was stopped");
    svc_audio_fake_reply("late", "too late", false);
    svc_audio_service();
    CHECK(svc_messages_count("jerv") == 3, "and an answer arriving after is never drawn");
    svc_settings_set(SET_TTS, 1);
}

static void test_a_hold_that_cannot_reach_jerv_is_refused_up_front(void)
{
    svc_net_fake_set(NET_OFF);
    CHECK(!svc_audio_talk_begin(TALK_TO_AGENT, "jerv") && strstr(svc_audio_talk_refused(), "Wi-Fi"),
          "offline, KEY says why instead of recording into nothing");
    svc_net_fake_set(NET_CONNECTED);
    CHECK(svc_audio_talk_begin(TALK_TO_PERSON, SVC_MESSAGES_THREAD_1), "a hold to a person still starts");
    svc_rtc_fake_advance_ms(1500);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "and still ends honestly unsent");
}

static void test_key_or_boot_over_a_reply_is_the_shells_stop(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.replying = f.speaking = true;
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_STOP_REPLY && st.key_owner == SH_KEY_FREE,
          "KEY while Jerv speaks stops the voice, in any app, and takes no hold");
    r = sh_route_button(&st, &f, SH_BTN_KEY_UP);
    CHECK(r.first == SH_ACT_NONE, "so its release sends nothing");
    r = sh_route_button(&st, &f, SH_BTN_BOOT);
    CHECK(r.first == SH_ACT_STOP_REPLY, "BOOT while Jerv speaks stops it rather than going back");
    f.speaking = false;
    r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_STILL_WORKING && st.key_owner == SH_KEY_FREE,
          "KEY while he is still working says so and starts no new talk");
    r = sh_route_button(&st, &f, SH_BTN_KEY_UP);
    CHECK(r.first == SH_ACT_NONE, "and its release sends nothing");
    r = sh_route_button(&st, &f, SH_BTN_BOOT);
    CHECK(r.first == SH_ACT_TO_APP && r.fallback == SH_ACT_GO_HOME,
          "BOOT while he works is ordinary BOOT: it leaves the thread, the turn goes on");
    f = home();
    f.replying = true;
    f.alarm_ringing = true;
    r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.fallback == SH_ACT_SNOOZE_ALARM, "a ringing alarm still takes KEY first");
    f = home();
    r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_TALK_JERV, "with no reply, KEY is Jerv's again");
}

static void test_the_speaker_settings(void)
{
    CHECK(settings_volume_from_slider(3) == VOLUME_FLOOR && settings_volume_from_slider(73) == 75 &&
              settings_volume_from_slider(100) == 100,
          "the volume slider snaps to fives and never reaches silence");
    CHECK(svc_settings_clamp(SET_VOLUME, 0) == VOLUME_FLOOR && svc_settings_clamp(SET_MIC_GAIN, 99) == MIC_GAIN_MAX &&
              svc_settings_clamp(SET_MIC_GAIN, -3) == 0,
          "whoever writes them, volume and microphone gain stay in range");
    CHECK(!strcmp(svc_settings_name(SET_VOLUME), "volume") && !strcmp(svc_settings_name(SET_MIC_GAIN), "mic_gain") &&
              svc_settings_default(SET_VOLUME) == 80 && svc_settings_default(SET_MIC_GAIN) == 24,
          "saved under short NVS keys; the microphone 6 dB under 0.0.10's near-clipping 30");
    bool spk;
    CHECK(!strcmp(settings_jerv_note(true, "ok", &spk), "Replies are spoken and shown as text.") && spk,
          "with sound working the card says what a reply does");
    CHECK(strstr(settings_jerv_note(true, "failed:i2c", &spk), "cannot record") && !spk,
          "a codec that did not start is said plainly");
    CHECK(strstr(settings_jerv_note(true, "mic-only:es8311", &spk), "text only") && !spk,
          "a silent speaker means text-only replies, and no volume slider");
}

static void test_play_tone_is_whitelisted_and_harmless(void)
{
    const dbg_cmd_info_t *c = dbg_cmd_find("play_tone");
    CHECK(c && !c->destructive && c->idempotent, "the box may ask for a tone and retry it");
}

/* ---- 0.0.11: the conversation menu, two agents, the siblings ---- */

static void test_a_still_finger_is_a_long_press_once_and_a_scroll_never(void)
{
    sh_lp_t lp = {0};
    CHECK(!sh_long_press(&lp, true, 100, 200, 1000), "a finger lands");
    CHECK(!sh_long_press(&lp, true, 105, 204, 1000 + SH_LONG_PRESS_MS - 1), "not before the threshold");
    CHECK(sh_long_press(&lp, true, 108, 206, 1000 + SH_LONG_PRESS_MS), "held still past it: a long press");
    CHECK(!sh_long_press(&lp, true, 108, 206, 1000 + 3 * SH_LONG_PRESS_MS), "once per stroke");
    CHECK(!sh_long_press(&lp, false, 0, 0, 5000), "lifted");
    CHECK(!sh_long_press(&lp, true, 100, 200, 6000), "the next stroke starts over");
    CHECK(!sh_long_press(&lp, true, 100, 200 + SH_TAP_SLOP, 6100), "it moves a scroll's distance");
    CHECK(!sh_long_press(&lp, true, 100, 200, 6000 + 2 * SH_LONG_PRESS_MS),
          "and coming back to rest does not make a scroll a long press");
    sh_long_press(&lp, false, 0, 0, 9000);
    CHECK(!sh_long_press(&lp, true, 300, 300, 10000) && !sh_long_press(&lp, false, 0, 0, 10300),
          "a tap is never one");
}

static void test_the_clock_face_opens_its_alarms_only_on_a_two_second_hold(void)
{
    ui_hold_t h = {0};
    CHECK(ui_hold_feed(&h, true, 240, 240, 0, CLOCK_HOLD_MS) == UI_HOLD_GROWING &&
              ui_hold_feed(&h, false, 240, 240, 120, CLOCK_HOLD_MS) == UI_HOLD_IDLE,
          "a tap opens nothing");
    ui_hold_feed(&h, true, 240, 240, 1000, CLOCK_HOLD_MS);
    CHECK(ui_hold_feed(&h, true, 244, 238, 1000 + 1900, CLOCK_HOLD_MS) == UI_HOLD_GROWING &&
              ui_hold_feed(&h, false, 0, 0, 1000 + 1950, CLOCK_HOLD_MS) == UI_HOLD_IDLE,
          "1.9 s held and let go opens nothing");
    ui_hold_feed(&h, true, 240, 240, 5000, CLOCK_HOLD_MS);
    CHECK(ui_hold_feed(&h, true, 240, 240, 5000 + CLOCK_HOLD_MS, CLOCK_HOLD_MS) == UI_HOLD_FIRE,
          "2 s held still opens the alarms list");
    CHECK(ui_hold_feed(&h, true, 240, 240, 5000 + 3 * CLOCK_HOLD_MS, CLOCK_HOLD_MS) == UI_HOLD_IDLE,
          "once: holding on does not open it again");
    ui_hold_feed(&h, false, 0, 0, 12000, CLOCK_HOLD_MS);
    ui_hold_feed(&h, true, 240, 240, 13000, CLOCK_HOLD_MS);
    CHECK(ui_hold_feed(&h, true, 240 + UI_HOLD_SLOP, 240, 13500, CLOCK_HOLD_MS) == UI_HOLD_IDLE &&
              ui_hold_feed(&h, true, 240, 240, 13000 + 2 * CLOCK_HOLD_MS, CLOCK_HOLD_MS) == UI_HOLD_IDLE,
          "a drag cancels it, even if the finger comes back and waits");
    ui_hold_feed(&h, false, 0, 0, 20000, CLOCK_HOLD_MS);
    ui_hold_feed(&h, true, 240, 240, 30000, CLOCK_HOLD_MS);
    CHECK(ui_hold_permille(&h, 30000 + UI_HOLD_SHOW_MS, CLOCK_HOLD_MS) == 0,
          "no ring for the first 0.3 s: a tap shows nothing");
    CHECK(ui_hold_permille(&h, 30000 + UI_HOLD_SHOW_MS + (CLOCK_HOLD_MS - UI_HOLD_SHOW_MS) / 2,
                           CLOCK_HOLD_MS) == 500 &&
              ui_hold_permille(&h, 30000 + 5 * CLOCK_HOLD_MS, CLOCK_HOLD_MS) == 1000,
          "then the ring fills over the rest of the two seconds");
    ui_hold_feed(&h, false, 0, 0, 40000, CLOCK_HOLD_MS);
    CHECK(ui_hold_permille(&h, 40100, CLOCK_HOLD_MS) == 0, "and lifting the finger empties it");
    CHECK(CLOCK_HOLD_MS >= 2000 && SH_LONG_PRESS_MS < CLOCK_HOLD_MS,
          "the face's hold is the owner's two seconds; a conversation's menu keeps its 600 ms");
}

static void test_an_agent_answer_is_kept_whole_on_the_glass(void)
{
    svc_messages_fake_clear();
    svc_audio_init();
    svc_settings_set(SET_TTS, 0);
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv"), "a hold to Jerv");
    svc_rtc_fake_advance_ms(2000);
    svc_audio_talk_end(true);
    char answer[1001];
    for (int i = 0; i < 100; i++) memcpy(answer + 10 * i, "Tide data ", 10);
    answer[1000] = '\0';
    svc_audio_fake_reply("high tide at Port St John", answer, false);
    svc_audio_service();
    msg_t m = last_jerv();
    const char *shown = svc_messages_text("jerv", &m);
    CHECK(strlen(m.text) < sizeof m.text && strlen(shown) == 1000 && !strncmp(shown, answer, 1000),
          "the answer is shown whole (1,000 bytes), past the 255 a bubble keeps");
    msg_t heard;
    svc_messages_get("jerv", 0, &heard);
    CHECK(svc_messages_text("jerv", &heard) == heard.text, "the question is its bubble's own text");
    svc_messages_clear("jerv");
    CHECK(svc_messages_text("jerv", &m) == m.text, "Clear chat lets the whole answer go too");
    svc_settings_set(SET_TTS, 1);
}

static void test_the_menu_opens_only_over_a_clear_conversation(void)
{
    sh_facts_t f = in_app();
    CHECK(sh_menu_may_open(&f, true), "over a conversation, a long press opens it");
    CHECK(!sh_menu_may_open(&f, false), "over Settings, the Clock or the Minecraft menu, never");
    CHECK(!sh_menu_may_open(&(sh_facts_t){0}, true), "nor on home");
    f.talking = true;
    CHECK(!sh_menu_may_open(&f, true), "nor under a hold");
    f = in_app();
    f.dark = true;
    CHECK(!sh_menu_may_open(&f, true), "nor on a dark screen");
    f = in_app();
    f.notices = 1;
    CHECK(!sh_menu_may_open(&f, true), "nor under a notice");
    f.notices_held = true;
    CHECK(sh_menu_may_open(&f, true), "a notice the face holds as a badge covers nothing");
    f = in_app();
    f.alarm_ringing = true;
    CHECK(!sh_menu_may_open(&f, true), "nor under a ringing alarm");
    f = in_app();
    f.menu_open = true;
    CHECK(!sh_menu_may_open(&f, true), "nor twice");
}

static void test_clear_chat_takes_two_taps_within_three_seconds(void)
{
    sh_menu_t m = {0};
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 0) == SH_MENU_DO_NOTHING, "a closed menu does nothing");
    sh_menu_open(&m);
    CHECK(!sh_menu_armed(&m, 0), "it opens unarmed");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 1000) == SH_MENU_DO_ARM && sh_menu_armed(&m, 1000),
          "the first tap arms it: Tap again to clear");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 1000 + SH_MENU_ARM_MS - 1) == SH_MENU_DO_CLEAR &&
              m.state == SH_MENU_CLOSED,
          "the second inside the window clears and closes");
    sh_menu_open(&m);
    sh_menu_input(&m, SH_MENU_IN_CLEAR, 5000);
    CHECK(!sh_menu_armed(&m, 5000 + SH_MENU_ARM_MS), "the arm lapses");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 5000 + SH_MENU_ARM_MS) == SH_MENU_DO_ARM,
          "and a tap after it only arms again");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CANCEL, 9000) == SH_MENU_DO_CLOSE && m.state == SH_MENU_CLOSED,
          "Cancel closes, clearing nothing");
    sh_menu_open(&m);
    sh_menu_input(&m, SH_MENU_IN_CLEAR, 10000);
    CHECK(sh_menu_input(&m, SH_MENU_IN_OUTSIDE, 10100) == SH_MENU_DO_CLOSE, "a tap outside closes, even armed");
    sh_menu_open(&m);
    CHECK(!m.armed, "and the next opening starts unarmed");
}

static void test_buttons_over_the_menu_only_close_it(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.menu_open = true;
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_CLOSE_MENU && st.key_owner == SH_KEY_FREE,
          "KEY closes the menu and starts no recording");
    f.menu_open = false;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_UP).first == SH_ACT_NONE, "so its release sends nothing");
    f.menu_open = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_CLOSE_MENU, "BOOT closes it, not the app");
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).first == SH_ACT_CLOSE_MENU,
          "BOOT held closes it rather than jumping to the Clock under it");
    f.replying = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_CLOSE_MENU,
          "over a reply too, the menu is what KEY reaches first");
    f.replying = false;
    f.notices = 1;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_NONE, "a notice waits for the menu to close");
    f.alarm_ringing = true;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_ALARM &&
              sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_TO_APP,
          "a ringing alarm outranks it");
}

static void test_jerv_and_dave_are_two_threads_and_two_conversations(void)
{
    char path[96];
    tk_converse_path(TK_AGENT_JERV, true, false, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/converse?persona=jerv&voice=true&jt=2"),
          "Jerv's talk, asking for the JT2 stream");
    tk_converse_path(TK_AGENT_DAVE, false, true, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/converse?persona=dave&voice=false&jt=2&new_thread=true"),
          "Dave's, text only, after Clear chat");
    tk_agent_t a = TK_AGENT_JERV;
    CHECK(tk_agent_from("dave", &a) && a == TK_AGENT_DAVE && !tk_agent_from("thread1", &a),
          "an agent is named by its thread; a person's thread is not one");
    CHECK(!strcmp(tk_agent_id((tk_agent_t)9), "jerv"), "an unknown agent is never sent as one");

    svc_messages_fake_clear();
    svc_audio_init();
    svc_settings_set(SET_TTS, 0);
    svc_messages_fake_add("jerv", &(msg_t){.id = 7, .text = "Jerv said this."});
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "dave"), "KEY in Dave's thread talks to Dave");
    CHECK(strstr(svc_audio_fake_last_path(), "persona=dave") != NULL, "the box is asked for Dave");
    svc_rtc_fake_advance_ms(2000);
    svc_audio_talk_end(true);
    svc_audio_fake_reply("where is my base", "North of spawn.", false);
    svc_audio_service();
    msg_t m;
    CHECK(svc_messages_count("dave") == 2 && svc_messages_get("dave", 1, &m) &&
              !strcmp(m.text, "North of spawn."),
          "Dave's answer is in Dave's thread");
    CHECK(svc_messages_count("jerv") == 1 && svc_messages_get("jerv", 0, &m) &&
              !strcmp(m.text, "Jerv said this."),
          "and Jerv's thread is exactly as it was");

    CHECK(svc_messages_clear("dave") && svc_messages_count("dave") == 0 &&
              svc_messages_count("jerv") == 1,
          "clearing Dave's thread never touches Jerv's");
    CHECK(!svc_messages_clear("quorra"), "there is no thread by a name");
    for (uint32_t i = 0; i < 20; i++) svc_messages_fake_add("dave", &(msg_t){.id = 100 + i});
    CHECK(svc_messages_count("dave") == 8 && svc_messages_count("jerv") == 1,
          "Dave's eight are his own: a busy Dave pushes out none of Jerv's");
    for (uint32_t i = 0; i < 9; i++) svc_messages_fake_add(SVC_MESSAGES_THREAD_1, &(msg_t){.id = 200 + i});
    CHECK(svc_messages_count(SVC_MESSAGES_THREAD_1) == 4, "a person's thread keeps four");
    svc_messages_fake_clear();
    svc_settings_set(SET_TTS, 1);
}

static void test_clear_chat_asks_that_agent_alone_for_a_new_conversation(void)
{
    svc_messages_fake_clear();
    svc_audio_init();
    svc_audio_fresh("jerv");
    CHECK(svc_audio_fresh_pending("jerv") && !svc_audio_fresh_pending("dave"), "Jerv's, not Dave's");
    svc_audio_fresh(SVC_MESSAGES_THREAD_1);
    CHECK(!svc_audio_fresh_pending(SVC_MESSAGES_THREAD_1), "a person's thread has no box conversation");

    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "dave"), "a talk to Dave");
    CHECK(strstr(svc_audio_fake_last_path(), "new_thread") == NULL, "carries no new_thread");
    svc_rtc_fake_advance_ms(1500);
    svc_audio_talk_end(true);
    svc_audio_fake_reply("hi", "Hello.", false);
    svc_audio_service();

    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv"), "the next talk to Jerv");
    CHECK(strstr(svc_audio_fake_last_path(), "persona=jerv") &&
              strstr(svc_audio_fake_last_path(), "&new_thread=true"),
          "asks the box for a new conversation");
    svc_rtc_fake_advance_ms(1500);
    svc_audio_talk_end(true);
    talk_net_nothing();
    talk_net_end(0, 0, 0);
    svc_audio_service();
    CHECK(svc_audio_fresh_pending("jerv"), "a hold the box heard nothing in keeps the request");

    svc_audio_talk_begin(TALK_TO_AGENT, "jerv");
    CHECK(strstr(svc_audio_fake_last_path(), "&new_thread=true") != NULL, "so the next one asks again");
    svc_rtc_fake_advance_ms(1500);
    svc_audio_talk_end(true);
    svc_audio_fake_reply("start over", "Fresh start.", false);
    svc_audio_service();
    CHECK(!svc_audio_fresh_pending("jerv"), "answered, the box has started it");
    svc_audio_talk_begin(TALK_TO_AGENT, "jerv");
    CHECK(strstr(svc_audio_fake_last_path(), "new_thread") == NULL, "and the talk after continues it");
    svc_rtc_fake_advance_ms(100);
    svc_audio_talk_end(false);
    svc_audio_service();
    svc_messages_fake_clear();
}

static const char BOX_JSON[] =
    "{\"siblings\": [{\"device_id\": \"11111111-aaaa-4bbb-8ccc-000000000001\", \"name\": \"Quorra\"},"
    " {\"name\": \"Tamsin\", \"extra\": {\"a\": [1, 2]}, \"device_id\": \"22222222-aaaa-4bbb-8ccc-000000000002\"}]}";

static void test_the_box_s_siblings_json_is_read_as_the_box_writes_it(void)
{
    sib_list_t l;
    CHECK(sib_parse(BOX_JSON, sizeof BOX_JSON - 1, &l) && l.n == 2, "two siblings");
    CHECK(!strcmp(l.s[0].name, "Quorra") && !strcmp(l.s[1].name, "Tamsin") &&
              !strcmp(l.s[1].device_id, "22222222-aaaa-4bbb-8ccc-000000000002"),
          "names and ids, whatever the key order, past keys it does not know");
    const char empty[] = "{\"siblings\":[]}";
    CHECK(sib_parse(empty, sizeof empty - 1, &l) && l.n == 0, "an empty list is a list");
    const char five[] =
        "{\"siblings\":[{\"device_id\":\"a\",\"name\":\"A\"},{\"device_id\":\"b\",\"name\":\"B\"},"
        "{\"device_id\":\"c\",\"name\":\"C\"},{\"device_id\":\"d\",\"name\":\"D\"},"
        "{\"device_id\":\"e\",\"name\":\"E\"},{\"name\":\"no id\"}]}";
    CHECK(sib_parse(five, sizeof five - 1, &l) && l.n == SIB_MAX && !strcmp(l.s[3].name, "D"),
          "at most four, the box's first four");
    const char escaped[] = "{\"siblings\":[{\"device_id\":\"x\",\"name\":\"Zo\\u00eb \\\"Z\\\"\"}]}";
    CHECK(sib_parse(escaped, sizeof escaped - 1, &l) && !strcmp(l.s[0].name, "Zo? \"Z\""),
          "escapes decoded; a letter the font lacks drawn as ?");
    const char raw[] = "{\"siblings\":[{\"device_id\":\"x\",\"name\":\"Ren\xc3\xa9\xe2\x98\x85\"}]}";
    CHECK(sib_parse(raw, sizeof raw - 1, &l) && !strcmp(l.s[0].name, "Ren??"),
          "raw UTF-8 too: one ? per character, not per byte");
    static const char *const BAD[] = {"", "[]", "{}", "{\"siblings\":{}}", "{\"siblings\":[1]}",
                                      "{\"siblings\":[{\"device_id\":\"x\"", "{\"other\":1}", "not json"};
    for (size_t i = 0; i < sizeof BAD / sizeof *BAD; i++)
        CHECK(!sib_parse(BAD[i], strlen(BAD[i]), &l), "anything else is refused, never half-read");
}

static void test_a_name_is_what_the_panel_can_draw(void)
{
    char out[SIB_NAME_BYTES + 1];
    sib_clean_name("  Big   Room \t", out, sizeof out);
    CHECK(!strcmp(out, "Big Room"), "spaces folded and trimmed");
    sib_clean_name("A name far longer than twenty-four bytes", out, sizeof out);
    CHECK(strlen(out) == SIB_NAME_BYTES, "cut at 24 bytes");
    CHECK(sib_initial("quorra") == 'Q' && sib_initial("  2nd") == '2' && sib_initial("??") == '?',
          "the tile's letter: the first letter or digit, upper-cased");
    const uint8_t a = sib_accent_for("Quorra", 0);
    CHECK(a < SIB_MAX && sib_accent_for("Quorra", 0) == a, "a colour from the name, the same every boot");
    CHECK(sib_accent_for("Quorra", (uint8_t)(1u << a)) != a, "and never one a sibling already has");
    CHECK(sib_accent_for("Quorra", 0x0F) < SIB_MAX, "with all four taken, still a colour");
}

static void test_a_thread_follows_its_panel_not_its_slot(void)
{
    sib_slots_t s = {0};
    sib_list_t l = {0};
    bool renamed;
    l.n = 2;
    snprintf(l.s[0].device_id, SIB_ID_LEN, "dev-a");
    snprintf(l.s[0].name, sizeof l.s[0].name, "Quorra");
    snprintf(l.s[1].device_id, SIB_ID_LEN, "dev-b");
    snprintf(l.s[1].name, sizeof l.s[1].name, "Tamsin");
    CHECK(sib_merge(&s, &l, &renamed) == 0x3 && s.used[0] && s.used[1], "the first list fills slots 0 and 1");
    CHECK(s.s[0].accent != s.s[1].accent, "in two different colours");
    /* The box's order changes and a panel is renamed: nobody moves. */
    sibling_t a = l.s[0];
    l.s[0] = l.s[1];
    l.s[1] = a;
    snprintf(l.s[1].name, sizeof l.s[1].name, "Q");
    CHECK(sib_merge(&s, &l, &renamed) == 0 && renamed && !strcmp(s.s[0].name, "Q") &&
              !strcmp(s.s[0].device_id, "dev-a"),
          "a renamed panel keeps its thread; the list's order moves nothing");
    CHECK(sib_merge(&s, &l, &renamed) == 0 && !renamed, "the same list again changes nothing");
    /* dev-a is retired and a new unit appears. */
    l.n = 2;
    snprintf(l.s[1].device_id, SIB_ID_LEN, "dev-c");
    snprintf(l.s[1].name, sizeof l.s[1].name, "Spare");
    CHECK(sib_merge(&s, &l, &renamed) == 0x1 && !strcmp(s.s[0].device_id, "dev-c") &&
              !strcmp(s.s[1].device_id, "dev-b"),
          "a new panel takes the freed slot, and that thread is cleared for it");
    l.n = 0;
    CHECK(sib_merge(&s, &l, &renamed) == 0x3 && !s.used[0] && !s.used[1], "an empty list frees both");
}

static void test_the_threads_show_the_box_s_names_and_survive_a_reboot(void)
{
    sib_port_fake_wipe();
    svc_messages_fake_clear();
    svc_siblings_init();
    char name[32];
    svc_siblings_name(0, name, sizeof name);
    CHECK(!strcmp(name, "Panel 1") && svc_siblings_initial(0) == '1' && svc_siblings_initial(1) == '2',
          "before the box answers, a first boot says Panel 1 / Panel 2 — never a compiled-in name");
    CHECK(!strcmp(svc_siblings_thread(0), SVC_MESSAGES_THREAD_1) && svc_siblings_thread(2) == NULL,
          "two threads, bound to slots 0 and 1");
    svc_messages_fake_add(SVC_MESSAGES_THREAD_1, &(msg_t){.id = 1, .mine = true, .text = "to nobody"});
    CHECK(svc_siblings_offer(BOX_JSON, sizeof BOX_JSON - 1), "the box's answer, as the main task gets it");
    svc_siblings_name(0, name, sizeof name);
    CHECK(!strcmp(name, "Panel 1"), "nothing changes until the UI task applies it");
    svc_siblings_service();
    svc_siblings_name(0, name, sizeof name);
    CHECK(!strcmp(name, "Quorra") && svc_siblings_initial(0) == 'Q', "then the thread is hers");
    svc_siblings_name(1, name, sizeof name);
    CHECK(!strcmp(name, "Tamsin") && svc_siblings_initial(1) == 'T', "and the other's");
    CHECK(svc_messages_count(SVC_MESSAGES_THREAD_1) == 0, "what was in the unbound thread is cleared");
    CHECK(svc_siblings_accent(0) != svc_siblings_accent(1), "two colours");
    CHECK(!svc_siblings_offer("{\"siblings\":", 12), "a cut-off answer is refused");
    svc_siblings_service();
    svc_siblings_name(0, name, sizeof name);
    CHECK(!strcmp(name, "Quorra"), "and changes nothing");
    char note[96];
    svc_siblings_note(note, sizeof note);
    CHECK(strstr(note, "sib=box,box:1,bad:1") != NULL, "telemetry says where the names came from");

    svc_siblings_init(); /* a reboot: the cache is all there is until the box answers */
    svc_siblings_name(1, name, sizeof name);
    CHECK(!strcmp(name, "Tamsin"), "after a reboot the names are back before the box answers");
    sibling_t s;
    CHECK(svc_siblings_slot(0, &s) && !strcmp(s.device_id, "11111111-aaaa-4bbb-8ccc-000000000001"),
          "bound to the same device");
    sib_port_fake_wipe();
}

/* ---- alarms (0.0.13): the zone, the schedule, the box's list, the queue, the sound ---- */

static int64_t utc(int y, int mo, int d, int h, int mi)
{
    civil_t c = {.year = (uint16_t)y, .mon = (uint8_t)mo, .mday = (uint8_t)d, .h = (uint8_t)h,
                 .m = (uint8_t)mi};
    return civil_to_epoch(&c);
}

static tz_rule_t zone_of(int32_t idx)
{
    tz_rule_t z;
    tz_parse(tz_posix(idx), &z);
    return z;
}

#define NEW_YORK 5

static void test_the_zone_rules_agree_with_the_c_library_every_hour_of_two_years(void)
{
    /* glibc reads the same POSIX strings: every zone in the table, every hour of 2026 and 2027,
     * must give the offset localtime gives — the two daylight-saving changes included. */
    const char *saved = getenv("TZ");
    char keep[64] = "";
    if (saved) snprintf(keep, sizeof keep, "%s", saved);
    int bad = 0;
    for (int32_t i = 0; i < TZ_ZONES; i++) {
        tz_rule_t z;
        CHECK(tz_parse(tz_posix(i), &z), tz_posix(i));
        setenv("TZ", tz_posix(i), 1);
        tzset();
        for (int64_t t = utc(2026, 1, 1, 0, 0); t < utc(2028, 1, 1, 0, 0); t += 3600) {
            time_t tt = (time_t)t;
            struct tm tm;
            localtime_r(&tt, &tm);
            if (tm.tm_gmtoff != tz_offset_at(&z, t) && bad++ < 3)
                fprintf(stderr, "  zone %s at %lld: libc %ld, ours %d\n", tz_posix(i), (long long)t,
                        (long)tm.tm_gmtoff, (int)tz_offset_at(&z, t));
        }
    }
    if (keep[0]) setenv("TZ", keep, 1);
    else unsetenv("TZ");
    tzset();
    CHECK(bad == 0, "every zone's offset matches localtime, hour by hour");
    tz_rule_t z;
    CHECK(!tz_parse("EST5EDT", &z), "a daylight name with no rule is refused, not guessed");
    CHECK(!tz_parse("", &z) && !tz_parse("E5", &z), "nor a name too short to be one");
}

static void test_wall_time_through_the_gap_and_the_overlap(void)
{
    const tz_rule_t z = zone_of(NEW_YORK);
    int64_t s, e;
    tz_transitions(&z, 2026, &s, &e);
    CHECK(s == utc(2026, 3, 8, 7, 0) && e == utc(2026, 11, 1, 6, 0),
          "New York: 8 March 02:00 EST and 1 November 02:00 EDT");
    CHECK(tz_wall_to_utc(&z, utc(2026, 3, 8, 2, 30)) == utc(2026, 3, 8, 7, 0),
          "02:30 on the morning the clocks skip it is the end of the gap, 03:00 EDT");
    CHECK(tz_wall_to_utc(&z, utc(2026, 11, 1, 1, 30)) == utc(2026, 11, 1, 5, 30),
          "01:30 on the morning it happens twice is the first one, EDT");
    CHECK(tz_wall_to_utc(&z, utc(2026, 7, 1, 6, 45)) == utc(2026, 7, 1, 10, 45), "summer: UTC-4");
    CHECK(tz_wall_to_utc(&z, utc(2026, 12, 1, 6, 45)) == utc(2026, 12, 1, 11, 45), "winter: UTC-5");
    const tz_rule_t syd = zone_of(11);
    CHECK(tz_wall_to_utc(&syd, utc(2026, 1, 10, 7, 0)) == utc(2026, 1, 9, 20, 0),
          "Sydney's summer is January: UTC+11, the date behind");
}

static alarm_t alarm_at(uint32_t id, int h, int m, uint8_t days)
{
    alarm_t a = {.id = id, .h = (uint8_t)h, .m = (uint8_t)m, .days = days, .on = true};
    return a;
}

static void test_when_an_alarm_next_rings(void)
{
    const tz_rule_t z = zone_of(NEW_YORK);
    alarm_t gap = alarm_at(1, 2, 30, 0x7F);
    CHECK(alarm_next_at(&gap, &z, utc(2026, 3, 8, 6, 0)) == utc(2026, 3, 8, 7, 0),
          "an every-day 02:30 rings at 03:00 on the morning the clocks go forward");
    CHECK(alarm_next_at(&gap, &z, utc(2026, 3, 8, 7, 0)) == utc(2026, 3, 9, 6, 30),
          "and the next morning at 02:30 EDT");
    alarm_t twice = alarm_at(2, 1, 30, 0x7F);
    const int64_t first = alarm_next_at(&twice, &z, utc(2026, 11, 1, 4, 0));
    CHECK(first == utc(2026, 11, 1, 5, 30), "01:30 on the morning the clocks go back: the first one");
    CHECK(alarm_next_at(&twice, &z, first) == utc(2026, 11, 2, 6, 30),
          "and not again an hour later: next is tomorrow's, EST");
    alarm_t midnight = alarm_at(3, 0, 0, 0x7F);
    const int64_t late = tz_wall_to_utc(&z, utc(2026, 10, 12, 23, 59) + 30);
    CHECK(alarm_next_at(&midnight, &z, late) == late + 30, "00:00 is thirty seconds after 23:59:30");
    alarm_t once = alarm_at(4, 7, 0, 0);
    const int64_t monday_8 = tz_wall_to_utc(&z, utc(2026, 10, 12, 8, 0));
    CHECK(alarm_next_at(&once, &z, monday_8) == tz_wall_to_utc(&z, utc(2026, 10, 13, 7, 0)),
          "a one-off already past today rings at its next occurrence, tomorrow");
    CHECK(alarm_next_at(&once, &z, monday_8 - 2 * 3600) == tz_wall_to_utc(&z, utc(2026, 10, 12, 7, 0)),
          "and today when today's is still ahead");
    alarm_t every = alarm_at(5, 21, 0, 0x7F);
    CHECK(alarm_next_at(&every, &z, monday_8) == tz_wall_to_utc(&z, utc(2026, 10, 12, 21, 0)),
          "an every-day alarm rings today");
    alarm_t work = alarm_at(6, 6, 45, 0x1F);
    const int64_t friday_22 = tz_wall_to_utc(&z, utc(2026, 10, 16, 22, 0));
    CHECK(alarm_next_at(&work, &z, friday_22) == tz_wall_to_utc(&z, utc(2026, 10, 19, 6, 45)),
          "Friday night: the weekday alarm is Monday's");
    alarm_t sunday = alarm_at(7, 9, 0, 1u << 6);
    const int64_t sunday_10 = tz_wall_to_utc(&z, utc(2026, 10, 18, 10, 0));
    CHECK(alarm_next_at(&sunday, &z, sunday_10) == tz_wall_to_utc(&z, utc(2026, 10, 25, 9, 0)),
          "Sunday-only, missed this Sunday: next Sunday");
    alarm_t off = alarm_at(8, 9, 0, 0x7F);
    off.on = false;
    CHECK(alarm_next_at(&off, &z, sunday_10) == -1, "an alarm switched off never rings");
}

static alarm_list_t list_of(const alarm_t *a, uint8_t n)
{
    alarm_list_t l = {.n = n};
    for (uint8_t i = 0; i < n; i++) l.a[i] = a[i];
    return l;
}

static void test_the_schedule_rings_once_snoozes_and_stops(void)
{
    const tz_rule_t z = zone_of(NEW_YORK);
    alarm_sched_t s;
    alarm_sched_init(&s);
    const alarm_t a[] = {alarm_at(10, 6, 45, 0x1F), alarm_at(11, 7, 0, 0)};
    alarm_list_t l = list_of(a, 2);
    alarm_sched_set_list(&s, &l);
    const int64_t at = tz_wall_to_utc(&z, utc(2026, 10, 12, 6, 45));
    CHECK(!alarm_sched_tick(&s, &z, at - 600, 0), "ten minutes early: nothing");
    CHECK(!alarm_sched_tick(&s, &z, at - 1, 1000), "a second early: nothing");
    CHECK(alarm_sched_tick(&s, &z, at, 2000) && s.ringing && s.ring.id == 10, "06:45: it rings");
    CHECK(!alarm_sched_tick(&s, &z, at + 1, 3000), "and does not start again a second later");
    const int64_t until = alarm_sched_snooze(&s, at + 20);
    CHECK(!s.ringing && s.snoozed && until == at + 20 + 9 * 60, "KEY: snoozed nine minutes");
    CHECK(!alarm_sched_tick(&s, &z, until - 1, 4000), "quiet until then");
    CHECK(alarm_sched_tick(&s, &z, until, 5000) && s.ring.id == 10 && !s.snoozed, "then again");
    uint32_t off;
    alarm_sched_stop(&s, until + 5, &off);
    CHECK(!s.ringing && off == 0 && s.dismissed == 1 && s.last_end == ALARM_END_STOPPED,
          "BOOT: stopped, and a weekday alarm stays on");
    const int64_t seven = tz_wall_to_utc(&z, utc(2026, 10, 12, 7, 0));
    CHECK(alarm_sched_tick(&s, &z, seven, 6000) && s.ring.id == 11, "the one-off at 07:00");
    alarm_sched_stop(&s, seven + 3, &off);
    CHECK(off == 11 && !s.list.a[1].on, "a one-off switches itself off once answered");
    CHECK(!alarm_sched_tick(&s, &z, seven + 86400, 7000), "and does not ring tomorrow");
    CHECK(alarm_sched_snooze(&s, seven) == -1 && !s.snoozed, "KEY with nothing ringing snoozes nothing");
}

static void test_an_alarm_changed_while_snoozed(void)
{
    const tz_rule_t z = zone_of(NEW_YORK);
    alarm_sched_t s;
    alarm_t a[] = {alarm_at(20, 6, 45, 0x7F)};
    snprintf(a[0].label, sizeof a[0].label, "Work");
    alarm_list_t l = list_of(a, 1);
    const int64_t at = tz_wall_to_utc(&z, utc(2026, 10, 12, 6, 45));

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at, 0);
    alarm_sched_snooze(&s, at);
    snprintf(l.a[0].label, sizeof l.a[0].label, "Gym");
    l.a[0].sound = ALARM_SOUND_RISE;
    CHECK(!alarm_sched_set_list(&s, &l) && s.snoozed && !strcmp(s.snooze_of.label, "Gym") &&
              s.snooze_of.sound == ALARM_SOUND_RISE,
          "a new label or sound from the box: the snooze keeps going, with them");
    l.a[0].m = 50;
    CHECK(alarm_sched_set_list(&s, &l) && !s.snoozed && s.snooze_cancelled == 1,
          "moved to another time while snoozed: the snooze ends, the new time stands");
    CHECK(!alarm_sched_tick(&s, &z, at + 9 * 60, 1000), "so nothing rings at the old snooze");

    alarm_sched_init(&s);
    l = list_of(a, 1);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at, 0);
    alarm_sched_snooze(&s, at);
    l.n = 0;
    CHECK(alarm_sched_set_list(&s, &l) && !s.snoozed, "deleted in the PWA while snoozed: gone");

    alarm_sched_init(&s);
    l = list_of(a, 1);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at, 0);
    alarm_sched_snooze(&s, at);
    CHECK(alarm_sched_set_on(&s, 20, false) && !s.snoozed, "switched off on the glass: gone");
    CHECK(!alarm_sched_set_on(&s, 99, false), "an id the panel does not hold is refused");
}

static void test_a_ring_nobody_answers_stops_at_ten_minutes(void)
{
    const tz_rule_t z = zone_of(NEW_YORK);
    alarm_sched_t s;
    alarm_sched_init(&s);
    const alarm_t a[] = {alarm_at(30, 6, 0, 0)};
    alarm_list_t l = list_of(a, 1);
    alarm_sched_set_list(&s, &l);
    const int64_t at = tz_wall_to_utc(&z, utc(2026, 10, 12, 6, 0));
    alarm_sched_tick(&s, &z, at, 50000);
    uint32_t off;
    CHECK(!alarm_sched_cap(&s, at + 599, 50000 + ALARM_RING_CAP_MS - 1, &off), "9:59 in: still ringing");
    CHECK(alarm_sched_cap(&s, at + 600, 50000 + ALARM_RING_CAP_MS, &off) && !s.ringing &&
              s.timed_out == 1 && s.last_end == ALARM_END_TIMED_OUT && off == 30,
          "ten minutes: it stops itself, says it timed out, and a one-off switches off");
    CHECK(!alarm_sched_cap(&s, at + 700, 50000 + ALARM_RING_CAP_MS * 2, &off), "once");
}

static void test_reboots_and_clock_steps_never_ring_twice_or_late(void)
{
    const tz_rule_t z = zone_of(NEW_YORK);
    const alarm_t a[] = {alarm_at(40, 6, 45, 0x7F), alarm_at(41, 6, 46, 0x7F)};
    const alarm_list_t l = list_of(a, 2);
    const int64_t at = tz_wall_to_utc(&z, utc(2026, 10, 12, 6, 45));
    alarm_sched_t s;

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    CHECK(alarm_sched_tick(&s, &z, at + 90, 0) && s.ring.id == 40,
          "a reboot (an update) across the alarm's minute: rung on the first check, 90 s late");

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    CHECK(!alarm_sched_tick(&s, &z, at + 1000, 0), "a boot a quarter of an hour later rings nothing");

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at - 3600, 0);
    CHECK(!alarm_sched_tick(&s, &z, at + 3 * 3600, 1) && s.missed == 2,
          "the box steps the clock hours forward: counted as missed, never rung late");

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at - 10, 0);
    CHECK(alarm_sched_tick(&s, &z, at, 1), "rings");
    uint32_t off;
    alarm_sched_stop(&s, at + 5, &off);
    CHECK(!alarm_sched_tick(&s, &z, at - 30, 2) && !alarm_sched_tick(&s, &z, at + 10, 3),
          "the clock stepped back across it: not rung a second time");
    CHECK(alarm_sched_tick(&s, &z, at + 60, 4) && s.ring.id == 41, "the 06:46 one still rings");

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at + 3 * 3600, 0);
    CHECK(!alarm_sched_tick(&s, &z, at - 60, 1) && alarm_sched_tick(&s, &z, at, 2) && s.ring.id == 40,
          "a clock three hours fast, corrected by the box: the alarm still rings at its real time");

    alarm_sched_init(&s);
    alarm_sched_set_list(&s, &l);
    alarm_sched_tick(&s, &z, at - 10, 0);
    CHECK(alarm_sched_tick(&s, &z, at + 70, 1) && s.ring.id == 40,
          "two due in one check: the earlier rings first");
    CHECK(alarm_sched_tick(&s, &z, at + 71, 2) && s.ring.id == 41 && s.taken_over == 1,
          "and the other on the next pass, taking the ring over");
}

static const char ALARMS_JSON[] =
    "{\"etag\":\"3f2a9c\",\"alarms\":["
    "{\"id\":7,\"at\":1260,\"days\":127,\"on\":true,\"label\":\"Girls to bed\",\"sound\":\"beeps\"},"
    "{\"id\":3,\"at\":405,\"days\":31,\"on\":true,\"label\":\"Work\",\"sound\":\"chime\",\"x\":[1,{\"y\":null}]},"
    "{\"id\":5,\"at\":510,\"days\":96,\"on\":false,\"label\":\"Caf\\u00e9 \\\"run\\\"\",\"sound\":\"rise\"},"
    "{\"at\":600,\"days\":0,\"on\":true},"
    "{\"id\":9,\"at\":1440,\"days\":0,\"on\":true}"
    "],\"max\":8}";

static void test_the_box_s_alarms_json_is_read_as_the_box_writes_it(void)
{
    alarm_list_t l;
    CHECK(alarm_parse(ALARMS_JSON, sizeof ALARMS_JSON - 1, &l), "the box's answer parses");
    CHECK(l.n == 3 && !strcmp(l.etag, "3f2a9c"), "three usable alarms, and the version");
    CHECK(l.a[0].id == 3 && l.a[0].h == 6 && l.a[0].m == 45 && l.a[0].days == 31 && l.a[0].on &&
              l.a[0].sound == ALARM_SOUND_CHIME && !strcmp(l.a[0].label, "Work"),
          "sorted by time of day; unknown keys skipped");
    CHECK(l.a[1].id == 5 && !l.a[1].on && l.a[1].sound == ALARM_SOUND_RISE &&
              !strcmp(l.a[1].label, "Caf? \"run\""),
          "escapes read, a code point the font lacks is one '?'");
    CHECK(l.a[2].id == 7 && l.a[2].sound == ALARM_SOUND_BEEPS, "and the evening one last");
    CHECK(!alarm_parse("{\"alarms\":[{\"id\":1,", 20, &l), "a cut-off answer is refused");
    CHECK(!alarm_parse("{\"etag\":\"x\"}", 12, &l), "an answer with no list is refused");
    CHECK(alarm_parse("{\"alarms\":[]}", 13, &l) && l.n == 0, "an empty list is a list");
    char big[2048] = "{\"alarms\":[";
    for (int i = 1; i <= 10; i++) {
        char one[96];
        snprintf(one, sizeof one, "%s{\"id\":%d,\"at\":%d,\"days\":0,\"on\":true}", i > 1 ? "," : "", i, i);
        strcat(big, one);
    }
    strcat(big, "]}");
    CHECK(alarm_parse(big, strlen(big), &l) && l.n == ALARM_MAX, "more than eight: eight kept");
    char label[40];
    alarm_clean_label("a very long label that will not fit at all", label, sizeof label);
    CHECK(strlen(label) == ALARM_LABEL_BYTES, "a label is at most 24 bytes");
}

static void test_switches_queue_for_the_box_and_the_last_one_wins(void)
{
    alarm_queue_t q = {0};
    alarm_put_t p;
    CHECK(!alarm_queue_peek(&q, &p), "empty");
    alarm_queue_put(&q, 3, false);
    alarm_queue_put(&q, 5, true);
    CHECK(alarm_queue_peek(&q, &p) && p.id == 3 && !p.on && alarm_queue_count(&q) == 2,
          "the oldest goes first");
    alarm_queue_put(&q, 3, true);
    CHECK(alarm_queue_count(&q) == 2, "a second switch of one alarm replaces the first");
    alarm_queue_done(&q, &p, 200);
    CHECK(alarm_queue_count(&q) == 2, "and the box's 200 for the old write does not clear the new");
    CHECK(alarm_queue_peek(&q, &p) && p.id == 5, "now alarm 5's is oldest");
    alarm_queue_done(&q, &p, -1);
    alarm_queue_done(&q, &p, 503);
    CHECK(alarm_queue_count(&q) == 2, "no answer, or a 503: kept and retried");
    alarm_queue_done(&q, &p, 404);
    CHECK(alarm_queue_count(&q) == 1, "a 404 (the alarm is gone) settles it");
    alarm_list_t l = {.n = 1};
    l.a[0] = alarm_at(3, 6, 45, 0x1F);
    l.a[0].on = false;
    alarm_queue_overlay(&q, &l);
    CHECK(l.a[0].on, "a list from before the box heard the switch shows the switch, not the list");
}

static void test_the_alarm_sounds(void)
{
    CHECK(alarm_ramp_q15(0) == ALARM_RAMP_FLOOR_Q15 && alarm_ramp_q15(ALARM_RAMP_MS) == 32768 &&
              alarm_ramp_q15(ALARM_RAMP_MS / 2) > alarm_ramp_q15(1000),
          "the volume climbs from 15 % to full over thirty seconds");
    static int16_t one[32000], two[32000];
    int32_t sum[ALARM_SOUNDS] = {0};
    for (uint8_t snd = 0; snd < ALARM_SOUNDS; snd++) {
        alarm_synth_t a, b;
        alarm_synth_init(&a, snd);
        alarm_synth_init(&b, snd);
        alarm_synth_fill(&a, one, 32000, 32768);
        for (int k = 0; k < 100; k++) alarm_synth_fill(&b, two + k * 320, 320, 32768);
        CHECK(!memcmp(one, two, sizeof one), "320-frame chunks are the same sound as one long fill");
        int peak = 0;
        for (int i = 0; i < 32000; i++) {
            const int v = one[i] < 0 ? -one[i] : one[i];
            if (v > peak) peak = v;
            sum[snd] += v / 64;
        }
        CHECK(peak > ALARM_PEAK / 2 && peak <= ALARM_PEAK, "loud, never past -6 dBFS");
        const uint32_t period = alarm_synth_period(snd);
        CHECK(one[period - 10] == 0, "each pattern ends in a pause");
    }
    CHECK(sum[0] != sum[1] && sum[1] != sum[2], "three different sounds");
    alarm_synth_t q;
    alarm_synth_init(&q, ALARM_SOUND_CHIME);
    alarm_synth_fill(&q, one, 3200, 4915);
    int quiet = 0;
    for (int i = 0; i < 3200; i++) quiet = one[i] > quiet ? one[i] : quiet;
    CHECK(quiet < ALARM_PEAK / 5, "at the ramp's floor it is a fifth as loud");
}

static void test_a_ringing_alarm_beats_everything(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.alarm_ringing = true;
    f.dark = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).fallback == SH_ACT_SNOOZE_ALARM,
          "on a dark screen KEY still snoozes");
    st.key_owner = SH_KEY_FREE;
    CHECK(sh_route_button(&st, &f, SH_BTN_PWR).first == SH_ACT_WAKE, "PWR wakes it");
    f.dark = false;
    CHECK(sh_route_button(&st, &f, SH_BTN_PWR).first == SH_ACT_NONE,
          "and cannot put the glass out from under Snooze and Stop");
    f.menu_open = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).fallback == SH_ACT_STOP_ALARM, "over the menu: stop");
    f.menu_open = false;
    f.talking = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).fallback == SH_ACT_SNOOZE_ALARM,
          "over the talk ring: snooze, never a recording");
    st.key_owner = SH_KEY_FREE;
    f.talking = false;
    f.speaking = f.replying = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).fallback == SH_ACT_STOP_ALARM,
          "while Jerv speaks: BOOT stops the alarm");
    f.speaking = f.replying = false;
    f.notices = 2;
    CHECK(sh_top_overlay(&st, &f) == SH_OV_ALARM &&
              sh_route_button(&st, &f, SH_BTN_BOOT).fallback == SH_ACT_STOP_ALARM,
          "over a notice: the alarm is on top and BOOT stops it, not dismisses the notice");
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT_LONG).fallback == SH_ACT_STOP_ALARM,
          "BOOT held is plain BOOT: stop");
    sh_facts_t h = home();
    unsigned a = sh_route_event(&h, APP_EV_ALARM_DUE);
    CHECK((a & SH_DO_ALARM_TAKES_OVER) && (a & SH_DO_WAKE) && (a & SH_DO_OPEN_ALARM_APP),
          "the alarm takes over: lit, the talk, menu and voice ended, the Clock up");
    a = sh_route_event(&h, APP_EV_ALARM_ENDED);
    CHECK((a & SH_DO_ALARM_GIVES_BACK) && (a & SH_DO_DELIVER),
          "its end gives the brightness back and tells the Clock");
}

static const char SERVICE_JSON[] =
    "{\"etag\":\"v1\",\"alarms\":[{\"id\":3,\"at\":405,\"days\":31,\"on\":true,\"label\":\"Work\","
    "\"sound\":\"rise\"},{\"id\":4,\"at\":420,\"days\":0,\"on\":true,\"label\":\"\",\"sound\":\"beeps\"}]}";

static void test_the_alarms_service_end_to_end(void)
{
    alarm_port_fake_wipe();
    svc_settings_set(SET_TZ, NEW_YORK);
    svc_alarms_init();
    svc_rtc_fake_set_date(2026, 10, 12); /* a Monday */
    svc_rtc_fake_set(6, 44);
    uint32_t ms = svc_rtc_uptime_ms();
    char tag[ALARM_ETAG_BYTES];
    svc_alarms_etag(tag, sizeof tag);
    CHECK(svc_alarms_count() == 0 && !tag[0], "a first boot holds nothing and asks without an etag");
    CHECK(svc_alarms_offer(SERVICE_JSON, sizeof SERVICE_JSON - 1), "the main task hands the list over");
    CHECK(svc_alarms_count() == 0, "nothing changes until the UI task applies it");
    svc_alarms_service(ms);
    svc_alarms_etag(tag, sizeof tag);
    CHECK(svc_alarms_count() == 2 && !strcmp(tag, "v1"), "applied, and the next fetch sends v1");
    alarm_next_t n;
    CHECK(svc_alarms_next(&n) && n.a.id == 3 && n.at_h == 6 && n.at_m == 45 && n.wait_min == 1,
          "next: 06:45, in a minute");
    svc_rtc_fake_advance_ms(60000);
    ms += 60000;
    svc_alarms_service(ms);
    alarm_t r;
    CHECK(svc_alarms_ringing(&r) && r.id == 3 && svc_audio_fake_ring_sound() == ALARM_SOUND_RISE,
          "06:45: ringing, in its own sound");
    const uint16_t back = svc_alarms_snooze();
    CHECK(back == 6 * 60 + 54 && !svc_alarms_ringing(NULL) && svc_audio_fake_ring_sound() == -1,
          "snoozed to 06:54, silent");
    CHECK(svc_alarms_next(&n) && n.snoozed && n.at_m == 54, "and the list says so");
    svc_alarms_set_on(3, false);
    alarm_put_t p;
    CHECK(svc_alarms_take_put(&p) && p.id == 3 && !p.on, "the switch on the glass waits for the box");
    CHECK(svc_alarms_next(&n) && !n.snoozed && n.a.id == 4, "switched off while snoozed: no snooze");
    svc_rtc_fake_advance_ms(15 * 60000);
    ms += 15 * 60000;
    svc_alarms_service(ms);
    CHECK(svc_alarms_ringing(&r) && r.id == 4 && svc_audio_fake_ring_sound() == ALARM_SOUND_BEEPS,
          "07:00: the one-off");
    const uint32_t saves = alarm_port_fake_saves();
    svc_alarms_stop();
    alarm_t got;
    svc_alarms_get(1, &got);
    CHECK(!svc_alarms_ringing(NULL) && got.id == 4 && !got.on, "stopped, and it switched itself off");
    CHECK(svc_alarms_offer(SERVICE_JSON, sizeof SERVICE_JSON - 1), "a list the box sent before it heard");
    svc_alarms_service(ms);
    svc_alarms_get(0, &got);
    CHECK(!got.on, "the switches still waiting win over it");
    svc_alarms_service(ms + 1000);
    CHECK(alarm_port_fake_saves() == saves, "nothing is written while changes are still coming");
    svc_alarms_service(ms + 5000);
    CHECK(alarm_port_fake_saves() == saves + 1, "then the burst is one write to flash");
    svc_alarms_put_done(&p, 200);
    svc_alarms_service(ms + 10000);

    svc_alarms_init(); /* a reboot with the box unreachable */
    CHECK(svc_alarms_count() == 2, "after a reboot the alarms are back before the box answers");
    CHECK(svc_alarms_take_put(&p) && p.id == 4 && !p.on, "and the one-off's switch-off still goes");
    svc_alarms_etag(tag, sizeof tag);
    CHECK(!strcmp(tag, "v1"), "with the version it held");
    char note[192];
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "alarm=n:2,") == note && strstr(note, "src:cache"), "telemetry says where they came from");
    CHECK(svc_alarms_test(ALARM_SOUND_CHIME) && svc_alarms_ringing(&r) && r.id == 0 &&
              !strcmp(r.label, "Test alarm"),
          "the debug relay's alarm_test rings now");
    svc_alarms_stop();
    CHECK(svc_alarms_count() == 2, "and stopping it switches nothing off");
    alarm_port_fake_wipe();
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
    test_alarm_text_reads_like_the_mock();
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
    test_every_flush_window_is_even_aligned_and_covers_the_dirty_area();
    test_brightness_maps_percent_to_the_0x51_byte_and_dark_is_zero();
    test_the_display_is_skipped_only_on_the_boot_after_a_fault();
    test_a_button_needs_three_steady_samples();
    test_one_pwr_press_seen_two_ways_toggles_once();
    test_the_pmic_registers_decode_as_the_probe_read_them();
    test_a_touch_report_needs_the_ack_byte_and_a_pressed_point();
    test_raw_axes_map_to_the_glass_swapped_and_mirrored();
    test_a_stroke_survives_a_quiet_poll_but_not_three();
    test_the_touch_ring_keeps_every_edge_and_coalesces_moves();
    test_a_tap_a_swipe_and_a_drag_are_told_apart();
    test_a_finger_on_a_dark_screen_buys_the_screen_and_nothing_else();
    test_key_outside_a_conversation_talks_to_jerv();
    test_the_talk_ring_takes_boot_and_holds_notices_back();
    test_the_clock_face_keeps_notices_as_a_badge();
    test_day_and_date_read_like_the_mock();
    test_calendar_dates_round_trip_through_unix_time();
    test_the_clock_chip_registers();
    test_the_box_date_header();
    test_when_the_box_time_replaces_the_panels();
    test_the_zone_table_keeps_daylight_saving();
    test_settings_are_written_once_the_panel_is_left_alone();
    test_disconnect_takes_two_taps_within_three_seconds();
    test_update_now_is_live_only_online_and_idle();
    test_a_hold_that_cannot_be_sent_says_so();
    test_boot_short_is_back_on_release_and_long_is_the_clock_once();
    test_boot_held_opens_the_clock_from_anywhere_but_the_clock();
    test_the_pickup_waits_three_seconds_backs_off_and_never_jumps_the_manifest();
    test_the_relay_reports_itself_in_one_token();
    test_a_request_id_is_uuid_text_or_it_is_dropped();
    test_a_registered_secret_is_scrubbed_from_a_log_line_even_cut_short();
    test_the_stream_is_byte_for_byte_what_the_box_decodes();
    test_the_ring_never_overwrites_and_wraps();
    test_a_hold_records_sends_and_hears_back();
    test_a_knock_or_a_cancel_lets_go_of_everything();
    test_key_or_boot_over_a_reply_stops_it();
    test_a_box_that_drops_mid_hold_is_told_at_the_release();
    test_text_only_never_opens_the_speaker();
    test_the_reply_stream_reads_in_any_split();
    test_a_stream_that_is_not_jt2_or_too_big_is_refused_cleanly();
    test_an_answer_longer_than_the_panel_keeps_is_cut_on_a_character();
    test_the_voice_starts_only_once_the_amp_has_settled_and_the_ring_has_filled();
    test_two_microphones_average_and_each_peak_is_kept();
    test_the_audio_token();
    test_a_talk_on_the_glass_end_to_end();
    test_a_hold_that_cannot_reach_jerv_is_refused_up_front();
    test_key_or_boot_over_a_reply_is_the_shells_stop();
    test_the_speaker_settings();
    test_play_tone_is_whitelisted_and_harmless();
    test_a_still_finger_is_a_long_press_once_and_a_scroll_never();
    test_the_clock_face_opens_its_alarms_only_on_a_two_second_hold();
    test_an_agent_answer_is_kept_whole_on_the_glass();
    test_the_menu_opens_only_over_a_clear_conversation();
    test_clear_chat_takes_two_taps_within_three_seconds();
    test_buttons_over_the_menu_only_close_it();
    test_jerv_and_dave_are_two_threads_and_two_conversations();
    test_clear_chat_asks_that_agent_alone_for_a_new_conversation();
    test_the_box_s_siblings_json_is_read_as_the_box_writes_it();
    test_a_name_is_what_the_panel_can_draw();
    test_a_thread_follows_its_panel_not_its_slot();
    test_the_threads_show_the_box_s_names_and_survive_a_reboot();
    test_the_zone_rules_agree_with_the_c_library_every_hour_of_two_years();
    test_wall_time_through_the_gap_and_the_overlap();
    test_when_an_alarm_next_rings();
    test_the_schedule_rings_once_snoozes_and_stops();
    test_an_alarm_changed_while_snoozed();
    test_a_ring_nobody_answers_stops_at_ten_minutes();
    test_reboots_and_clock_steps_never_ring_twice_or_late();
    test_the_box_s_alarms_json_is_read_as_the_box_writes_it();
    test_switches_queue_for_the_box_and_the_last_one_wins();
    test_the_alarm_sounds();
    test_a_ringing_alarm_beats_everything();
    test_the_alarms_service_end_to_end();
    printf("%d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
