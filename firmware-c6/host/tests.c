/* Host suite for the panel's pure modules. One behaviour per test, named as a sentence; every
 * CHECK message says what is true. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <time.h>

#include "app_event.h"
#include "axp_logic.h"
#include "box_logic.h"
#include "btn_logic.h"
#include "clock_model.h"
#include "dbg_core.h"
#include "dbg_json.h"
#include "disp_guard.h"
#include "disp_logic.h"
#include "jerv_model.h"
#include "minutes.h"
#include "notice_policy.h"
#include "settings_model.h"
#include "shell_core.h"
#include "shell_evq.h"
#include "settings_store.h"
#include "svc_audio_fake.h"
#include "talk_core.h"
#include "svc_box_fake.h"
#include "svc_messages_fake.h"
#include "svc_net_fake.h"
#include "svc_notice_fake.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"
#include "thread_model.h"
#include "time_logic.h"
#include "tp_logic.h"
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
    CHECK(!strcmp(s, "Next 06:45 · in 9 h 4 min"), "hours and minutes");
    clock_fmt_next(&a, 12, s, sizeof s);
    CHECK(!strcmp(s, "Next 06:45 · in 12 min"), "minutes alone under an hour");
    clock_fmt_days(0x1F, "Work", s, sizeof s);
    CHECK(!strcmp(s, "Weekdays · Work"), "weekdays with a label");
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
    CHECK(svc_messages_unplayed_total() == 1, "Quorra's unheard message is counted, Jerv's text is not");
    char who[16] = "";
    CHECK(svc_messages_first_unplayed(who) && !strcmp(who, "quorra"), "and a tap opens her thread");
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

static void frame_bytes(uint8_t *b, size_t *n, uint8_t flags, const char *heard, const char *reply,
                        const char *pcm)
{
    size_t k = 0;
    memcpy(b, "JT1", 3);
    k = 3;
    b[k++] = flags;
    const size_t hl = strlen(heard), rl = strlen(reply);
    b[k++] = (uint8_t)hl;
    b[k++] = (uint8_t)(hl >> 8);
    memcpy(b + k, heard, hl);
    k += hl;
    b[k++] = (uint8_t)rl;
    b[k++] = (uint8_t)(rl >> 8);
    memcpy(b + k, reply, rl);
    k += rl;
    memcpy(b + k, pcm, strlen(pcm));
    *n = k + strlen(pcm);
}

static void test_the_reply_frame_reads_in_any_split(void)
{
    uint8_t b[1200];
    size_t n;
    frame_bytes(b, &n, TF_FLAG_AUDIO, "what time is sunset", "About seven tonight.", "PCMPCM");
    tk_frame_t f;
    tk_frame_init(&f);
    const size_t used = tk_frame_feed(&f, b, n);
    CHECK(tk_frame_done(&f) && used == n - 6 && !strcmp(f.heard, "what time is sunset") &&
              !strcmp(f.reply, "About seven tonight.") && (f.flags & TF_FLAG_AUDIO),
          "whole, the words come out and the speech is what is left");
    tk_frame_init(&f);
    size_t head = 0;
    for (size_t i = 0; i < n && !tk_frame_done(&f); i++) head += tk_frame_feed(&f, b + i, 1);
    CHECK(tk_frame_done(&f) && head == n - 6 && !strcmp(f.reply, "About seven tonight."),
          "a byte at a time, the same");
    frame_bytes(b, &n, 0, "hi", "", "");
    tk_frame_init(&f);
    CHECK(tk_frame_feed(&f, b, n) == n && tk_frame_done(&f) && f.reply[0] == '\0' && !(f.flags & TF_FLAG_AUDIO),
          "an empty reply and no speech is still a whole frame");
    tk_frame_init(&f);
    CHECK(tk_frame_feed(&f, (const uint8_t *)"<html>", 6) <= 6 && tk_frame_bad(&f),
          "anything else — an error page — is refused at the first byte");
    /* 300 two-byte letters: more than a bubble holds. */
    char longr[601];
    for (int i = 0; i < 300; i++) memcpy(longr + 2 * i, "\xC3\xA9", 2);
    longr[600] = '\0';
    frame_bytes(b, &n, 0, "x", longr, "");
    tk_frame_init(&f);
    tk_frame_feed(&f, b, n);
    const size_t len = strlen(f.reply);
    CHECK(tk_frame_done(&f) && len < sizeof f.reply && !strcmp(f.reply + len - 3, "…") &&
              (len - 3) % 2 == 0,
          "a reply too long for a bubble is cut on a character, with an ellipsis");
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
    CHECK(svc_audio_talk_begin(TALK_TO_JERV, "jerv") && svc_audio_talking(), "KEY down records");
    svc_rtc_fake_advance_ms(3200);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "a three-second hold is sent");
    msg_t m = last_jerv();
    CHECK(m.mine && m.voice && m.pending && m.dur_s == 3 && !strcmp(m.text, TALK_HEARING),
          "the bubble says it is being heard, never a made-up transcript");
    CHECK(svc_audio_replying(), "and Jerv is thinking");
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
    CHECK(svc_audio_stop_reply() && svc_audio_playing() == 0 && !svc_audio_replying(),
          "BOOT stops the voice; the words stay");
    svc_audio_service();
    CHECK(svc_audio_talk_begin(TALK_TO_JERV, "jerv"), "the next hold may start");
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
    CHECK(!svc_audio_talk_begin(TALK_TO_JERV, "jerv") && strstr(svc_audio_talk_refused(), "Wi-Fi"),
          "offline, KEY says why instead of recording into nothing");
    svc_net_fake_set(NET_CONNECTED);
    CHECK(svc_audio_talk_begin(TALK_TO_PERSON, "quorra"), "a hold to a person still starts");
    svc_rtc_fake_advance_ms(1500);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "and still ends honestly unsent");
}

static void test_key_or_boot_over_a_reply_is_the_shells_stop(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.replying = true;
    sh_route_t r = sh_route_button(&st, &f, SH_BTN_KEY_DOWN);
    CHECK(r.first == SH_ACT_STOP_REPLY && st.key_owner == SH_KEY_FREE,
          "KEY over a reply stops it, in any app, and takes no hold");
    r = sh_route_button(&st, &f, SH_BTN_KEY_UP);
    CHECK(r.first == SH_ACT_NONE, "so its release sends nothing");
    r = sh_route_button(&st, &f, SH_BTN_BOOT);
    CHECK(r.first == SH_ACT_STOP_REPLY, "BOOT over a reply stops it rather than going back");
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
              svc_settings_default(SET_VOLUME) == 80 && svc_settings_default(SET_MIC_GAIN) == 30,
          "saved under short NVS keys, defaulting to the middle of the vendor's range");
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
    test_the_reply_frame_reads_in_any_split();
    test_two_microphones_average_and_each_peak_is_kept();
    test_the_audio_token();
    test_a_talk_on_the_glass_end_to_end();
    test_a_hold_that_cannot_reach_jerv_is_refused_up_front();
    test_key_or_boot_over_a_reply_is_the_shells_stop();
    test_the_speaker_settings();
    test_play_tone_is_whitelisted_and_harmless();
    printf("%d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
