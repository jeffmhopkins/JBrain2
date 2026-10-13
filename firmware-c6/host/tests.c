/* Host suite for the panel's pure modules. One behaviour per test, named as a sentence; every
 * CHECK message says what is true. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <time.h>
#include <math.h>

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
#include "convo_wire.h"
#include "minutes.h"
#include "jn1.h"
#include "notice_logic.h"
#include "notice_view.h"
#include "notice_policy.h"
#include "notice_port.h"
#include "notice_port_fake.h"
#include "notice_store.h"
#include "orient_logic.h"
#include "orient_port_fake.h"
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
#include "svc_notice_drv.h"
#include "svc_notice_fake.h"
#include "sha256.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"
#include "svc_calib.h"
#include "svc_display.h"
#include "svc_display_fake.h"
#include "svc_orient.h"
#include "svc_orient_drv.h"
#include "tp_cal_port.h"
#include "svc_alarms_drv.h"
#include "svc_siblings_drv.h"
#include "svc_convo.h"
#include "svc_convo_drv.h"
#include "post_port_fake.h"
#include "svc_post_drv.h"
#include "thread_model.h"
#include "time_logic.h"
#include "tp_logic.h"
#include "emoji_logic.h"
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
    CHECK(!strcmp(settings_rotate_note(true, true), "Follows which way is down") &&
              !strcmp(settings_rotate_note(false, true), "Locked upright"),
          "auto-rotate says what it does (0.0.18)");
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
    thread_bubble(&theirs, 4, 26000, &b);
    CHECK(!b.unplayed && b.playing && b.prog == 40, "the dot goes the moment it starts");
    /* 0.0.17 (owner, 2026-10-13: "I expect to go from left to right"): the bar follows what has
       played, against the box's milliseconds, and never runs past its end. */
    msg_t hers = {.id = 7, .voice = true, .dur_s = 5, .dur_ms = 5100};
    thread_bubble(&hers, 7, 0, &b);
    CHECK(b.playing && b.prog == 0, "a play starts with an empty bar");
    thread_bubble(&hers, 7, 2550, &b);
    CHECK(b.prog == 50, "half of 5.1 s played: half the bar, not half of the rounded 5 s");
    thread_bubble(&hers, 7, 5000, &b);
    CHECK(b.prog == 98, "5 s of 5.1: not yet full");
    thread_bubble(&hers, 7, 9000, &b);
    CHECK(b.prog == 100, "the speaker's tail past its length: full, not more");
    hers.played = true;
    thread_bubble(&hers, 0, 0, &b);
    CHECK(!b.playing && !b.unplayed && b.prog == 100, "heard and stopped: a full bar, no dot");
    msg_t old = {.id = 8, .voice = true, .dur_s = 4};
    CHECK(thread_play_pct(&old, 1000) == 25, "no milliseconds from the box: its seconds");
    msg_t none = {.id = 9, .voice = true};
    CHECK(thread_play_pct(&none, 1000) == 0, "no length at all: an empty bar, never a divide by zero");

    /* What a thread's list is built from: a play, a stop or a message heard is not a rebuild. */
    msg_t a = {.id = 1, .voice = true, .dur_s = 3, .text = "Daddy!"};
    msg_t a_heard = a;
    a_heard.played = true;
    const uint32_t s1 = thread_shape_end(thread_shape_msg(THREAD_SHAPE0, &a), 1, true);
    CHECK(s1 == thread_shape_end(thread_shape_msg(THREAD_SHAPE0, &a_heard), 1, true),
          "heard or not, the same list: the dot changes where it stands");
    msg_t a_text = a;
    snprintf(a_text.text, sizeof a_text.text, "Daddy!!");
    CHECK(s1 != thread_shape_end(thread_shape_msg(THREAD_SHAPE0, &a_text), 1, true),
          "a transcript filled in is a rebuild");
    msg_t a_status = a;
    snprintf(a_status.status, sizeof a_status.status, "Played");
    CHECK(s1 != thread_shape_end(thread_shape_msg(THREAD_SHAPE0, &a_status), 1, true),
          "so is a send's status");
    CHECK(thread_shape_end(THREAD_SHAPE0, 0, false) != thread_shape_end(THREAD_SHAPE0, 0, true),
          "and Fetching... giving way to No messages yet");
    msg_t b2 = {.id = 2, .voice = true, .text = "Bye"};
    const uint32_t ab = thread_shape_msg(thread_shape_msg(THREAD_SHAPE0, &a), &b2);
    const uint32_t ba = thread_shape_msg(thread_shape_msg(THREAD_SHAPE0, &b2), &a);
    CHECK(ab != ba && thread_shape_end(ab, 2, true) != s1, "and a message added, or the order moved");

    /* The heap's low-water mark, named with what the panel was doing when it was set. */
    dbg_low_t low = {0};
    CHECK(dbg_low_feed(&low, 60000, "idle", false, false) && !strcmp(low.at, "idle"), "the first reading");
    CHECK(!dbg_low_feed(&low, 61000, "pickup", true, false) && !strcmp(low.at, "idle"),
          "a higher reading names nothing");
    CHECK(dbg_low_feed(&low, 44084, "play", true, true) && low.min == 44084 &&
              !strcmp(low.at, "play+handshake+ring"),
          "a new low: the work, inside a handshake, with a ring held");
    CHECK(!dbg_low_feed(&low, 44084, "report", false, false) && !strcmp(low.at, "play+handshake+ring"),
          "the same low again keeps who set it");
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

static void test_a_commands_answer_is_its_own_phase_for_the_heap_low(void)
{
    /* 0.0.19's 50,972 B "@pickup" was a screenshot's stream: the pickup's name hid it. */
    CHECK(!strcmp(dbg_answer_phase("screenshot"), "screenshot"), "a screenshot is named for itself");
    CHECK(!strcmp(dbg_answer_phase("snapshot"), "answer"), "any other command is an answer");
    CHECK(!strcmp(dbg_answer_phase(""), "answer"), "so is a request that did not parse");
    CHECK(dbg_answer_phase("screenshot") == dbg_answer_phase("screenshot"),
          "a literal, so the pointer alone may cross tasks");
    dbg_low_t low = {0};
    dbg_low_feed(&low, 57656, "pickup", false, false);
    CHECK(dbg_low_feed(&low, 51448, dbg_answer_phase("screenshot"), false, false) &&
              !strcmp(low.at, "screenshot"),
          "the low a screenshot sets reads @screenshot, not @pickup");
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

static void test_a_play_rides_a_smaller_ring_than_a_talk(void)
{
    CHECK(tk_ring_bytes(TK_KIND_AGENT) == 16384 && tk_ring_bytes(TK_KIND_POST) == 16384,
          "a talk and a voice message keep 0.0.10's half second of radio slack");
    CHECK(tk_ring_bytes(TK_KIND_LISTEN) == 6144, "a stored message plays through 6 KB");
    CHECK(tk_ring_bytes(TK_KIND_LISTEN) >= TK_SPK_START_BYTES + TK_NET_SLICE,
          "which still holds the speaker's 150 ms start and one more network slice");
    /* The play's ring fills and drains through a stream several times its size, as the speaker
       takes 20 ms chunks and the network puts slices: nothing lost, nothing reordered. */
    static uint8_t mem[TK_RING_PLAY];
    tk_ring_t r;
    tk_ring_init(&r, mem, sizeof mem);
    uint8_t in[TK_NET_SLICE], out[TALK_CHUNK_BYTES];
    uint32_t put = 0, got = 0;
    bool order = true;
    while (got < 5u * TK_RING_PLAY) {
        for (uint32_t i = 0; i < sizeof in; i++) in[i] = (uint8_t)(put + i);
        put += tk_ring_write(&r, in, (uint32_t)sizeof in);
        const uint32_t n = tk_ring_read(&r, out, sizeof out);
        for (uint32_t i = 0; i < n; i++) order &= out[i] == (uint8_t)(got + i);
        got += n;
    }
    CHECK(order && tk_ring_used(&r) == put - got && tk_ring_used(&r) <= TK_RING_PLAY,
          "a play's stream comes out of 6 KB in order");
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

/* The working view's phase line, as the old one-line status was read. */
static bool status_of(const char *thread, char *out, size_t cap)
{
    svc_work_t w;
    if (!svc_audio_work(thread, &w)) return false;
    snprintf(out, cap, "%s", w.phase);
    return true;
}

static void test_a_talk_on_the_glass_end_to_end(void)
{
    svc_messages_fake_clear();
    svc_audio_init();
    svc_settings_set(SET_TTS, 1);
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv") && svc_audio_talking(), "KEY down records");
    CHECK(talk_ring_bytes() == TK_RING_TALK, "into a talk's 16 KB ring, taken at KEY down");
    svc_rtc_fake_advance_ms(3200);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "a three-second hold is sent");
    msg_t m = last_jerv();
    CHECK(m.mine && m.voice && m.pending && m.dur_s == 3 && !strcmp(m.text, TALK_HEARING),
          "the bubble says it is being heard, never a made-up transcript");
    CHECK(svc_audio_replying(), "and Jerv is thinking");
    char status[64];
    CHECK(!status_of("jerv", status, sizeof status),
          "no status line until the box has heard the question");
    svc_audio_fake_progress("what time is sunset", NULL);
    svc_audio_service();
    m = last_jerv();
    CHECK(!m.pending && !strcmp(m.text, "what time is sunset") && svc_messages_count("jerv") == 1,
          "the transcript replaces \"Transcribing…\" the moment the box has it");
    CHECK(status_of("jerv", status, sizeof status) && !strcmp(status, "Thinking…"),
          "and the status line says Jerv is thinking");
    svc_audio_fake_progress(NULL, "Searching the web: sunset…");
    svc_audio_service();
    CHECK(status_of("jerv", status, sizeof status) &&
              !strcmp(status, "Searching the web: sunset…") && !status_of("dave", status, sizeof status),
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
    CHECK(!status_of("jerv", status, sizeof status) && svc_audio_speaking(),
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
    CHECK(!svc_audio_talk_begin(TALK_TO_PERSON, SVC_MESSAGES_THREAD_1) &&
              strstr(svc_audio_talk_refused(), "not known"),
          "a hold to a twin whose panel the box has not named yet is refused, not recorded");
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

/* ---- emoji ---- */

static void test_emoji_are_drawn_skipped_or_marked_and_text_is_left_alone(void)
{
    bool sorted = EMOJI_SET_N > 0;
    for (uint16_t i = 1; i < EMOJI_SET_N; i++)
        if (EMOJI_SET[i] <= EMOJI_SET[i - 1]) sorted = false;
    CHECK(sorted, "the set is strictly ascending, so the search finds every entry");
    bool found = true;
    for (uint16_t i = 0; i < EMOJI_SET_N; i++) {
        uint16_t at = 0xFFFF;
        if (emoji_kind(EMOJI_SET[i], &at) != EMOJI_IN_SET || at != i) found = false;
    }
    CHECK(found, "every emoji of the set is found at its own index (its image's place)");

    uint16_t at = 0;
    CHECK(emoji_kind(0x1F436, &at) == EMOJI_IN_SET && EMOJI_SET[at] == 0x1F436,
          "the dog face is carried");
    CHECK(emoji_kind(0x2705, NULL) == EMOJI_IN_SET && emoji_kind(0x274C, NULL) == EMOJI_IN_SET &&
              emoji_kind(0x2B50, NULL) == EMOJI_IN_SET && emoji_kind(0x1F389, NULL) == EMOJI_IN_SET &&
              emoji_kind(0x1F525, NULL) == EMOJI_IN_SET,
          "check, cross, star, party popper and fire are carried");

    CHECK(emoji_kind(0xFE0F, NULL) == EMOJI_INVISIBLE && emoji_kind(0xFE0E, NULL) == EMOJI_INVISIBLE,
          "variation selectors take no room");
    CHECK(emoji_kind(0x200D, NULL) == EMOJI_INVISIBLE,
          "a ZWJ takes no room, so its sequence shows its parts side by side");
    CHECK(emoji_kind(0x1F3FB, NULL) == EMOJI_INVISIBLE && emoji_kind(0x1F3FF, NULL) == EMOJI_INVISIBLE,
          "a skin tone takes no room and leaves the base emoji");
    CHECK(emoji_kind(0x1F1FA, NULL) == EMOJI_INVISIBLE && emoji_kind(0x1F1F8, NULL) == EMOJI_INVISIBLE,
          "a flag's regional indicators take no room");
    CHECK(emoji_kind(0x20E3, NULL) == EMOJI_INVISIBLE && emoji_kind(0xE0067, NULL) == EMOJI_INVISIBLE,
          "a keycap and a tag character take no room");

    CHECK(emoji_kind(0x1FAE8, NULL) == EMOJI_UNKNOWN && emoji_kind(0x1F9A9, NULL) == EMOJI_UNKNOWN,
          "an emoji the set lacks is the placeholder, never LVGL's box");

    const uint32_t text[] = {'A', '~', 0xE9, 0xB7, 0x2013, 0x2014, 0x2026, 0x2192, 0x2212,
                             0x4E2D, 0x0416, 0xF00C, 0xF04B};
    bool plain = true;
    for (size_t i = 0; i < sizeof text / sizeof *text; i++)
        if (emoji_kind(text[i], NULL) != EMOJI_NOT) plain = false;
    CHECK(plain, "letters, the marks font's punctuation, CJK and LVGL's symbols stay plain text");
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
    sh_menu_open(&m, SH_MENU_CLEAR);
    CHECK(!sh_menu_armed(&m, 0), "it opens unarmed");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 1000) == SH_MENU_DO_ARM && sh_menu_armed(&m, 1000),
          "the first tap arms it: Tap again to clear");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 1000 + SH_MENU_ARM_MS - 1) == SH_MENU_DO_CLEAR &&
              m.state == SH_MENU_CLOSED,
          "the second inside the window clears and closes");
    sh_menu_open(&m, SH_MENU_CLEAR);
    sh_menu_input(&m, SH_MENU_IN_CLEAR, 5000);
    CHECK(!sh_menu_armed(&m, 5000 + SH_MENU_ARM_MS), "the arm lapses");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 5000 + SH_MENU_ARM_MS) == SH_MENU_DO_ARM,
          "and a tap after it only arms again");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CANCEL, 9000) == SH_MENU_DO_CLOSE && m.state == SH_MENU_CLOSED,
          "Cancel closes, clearing nothing");
    sh_menu_open(&m, SH_MENU_CLEAR);
    sh_menu_input(&m, SH_MENU_IN_CLEAR, 10000);
    CHECK(sh_menu_input(&m, SH_MENU_IN_OUTSIDE, 10100) == SH_MENU_DO_CLOSE, "a tap outside closes, even armed");
    sh_menu_open(&m, SH_MENU_CLEAR);
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
    CHECK(!strcmp(path, "/endpoint/converse?persona=jerv&voice=true&jt=2&steps=1"),
          "Jerv's talk, asking for the JT2 stream with its steps");
    tk_converse_path(TK_AGENT_DAVE, false, true, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/converse?persona=dave&voice=false&jt=2&steps=1&new_thread=true"),
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

/* ---- 0.0.14: the steps, the spinner's redraws, KEY routing ---- */

static void test_step_records_read_in_any_split_and_an_old_panel_skips_them(void)
{
    uint8_t b[256];
    size_t k = 0;
    memcpy(b, "JT2", 3);
    k = 3;
    const uint8_t start0[] = {0, 'w', 'e', 'b', '_', 's', 'e', 'a', 'r', 'c', 'h'};
    k += rec(b + k, TS_STEP_START, start0, sizeof start0);
    const uint8_t done0[] = {0, 1};
    k += rec(b + k, TS_STEP_DONE, done0, sizeof done0);
    const uint8_t done1[] = {1, 0};
    k += rec(b + k, TS_STEP_DONE, done1, sizeof done1);
    const uint8_t end[] = {TS_END_OK};
    k += rec(b + k, TS_END, end, 1);
    static const size_t STEPS[] = {4096, 1, 2, 5};
    for (size_t i = 0; i < sizeof STEPS / sizeof *STEPS; i++) {
        tk_stream_t *st = malloc(sizeof *st);
        tk_stream_init(st);
        int starts = 0, oks = 0, fails = 0, ends = 0;
        for (size_t at = 0; at < k;) {
            const size_t len = k - at < STEPS[i] ? k - at : STEPS[i];
            size_t off = 0;
            while (off < len) {
                tk_stream_ev_t ev;
                const uint8_t *pcm;
                size_t pn;
                const size_t used = tk_stream_feed(st, b + at + off, len - off, &ev, &pcm, &pn);
                off += used;
                if (ev == TS_EV_STEP_START && st->flags == 0 && !strcmp(st->text, "web_search")) starts++;
                if (ev == TS_EV_STEP_DONE) tk_stream_step_ok(st) ? oks++ : fails++;
                if (ev == TS_EV_END) ends++;
                if (!used) break;
            }
            at += len;
        }
        CHECK(starts == 1 && oks == 1 && fails == 1 && ends == 1 && !tk_stream_bad(st),
              "a step's index, label and outcome come out whole, however the reads split them");
        free(st);
    }
    /* What 0.0.13 does with them: an unknown type, skipped by its length. */
    uint8_t old[64];
    size_t n = 0;
    memcpy(old, "JT2", 3);
    n = 3;
    n += rec(old + n, 0x09, "\x00step", 5);
    n += rec(old + n, TS_HEARD, "hi", 2);
    jt2_seen_t got;
    jt2_read(old, n, 3, &got);
    CHECK(got.heard == 1 && !got.bad, "a record type this panel does not know is skipped, not fatal");
}

static void test_the_steps_keep_the_last_four_and_count_the_rest(void)
{
    tk_steps_t s;
    tk_steps_init(&s);
    tk_steps_start(&s, 0, "web_search \xC2\xB7 tide Port St John");
    CHECK(s.n == 1 && s.shown[0].state == WORK_RUNNING && tk_steps_folded(&s) == 0, "a step starts running");
    tk_steps_done(&s, 0, true);
    tk_steps_start(&s, 1, "web_fetch \xC2\xB7 tide-forecast.com");
    tk_steps_done(&s, 1, false);
    CHECK(s.shown[0].state == WORK_OK && s.shown[1].state == WORK_FAILED, "each ends as the box said");
    tk_steps_start(&s, 1, "web_fetch \xC2\xB7 tide-forecast.com \xC2\xB7 page 2");
    CHECK(s.n == 2 && s.total == 2 && strstr(s.shown[1].label, "page 2"),
          "the same index again relabels it, never adds one");
    for (uint8_t i = 2; i < 7; i++) tk_steps_start(&s, i, "weather");
    CHECK(s.n == WORK_SHOWN && s.total == 7 && tk_steps_folded(&s) == 3 && s.shown[0].index == 3 &&
              s.shown[3].index == 6,
          "the newest four are shown, oldest first; the rest are \"+3 more\"");
    tk_steps_start(&s, 1, "a late relabel");
    tk_steps_done(&s, 0, true);
    CHECK(s.total == 7 && s.shown[0].index == 3, "a folded step's late news changes nothing");
    char long_label[200];
    memset(long_label, 'x', sizeof long_label - 1);
    long_label[sizeof long_label - 1] = 0;
    tk_steps_start(&s, 7, long_label);
    CHECK(strlen(s.shown[3].label) < WORK_LABEL, "a label never overruns its slot");

    char line[24];
    tk_steps_summary(&s, 58400, line, sizeof line);
    CHECK(!strcmp(line, "8 steps \xC2\xB7 58 s"), "the answer's line: steps and seconds");
    tk_steps_summary(&s, 125000, line, sizeof line);
    CHECK(!strcmp(line, "8 steps \xC2\xB7 2:05"), "past a minute, a clock");
    tk_steps_t one;
    tk_steps_init(&one);
    tk_steps_start(&one, 0, "weather");
    tk_steps_summary(&one, 9000, line, sizeof line);
    CHECK(!strcmp(line, "1 step \xC2\xB7 9 s"), "one step is a step");
    tk_steps_init(&one);
    tk_steps_summary(&one, 9000, line, sizeof line);
    CHECK(!line[0], "a turn with no tool has no line");
    tk_steps_t big = {.total = 65535, .n = 0};
    tk_steps_summary(&big, 3599000, line, sizeof line);
    CHECK(strlen(line) < sizeof line && line[0], "the longest summary fits a status");

    char clock[12];
    talk_clock(42000, clock, sizeof clock);
    CHECK(!strcmp(clock, "0:42"), "the working line's clock");
    talk_clock(725000, clock, sizeof clock);
    CHECK(!strcmp(clock, "12:05"), "in minutes and seconds");
}

static void test_key_inside_a_conversation_continues_it_and_anywhere_else_starts_jerv_anew(void)
{
    talk_route_t r = talk_key_route("jerv");
    CHECK(r.kind == TALK_TO_AGENT && !strcmp(r.agent, "jerv"), "inside Jerv's thread: Jerv, continued");
    r = talk_key_route("dave");
    CHECK(r.kind == TALK_TO_AGENT && !strcmp(r.agent, "dave"), "inside Dave's: Dave, continued");
    r = talk_key_route(NULL);
    CHECK(r.kind == TALK_TO_NEW_AGENT && !strcmp(r.agent, "jerv"), "home, the clock: a new Jerv conversation");
    r = talk_key_route("minecraft");
    CHECK(r.kind == TALK_TO_NEW_AGENT && !strcmp(r.agent, "jerv"),
          "Minecraft's menu is outside any conversation: Jerv anew, never Dave");
    r = talk_key_route(SVC_MESSAGES_THREAD_1);
    CHECK(r.kind == TALK_TO_NEW_AGENT && !strcmp(r.agent, "jerv"), "a person's thread is not Jerv's either");

    svc_messages_fake_clear();
    svc_audio_init();
    svc_settings_set(SET_TTS, 0);
    svc_messages_fake_add("jerv", &(msg_t){.id = 7, .text = "An old answer."});
    svc_messages_fake_add("dave", &(msg_t){.id = 8, .text = "Dave's old answer."});
    r = talk_key_route(NULL);
    CHECK(svc_audio_talk_begin(r.kind, r.agent), "KEY from the clock records");
    CHECK(strstr(svc_audio_fake_last_path(), "persona=jerv") &&
              strstr(svc_audio_fake_last_path(), "&new_thread=true"),
          "and asks the box for a fresh conversation");
    svc_audio_talk_end(false);
    CHECK(svc_messages_count("jerv") == 1, "a cancelled hold leaves the old conversation on the glass");
    svc_audio_service();

    CHECK(svc_audio_talk_begin(r.kind, r.agent), "again, and this time sent");
    svc_rtc_fake_advance_ms(1800);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "sent");
    msg_t m;
    CHECK(svc_messages_count("jerv") == 1 && svc_messages_get("jerv", 0, &m) && m.mine && m.pending,
          "the thread now holds just the new question");
    CHECK(svc_messages_count("dave") == 1, "Dave's thread is untouched");
    CHECK(svc_audio_fresh_pending("jerv"), "until the box answers, Jerv's next talk asks anew too");
    svc_audio_fake_reply("tides", "High at four.", false);
    svc_audio_service();
    CHECK(svc_messages_count("jerv") == 2 && !svc_audio_fresh_pending("jerv"),
          "answered: the new conversation is the one");

    r = talk_key_route("jerv");
    CHECK(svc_audio_talk_begin(r.kind, r.agent), "KEY inside Jerv's thread");
    CHECK(strstr(svc_audio_fake_last_path(), "new_thread") == NULL, "continues it");
    svc_rtc_fake_advance_ms(1800);
    svc_audio_talk_end(true);
    CHECK(svc_messages_count("jerv") == 3, "and adds to it");
    svc_audio_fake_reply("more", "Sure.", false);
    svc_audio_service();
    svc_messages_fake_clear();
    svc_settings_set(SET_TTS, 1);
}

static void test_the_working_view_lists_steps_then_folds_into_the_answer_s_line(void)
{
    svc_messages_fake_clear();
    svc_audio_init();
    svc_settings_set(SET_TTS, 0);
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv"), "a talk to Jerv");
    svc_rtc_fake_advance_ms(2000);
    svc_audio_talk_end(true);
    svc_work_t w;
    CHECK(!svc_audio_work("jerv", &w), "no working view before the box has heard");
    svc_audio_fake_progress("high tide", "Thinking\xE2\x80\xA6");
    svc_rtc_fake_advance_ms(4000);
    CHECK(svc_audio_work("jerv", &w) && w.n == 0 && !strcmp(w.phase, "Thinking\xE2\x80\xA6") &&
              w.elapsed_ms == 4000,
          "thinking, with the clock running since the question went");
    const uint32_t seq0 = w.seq;
    svc_audio_fake_step(0, "web_search \xC2\xB7 tide Port St John");
    CHECK(svc_audio_work("jerv", &w) && w.n == 1 && w.step[0].state == WORK_RUNNING && w.seq != seq0,
          "a tool starting is a change to draw");
    svc_rtc_fake_advance_ms(1000);
    const uint32_t seq1 = w.seq;
    CHECK(svc_audio_work("jerv", &w) && w.seq == seq1 && w.elapsed_ms == 5000,
          "the clock alone is not a change");
    svc_audio_fake_step_done(0, true);
    svc_audio_fake_step(1, "web_fetch \xC2\xB7 tide-forecast.com");
    svc_audio_fake_step_done(1, true);
    CHECK(svc_audio_work("jerv", &w) && w.n == 2 && w.step[1].state == WORK_OK && !w.folded,
          "two steps, both done");
    CHECK(!svc_audio_work("dave", &w), "Dave's thread shows none of it");
    svc_rtc_fake_advance_ms(53000);
    svc_audio_fake_reply("high tide", "High tide is at 4:12 pm.", false);
    svc_audio_service();
    msg_t m = last_jerv();
    CHECK(!strcmp(m.text, "High tide is at 4:12 pm.") && !strcmp(m.status, "2 steps \xC2\xB7 58 s"),
          "the answer carries the steps as one dim line");
    CHECK(!svc_audio_work("jerv", &w), "and the working view is gone");

    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv"), "the next talk");
    svc_rtc_fake_advance_ms(2000);
    svc_audio_talk_end(true);
    svc_audio_fake_progress("hi", NULL);
    CHECK(svc_audio_work("jerv", &w) && w.n == 0, "starts with no steps");
    svc_audio_fake_reply("hi", "Hello.", false);
    svc_audio_service();
    m = last_jerv();
    CHECK(!m.status[0], "and a turn with no tool gets no line");
    svc_messages_fake_clear();
    svc_settings_set(SET_TTS, 1);
}

static void test_a_status_change_redraws_only_the_working_view(void)
{
    /* The spinner bug (owner, 2026-10-12): every status line rebuilt the whole list, and with it
       the spinner, which started its sweep again. Now only the messages rebuild the list. */
    const convo_sig_t drawn = {.rev = 4, .speaking = 0, .tts = true, .working = true, .work_seq = 2};
    convo_sig_t now = drawn;
    CHECK(convo_redraw(&drawn, &now) == CONVO_SAME, "nothing moved: nothing to draw");
    now.work_seq = 3;
    CHECK(convo_redraw(&drawn, &now) == CONVO_WORK, "a new status or step: the working view, in place");
    now.rev = 5;
    CHECK(convo_redraw(&drawn, &now) == CONVO_REBUILD, "a message changed: the list");
    now = drawn;
    now.working = false;
    CHECK(convo_redraw(&drawn, &now) == CONVO_REBUILD, "the working view going: the list");
    now = drawn;
    now.speaking = 9;
    CHECK(convo_redraw(&drawn, &now) == CONVO_REBUILD, "\"Speaking…\" coming: the list");
    now = drawn;
    now.can_speak = true;
    CHECK(convo_redraw(&drawn, &now) == CONVO_REBUILD, "Listen buttons coming: the list");

    svc_messages_fake_clear();
    const uint32_t r0 = svc_messages_rev("jerv");
    svc_messages_fake_add("jerv", &(msg_t){.id = 1, .text = "q"});
    const uint32_t r1 = svc_messages_rev("jerv");
    CHECK(r1 != r0 && svc_messages_rev("dave") == svc_messages_rev("dave"), "adding moves the thread's rev");
    svc_audio_init();
    CHECK(svc_audio_talk_begin(TALK_TO_AGENT, "jerv"), "a talk");
    svc_rtc_fake_advance_ms(2000);
    svc_audio_talk_end(true);
    svc_audio_fake_progress("q", NULL);
    svc_audio_service();
    const uint32_t r2 = svc_messages_rev("jerv");
    svc_audio_fake_progress(NULL, "Writing the answer\xE2\x80\xA6");
    svc_audio_fake_step(0, "weather");
    svc_audio_service();
    CHECK(svc_messages_rev("jerv") == r2, "a status line or a step leaves the messages as they were");
    svc_messages_mark_played("jerv", 1);
    svc_messages_mark_played("jerv", 1);
    svc_messages_clear("jerv");
    CHECK(svc_messages_rev("jerv") != r2 && svc_messages_rev("nobody") == 0, "clearing moves it");
    svc_audio_stop_reply();
    svc_audio_service();
    svc_messages_fake_clear();
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

/* alarm_link.c sizes its fetch buffer from ALARM_JSON_MAX. Build the box's longest answer in its
 * own shape (panel_alarms.py's `Alarms`, compact): every number at its widest, every label full
 * of a character the box allows. It must fit the bound, and parse. */
static int longest_answer(char *body, size_t cap, const char *id)
{
    char tag[ALARM_ETAG_BYTES];
    memset(tag, 'f', sizeof tag - 1);
    tag[sizeof tag - 1] = '\0';
    char label[ALARM_LABEL_BYTES + 1];
    memset(label, '~', ALARM_LABEL_BYTES);
    label[ALARM_LABEL_BYTES] = '\0';
    int n = snprintf(body, cap, "{\"etag\":\"%s\",\"alarms\":[", tag);
    for (int i = 0; i < ALARM_MAX; i++)
        n += snprintf(body + n, cap - (size_t)n,
                      "%s{\"at\":1439,\"days\":127,\"on\":false,\"label\":\"%s\",\"sound\":\"chime\","
                      "\"id\":%s}",
                      i ? "," : "", label, id);
    n += snprintf(body + n, cap - (size_t)n,
                  "],\"max\":8,\"panel_etag\":\"%s\",\"panel_seen_s\":4294967295}", tag);
    return n;
}

static void test_the_box_s_longest_alarms_answer_fits_the_fetch_buffer(void)
{
    static char body[ALARM_JSON_MAX + 64];
    /* The box's ids are bigint: nineteen digits at the widest it could ever write. */
    int n = longest_answer(body, sizeof body, "9223372036854775807");
    CHECK((size_t)n == strlen(body) && (size_t)n <= ALARM_JSON_MAX,
          "eight full alarms are within the bound the fetch buffer is sized from");
    /* The panel keeps a 32-bit id; the widest of those must still read whole. */
    n = longest_answer(body, sizeof body, "4294967295");
    alarm_list_t l;
    char label[ALARM_LABEL_BYTES + 1];
    memset(label, '~', ALARM_LABEL_BYTES);
    label[ALARM_LABEL_BYTES] = '\0';
    CHECK(alarm_parse(body, (size_t)n, &l) && l.n == ALARM_MAX && !strcmp(l.a[0].label, label),
          "and the panel reads all eight");
    CHECK(ALARM_JSON_MAX < 1100, "the bound is a buffer the main task can afford");
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

/* Telemetry (the main task) and the debug snapshot (either task) read a view the UI task
 * publishes, never the schedule it is changing: a list handed over shows up there only once the
 * UI task has applied it, and "next" is worked out by the UI task, in its zone. */
static void test_other_tasks_read_the_alarms_the_ui_task_published(void)
{
    alarm_port_fake_wipe();
    svc_settings_set(SET_TZ, NEW_YORK);
    svc_alarms_init();
    svc_rtc_fake_set_date(2026, 10, 12); /* a Monday */
    svc_rtc_fake_set(6, 30);
    uint32_t ms = svc_rtc_uptime_ms();
    char note[192];
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "alarm=n:0,next:-,") == note, "before the UI task's first pass: nothing");
    CHECK(svc_alarms_offer(SERVICE_JSON, sizeof SERVICE_JSON - 1), "the main task hands a list over");
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "alarm=n:0,next:-,") == note,
          "telemetry does not see a list the UI task has not applied");
    svc_alarms_service(ms);
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "alarm=n:2,next:+15,") == note, "applied and published: 06:45, in 15 min");
    svc_alarms_set_on(3, false);
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "next:+30,"), "a switch on the glass is published at once: 07:00 is next");
    svc_rtc_fake_advance_ms(10 * 60000);
    svc_alarms_service(ms + 500);
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "next:+30,"), "between passes the view is not recomputed");
    svc_alarms_service(ms + 1000);
    svc_alarms_note(note, sizeof note);
    CHECK(strstr(note, "next:+20,"), "a second on, it is");
    svc_settings_set(SET_TZ, 0);
    svc_alarms_service(ms + 2000);
    alarm_next_t n;
    CHECK(svc_alarms_next(&n) && n.at_h == 7 && n.at_m == 0,
          "a change of zone is parsed whole before it is used: the alarm stays at 07:00 local");
    svc_settings_set(SET_TZ, NEW_YORK);
    alarm_port_fake_wipe();
}

/* ---- 0.0.15: New conversation, Recent, switching (owner, 2026-10-12) ---- */

static void test_an_agent_s_menu_is_new_recent_and_cancel(void)
{
    sh_menu_t m = {0};
    sh_menu_open(&m, SH_MENU_CONVO);
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 0) == SH_MENU_DO_NOTHING && m.state == SH_MENU_OPEN,
          "an agent's menu has no Clear chat to reach");
    CHECK(sh_menu_input(&m, SH_MENU_IN_NEW, 10) == SH_MENU_DO_NEW && m.state == SH_MENU_CLOSED,
          "New conversation is one tap, no confirm, and closes the menu");
    sh_menu_open(&m, SH_MENU_CONVO);
    CHECK(sh_menu_input(&m, SH_MENU_IN_RECENT, 20) == SH_MENU_DO_RECENT && m.state == SH_MENU_CLOSED,
          "Recent conversations closes the menu and opens the list");
    sh_menu_open(&m, SH_MENU_CONVO);
    CHECK(sh_menu_input(&m, SH_MENU_IN_CANCEL, 30) == SH_MENU_DO_CLOSE, "Cancel closes");
    sh_menu_open(&m, SH_MENU_CONVO);
    CHECK(sh_menu_input(&m, SH_MENU_IN_OUTSIDE, 40) == SH_MENU_DO_CLOSE, "so does a tap outside");
    CHECK(sh_menu_input(&m, SH_MENU_IN_NEW, 50) == SH_MENU_DO_NOTHING, "a closed menu does nothing");
    sh_menu_open(&m, SH_MENU_CLEAR);
    CHECK(sh_menu_input(&m, SH_MENU_IN_NEW, 60) == SH_MENU_DO_NOTHING &&
              sh_menu_input(&m, SH_MENU_IN_RECENT, 60) == SH_MENU_DO_NOTHING && m.state == SH_MENU_OPEN,
          "a person's thread keeps Clear chat and has neither");
    CHECK(sh_menu_input(&m, SH_MENU_IN_CLEAR, 70) == SH_MENU_DO_ARM, "its Clear still arms first");
}

static void test_new_conversation_is_one_tap_and_does_nothing_when_already_new(void)
{
    CHECK(convo_new_decide(false, 4) == CONVO_NEW_START, "a conversation with messages: a new one");
    CHECK(convo_new_decide(true, 2) == CONVO_NEW_START,
          "a new one already begun (KEY from outside) can be left for another");
    CHECK(convo_new_decide(false, 0) == CONVO_NEW_START,
          "an empty thread after a restart is still the box's last conversation: a new one");
    CHECK(convo_new_decide(true, 0) == CONVO_NEW_ALREADY, "new and empty: nothing, and it says so");
    CHECK(!strcmp(CONVO_TOAST_ALREADY, "Already a new conversation"), "the mock's words");
    CHECK(!strcmp(CONVO_TOAST_NEW, "New conversation — the last one is in Recent"), "the mock's words");

    CHECK(!strcmp(convo_subtitle(false, "Shopping list"), "Shopping list"), "the box's title");
    CHECK(!strcmp(convo_subtitle(false, NULL), "Desk panel"), "Desk panel until the box has said");
    CHECK(!strcmp(convo_subtitle(false, ""), "New conversation"), "no conversation on the box yet");
    CHECK(!strcmp(convo_subtitle(true, "Shopping list"), "New conversation"),
          "a new one asked for: not the old one's title");
    CHECK(convo_row_is_current(true, false) && !convo_row_is_current(true, true) &&
              !convo_row_is_current(false, false),
          "the box's current row is Current unless a new conversation was asked for since");
}

static void test_a_row_s_time_reads_like_the_mock(void)
{
    /* Monday 12 Oct 2026, 18:00 in New York (UTC-4): 22:00 UTC. */
    const int64_t now = 1791842400;
    const int32_t ny = -4 * 3600;
    char s[24];
    convo_when(now - 20, now, ny, s, sizeof s);
    CHECK(!strcmp(s, "Just now"), "under a minute");
    convo_when(now + 30, now, ny, s, sizeof s);
    CHECK(!strcmp(s, "Just now"), "a box clock a little ahead is not the future");
    convo_when(now - 12 * 60, now, ny, s, sizeof s);
    CHECK(!strcmp(s, "12 min ago"), "under an hour");
    convo_when(now - 9 * 3600 - 48 * 60, now, ny, s, sizeof s);
    CHECK(!strcmp(s, "Today 08:12"), "earlier today, 24-hour");
    convo_when(now - (22 * 3600 + 20 * 60), now, ny, s, sizeof s);
    CHECK(!strcmp(s, "Yesterday 19:40"), "yesterday");
    convo_when(now - (2 * 86400 + 7 * 3600 + 55 * 60), now, ny, s, sizeof s);
    CHECK(!strcmp(s, "Sat 10:05"), "this week: the day");
    convo_when(now - 7 * 86400, now, ny, s, sizeof s);
    CHECK(!strcmp(s, "5 Oct"), "older: the date");
    convo_when(now - 3 * 3600, now, 5 * 3600 + 1800, s, sizeof s);
    CHECK(!strcmp(s, "Today 00:30"), "the zone decides the day: 03:30 local, three hours back");
    convo_when(now - 4 * 3600, now, 5 * 3600 + 1800, s, sizeof s);
    CHECK(!strcmp(s, "Yesterday 23:30"), "and across its midnight");
}

static void test_recent_opens_lists_switches_and_closes_as_the_mock_does(void)
{
    rc_t r = {0};
    CHECK(rc_step(&r, RC_IN_KEY, 0, false) == RC_DO_NOTHING, "closed, KEY is the thread's");
    CHECK(rc_step(&r, RC_IN_OPEN, 0, false) == RC_DO_FETCH && r.mode == RC_LOADING,
          "opened: Loading… while the box is asked");
    CHECK(rc_step(&r, RC_IN_OPEN, 0, false) == RC_DO_NOTHING, "asked once");
    CHECK(rc_step(&r, RC_IN_TAP, 0, false) == RC_DO_NOTHING, "no row to tap while loading");
    CHECK(rc_step(&r, RC_IN_LISTED, 4, false) == RC_DO_DRAW && r.mode == RC_LIST, "the rows");
    CHECK(rc_step(&r, RC_IN_LISTED, 4, false) == RC_DO_NOTHING, "a late second answer changes nothing");
    CHECK(rc_step(&r, RC_IN_TAP, 0, true) == RC_DO_CLOSE && r.mode == RC_CLOSED,
          "the current row: back to the thread, nothing fetched");

    r = (rc_t){0};
    rc_step(&r, RC_IN_OPEN, 0, false);
    rc_step(&r, RC_IN_LISTED, 4, false);
    CHECK(rc_step(&r, RC_IN_TAP, -1, false) == RC_DO_NOTHING, "a row that is not there");
    CHECK(rc_step(&r, RC_IN_TAP, 2, false) == RC_DO_SWITCH && r.mode == RC_OPENING && r.opening == 2,
          "another row: Opening… on it, and the switch asked");
    CHECK(rc_step(&r, RC_IN_TAP, 1, false) == RC_DO_NOTHING, "one switch at a time");
    CHECK(rc_step(&r, RC_IN_KEY, 0, false) == RC_DO_WAIT && r.mode == RC_OPENING,
          "KEY mid-switch says so and starts nothing");
    CHECK(rc_step(&r, RC_IN_NEW, 0, false) == RC_DO_WAIT, "nor does New");
    CHECK(rc_step(&r, RC_IN_SWITCH_FAILED, 0, false) == RC_DO_NOT_OPENED && r.mode == RC_LIST,
          "a switch that failed: back to the list, saying so");
    rc_step(&r, RC_IN_TAP, 2, false);
    CHECK(rc_step(&r, RC_IN_SWITCHED, 0, false) == RC_DO_CLOSE && r.mode == RC_CLOSED,
          "landed: the thread, now that conversation");
    CHECK(rc_step(&r, RC_IN_SWITCHED, 0, false) == RC_DO_NOTHING, "once");

    r = (rc_t){0};
    rc_step(&r, RC_IN_OPEN, 0, false);
    CHECK(rc_step(&r, RC_IN_LISTED, 0, false) == RC_DO_DRAW && r.mode == RC_EMPTY, "none: the empty state");
    CHECK(rc_step(&r, RC_IN_KEY, 0, false) == RC_DO_CLOSE_TALK && r.mode == RC_CLOSED,
          "KEY closes Recent and talks in the current conversation");
    rc_step(&r, RC_IN_OPEN, 0, false);
    CHECK(rc_step(&r, RC_IN_FAILED, 0, false) == RC_DO_DRAW && r.mode == RC_FAILED, "no answer");
    CHECK(rc_step(&r, RC_IN_OPEN, 0, false) == RC_DO_FETCH, "Try again asks again");
    CHECK(rc_step(&r, RC_IN_BACK, 0, false) == RC_DO_CLOSE && r.mode == RC_CLOSED, "BOOT goes back");
    CHECK(rc_step(&r, RC_IN_BACK, 0, false) == RC_DO_NOTHING, "and closed, BOOT is not Recent's");
    rc_step(&r, RC_IN_OPEN, 0, false);
    CHECK(rc_step(&r, RC_IN_NEW, 0, false) == RC_DO_CLOSE_NEW && r.mode == RC_CLOSED,
          "the + row starts a new conversation from Recent too");
    CHECK(rc_step(&r, RC_IN_NEW, 0, false) == RC_DO_NOTHING, "not when closed");
}

static const char RECENT_JSON[] =
    "{\"items\":[{\"id\":\"11111111-2222-4333-8444-555555555555\",\"title\":\"Shopping list\","
    "\"at\":1791841800,\"origin\":\"panel\",\"snippet\":\"Added milk to your list.\",\"current\":true},"
    "{\"current\":false,\"snippet\":\"You: Good for rock pools later?\",\"origin\":\"pwa\",\"future\":"
    "{\"a\":[1,2,{\"b\":null}]},\"at\":1791841000,\"title\":\"Tides at Port St John\","
    "\"id\":\"22222222-2222-4333-8444-555555555555\"}],\"etag\":\"x\"}";

static void test_the_box_s_recent_json_is_read_as_the_box_writes_it(void)
{
    convo_list_t l;
    CHECK(convo_parse_recent(RECENT_JSON, sizeof RECENT_JSON - 1, &l) && l.n == 2, "two rows");
    CHECK(!strcmp(l.row[0].title, "Shopping list") && l.row[0].current &&
              l.row[0].origin == CONVO_FROM_PANEL && l.row[0].at == 1791841800,
          "the current one, started at the panel");
    CHECK(!strcmp(l.row[1].id, "22222222-2222-4333-8444-555555555555") &&
              l.row[1].origin == CONVO_FROM_PWA && !l.row[1].current &&
              !strcmp(l.row[1].snippet, "You: Good for rock pools later?"),
          "fields in any order, an unknown one skipped whole");

    static const char EMPTY[] = "{\"items\": []}";
    CHECK(convo_parse_recent(EMPTY, sizeof EMPTY - 1, &l) && l.n == 0, "an empty list is a list");
    static const char *const BAD[] = {"", "[]", "{\"items\":{}}", "{\"rows\":[]}",
                                      "{\"items\":[{\"id\":\"x\"", "{\"items\":[{\"at\":1.5}]}",
                                      "{\"items\":[{\"title\":\"no end}]}"};
    for (size_t i = 0; i < sizeof BAD / sizeof *BAD; i++)
        CHECK(!convo_parse_recent(BAD[i], strlen(BAD[i]), &l) && l.n == 0, "not the shape: no rows");

    static const char DROPPED[] =
        "{\"items\":[{\"title\":\"no id\",\"origin\":\"pwa\"},"
        "{\"id\":\"33333333-2222-4333-8444-555555555555\",\"origin\":\"game\"},"
        "{\"id\":\"\",\"origin\":\"pwa\"},"
        "{\"id\":\"44444444-2222-4333-8444-555555555555\",\"origin\":\"pwa\",\"title\":null,"
        "\"at\":null,\"current\":null}]}";
    CHECK(convo_parse_recent(DROPPED, sizeof DROPPED - 1, &l) && l.n == 1 &&
              !strcmp(l.row[0].id, "44444444-2222-4333-8444-555555555555") && !l.row[0].title[0],
          "a row with no id or an origin the panel has no glyph for is dropped; nulls are empty");

    /* Oversize: a title three times a row's and a last line with a character across the cut. */
    char big[1200];
    int o = snprintf(big, sizeof big, "{\"items\":[{\"id\":\"55555555-2222-4333-8444-555555555555\","
                                       "\"origin\":\"pwa\",\"title\":\"");
    for (int i = 0; i < 120; i++) big[o++] = 'T';
    o += snprintf(big + o, sizeof big - (size_t)o, "\",\"snippet\":\"");
    for (int i = 0; i < 39; i++) o += snprintf(big + o, sizeof big - (size_t)o, "\xC3\xA9");
    o += snprintf(big + o, sizeof big - (size_t)o, "\\u00e9\\ud83d\\ude00 tail\\n\\\"q\\\"\"}]}");
    CHECK(convo_parse_recent(big, (size_t)o, &l) && l.n == 1, "an oversize row still reads");
    CHECK(strlen(l.row[0].title) == CONVO_TITLE_BYTES - 1 &&
              !strcmp(l.row[0].title + CONVO_TITLE_BYTES - 4, "\xE2\x80\xA6"),
          "a title cut to its 40 bytes, the last three the ellipsis");
    const size_t sn = strlen(l.row[0].snippet);
    CHECK(sn < CONVO_SNIPPET_BYTES && !strcmp(l.row[0].snippet + sn - 3, "\xE2\x80\xA6"),
          "a last line cut to fit, with the ellipsis");
    CHECK((sn - 3) % 2 == 0, "and never through the middle of a character");

    /* Eleven rows: the panel keeps ten. */
    char many[4096];
    o = snprintf(many, sizeof many, "{\"items\":[");
    for (int i = 0; i < 11; i++)
        o += snprintf(many + o, sizeof many - (size_t)o,
                      "%s{\"id\":\"%08d-2222-4333-8444-555555555555\",\"origin\":\"panel\"}",
                      i ? "," : "", i);
    o += snprintf(many + o, sizeof many - (size_t)o, "]}");
    CHECK(convo_parse_recent(many, (size_t)o, &l) && l.n == CONVO_ROWS &&
              !strcmp(l.row[9].id, "00000009-2222-4333-8444-555555555555"),
          "past ten rows are ignored");
    CHECK(sizeof(convo_list_t) <= 2000, "the list the heap holds while Recent is open stays small");
}

typedef struct {
    int n;
    bool mine[8];
    char text[8][MSG_LONG_TEXT];
} got_msgs_t;

static void got_msg(void *user, bool mine, const char *text)
{
    got_msgs_t *g = user;
    if (g->n >= 8) return;
    g->mine[g->n] = mine;
    snprintf(g->text[g->n], sizeof g->text[g->n], "%s", text);
    g->n++;
}

static void test_a_switched_thread_s_json_is_read_as_the_box_writes_it(void)
{
    static const char T[] =
        "{\"id\":\"22222222-2222-4333-8444-555555555555\",\"title\":\"Tides at Port St John\","
        "\"messages\":[{\"mine\":true,\"text\":\"When's low tide today?\"},"
        "{\"mine\":false,\"text\":\"Low 14:52, high 21:07.\",\"extra\":[true]},"
        "{\"mine\":false,\"text\":\"\"},{\"text\":\"no mine field\"}],\"more\":true}";
    char title[CONVO_TITLE_BYTES], scratch[MSG_LONG_TEXT];
    bool more = false;
    got_msgs_t *g = calloc(1, sizeof *g);
    CHECK(convo_parse_thread(T, sizeof T - 1, title, sizeof title, &more, scratch, sizeof scratch,
                             got_msg, g),
          "reads");
    CHECK(!strcmp(title, "Tides at Port St John") && more, "the title, and older messages in the app");
    CHECK(g->n == 3 && g->mine[0] && !g->mine[1] && !strcmp(g->text[1], "Low 14:52, high 21:07.") &&
              !g->mine[2],
          "each message in order; an empty one is skipped; no `mine` is the agent's");

    char shortbuf[8];
    memset(g, 0, sizeof *g);
    CHECK(convo_parse_thread(T, sizeof T - 1, title, sizeof title, &more, shortbuf, sizeof shortbuf,
                             got_msg, g) &&
              strlen(g->text[0]) <= 7,
          "a text cut to the caller's buffer");
    static const char BAD[] = "{\"messages\":[{\"mine\":true,\"text\":\"a\"},{\"mine\":tru}]}";
    memset(g, 0, sizeof *g);
    CHECK(!convo_parse_thread(BAD, sizeof BAD - 1, title, sizeof title, &more, scratch, sizeof scratch,
                              got_msg, g) &&
              g->n == 1,
          "a fault stops the reading where it is");
    CHECK(!convo_parse_thread(T, sizeof T - 1, title, sizeof title, &more, scratch, 0, got_msg, g),
          "no buffer, no reading");
    static const char NONE[] = "{\"id\":\"\",\"title\":\"\",\"messages\":[],\"more\":false}";
    CHECK(convo_parse_thread(NONE, sizeof NONE - 1, title, sizeof title, &more, scratch, sizeof scratch,
                             got_msg, g) &&
              !title[0] && !more,
          "no thread on the box: an empty title");
    free(g);

    char path[64], body[64];
    convo_path(CONVO_RQ_RECENT, "jerv", false, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/convo/jerv/recent"), "Recent's path");
    convo_path(CONVO_RQ_SWITCH, "dave", false, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/convo/dave/select"), "a switch, first");
    convo_path(CONVO_RQ_SWITCH, "dave", true, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/convo/dave/messages?limit=3"), "then its last exchanges");
    convo_path(CONVO_RQ_TITLE, "jerv", false, path, sizeof path);
    CHECK(!strcmp(path, "/endpoint/convo/jerv/messages?limit=0"), "the title alone");
    CHECK(convo_select_body("22222222-2222-4333-8444-555555555555", body, sizeof body) &&
              !strcmp(body, "{\"session_id\":\"22222222-2222-4333-8444-555555555555\"}"),
          "the switch's body");
    CHECK(!convo_select_body("22222222-2222-4333-8444-55555555555\"", body, sizeof body) &&
              !convo_select_body("short", body, sizeof body) && !convo_select_body(NULL, body, sizeof body),
          "anything but a session id is never sent");
    CHECK(convo_agent_ok("jerv") && convo_agent_ok("dave") && !convo_agent_ok(SVC_MESSAGES_THREAD_1) &&
              !convo_agent_ok(NULL),
          "only the agents have conversations on the box");
    char fit[8];
    convo_fit(fit, sizeof fit, "abcdefghij", 10);
    CHECK(!strcmp(fit, "abcd\xE2\x80\xA6"), "convo_fit cuts with the ellipsis");
    convo_fit(fit, sizeof fit, "abc", 3);
    CHECK(!strcmp(fit, "abc"), "and keeps what fits");
}

static char *heap(const char *s)
{
    char *b = malloc(strlen(s) + 1);
    strcpy(b, s);
    return b;
}

static void test_the_conversation_service_end_to_end(void)
{
    svc_messages_fake_clear();
    svc_audio_init();
    convo_req_t r;
    while (svc_convo_take(&r)) {
    }
    svc_convo_service();

    /* Recent: asked, fetched by the main task, applied by the UI task. */
    CHECK(svc_convo_recent(NULL) == CONVO_IDLE, "nothing open");
    CHECK(!svc_convo_recent_open(SVC_MESSAGES_THREAD_1), "a person's thread has no Recent");
    CHECK(svc_convo_recent_open("jerv") && svc_convo_recent(NULL) == CONVO_WAITING, "asked");
    CHECK(svc_convo_take(&r) && r.kind == CONVO_RQ_RECENT && !strcmp(r.agent, "jerv"),
          "the main task takes it");
    CHECK(!svc_convo_take(&r) || r.kind != CONVO_RQ_RECENT, "once");
    svc_convo_done(&r, 200, heap(RECENT_JSON), sizeof RECENT_JSON - 1);
    CHECK(svc_convo_recent(NULL) == CONVO_WAITING, "not until the UI task applies it");
    svc_convo_service();
    const convo_list_t *l = NULL;
    CHECK(svc_convo_recent(&l) == CONVO_READY && l && l->n == 2, "the list");
    svc_convo_recent_close();
    CHECK(svc_convo_recent(&l) == CONVO_IDLE && !l, "closed: freed");

    /* An answer for a list closed meanwhile is dropped, not applied to the next. */
    svc_convo_recent_open("dave");
    convo_req_t old;
    svc_convo_take(&old);
    svc_convo_recent_close();
    svc_convo_recent_open("dave");
    svc_convo_done(&old, 200, heap(RECENT_JSON), sizeof RECENT_JSON - 1);
    svc_convo_service();
    CHECK(svc_convo_recent(NULL) == CONVO_WAITING, "a stale answer is dropped");
    svc_convo_take(&r);
    svc_convo_done(&r, 503, NULL, 0);
    svc_convo_service();
    CHECK(svc_convo_recent(NULL) == CONVO_FAILED, "the box's refusal: Try again");
    svc_convo_recent_close();

    /* A switch: the box's last exchanges replace the panel's view; the title and the chip. */
    msg_t m = {.id = 7, .mine = true};
    snprintf(m.text, sizeof m.text, "Add milk.");
    svc_messages_fake_add("jerv", &m);
    svc_audio_fresh("jerv");
    svc_convo_switch("jerv", "22222222-2222-4333-8444-555555555555");
    CHECK(svc_convo_switching() == CONVO_WAITING, "Opening…");
    CHECK(svc_convo_take(&r) && r.kind == CONVO_RQ_SWITCH &&
              !strcmp(r.id, "22222222-2222-4333-8444-555555555555"),
          "the switch, with the conversation it names");
    char long_answer[1100];
    memset(long_answer, 'w', 999);
    long_answer[999] = '\0';
    char thread[1400];
    snprintf(thread, sizeof thread,
             "{\"id\":\"x\",\"title\":\"Tides at Port St John\",\"messages\":[{\"mine\":true,"
             "\"text\":\"When's low tide?\"},{\"mine\":false,\"text\":\"%s\"}],\"more\":true}",
             long_answer);
    svc_convo_done(&r, 200, heap(thread), strlen(thread));
    svc_convo_service();
    CHECK(svc_convo_switching() == CONVO_READY, "landed");
    CHECK(svc_messages_count("jerv") == 2, "the thread is that conversation's, not the old one's");
    msg_t got;
    svc_messages_get("jerv", 1, &got);
    CHECK(!got.mine && strlen(svc_messages_text("jerv", &got)) == 999 && strlen(got.text) < 256,
          "its newest answer is kept whole, its bubble cut");
    CHECK(!strcmp(svc_convo_title("jerv"), "Tides at Port St John") && svc_convo_earlier("jerv"),
          "its title, and earlier messages in the app");
    CHECK(!svc_audio_fresh_pending("jerv"), "the next talk continues it, not a new one");
    CHECK(svc_convo_title("dave") == NULL && !svc_convo_earlier("dave"), "Dave's is untouched");

    /* The title, asked again: the same one changes nothing; a new conversation forgets it. */
    const uint32_t rev = svc_convo_rev();
    svc_convo_refresh_title("jerv");
    svc_convo_take(&r);
    static const char SAME[] = "{\"title\":\"Tides at Port St John\",\"messages\":[]}";
    svc_convo_done(&r, 200, heap(SAME), sizeof SAME - 1);
    svc_convo_service();
    CHECK(svc_convo_rev() == rev, "the same title draws nothing");
    svc_audio_fresh("jerv");
    svc_convo_service();
    CHECK(svc_convo_title("jerv") == NULL && !svc_convo_earlier("jerv") && svc_convo_rev() != rev,
          "a new conversation, asked for anywhere, forgets the old title and the chip");
    svc_convo_refresh_title("jerv");
    svc_convo_take(&r);
    svc_convo_done(&r, 200, heap(SAME), sizeof SAME - 1);
    svc_convo_service();
    CHECK(svc_convo_title("jerv") == NULL, "an old title arriving after it is not this one's");
    svc_audio_continue("jerv");
    svc_convo_new("jerv");

    /* A switch the box refuses changes nothing on the panel. */
    const uint16_t before = svc_messages_count("jerv");
    svc_convo_switch("jerv", "33333333-2222-4333-8444-555555555555");
    svc_convo_take(&r);
    svc_convo_done(&r, 404, NULL, 0);
    svc_convo_service();
    CHECK(svc_convo_switching() == CONVO_FAILED && svc_messages_count("jerv") == before,
          "refused: the thread stays as it was");
    /* Switched, but its messages did not come: an empty thread, and the title asked for. */
    svc_convo_switch("dave", "44444444-2222-4333-8444-555555555555");
    svc_convo_take(&r);
    svc_convo_done(&r, 200, NULL, 0);
    svc_convo_service();
    CHECK(svc_convo_switching() == CONVO_READY && svc_messages_count("dave") == 0 &&
              svc_convo_earlier("dave"),
          "the switch held: the rest is in the app");
    CHECK(svc_convo_take(&r) && r.kind == CONVO_RQ_TITLE && !strcmp(r.agent, "dave"),
          "and its title is asked for");

    /* A burst of the same ask is one fetch; a full queue drops the oldest. */
    for (int i = 0; i < 3; i++) svc_convo_refresh_title("jerv");
    CHECK(svc_convo_take(&r) && r.kind == CONVO_RQ_TITLE && !svc_convo_take(&r), "three asks, one fetch");
    svc_convo_refresh_title("jerv");
    svc_convo_refresh_title("dave");
    svc_convo_recent_open("jerv");
    svc_convo_switch("dave", "55555555-2222-4333-8444-555555555555");
    svc_convo_recent_open("dave");
    int n = 0;
    while (svc_convo_take(&r)) n++;
    CHECK(n == 4, "four wait at most");
    for (int i = 0; i < 6; i++) {
        convo_req_t stale = {.kind = CONVO_RQ_TITLE, .seq = 1};
        snprintf(stale.agent, sizeof stale.agent, "jerv");
        svc_convo_done(&stale, 200, heap(SAME), sizeof SAME - 1);
    }
    svc_convo_service();
    svc_convo_recent_close();
    char note[96];
    svc_convo_note(note, sizeof note);
    CHECK(strncmp(note, "convo=rq:", 9) == 0, "its telemetry token");
    svc_messages_fake_clear();
}

/* ---- 0.0.16: voice post with the twins' panels ---- */

#define Q_DEV "0b8e5f7a-1111-4c2d-9e3f-5a6b7c8d9e01"
#define T_DEV "0b8e5f7a-2222-4c2d-9e3f-5a6b7c8d9e02"
#define M1 "6b000000-0000-4000-8000-000000000001"
#define M2 "6b000000-0000-4000-8000-000000000002"
#define M3 "6b000000-0000-4000-8000-000000000003"
#define M4 "6b000000-0000-4000-8000-000000000004"
#define M5 "6b000000-0000-4000-8000-000000000005"

static const char POST_SIBS[] =
    "{\"siblings\":[{\"device_id\":\"" Q_DEV "\",\"name\":\"Quorra\"},"
    "{\"device_id\":\"" T_DEV "\",\"name\":\"Tamsin\"}]}";

static void test_post_paths_take_only_box_ids(void)
{
    char p[POST_PATH_BYTES];
    CHECK(post_id_ok(M1) && !post_id_ok("6B000000-0000-4000-8000-000000000001") && !post_id_ok("x") &&
              !post_id_ok(M1 "0") && !post_id_ok(NULL) && !post_id_ok("../../../endpoint/debug/next?x=1234"),
          "an id is 36 of [0-9a-f-], nothing else reaches a path");
    CHECK(post_thread_path(Q_DEV, p, sizeof p) && !strcmp(p, "/endpoint/messages/" Q_DEV "?limit=4"),
          "a thread asks for the four a person keeps");
    CHECK(post_send_path(Q_DEV, p, sizeof p) && !strcmp(p, "/endpoint/messages/" Q_DEV "/send"), "send");
    CHECK(post_pcm_path(Q_DEV, M1, p, sizeof p) && !strcmp(p, "/endpoint/messages/" Q_DEV "/" M1 "/pcm"),
          "play");
    CHECK(post_played_path(Q_DEV, M1, p, sizeof p) &&
              !strcmp(p, "/endpoint/messages/" Q_DEV "/" M1 "/played"),
          "played");
    CHECK(!post_send_path("", p, sizeof p) && !p[0] && !post_pcm_path(Q_DEV, "nope", p, sizeof p) && !p[0],
          "no panel or no id: no path at all");
    char tiny[20];
    CHECK(!post_thread_path(Q_DEV, tiny, sizeof tiny) && !tiny[0], "a path that does not fit is none");
}

static void test_post_upload_is_framed_and_capped(void)
{
    CHECK(POST_MAX_BYTES == 960000u, "30 s of 16 kHz s16: exactly the box's MAX_MESSAGE_BYTES");
    CHECK(post_upload_room(0, 1024) == 1024 && post_upload_room(POST_MAX_BYTES - 100, 1024) == 100 &&
              post_upload_room(POST_MAX_BYTES, 1024) == 0 && post_upload_room(POST_MAX_BYTES + 5, 1) == 0,
          "the upload stops at the box's cap, mid-chunk if it must");
    CHECK(!strcmp(POST_SHA_HEADER, "X-Jpanel-Sha256") && !strcmp(POST_SHA_TRAILER, "trailer") &&
              POST_DIGEST_BYTES == 32,
          "the digest follows the audio, as the box's `trailer` reads it");
}

static void test_post_thread_json_reads_as_the_box_writes_it(void)
{
    static post_thread_t t;
    const char ok[] =
        "{\"device_id\":\"" Q_DEV "\",\"name\":\"Quorra\",\"tag\":\"a1b2c3d4\",\"messages\":["
        "{\"id\":\"" M1 "\",\"mine\":false,\"text\":\"Daddy, when are you coming home?\",\"ms\":4600,"
        "\"played\":false,\"voice\":true,\"extra\":{\"a\":[1,2]}},"
        "{\"id\":\"" M2 "\",\"mine\":true,\"text\":\"Soon!\",\"ms\":1200,\"played\":true,\"voice\":true}]}";
    CHECK(post_parse_thread(ok, sizeof ok - 1, &t) && t.n == 2 && !strcmp(t.tag, "a1b2c3d4"),
          "two messages and the tag");
    CHECK(!t.line[0].mine && !t.line[0].played && t.line[0].ms == 4600 && !strcmp(t.line[0].id, M1) &&
              !strcmp(t.line[0].text, "Daddy, when are you coming home?"),
          "hers, unplayed, with her words and length; unknown fields skipped");
    CHECK(t.line[1].mine && t.line[1].played, "his, which she has heard");
    const char six[] = "{\"messages\":[{\"id\":\"" M1 "\"},{\"id\":\"bad\"},{\"id\":\"" M2 "\"},{\"id\":\"" M3
                       "\"},{\"id\":\"" M4 "\"},{\"id\":\"" M5 "\"},{\"text\":\"no id\"}]}";
    CHECK(post_parse_thread(six, sizeof six - 1, &t) && t.n == POST_KEEP && !strcmp(t.line[0].id, M2) &&
              !strcmp(t.line[3].id, M5),
          "past four the newest are kept; a message with no usable id is dropped");
    char long_json[1400];
    int o = snprintf(long_json, sizeof long_json, "{\"messages\":[{\"id\":\"" M1 "\",\"text\":\"");
    for (int i = 0; i < 150; i++) o += snprintf(long_json + o, sizeof long_json - (size_t)o, "\xc3\xa9");
    snprintf(long_json + o, sizeof long_json - (size_t)o, "\\u00e9\"}]}");
    CHECK(post_parse_thread(long_json, strlen(long_json), &t) && t.n == 1 && strlen(t.line[0].text) < 256 &&
              !strcmp(t.line[0].text + strlen(t.line[0].text) - 3, "\xE2\x80\xA6"),
          "a text past a bubble is cut on a character with an ellipsis");
    const char esc[] = "{\"messages\":[{\"id\":\"" M1 "\",\"text\":\"a\\nb \\\"c\\\" \\ud83d\"}]}";
    CHECK(post_parse_thread(esc, sizeof esc - 1, &t) && !strcmp(t.line[0].text, "a b \"c\" ?"),
          "escapes: a newline is a space, a lone surrogate a question mark");
    static const char *const BAD[] = {"", "[]", "{}", "{\"messages\":{}}", "{\"messages\":[1]}",
                                      "{\"messages\":[{\"id\":", "{\"tag\":\"x\"}", "nope",
                                      "{\"messages\":[{\"ms\":1.5}]}"};
    for (size_t i = 0; i < sizeof BAD / sizeof *BAD; i++)
        CHECK(!post_parse_thread(BAD[i], strlen(BAD[i]), &t) && t.n == 0, "not the box's shape: nothing");
}

static void test_post_sent_answer_and_bubbles(void)
{
    char id[POST_ID_BYTES], text[256];
    uint32_t ms;
    const char ok[] = "{\"id\":\"" M3 "\",\"text\":\"Night night\",\"ms\":2100}";
    CHECK(post_parse_sent(ok, sizeof ok - 1, id, text, sizeof text, &ms) && !strcmp(id, M3) &&
              !strcmp(text, "Night night") && ms == 2100,
          "the send's 201: its id, her transcript of his words, the length");
    CHECK(!post_parse_sent("{\"text\":\"x\"}", 12, id, text, sizeof text, &ms) && !id[0],
          "an answer with no id is not one");
    post_line_t l = {.mine = false, .voice = true, .ms = 4600};
    msg_t m;
    post_to_msg(&l, 700001, &m);
    CHECK(m.id == 700001 && !m.mine && m.dur_s == 5 && !strcmp(m.text, POST_NO_TEXT) && !m.status[0],
          "hers with no transcript says it is a voice message, rounded to seconds");
    l.mine = true;
    post_to_msg(&l, 2, &m);
    CHECK(!strcmp(m.status, "Sent"), "his, not heard yet: Sent");
    l.played = true;
    post_to_msg(&l, 2, &m);
    CHECK(!strcmp(m.status, "Played"), "his, heard on her panel: Played");
}

static void test_post_unread_badge_and_chime_rules(void)
{
    static post_thread_t t;
    post_seen_t seen = {0};
    const char two[] = "{\"messages\":[{\"id\":\"" M1 "\",\"mine\":false,\"played\":false},"
                       "{\"id\":\"" M2 "\",\"mine\":true,\"played\":false}]}";
    post_parse_thread(two, sizeof two - 1, &t);
    CHECK(post_arrivals(&seen, &t) == 0, "the first list of a boot is the backlog: badged, not chimed");
    CHECK(post_arrivals(&seen, &t) == 0, "the same list again is nothing new");
    const char three[] = "{\"messages\":[{\"id\":\"" M1 "\",\"mine\":false,\"played\":false},"
                         "{\"id\":\"" M2 "\",\"mine\":true},{\"id\":\"" M3 "\",\"mine\":false,\"played\":false},"
                         "{\"id\":\"" M4 "\",\"mine\":false,\"played\":true}]}";
    post_parse_thread(three, sizeof three - 1, &t);
    CHECK(post_arrivals(&seen, &t) == 1, "one new of hers, unplayed: one arrival (his and played ones never)");
    CHECK(post_chime(1, true, false, false), "chimes");
    CHECK(!post_chime(0, true, false, false) && !post_chime(1, false, false, false) &&
              !post_chime(1, true, true, false) && !post_chime(1, true, false, true),
          "never with nothing new, the chime off, at night, or over anything else on the speaker");
    post_tag_t tag = {0};
    CHECK(post_tag_feed(&tag, "a1b2c3d4") && !post_tag_feed(&tag, "a1b2c3d4") && post_tag_feed(&tag, "ffff0000"),
          "the first tag and every change are a fetch; the same tag is not");
    CHECK(!post_tag_feed(&tag, "") && !post_tag_feed(&tag, NULL) && !strcmp(tag.last, "ffff0000"),
          "a pickup with no tag says nothing");
}

static void test_tk_listen_claims_without_a_hold(void)
{
    tk_t t;
    tk_init(&t);
    CHECK(tk_listen(&t, 7) == TK_BEGIN_OK && t.phase == TK_SEND && t.kind == TK_KIND_LISTEN && t.voice,
          "a play starts at the send with its speech wanted");
    CHECK(tk_net_claim(&t) && tk_net_next(&t) == TK_NET_FINISH, "the network takes it with no upload");
    tk_net_reply(&t, true);
    CHECK(t.phase == TK_PLAY && tk_audio_want(&t) == TK_AUDIO_SPK, "the audio plays through the reply path");
    CHECK(tk_listen(&t, 8) == TK_BEGIN_BUSY, "a second play waits for the first to let go");
    tk_net_end(&t);
    tk_audio_drained(&t);
    CHECK(tk_take(&t, NULL, 0) == TK_RES_SPOKEN && tk_quiet(&t), "played to its end");
    CHECK(tk_begin(&t, 0, false, 9) == TK_BEGIN_OK && t.kind == TK_KIND_AGENT, "a talk after is a talk");
    tk_set_post(&t);
    CHECK(t.kind == TK_KIND_POST && !t.fresh, "or a voice message to a twin");
}

static void test_a_person_thread_keeps_the_panels_own_bubbles(void)
{
    svc_messages_fake_clear();
    msg_t sending = {.id = 1000, .mine = true, .voice = true, .pending = true, .text = "Sending…"};
    svc_messages_fake_add(SVC_MESSAGES_THREAD_1, &sending);
    msg_t box[POST_KEEP];
    for (int i = 0; i < POST_KEEP; i++) box[i] = (msg_t){.id = 700000u + (uint32_t)i, .voice = true};
    CHECK(svc_messages_fake_set(SVC_MESSAGES_THREAD_1, box, POST_KEEP), "the box's list replaces the thread");
    msg_t last;
    svc_messages_get(SVC_MESSAGES_THREAD_1, 3, &last);
    CHECK(svc_messages_count(SVC_MESSAGES_THREAD_1) == 4 && last.id == 1000,
          "but a send on its way stays, last, and the oldest of the box's gives way");
    CHECK(!svc_messages_fake_set(SVC_MESSAGES_THREAD_1, box, POST_KEEP), "the same list is no change");
    CHECK(svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == 3, "her three unplayed are the tile's badge");
    CHECK(!svc_messages_fake_set("jerv", box, 1), "an agent's thread is never the box's post");
    svc_messages_fake_clear();
}

static void post_answer(const char *json)
{
    post_req_t r;
    while (svc_post_take(&r)) {
        if (r.kind != POST_RQ_THREAD) {
            svc_post_done(&r, 204, NULL, 0);
            continue;
        }
        char *b = malloc(strlen(json) + 1);
        strcpy(b, json);
        svc_post_done(&r, 200, b, strlen(json));
    }
    svc_post_service();
}

static const char Q_LIST[] =
    "{\"tag\":\"t1\",\"messages\":[{\"id\":\"" M1 "\",\"mine\":true,\"text\":\"Have a good day\",\"ms\":2000,"
    "\"played\":true},{\"id\":\"" M2 "\",\"mine\":false,\"text\":\"Daddy!\",\"ms\":3000,\"played\":false}]}";

static void test_the_people_s_threads_end_to_end(void)
{
    sib_port_fake_wipe();
    svc_messages_fake_clear();
    svc_siblings_init();
    post_port_fake_wipe();
    svc_post_init();
    svc_net_fake_set(NET_CONNECTED);
    svc_siblings_offer(POST_SIBS, sizeof POST_SIBS - 1);
    svc_siblings_service();
    svc_post_service();
    post_req_t r;
    CHECK(svc_post_take(&r) && r.kind == POST_RQ_THREAD && !strcmp(r.path, "/endpoint/messages/" Q_DEV "?limit=4"),
          "a bound thread is fetched at once");
    CHECK(svc_post_take(&r) && strstr(r.path, T_DEV), "both of them");
    CHECK(!svc_post_take(&r), "and nothing else");
    CHECK(!svc_post_loaded(SVC_MESSAGES_THREAD_1), "not loaded until the box answers");
    svc_post_open(SVC_MESSAGES_THREAD_1);
    svc_post_service();
    post_answer(Q_LIST);
    CHECK(svc_post_loaded(SVC_MESSAGES_THREAD_1) && svc_messages_count(SVC_MESSAGES_THREAD_1) == 2,
          "the box's two messages are the thread");
    CHECK(svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == 1 && svc_messages_unplayed_total() == 1,
          "her unplayed one is the tile's badge and the clock's");
    CHECK(!svc_post_take_chime(false), "the backlog at boot does not chime");

    /* A new one: the pickup's tag moves, both threads are fetched, the new one chimes. */
    svc_post_tag("t2");
    svc_post_service();
    CHECK(svc_post_take(&r) && svc_post_take(&r) && !svc_post_take(&r), "a moved tag fetches both threads");
    svc_post_tag("t2");
    svc_post_service();
    CHECK(!svc_post_take(&r), "the same tag again fetches nothing");
    static const char Q_NEW[] =
        "{\"tag\":\"t2\",\"messages\":[{\"id\":\"" M1 "\",\"mine\":true,\"played\":true},"
        "{\"id\":\"" M2 "\",\"mine\":false,\"text\":\"Daddy!\",\"ms\":3000,\"played\":false},"
        "{\"id\":\"" M3 "\",\"mine\":false,\"text\":\"I love you\",\"ms\":1500,\"played\":false}]}";
    svc_post_open(SVC_MESSAGES_THREAD_1);
    svc_post_service();
    post_answer(Q_NEW);
    CHECK(svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == 2, "two waiting: the badge says 2");
    svc_settings_set(SET_NOTIF_CHIME, 0);
    CHECK(!svc_post_take_chime(false), "the chime switch off: silent");
    svc_post_open(SVC_MESSAGES_THREAD_1);
    svc_post_service();
    static const char Q_NEWER[] =
        "{\"messages\":[{\"id\":\"" M2 "\",\"mine\":false,\"played\":false},{\"id\":\"" M3
        "\",\"mine\":false,\"played\":false},{\"id\":\"" M4 "\",\"mine\":false,\"played\":false}]}";
    svc_settings_set(SET_NOTIF_CHIME, 1);
    svc_rtc_fake_set(12, 0);
    post_answer(Q_NEWER);
    CHECK(!svc_post_take_chime(true), "busy with the speaker: no chime");
    svc_post_open(SVC_MESSAGES_THREAD_1);
    svc_post_service();
    static const char Q_NEWEST[] =
        "{\"messages\":[{\"id\":\"" M3 "\",\"mine\":false,\"played\":false},{\"id\":\"" M4
        "\",\"mine\":false,\"played\":false},{\"id\":\"" M5 "\",\"mine\":false,\"played\":false}]}";
    post_answer(Q_NEWEST);
    CHECK(svc_post_take_chime(false), "a new one in the day, chime on, speaker free: one chime");
    CHECK(!svc_post_take_chime(false), "once");

    /* Play her newest. 0.0.17 (owner, 2026-10-13: "A message should no longer be considered
       unread if I even hit play once on it"): heard once its audio starts, here and on the box;
       a play the box never answers with audio leaves it unheard and stamps nothing. */
    msg_t m;
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    const uint16_t badge = svc_messages_unplayed(SVC_MESSAGES_THREAD_1);
    const uint16_t clock_badge = svc_messages_unplayed_total();
    CHECK(svc_audio_play(m.id) && svc_audio_playing() == m.id, "a tap plays her message");
    CHECK(!strcmp(svc_audio_fake_last_path(), "/endpoint/messages/" Q_DEV "/" M5 "/pcm"),
          "from the box's own PCM route for that message");
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(!m.played && !svc_post_take(&r), "not heard before the box has answered with audio");
    svc_audio_fake_listen(false, false, false);
    svc_audio_service();
    svc_post_service();
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(!svc_audio_playing() && !m.played && svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == badge &&
              !svc_post_take(&r),
          "the box refused the play: still unread, no stamp, the badges as they were");
    CHECK(svc_audio_play(m.id), "again");
    CHECK(!talk_ring_held(), "no ring until the box answers the play's GET");
    CHECK(svc_audio_talk_begin(TALK_TO_PERSON, SVC_MESSAGES_THREAD_1) == false,
          "a hold waits for the play to finish");
    CHECK(!talk_ring_held(), "and takes no ring of its own while it waits (0.0.18)");
    svc_audio_fake_listen(true, false, false);
    svc_audio_service();
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(m.played && svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == badge - 1 &&
              svc_messages_unplayed_total() == clock_badge - 1,
          "its audio starts: heard, the tile's badge and the clock's drop at once");
    CHECK(svc_post_take(&r) && r.kind == POST_RQ_PLAYED &&
              !strcmp(r.path, "/endpoint/messages/" Q_DEV "/" M5 "/played"),
          "and its stamp is queued for the box, so the PWA shows it played too");
    post_req_t extra;
    CHECK(!svc_post_take(&extra), "exactly one stamp");
    svc_post_done(&r, -1, NULL, 0);
    CHECK(talk_ring_held(), "the ring once the audio arrives");
    CHECK(talk_ring_bytes() == TK_RING_PLAY, "a play's own 6 KB ring, not a talk's 16 KB (0.0.18)");
    CHECK(svc_audio_speaking() && svc_audio_playing() == m.id, "playing through the reply's speaker path");
    CHECK(svc_audio_played_ms() == 0, "nothing at the speaker yet: the bar is empty");
    svc_audio_fake_played(1200);
    CHECK(svc_audio_played_ms() == 1200, "the bar follows the samples the speaker took");
    svc_audio_stop();
    svc_audio_service();
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(!svc_audio_playing() && m.played && svc_audio_played_ms() == 0,
          "stopped part way: still heard, and no play's progress left over");
    CHECK(!talk_ring_held(), "the ring is given back once nothing plays");
    char before[200], after[200];
    /* The first stamp's flash write, two seconds on, before the replays are compared. */
    svc_rtc_fake_advance_ms(2100);
    svc_post_service();
    svc_post_note(before, sizeof before);
    const unsigned saves = post_port_fake_saves();
    CHECK(svc_audio_play(m.id), "and again");
    CHECK(svc_audio_played_ms() == 0, "a new play starts its bar from empty");
    svc_audio_fake_listen(true, true, false);
    svc_audio_service();
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(m.played && !svc_audio_playing(), "played to the end but its digest did not match: still heard");
    CHECK(svc_audio_play(m.id), "and again");
    svc_audio_fake_listen(true, true, true);
    svc_audio_service();
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(m.played && !svc_audio_playing(), "heard whole: played here");
    svc_rtc_fake_advance_ms(3000);
    svc_post_service();
    svc_post_note(after, sizeof after);
    CHECK(!strcmp(before, after) && post_port_fake_saves() == saves,
          "replaying a heard message stamps nothing: the list, its counts and flash untouched");
    CHECK(!svc_post_take(&r), "and queues nothing");
    svc_post_open(SVC_MESSAGES_THREAD_1);
    svc_post_service();
    post_answer(Q_NEWEST);
    svc_messages_get(SVC_MESSAGES_THREAD_1, 2, &m);
    CHECK(m.played, "a list from before the box heard keeps it played");
    CHECK(!svc_post_take(&r), "the failed stamp waits out its backoff rather than riding the list");
    svc_rtc_fake_advance_ms(5000);
    svc_post_service();
    CHECK(svc_post_take(&r) && r.kind == POST_RQ_PLAYED && strstr(r.path, M5),
          "after 5 s it is asked for again");
    svc_post_done(&r, 204, NULL, 0);
    svc_post_service();
    svc_rtc_fake_advance_ms(400000);
    svc_post_service();
    CHECK(!svc_post_take(&r), "confirmed, it is never sent again");
    CHECK(svc_audio_play(m.id) && (svc_audio_fake_listen(false, false, false), svc_audio_service(), true) &&
              !svc_audio_playing(),
          "a play the box refuses ends quietly");
    CHECK(!svc_audio_play(123) && strstr(svc_audio_talk_refused(), "not on the box"),
          "a bubble that is not the box's cannot be played");

    /* KEY in her thread: a voice message to her, through the box's send. */
    CHECK(svc_audio_talk_begin(TALK_TO_PERSON, SVC_MESSAGES_THREAD_1), "KEY in her thread records to her");
    CHECK(!strcmp(svc_audio_fake_last_path(), "/endpoint/messages/" Q_DEV "/send"), "to her panel's send");
    svc_rtc_fake_advance_ms(2200);
    CHECK(svc_audio_talk_end(true) == TALK_SENT, "released: sent");
    const uint16_t n = svc_messages_count(SVC_MESSAGES_THREAD_1);
    svc_messages_get(SVC_MESSAGES_THREAD_1, (uint16_t)(n - 1), &m);
    CHECK(m.mine && m.pending && !strcmp(m.text, TALK_POSTING) && m.dur_s == 2, "Sending…, two seconds");
    svc_post_open(SVC_MESSAGES_THREAD_1);
    svc_post_service();
    post_answer(Q_NEWEST);
    svc_messages_get(SVC_MESSAGES_THREAD_1, (uint16_t)(svc_messages_count(SVC_MESSAGES_THREAD_1) - 1), &m);
    CHECK(m.pending, "a list landing meanwhile keeps the send on its way");
    svc_audio_fake_posted(201, "Night night, sweetheart");
    svc_audio_service();
    svc_post_service();
    svc_messages_get(SVC_MESSAGES_THREAD_1, (uint16_t)(svc_messages_count(SVC_MESSAGES_THREAD_1) - 1), &m);
    CHECK(!m.pending && !strcmp(m.text, "Night night, sweetheart") && !strcmp(m.status, TALK_POSTED),
          "sent: her transcript of his words, and Sent");
    bool fetched = false;
    while (svc_post_take(&r)) fetched |= r.kind == POST_RQ_THREAD && strstr(r.path, Q_DEV) != NULL;
    CHECK(fetched, "and the thread is fetched for the box's copy");
    while (svc_post_take(&r)) {
    }
    CHECK(svc_audio_talk_begin(TALK_TO_PERSON, SVC_MESSAGES_THREAD_2), "to her sister");
    CHECK(strstr(svc_audio_fake_last_path(), T_DEV "/send") != NULL, "goes to her sister's panel");
    svc_rtc_fake_advance_ms(1500);
    svc_audio_talk_end(true);
    svc_audio_fake_posted(422, NULL);
    svc_audio_service();
    svc_messages_get(SVC_MESSAGES_THREAD_2, (uint16_t)(svc_messages_count(SVC_MESSAGES_THREAD_2) - 1), &m);
    CHECK(!strcmp(m.status, "Not sent") && strstr(m.text, "damaged"), "a damaged upload says so: Not sent");
    CHECK(svc_audio_talk_begin(TALK_TO_PERSON, SVC_MESSAGES_THREAD_2), "again");
    svc_rtc_fake_advance_ms(1500);
    svc_audio_talk_end(true);
    svc_audio_fake_posted(400, NULL);
    svc_audio_service();
    svc_messages_get(SVC_MESSAGES_THREAD_2, (uint16_t)(svc_messages_count(SVC_MESSAGES_THREAD_2) - 1), &m);
    CHECK(strstr(m.text, "Nothing heard"), "silence is not a message");

    /* A different panel bound to the thread: what it showed was somebody else's conversation. */
    static const char OTHER[] = "{\"siblings\":[{\"device_id\":\"0b8e5f7a-3333-4c2d-9e3f-5a6b7c8d9e03\","
                                "\"name\":\"Quorra\"},{\"device_id\":\"" T_DEV "\",\"name\":\"Tamsin\"}]}";
    svc_siblings_offer(OTHER, sizeof OTHER - 1);
    svc_siblings_service();
    svc_post_service();
    CHECK(!svc_post_loaded(SVC_MESSAGES_THREAD_1) && svc_post_take(&r) && strstr(r.path, "3333"),
          "a replaced panel starts its thread over, fetched from the box");
    char note[160];
    svc_post_note(note, sizeof note);
    CHECK(!strncmp(note, "post=rq:", 8) && strstr(note, "tag:t2"), "telemetry's post= token");
    while (svc_post_take(&r)) {
    }
    svc_messages_fake_clear();
    sib_port_fake_wipe();
}

/* Review of 0.0.16: a "played" the box did not take is retried on a backoff, kept across a reboot
 * and past two plays, and dropped only when the box says the message is gone. */
static void stamp_answer(int status, const char *expect_id, const char *what)
{
    post_req_t r;
    bool got = svc_post_take(&r);
    CHECK(got && r.kind == POST_RQ_PLAYED && strstr(r.path, expect_id), what);
    if (got) svc_post_done(&r, status, NULL, 0);
    svc_post_service();
}

static void hear(uint16_t at)
{
    msg_t m;
    svc_messages_get(SVC_MESSAGES_THREAD_1, at, &m);
    svc_post_heard(SVC_MESSAGES_THREAD_1, m.id);
}

/* One number from the debug snapshot's `post` section. */
static long snap_int(const char *key)
{
    static char buf[8192];
    dbg_json_t w;
    dbg_json_init(&w, buf, sizeof buf);
    dbg_snapshot(&w);
    char want[48];
    snprintf(want, sizeof want, "\"post\":{");
    const char *post = strstr(dbg_json_text(&w), want);
    snprintf(want, sizeof want, "\"%s\":", key);
    const char *at = post ? strstr(post, want) : NULL;
    return at ? atol(at + strlen(want)) : -1;
}

static void test_played_stamps_survive_failures_reboots_and_many_plays(void)
{
    static const char FOUR[] =
        "{\"messages\":[{\"id\":\"" M1 "\",\"mine\":false},{\"id\":\"" M2 "\",\"mine\":false},"
        "{\"id\":\"" M3 "\",\"mine\":false},{\"id\":\"" M4 "\",\"mine\":false}]}";
    sib_port_fake_wipe();
    post_port_fake_wipe();
    svc_messages_fake_clear();
    svc_siblings_init();
    svc_post_init();
    svc_siblings_offer(POST_SIBS, sizeof POST_SIBS - 1);
    svc_siblings_service();
    svc_post_service();
    post_answer(FOUR);
    post_req_t r;
    while (svc_post_take(&r)) {
    }
    CHECK(svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == 4, "four of hers, unplayed");

    /* The box is down: three plays, more than 0.0.16's first cut kept. */
    hear(0);
    stamp_answer(-1, M1, "the first stamp goes at once");
    hear(1);
    hear(2);
    CHECK(!svc_post_take(&r), "one stamp at a time: the others wait behind the failed head");
    CHECK(svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == 1, "all three are played here, the badge says 1");
    svc_rtc_fake_advance_ms(4900);
    svc_post_service();
    CHECK(!svc_post_take(&r), "not before 5 s");
    svc_rtc_fake_advance_ms(200);
    svc_post_service();
    stamp_answer(503, M1, "after 5 s, again");
    svc_rtc_fake_advance_ms(14000);
    svc_post_service();
    CHECK(!svc_post_take(&r), "then not before 15 s");
    svc_rtc_fake_advance_ms(1100);
    svc_post_service();
    stamp_answer(-1, M1, "after 15 s, again");
    CHECK(post_backoff_ms(3) == 60000 && post_backoff_ms(4) == 300000 && post_backoff_ms(40) == 300000,
          "then a minute, then every five");
    const unsigned saves = post_port_fake_saves();

    /* A reboot with three stamps waiting: the box's list still says unplayed. */
    svc_messages_fake_clear();
    svc_siblings_init();
    svc_post_init();
    svc_post_service();
    bool sent = false;
    while (svc_post_take(&r)) {
        if (r.kind == POST_RQ_PLAYED) {
            sent = strstr(r.path, M1) != NULL;
            svc_post_done(&r, 204, NULL, 0);
            continue;
        }
        char *b = malloc(sizeof FOUR);
        memcpy(b, FOUR, sizeof FOUR);
        svc_post_done(&r, 200, b, sizeof FOUR - 1);
    }
    svc_post_service();
    CHECK(saves >= 1 && svc_messages_unplayed(SVC_MESSAGES_THREAD_1) == 1,
          "after a reboot the three heard ones stay played, whatever the box's list says");
    CHECK(sent, "and the waiting stamp is sent at once");
    stamp_answer(404, M2, "the next one follows at once");
    stamp_answer(204, M3, "a message the box no longer has (404) is dropped, and the third goes");
    svc_rtc_fake_advance_ms(400000);
    svc_post_service();
    CHECK(!svc_post_take(&r), "nothing is retried forever");
    svc_rtc_fake_advance_ms(3000);
    svc_post_service();
    svc_post_init();
    svc_post_service();
    bool stamped = false;
    while (svc_post_take(&r)) stamped |= r.kind == POST_RQ_PLAYED;
    CHECK(!stamped, "and once confirmed, a reboot sends none of them again");

    /* A full list and a message heard again: nothing is pushed out, nothing counted dropped. */
    sib_port_fake_wipe();
    post_port_fake_wipe();
    svc_messages_fake_clear();
    svc_siblings_init();
    svc_post_init();
    svc_siblings_offer(POST_SIBS, sizeof POST_SIBS - 1);
    svc_siblings_service();
    svc_post_service();
    post_answer(FOUR);
    while (svc_post_take(&r)) {
    }
    msg_t hm;
    for (uint16_t k = 0; k < 4; k++) {
        svc_messages_get(SVC_MESSAGES_THREAD_1, k, &hm);
        svc_post_heard(SVC_MESSAGES_THREAD_1, hm.id);
        svc_messages_get(SVC_MESSAGES_THREAD_2, k, &hm);
        svc_post_heard(SVC_MESSAGES_THREAD_2, hm.id);
    }
    CHECK(snap_int("stamps_waiting") == POST_STAMPS && snap_int("stamps_dropped") == 0,
          "eight heard with the box away: the list is full, none dropped");
    svc_messages_get(SVC_MESSAGES_THREAD_1, 0, &hm);
    svc_post_heard(SVC_MESSAGES_THREAD_1, hm.id);
    CHECK(snap_int("stamps_waiting") == POST_STAMPS && snap_int("stamps_dropped") == 0,
          "the same one again is a no-op: every waiting stamp kept, stamps_dropped unchanged");
    while (svc_post_take(&r)) {
    }

    /* More than the list holds: the oldest gives way, the newest eight are kept. */
    post_stamps_t l = {0};
    char id[POST_ID_BYTES];
    for (int i = 0; i < POST_STAMPS + 2; i++) {
        snprintf(id, sizeof id, "6b000000-0000-4000-8000-0000000000%02d", i);
        post_stamps_add(&l, Q_DEV, id);
    }
    CHECK(l.n == POST_STAMPS && strstr(l.s[0].id, "02") && !post_stamps_add(&l, Q_DEV, l.s[3].id),
          "eight kept, deduplicated");
    l.s[1].id[0] = 'X';
    l.n = 9;
    post_stamps_clean(&l);
    CHECK(l.n == POST_STAMPS - 1, "a list from flash that is not box ids is cleaned");
    CHECK(post_stamp_done(204) && post_stamp_done(404) && !post_stamp_done(-1) && !post_stamp_done(503) &&
              !post_stamp_done(403),
          "done is 204 or 404; no answer, a 5xx or a 403 is retried");
    while (svc_post_take(&r)) {
    }
    svc_messages_fake_clear();
    sib_port_fake_wipe();
    post_port_fake_wipe();
}

/* ---- 0.0.18: auto-rotate ---- */

static void test_a_turn_and_its_touch_inverse_agree_everywhere(void)
{
    bool round = true, forward = true, corners = true;
    for (uint8_t rot = 0; rot < 4; rot++)
        for (int32_t y = 0; y < 480; y += 7)
            for (int32_t x = 0; x < 480; x += 5) {
                int32_t px, py;
                disp_rot_point(rot, 480, x, y, &px, &py);
                int16_t lx, ly, gx, gy;
                tp_unrotate(rot, (int16_t)px, (int16_t)py, &lx, &ly);
                tp_rotate(rot, (int16_t)x, (int16_t)y, &gx, &gy);
                round &= lx == x && ly == y;
                forward &= gx == px && gy == py;
                corners &= px >= 0 && px < 480 && py >= 0 && py < 480;
            }
    CHECK(round, "a tap on the glass of a turned panel lands where the turned screen drew it");
    CHECK(forward, "touch's own forward turn is the display's");
    CHECK(corners, "and nothing lands off the glass");
    int32_t px, py;
    disp_rot_point(1, 480, 0, 0, &px, &py);
    CHECK(px == 0 && py == 479,
          "turned right (its right edge down), the viewer's top-left is the glass's bottom-left");
    disp_rot_point(3, 480, 0, 0, &px, &py);
    CHECK(px == 479 && py == 0, "turned left, it is the glass's top-right");
    disp_rot_point(2, 480, 0, 0, &px, &py);
    CHECK(px == 479 && py == 479, "upside down, the bottom-right");
}

static uint16_t logical_px(int32_t x, int32_t y)
{
    return (uint16_t)(x * 131 + y * 7 + (x ^ y));
}

/* Every strip LVGL could hand the flush, turned the device's way onto a glass, which must then
   hold the upright screen turned. */
static void test_turned_strips_build_the_turned_screen(void)
{
    static uint16_t glass[480 * 480], strip[480 * 40], piece[DISP_ROT_PIECE_PX];
    const disp_win_t areas[] = {{0, 0, 479, 39}, {100, 202, 323, 241}, {0, 440, 479, 479},
                                {6, 6, 7, 7}, {470, 0, 479, 479}};
    for (uint8_t rot = 0; rot < 4; rot++) {
        bool even = true, inside = true, whole = true;
        memset(glass, 0, sizeof glass);
        /* The whole screen in 40-row strips, then the odd areas over it. */
        for (int k = 0; k < 12 + 5; k++) {
            disp_win_t a = k < 12 ? (disp_win_t){0, k * 40, 479, k * 40 + 39} : areas[k - 12];
            if (k >= 12 && (a.y2 - a.y1 + 1) * (a.x2 - a.x1 + 1) > 480 * 40) {
                /* Taller than a strip: LVGL would cut it in 40-row bands. */
                a.y2 = a.y1 + 480 * 40 / (a.x2 - a.x1 + 1) - 1;
                a.y2 = a.y1 + ((a.y2 - a.y1 + 1) & ~1) - 1;
            }
            disp_area_even(&a.x1, &a.y1, &a.x2, &a.y2, 480, 480);
            const int32_t w = a.x2 - a.x1 + 1, h = a.y2 - a.y1 + 1;
            for (int32_t j = 0; j < h; j++)
                for (int32_t i = 0; i < w; i++) strip[j * w + i] = logical_px(a.x1 + i, a.y1 + j);
            disp_win_t wins[32];
            const uint16_t *bufs[32];
            int n = 0;
            if (rot == 0 || rot == 2) {
                if (rot == 2) disp_reverse(strip, (uint32_t)(w * h));
                wins[n] = disp_rot_area(rot, 480, a);
                bufs[n++] = strip;
            }
            const int32_t k = disp_rot_piece_rows(h);
            even &= !(k & 1) && k * h <= DISP_ROT_PIECE_PX;
            for (int32_t r0 = 0; (rot & 1) && r0 < w; r0 += k) {
                const disp_win_t g = disp_rot_piece(rot, 480, a, strip, r0, k, piece);
                const int32_t gw = g.x2 - g.x1 + 1;
                for (int32_t y = g.y1; y <= g.y2; y++)
                    memcpy(&glass[y * 480 + g.x1], &piece[(y - g.y1) * gw], (size_t)gw * 2);
                even &= !(g.x1 & 1) && !(g.y1 & 1) && (g.x2 & 1) && (g.y2 & 1);
                inside &= g.x1 >= 0 && g.y1 >= 0 && g.x2 < 480 && g.y2 < 480;
            }
            for (int m = 0; m < n; m++) {
                const disp_win_t g = wins[m];
                const int32_t gw = g.x2 - g.x1 + 1;
                whole &= gw * (g.y2 - g.y1 + 1) == w * h;
                for (int32_t y = g.y1; y <= g.y2; y++)
                    memcpy(&glass[y * 480 + g.x1], &bufs[m][(y - g.y1) * gw], (size_t)gw * 2);
                even &= !(g.x1 & 1) && !(g.y1 & 1) && (g.x2 & 1) && (g.y2 & 1);
                inside &= g.x1 >= 0 && g.y1 >= 0 && g.x2 < 480 && g.y2 < 480;
            }
        }
        bool same = true;
        for (int32_t y = 0; y < 480; y++)
            for (int32_t x = 0; x < 480; x++) {
                int32_t px, py;
                disp_rot_point(rot, 480, x, y, &px, &py);
                same &= glass[py * 480 + px] == logical_px(x, y);
            }
        char m[96];
        snprintf(m, sizeof m, "turned %d°: every strip lands on the glass as the turned screen", rot * 90);
        CHECK(same && whole, m);
        snprintf(m, sizeof m, "turned %d°: every window even-aligned and on the glass", rot * 90);
        CHECK(even && inside, m);
    }
}

/* Raw counts for gravity's "down" pointing `deg` clockwise from the glass's bottom, `g_mg` of it
   in the glass's plane and the rest through it, with the default axes (down = -reading). */
static void tilt(int deg, int g_mg, int16_t a[3])
{
    const double r = deg * 3.14159265358979 / 180.0, g = ORIENT_1G * g_mg / 1000.0;
    const double dx = sin(r) * g, dy = cos(r) * g;
    const double rest = (double)ORIENT_1G * ORIENT_1G - g * g;
    a[0] = (int16_t)-dx;
    a[1] = (int16_t)-dy;
    a[2] = (int16_t)(rest > 0 ? sqrt(rest) : 0);
}

static void test_down_needs_a_clear_margin_and_a_dwell(void)
{
    orient_t o;
    orient_init(&o);
    int16_t a[3];
    tilt(0, 950, a);
    CHECK(!orient_feed(&o, 0, a, 0) && o.rot == 0 && o.seen == ORIENT_SEEN_EDGE && o.edge == 0,
          "upright on its stand: its own bottom is down, nothing to do");
    tilt(90, 950, a);
    CHECK(!orient_feed(&o, 0, a, 1000) && o.has_cand && o.cand == 1, "turned right: a candidate");
    CHECK(!orient_feed(&o, 0, a, 1000 + ORIENT_DWELL_MS - 1), "held, but not yet for its dwell");
    CHECK(orient_feed(&o, 0, a, 1000 + ORIENT_DWELL_MS) && o.rot == 1 && o.changes == 1,
          "held for 700 ms: the screen turns, its right edge now the bottom");
    tilt(45, 950, a);
    CHECK(!orient_feed(&o, 0, a, 3000) && o.seen == ORIENT_SEEN_BETWEEN && o.rot == 1,
          "leaned at 45°: between two edges, it holds");
    tilt(64, 950, a);
    CHECK(!orient_feed(&o, 0, a, 3100) && o.seen == ORIENT_SEEN_BETWEEN && o.rot == 1,
          "26° off the right edge's axis is not clearly any edge: it holds");
    tilt(29, 950, a);
    for (uint32_t t = 4000; t < 6000; t += 125) orient_feed(&o, 0, a, t);
    CHECK(o.rot == 1 && o.seen == ORIENT_SEEN_BETWEEN,
          "29° from upright is 16° past the line: not past the 25° margin, so it never turns back");
    tilt(19, 950, a);
    for (uint32_t t = 6000; t < 6000 + ORIENT_DWELL_MS + 125; t += 125) orient_feed(&o, 0, a, t);
    CHECK(o.rot == 0 && o.changes == 2, "19° from upright is within 20° of its axis: back upright");

    tilt(180, 950, a);
    orient_feed(&o, 0, a, 10000);
    orient_feed(&o, 0, a, 10300);
    tilt(180, 300, a);
    CHECK(!orient_feed(&o, 0, a, 10500) && o.seen == ORIENT_SEEN_FLAT && !o.has_cand,
          "laid nearly flat on the desk: no down on the glass, and the dwell starts over");
    tilt(180, 950, a);
    orient_feed(&o, 0, a, 10600);
    CHECK(!orient_feed(&o, 0, a, 11000) && o.rot == 0, "the dwell counts from where it started again");
    a[1] = (int16_t)(a[1] * 3 / 2);
    CHECK(!orient_feed(&o, 0, a, 11400) && o.seen == ORIENT_SEEN_MOVING && !o.has_cand,
          "a hand swinging it (1.5 g) is movement, not gravity");
    tilt(180, 950, a);
    orient_feed(&o, 0, a, 11500);
    CHECK(orient_feed(&o, 0, a, 11500 + ORIENT_DWELL_MS) && o.rot == 2, "then upside down, after its dwell");
    tilt(270, 950, a);
    orient_feed(&o, 0, a, 20000);
    orient_forget(&o);
    CHECK(!orient_feed(&o, 0, a, 20000 + ORIENT_DWELL_MS) && o.rot == 2,
          "a forgotten candidate (the screen went dark) starts its dwell again");
    CHECK(orient_feed(&o, 0, a, 20000 + 2 * ORIENT_DWELL_MS) && o.rot == 3, "turned left, in time");

    /* The chip's axes against the glass: the setting that the board settles. */
    int32_t dx, dy;
    const int16_t up_y[3] = {0, -16000, 0};
    orient_down(0, up_y, &dx, &dy);
    CHECK(dx == 0 && dy == 16000, "axes 0: the chip's -Y reading is the glass's bottom");
    orient_down(1, up_y, &dx, &dy);
    CHECK(dx == 16000 && dy == 0, "axes 1 swaps X and Y");
    orient_down(1 | 2, up_y, &dx, &dy);
    CHECK(dx == -16000 && dy == 0, "and 2 flips the glass's x");
    orient_down(4, up_y, &dx, &dy);
    CHECK(dx == 0 && dy == -16000, "and 4 its y");
    CHECK(!strcmp(orient_edge_name(1), "right") && !strcmp(orient_seen_name(ORIENT_SEEN_FLAT), "flat"),
          "the snapshot's words");
}

static uint8_t s_turned_to = 0xFF;
static void turned(uint8_t q)
{
    s_turned_to = q;
}

static void test_the_service_reads_only_while_lit_and_on(void)
{
    svc_settings_set(SET_AUTO_ROTATE, 1);
    svc_settings_set(SET_IMU_AXES, 0);
    svc_display_set_dark(false);
    orient_port_fake_fail(NULL);
    CHECK(svc_orient_start() && svc_orient_available(), "the sensor answers at start");
    svc_display_on_rotated(turned);
    int16_t a[3];
    tilt(0, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    uint32_t t = 100000;
    svc_orient_service(t);
    const unsigned r0 = orient_port_fake_reads();
    for (int i = 0; i < 40; i++) svc_orient_service(t += 25); /* a second of UI passes */
    const unsigned per_s = orient_port_fake_reads() - r0;
    CHECK(orient_port_fake_powered() && per_s >= 7 && per_s <= 9, "lit and on: read at 8 Hz, not every pass");
    tilt(270, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 40; i++) svc_orient_service(t += 25);
    CHECK(svc_orient_rotation() == 3 && svc_display_rotation() == 3 && s_turned_to == 3,
          "turned left: the glass turns, and touch is told");
    svc_display_set_dark(true);
    svc_orient_service(t += 25);
    const unsigned dark0 = orient_port_fake_reads();
    tilt(0, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
    CHECK(!orient_port_fake_powered() && orient_port_fake_reads() == dark0 && svc_orient_rotation() == 3,
          "dark: the accelerometer powered down, nothing read, the turn it had kept");
    svc_display_set_dark(false);
    svc_orient_hold(true);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
    CHECK(svc_orient_rotation() == 3, "held for calibration: it does not turn under the crosses");
    svc_orient_hold(false);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
    CHECK(svc_orient_rotation() == 0 && s_turned_to == 0, "let go: upright again after its dwell");
    tilt(90, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
    CHECK(svc_orient_rotation() == 1, "turned right");
    svc_settings_set(SET_AUTO_ROTATE, 0);
    svc_orient_service(t += 25);
    CHECK(svc_orient_rotation() == 0 && svc_display_rotation() == 0 && !orient_port_fake_powered(),
          "auto-rotate off: straight back upright, the sensor off");
    svc_settings_set(SET_AUTO_ROTATE, 1);
    svc_settings_set(SET_IMU_AXES, 99);
    CHECK(svc_settings_get(SET_IMU_AXES) == IMU_AXES_MAX, "the axes setting clamps to its three bits");
    svc_settings_set(SET_IMU_AXES, 0);
    svc_orient_service(t += 25);
    orient_port_fake_fail("id");
    CHECK(!svc_orient_start() && !svc_orient_available(), "a sensor that does not answer: unavailable");
    char note[96];
    svc_orient_note(note, sizeof note);
    CHECK(strstr(note, "orient=failed:id") == note, "and telemetry names the step");
    for (int i = 0; i < 40; i++) svc_orient_service(t += 25);
    CHECK(svc_orient_rotation() == 0, "the panel runs upright");
    CHECK(strstr(settings_rotate_note(true, false), "tilt sensor") && !strcmp(settings_rotate_note(false, true), "Locked upright") &&
              !strcmp(settings_rotate_note(true, true), "Follows which way is down"),
          "Settings says what the switch does, or why it is dead");
    orient_port_fake_fail(NULL);
    svc_orient_start();
    svc_display_on_rotated(NULL);
}

/* A quarter turn the display cannot take (no 2.5 KB for its piece buffer, the play's low-heap
   case): the glass stays, and touch and calibration stay with the glass, not with the request. */
static void test_touch_and_calibration_follow_the_glass_not_the_request(void)
{
    svc_settings_set(SET_AUTO_ROTATE, 1);
    svc_settings_set(SET_IMU_AXES, 0);
    svc_display_set_dark(false);
    orient_port_fake_fail(NULL);
    svc_orient_start();
    svc_orient_hold(false);
    s_turned_to = 0xFF;
    svc_display_on_rotated(turned);
    int16_t a[3];
    uint32_t t = 2000000;
    tilt(0, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
    CHECK(svc_display_rotation() == 0, "upright to begin with");
    s_turned_to = 0xFF;
    svc_display_fake_no_piece(true);
    tilt(90, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 80; i++) {
        svc_orient_service(t += 25);
        svc_display_fake_service();
    }
    CHECK(svc_display_rotation() == 0 && svc_orient_rotation() == 0,
          "turned right but no memory for the piece buffer: the glass stays upright, and says so");
    CHECK(s_turned_to == 0xFF, "touch is not told of a turn the glass did not take");
    /* A calibration made now is turned back into the glass's frame with what is showing. Taps
       landing 10 px right of every cross on the upright screen are a glass x error of +10; read
       through the requested quarter turn it would have been a y error. */
    tp_cal_port_fake_wipe();
    svc_calib_reset();
    svc_calib_begin();
    calib_pair_t pairs[CAL_TARGETS];
    for (uint8_t i = 0; i < CAL_TARGETS; i++) {
        int16_t x, y;
        calib_target(i, &x, &y);
        pairs[i] = (calib_pair_t){x, y, (int16_t)(x + 10), y};
    }
    CHECK(svc_calib_finish(pairs, CAL_TARGETS) == CALIB_SAVED, "the calibration fits");
    tp_cal_t c;
    bool on;
    tp_cal_port_fake_applied(&c, &on);
    CHECK(on && c.c < -9 * TP_Q16 && c.c > -11 * TP_Q16 && c.f > -TP_Q16 && c.f < TP_Q16,
          "and corrects the glass's x, the frame that is showing, not the requested turn's");
    svc_display_fake_no_piece(false);
    for (int i = 0; i < 4; i++) {
        svc_orient_service(t += 25);
        svc_display_fake_service();
    }
    CHECK(svc_display_rotation() == 1 && svc_orient_rotation() == 1 && s_turned_to == 1,
          "the memory back: the glass, orient and touch all turn together");
    svc_calib_reset();
    tilt(0, 950, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
    svc_display_on_rotated(NULL);
}

/* The raw reading a chip mounted as `axes` gives for gravity's "down" (dx, dy) on the glass: the
   inverse of orient_down. */
static void raw_for(uint8_t axes, int32_t dx, int32_t dy, int16_t a[3])
{
    const int32_t u = axes & 2 ? dx : -dx, v = axes & 4 ? dy : -dy;
    a[0] = (int16_t)(axes & 1 ? v : u);
    a[1] = (int16_t)(axes & 1 ? u : v);
    a[2] = 5600;
}

static int orient_snap_int(const char *key)
{
    static char out[8192];
    dbg_json_t w;
    dbg_json_init(&w, out, sizeof out);
    dbg_cmd_run("{\"id\":\"ax\",\"cmd\":\"snapshot\"}", &w);
    char k[48];
    snprintf(k, sizeof k, "\"%s\":", key);
    const char *at = strstr(dbg_json_text(&w), k);
    return at ? atoi(at + strlen(k)) : -99;
}

/* Two holds settle the whole `imu_axes` value from the box, whatever way the chip is mounted:
   upright gives the swap and the y flip, then on its right edge the x flip that tells 90° from
   270°. */
static void test_two_holds_settle_the_sensor_s_axes(void)
{
    svc_settings_set(SET_AUTO_ROTATE, 1);
    svc_display_set_dark(false);
    orient_port_fake_fail(NULL);
    svc_orient_start();
    svc_orient_hold(false);
    uint32_t t = 4000000;
    bool all = true;
    for (uint8_t truth = 0; truth <= IMU_AXES_MAX; truth++) {
        svc_settings_set(SET_IMU_AXES, 0);
        int16_t a[3];
        raw_for(truth, 0, 15400, a); /* standing upright */
        orient_port_fake_set(a[0], a[1], a[2]);
        svc_orient_service(t += 200);
        const int up = orient_snap_int("axes_if_upright");
        svc_settings_set(SET_IMU_AXES, up < 0 ? 0 : up);
        raw_for(truth, 15400, 0, a); /* on its right edge */
        orient_port_fake_set(a[0], a[1], a[2]);
        svc_orient_service(t += 200);
        const int right = orient_snap_int("axes_if_right_edge");
        all &= up >= 0 && right == truth;
    }
    CHECK(all, "for every way the chip can sit, upright then right edge names its imu_axes");
    svc_settings_set(SET_IMU_AXES, 0);
    int16_t a[3];
    raw_for(0, 0, 15400, a);
    orient_port_fake_set(a[0], a[1], a[2]);
    for (int i = 0; i < 80; i++) svc_orient_service(t += 25);
}

/* ---- 0.0.18: touch calibration ---- */

static void test_the_fit_finds_a_panel_s_error_and_refuses_nonsense(void)
{
    const tp_pt_t want[4] = {{44, 44}, {436, 44}, {436, 436}, {44, 436}};
    tp_pt_t got[4];
    tp_cal_t c;
    CHECK(tp_cal_fit(want, want, 4, &c) == TP_FIT_OK && c.a == TP_Q16 && c.e == TP_Q16 && c.b == 0 &&
              c.c == 0 && c.f == 0,
          "taps exactly on the crosses: no correction");
    for (int i = 0; i < 4; i++) got[i] = (tp_pt_t){(int16_t)(want[i].x - 12), (int16_t)(want[i].y + 9)};
    CHECK(tp_cal_fit(want, got, 4, &c) == TP_FIT_OK, "a panel reading 12 px left and 9 px low");
    int16_t x, y;
    tp_cal_apply(&c, 228, 249, &x, &y);
    CHECK(x == 240 && y == 240, "is corrected everywhere, the middle included");
    /* 6% short of the edges, about the middle: what "corners that stop short" looks like. */
    for (int i = 0; i < 4; i++)
        got[i] = (tp_pt_t){(int16_t)(240 + (want[i].x - 240) * 94 / 100), (int16_t)(240 + (want[i].y - 240) * 94 / 100)};
    CHECK(tp_cal_fit(want, got, 4, &c) == TP_FIT_OK, "a panel whose taps stop short of the edges");
    tp_cal_apply(&c, (int16_t)(240 + 239 * 94 / 100), 240, &x, &y);
    CHECK(x >= 477 && y == 240, "reaches the edge again once scaled");
    tp_cal_apply(&c, 0, 0, &x, &y);
    CHECK(x == 0 && y == 0, "and is clamped to the glass");

    const tp_pt_t same[4] = {{200, 200}, {200, 200}, {200, 200}, {200, 200}};
    CHECK(tp_cal_fit(want, same, 4, &c) == TP_FIT_DEGENERATE, "four taps in one place fit nothing");
    const tp_pt_t line[4] = {{44, 44}, {200, 200}, {300, 300}, {436, 436}};
    CHECK(tp_cal_fit(want, line, 4, &c) == TP_FIT_DEGENERATE, "nor four in a line");
    CHECK(tp_cal_fit(want, want, 2, &c) == TP_FIT_DEGENERATE, "nor two");
    for (int i = 0; i < 4; i++)
        got[i] = (tp_pt_t){(int16_t)(240 + (want[i].x - 240) * 70 / 100), (int16_t)(240 + (want[i].y - 240) * 70 / 100)};
    CHECK(tp_cal_fit(want, got, 4, &c) == TP_FIT_WILD, "a 30% scale is a bad run, not a panel");
    for (int i = 0; i < 4; i++) got[i] = (tp_pt_t){(int16_t)(want[i].x + 60), want[i].y};
    CHECK(tp_cal_fit(want, got, 4, &c) == TP_FIT_WILD, "so is a 60 px shift");
    memcpy(got, want, sizeof got);
    got[2].x = (int16_t)(got[2].x - 60);
    CHECK(tp_cal_fit(want, got, 4, &c) != TP_FIT_OK, "one corner 60 px out is refused");
    /* A twist no straight correction has: opposite corners pulled opposite ways. */
    for (int i = 0; i < 4; i++) got[i] = (tp_pt_t){(int16_t)(want[i].x + (i % 2 ? -30 : 30)), want[i].y};
    CHECK(tp_cal_fit(want, got, 4, &c) == TP_FIT_SCATTERED, "taps that disagree with every straight fit");
    tp_cal_identity(&c);
    tp_cal_apply(&c, 17, 463, &x, &y);
    CHECK(x == 17 && y == 463, "identity changes nothing");
}

/* The raw report that the 0.0.7 mapping puts at glass (x, y): x = 479 - rawB, y = rawA. */
static void raw_at(int16_t x, int16_t y, uint16_t *a, uint16_t *b)
{
    *a = (uint16_t)y;
    *b = (uint16_t)(479 - x);
}

static void test_calibration_lives_in_the_glass_frame_and_rotation_follows(void)
{
    /* A panel that reads every tap 10 px right and 6 px high of the finger, in the glass's frame. */
    const int16_t ex = 10, ey = -6;
    for (uint8_t rot = 0; rot < 4; rot++) {
        tp_cal_port_fake_wipe();
        svc_calib_reset();
        svc_calib_begin();
        tp_cal_t applied;
        bool on;
        tp_cal_port_fake_applied(&applied, &on);
        CHECK(!on && svc_calib_capturing(), "calibrating: taps arrive with no correction");
        calib_pair_t pairs[CAL_TARGETS];
        for (uint8_t i = 0; i < CAL_TARGETS; i++) {
            int16_t tx, ty, gx, gy, lx, ly;
            calib_target(i, &tx, &ty);
            /* The cross, where the turned screen drew it; the finger on it; where the panel read
               it; and the upright screen's coordinates the app saw. */
            tp_rotate(rot, tx, ty, &gx, &gy);
            uint16_t a, b;
            raw_at((int16_t)(gx + ex), (int16_t)(gy + ey), &a, &b);
            tp_xf_t raw = {.rot = rot};
            tp_cal_identity(&raw.cal);
            tp_xform(&raw, a, b, &lx, &ly);
            pairs[i] = (calib_pair_t){tx, ty, lx, ly};
        }
        /* svc_calib reads the rotation from svc_orient; the fake sensor is put where `rot` is. */
        int16_t g[3];
        tilt(rot * 90, 950, g);
        orient_port_fake_set(g[0], g[1], g[2]);
        svc_orient_hold(false);
        for (uint32_t t = 0; t < 3000 && svc_orient_rotation() != rot; t += 25) svc_orient_service(500000 + rot * 10000 + t);
        CHECK(svc_orient_rotation() == rot, "the panel turned as the test says");
        CHECK(svc_calib_finish(pairs, CAL_TARGETS) == CALIB_SAVED && svc_calib_custom() && !svc_calib_capturing(),
              "the four corners fit and are saved");
        tp_cal_port_fake_applied(&applied, &on);
        bool lands = true;
        for (int16_t y = 20; y < 460; y += 31)
            for (int16_t x = 20; x < 460; x += 29) {
                int16_t gx, gy, lx, ly;
                tp_rotate(rot, x, y, &gx, &gy);
                uint16_t a, b;
                raw_at((int16_t)(gx + ex), (int16_t)(gy + ey), &a, &b);
                const tp_xf_t xf = {.cal = applied, .cal_on = on, .rot = rot};
                tp_xform(&xf, a, b, &lx, &ly);
                lands &= abs(lx - x) <= 1 && abs(ly - y) <= 1;
            }
        char m[96];
        snprintf(m, sizeof m, "turned %d°: every tap lands under the finger once calibrated", rot * 90);
        CHECK(on && lands, m);
    }
    /* The calibration made upright holds after the panel turns: it is the glass's, not the view's. */
    tp_cal_t applied;
    bool on;
    tp_cal_port_fake_applied(&applied, &on);
    int16_t lx, ly;
    uint16_t a, b;
    raw_at(240 + ex, 120 + ey, &a, &b);
    const tp_xf_t xf = {.cal = applied, .cal_on = on, .rot = 1};
    tp_xform(&xf, a, b, &lx, &ly);
    CHECK(lx == 479 - 120 && ly == 240, "one calibration serves every rotation");
    tp_track_t t = {.xf = &xf};
    const tp_report_t down = {TP_DOWN, a, b};
    CHECK(tp_track(&t, &down) == TP_EV_DOWN && t.x == lx && t.y == ly, "and the touch task's stroke uses it");

    svc_calib_begin();
    svc_calib_cancel();
    tp_cal_port_fake_applied(&applied, &on);
    CHECK(on && svc_calib_custom() && !svc_calib_capturing(), "BOOT mid-run: the old calibration back in force");
    svc_calib_begin();
    const calib_pair_t bad[4] = {{44, 44, 44, 44}, {436, 44, 436, 44}, {436, 436, 300, 436}, {44, 436, 44, 436}};
    const calib_result_t r = svc_calib_finish(bad, 4);
    CHECK(r == CALIB_SCATTERED || r == CALIB_WILD, "a run whose taps disagree is refused");
    tp_cal_port_fake_applied(&applied, &on);
    CHECK(on && svc_calib_custom(), "and the calibration before it stays");
    const calib_pair_t good[4] = {{44, 44, 44, 44}, {436, 44, 436, 44}, {436, 436, 436, 436}, {44, 436, 44, 436}};
    tp_cal_port_fake_refuse_save(true);
    svc_calib_begin();
    CHECK(svc_calib_finish(good, 4) == CALIB_NOT_SAVED, "flash refusing it is said, and it is in force anyway");
    tp_cal_port_fake_refuse_save(false);
    svc_calib_reset();
    tp_cal_port_fake_applied(&applied, &on);
    CHECK(!on && !svc_calib_custom() && svc_calib_date() == 0, "Reset calibration: the measured mapping alone");
    for (uint32_t t2 = 0; t2 < 3000 && svc_orient_rotation() != 0; t2 += 25) {
        int16_t g[3];
        tilt(0, 950, g);
        orient_port_fake_set(g[0], g[1], g[2]);
        svc_orient_service(900000 + t2);
    }
}

static void test_the_calibration_screen_counts_taps_that_agree(void)
{
    calib_flow_t f;
    calib_flow_start(&f);
    int16_t x, y;
    calib_target(0, &x, &y);
    CHECK(x == 44 && y == 44, "the first cross is the mock's top-left knot");
    calib_target(2, &x, &y);
    CHECK(x == 436 && y == 436, "the third the bottom-right");
    CHECK(calib_flow_tap(&f, 200, 200) == CALF_MISSED && f.taps == 0, "a tap nowhere near is not counted");
    CHECK(!strcmp(calib_flow_title(CALF_MISSED), "Tap the centre of the cross"), "and says so");
    CHECK(calib_flow_tap(&f, 50, 40) == CALF_COUNTED && calib_flow_tap(&f, 48, 42) == CALF_COUNTED,
          "two taps on the cross");
    CHECK(calib_flow_tap(&f, 90, 44) == CALF_DISAGREED && f.taps == 0 && f.target == 0,
          "a third 40 px from the others: that cross again");
    CHECK(!strcmp(calib_flow_title(CALF_DISAGREED), "Those taps disagreed: again"), "and says so");
    calib_flow_tap(&f, 50, 40);
    calib_flow_tap(&f, 48, 42);
    CHECK(calib_flow_tap(&f, 52, 41) == CALF_NEXT && f.target == 1, "three that agree: the next cross");
    CHECK(f.done[0].got_x == 50 && f.done[0].got_y == 41 && f.done[0].want_x == 44,
          "kept as the cross and the taps' mean");
    for (uint8_t i = 1; i < CAL_TARGETS; i++) {
        calib_target(i, &x, &y);
        for (int k = 0; k < CAL_TAPS; k++) calib_flow_tap(&f, x, y);
    }
    CHECK(f.target == CAL_TARGETS && calib_flow_tap(&f, 1, 1) == CALF_DONE, "four crosses: done");
    char line[48];
    calib_flow_line(1, line, sizeof line);
    CHECK(!strcmp(line, "2 of 4 · BOOT cancels"), "the line under the prompt, as the mock words it");
    settings_calib_when(20261013, 20261013, line, sizeof line);
    CHECK(!strcmp(line, "Today"), "calibrated today");
    settings_calib_when(20261009, 20261013, line, sizeof line);
    CHECK(!strcmp(line, "9 Oct"), "or on the day it was");
    settings_calib_when(0, 20261013, line, sizeof line);
    CHECK(!strcmp(line, "Never"), "or never");
    CHECK(!strcmp(settings_calib_result(CALIB_SAVED), "Calibration saved") &&
              strstr(settings_calib_result(CALIB_WILD), "Try again"),
          "the toasts");
}

/* ---- push notices: JN1 against the box's fixtures, the files, the queue (0.0.19) ---- */

#define JN1_FIXTURES "../../backend/tests/fixtures/jpanel_notice/"

static uint8_t *read_fixture(const char *name, size_t *len)
{
    char path[160];
    snprintf(path, sizeof path, JN1_FIXTURES "%s", name);
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *b = malloc((size_t)n + 1);
    if (b && fread(b, 1, (size_t)n, f) != (size_t)n) {
        free(b);
        b = NULL;
    }
    fclose(f);
    if (b) {
        b[n] = '\0';
        *len = (size_t)n;
    }
    return b;
}

/* What `decode()` made of each fixture, from vectors.json — which the test reads too, so a
   regenerated fixture that changes any of these fails here rather than passing on stale values. */
typedef struct {
    const char *name, *sha256, *sender, *title, *body;
    uint8_t n_choices;
    const char *choices[4];
    bool chime;
    uint16_t hide_s;
    uint32_t ttl_s;
    uint8_t mode;
    uint16_t w, h, stripes;
    const char *px_sha;
    uint16_t audio_ms;
    const char *pcm_sha;
} jn1_vector_t;

static const jn1_vector_t JN1_VECTORS[] = {
    {"plain", "64b29a78d9543190d158e1c90af261896a293ab988a62275bc3f8da8957e8428", "Ops",
     "Disk 91% full", "The box is running low on space.", 0, {0}, false, 0, 900, 0, 0, 0, 0, NULL,
     0, NULL},
    {"question", "004c3adb68345fb74bf5b23ff2fea28935ae706e7a3cba152abd5c59d0243b1f", "Jerv",
     "Run the cleanup now?", "Frees about 38 GB.", 2, {"Yes", "No"}, true, 30, 900, 0, 0, 0, 0,
     NULL, 0, NULL},
    {"banner", "7fb86e0fcffa321e0439455e5bbe321d91cfe9444f5723575a2b119e0713bd1f",
     "Nightly backup", "Backup done", "", 3, {"OK", "Show me", "Later"}, false, 0, 600, 2, 480, 48,
     2, "ed1c30aa74aa70f8b24ee44f92683a86b508c1aa4455c607e15268dec7110b82", 0, NULL},
    {"full_sound", "12da6ee7209055c248c597244eda1e0e1c9d2f131c76bd1739bafe09134a6e86", "Jerv",
     "Look", "", 0, {0}, true, 8, 120, 1, 480, 480, 12,
     "72b20d214869463c261a409996d69ab31723b2510aa60b3fe774fd37243f065f", 250,
     "f02407a93f68fef81ae3a880a34b1309b8bdf13482cee91467a046a419ab3ae0"},
};
#define JN1_ID "6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

static void sha_of(const void *b, size_t n, char out[65])
{
    sha256_t s;
    sha256_init(&s);
    sha256_update(&s, b, n);
    sha256_hex(&s, out);
}

/* Little-endian rgb565, the bytes the box hashed. */
static void sha_px(sha256_t *s, const uint16_t *px, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        const uint8_t le[2] = {(uint8_t)px[i], (uint8_t)(px[i] >> 8)};
        sha256_update(s, le, 2);
    }
}

typedef struct {
    jn1_ev_t last;
    uint32_t records;
    sha256_t px, pcm;
    bool px_bad;
} jn1_run_t;

static uint16_t s_px[480 * 40];

/* Feeds `b` in slices of `slice` bytes (0: whole), hashing the pixels and the sound as each
   record lands, the way a panel with a 480x40 buffer would see them. */
static jn1_ev_t jn1_run(const uint8_t *b, size_t n, size_t slice, jn1_notice_t *out, jn1_run_t *run)
{
    static uint8_t rec[JN1_BUF_BYTES];
    jn1_reader_t r;
    jn1_init(&r, rec, out);
    memset(run, 0, sizeof *run);
    sha256_init(&run->px);
    sha256_init(&run->pcm);
    size_t at = 0;
    jn1_ev_t ev = JN1_EV_NONE;
    while (at < n && ev != JN1_EV_BAD) {
        const size_t want = slice && slice < n - at ? slice : n - at;
        const size_t used = jn1_feed(&r, b + at, want, &ev);
        at += used;
        if (ev == JN1_EV_RECORD || ev == JN1_EV_END) {
            run->records++;
            if (r.rec_n > 3 && r.rec[0] == JN1_STRIPE) {
                uint16_t y0, rows;
                if (jn1_stripe_pixels(r.rec + 3, r.rec_n - 3, out->img_w, s_px, 480 * 40, &y0, &rows))
                    sha_px(&run->px, s_px, (uint32_t)rows * out->img_w);
                else
                    run->px_bad = true;
            }
            if (r.rec[0] == JN1_PCM) sha256_update(&run->pcm, r.rec + 3, r.rec_n - 3);
        }
        if (ev != JN1_EV_BAD && used == 0) break;
        if (ev != JN1_EV_BAD) run->last = ev;
    }
    if (ev == JN1_EV_BAD) run->last = ev;
    return jn1_torn(&r) && run->last != JN1_EV_BAD ? JN1_EV_NONE : run->last;
}

static void test_jn1_reads_every_fixture_as_the_box_decodes_it(void)
{
    size_t vlen = 0;
    char *vectors = (char *)read_fixture("vectors.json", &vlen);
    CHECK(vectors != NULL, "the box's vectors.json is where the suite looks for it");
    if (!vectors) return;
    static const size_t SLICES[] = {0, 1, 2, 3, 5, 7, 64, 1000, 4099};
    for (size_t v = 0; v < sizeof JN1_VECTORS / sizeof *JN1_VECTORS; v++) {
        const jn1_vector_t *x = &JN1_VECTORS[v];
        CHECK(strstr(vectors, x->sha256) && (!x->px_sha || strstr(vectors, x->px_sha)) &&
                  (!x->pcm_sha || strstr(vectors, x->pcm_sha)),
              "the expected digests are vectors.json's own");
        char file[32];
        snprintf(file, sizeof file, "%s.jn1", x->name);
        size_t n = 0;
        uint8_t *b = read_fixture(file, &n);
        CHECK(b != NULL, x->name);
        if (!b) continue;
        char sha[65];
        sha_of(b, n, sha);
        CHECK(!strcmp(sha, x->sha256), "the fixture is the one vectors.json describes");
        for (size_t s = 0; s < sizeof SLICES / sizeof *SLICES; s++) {
            jn1_notice_t got;
            jn1_run_t run;
            const jn1_ev_t ev = jn1_run(b, n, SLICES[s], &got, &run);
            char id[37], px[65], pcm[65];
            jn1_uuid_text(got.id, id);
            sha256_hex(&run.px, px);
            sha256_hex(&run.pcm, pcm);
            CHECK(ev == JN1_EV_END && got.bytes == n, "the whole stream reads, in any split");
            CHECK(!strcmp(id, JN1_ID) && !strcmp(got.sender, x->sender) &&
                      !strcmp(got.title, x->title) && !strcmp(got.body, x->body),
                  "id, sender, title and body as decode() reads them");
            bool choices = got.n_choices == x->n_choices;
            for (uint8_t c = 0; choices && c < got.n_choices; c++)
                choices = !strcmp(got.choices[c], x->choices[c]);
            CHECK(choices && !(got.flags & JN1_FLAG_QUESTION) == !x->n_choices, "the choices, in order");
            CHECK(!(got.flags & JN1_FLAG_CHIME) == !x->chime && got.hide_s == x->hide_s &&
                      got.ttl_s == x->ttl_s,
                  "chime, hide and the answer window");
            CHECK(got.img_mode == x->mode && got.img_w == x->w && got.img_h == x->h &&
                      got.stripes == x->stripes && !run.px_bad,
                  "the picture's mode, size and stripes");
            CHECK(!x->px_sha || !strcmp(px, x->px_sha), "every pixel decode() decodes");
            CHECK(got.audio_ms == x->audio_ms && (!x->pcm_sha || !strcmp(pcm, x->pcm_sha)) &&
                      !(got.flags & JN1_FLAG_SOUND) == !x->pcm_sha,
                  "the sound, byte for byte");
            CHECK(!x->stripes || got.stripe_at > 3, "where the picture starts is kept");
        }
        free(b);
    }
    free(vectors);
}

/* ---- crafted streams: what decode() refuses, refused here ---- */

typedef struct {
    uint8_t b[8192];
    size_t n;
} jn_t;

static void jn_rec(jn_t *s, uint8_t kind, const void *p, size_t len)
{
    s->b[s->n] = kind;
    s->b[s->n + 1] = (uint8_t)len;
    s->b[s->n + 2] = (uint8_t)(len >> 8);
    if (len) memcpy(s->b + s->n + 3, p, len);
    s->n += 3 + len;
}

static void jn_meta(jn_t *s, uint8_t flags, uint8_t mode, uint16_t w, uint16_t h, uint16_t ms)
{
    uint8_t m[33] = {0};
    m[0] = 0x6a;
    m[16] = flags;
    m[19] = 60;
    m[23] = mode;
    m[24] = (uint8_t)w;
    m[25] = (uint8_t)(w >> 8);
    m[26] = (uint8_t)h;
    m[27] = (uint8_t)(h >> 8);
    m[28] = (uint8_t)ms;
    m[29] = (uint8_t)(ms >> 8);
    memcpy(m + 30, "Ops", 3);
    memcpy(s->b, "JN1", 3);
    s->n = 3;
    jn_rec(s, JN1_META, m, sizeof m);
}

static void jn_text(jn_t *s, uint8_t kind, const char *t)
{
    jn_rec(s, kind, t, strlen(t));
}

static void jn_end(jn_t *s)
{
    const uint32_t at = (uint32_t)s->n;
    const uint8_t e[5] = {0, (uint8_t)at, (uint8_t)(at >> 8), (uint8_t)(at >> 16), (uint8_t)(at >> 24)};
    jn_rec(s, JN1_END, e, sizeof e);
}

/* One stripe of `rows` x `w` in a single colour, as runs of at most 128. */
static void jn_stripe(jn_t *s, uint16_t y0, uint16_t rows, uint16_t w, int extra)
{
    uint8_t p[4096];
    size_t k = 4;
    p[0] = (uint8_t)y0, p[1] = (uint8_t)(y0 >> 8), p[2] = (uint8_t)rows, p[3] = (uint8_t)(rows >> 8);
    int left = rows * w + extra;
    while (left > 0) {
        const int run = left > 128 ? 128 : left;
        p[k++] = (uint8_t)(run - 1);
        p[k++] = 0x1f;
        p[k++] = 0xf8;
        left -= run;
    }
    jn_rec(s, JN1_STRIPE, p, k);
}

static jn1_ev_t jn_read(const jn_t *s, const char **why)
{
    static uint8_t rec[JN1_BUF_BYTES];
    jn1_notice_t n;
    jn1_reader_t r;
    jn1_init(&r, rec, &n);
    jn1_ev_t ev = JN1_EV_NONE, last = JN1_EV_NONE;
    size_t at = 0;
    while (at < s->n && ev != JN1_EV_BAD) {
        at += jn1_feed(&r, s->b + at, s->n - at, &ev);
        if (ev != JN1_EV_NONE) last = ev;
    }
    *why = r.why ? r.why : "";
    return last == JN1_EV_END && jn1_ended(&r) ? JN1_EV_END : last == JN1_EV_BAD ? JN1_EV_BAD : JN1_EV_NONE;
}

static void test_jn1_refuses_what_decode_refuses(void)
{
    const char *why;
    jn_t s;
    /* The well-formed base each case below breaks one way. */
    jn_meta(&s, JN1_FLAG_QUESTION, 0, 0, 0, 0);
    jn_text(&s, JN1_TITLE, "T");
    jn_text(&s, JN1_BODY, "B");
    jn_text(&s, JN1_CHOICE, "Yes");
    jn_end(&s);
    CHECK(jn_read(&s, &why) == JN1_EV_END, "the base stream is whole");

    /* Torn: every prefix is incomplete, never accepted and never mistaken for a refusal. */
    bool torn_ok = true;
    for (size_t cut = 0; cut < s.n; cut++) {
        jn_t t = s;
        t.n = cut;
        torn_ok = torn_ok && jn_read(&t, &why) == JN1_EV_NONE;
    }
    CHECK(torn_ok, "a stream cut anywhere short of its end is torn, not whole");

    jn_t t = s;
    t.b[t.n - 4]++;
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "END"), "END's byte count disagreeing");
    t = s;
    t.b[t.n - 5] = 1;
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "END's code not 0");
    t = s;
    t.b[t.n++] = 0;
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "after END"), "a byte after END");
    t = s;
    t.b[0] = 'X';
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "not JN1");

    /* Out of order: BODY before TITLE, TITLE twice, CHOICE before BODY, a second META. */
    jn_meta(&t, JN1_FLAG_QUESTION, 0, 0, 0, 0);
    jn_text(&t, JN1_BODY, "B");
    jn_text(&t, JN1_TITLE, "T");
    jn_text(&t, JN1_CHOICE, "Yes");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "order"), "BODY before TITLE");
    jn_meta(&t, 0, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_text(&t, JN1_TITLE, "T");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "TITLE twice");
    jn_meta(&t, JN1_FLAG_QUESTION, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_text(&t, JN1_CHOICE, "Yes");
    jn_text(&t, JN1_BODY, "B");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "CHOICE before BODY");
    jn_meta(&t, 0, 0, 0, 0, 0);
    uint8_t meta2[30] = {0};
    jn_rec(&t, JN1_META, meta2, sizeof meta2);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "a second META");
    memcpy(t.b, "JN1", 3);
    t.n = 3;
    jn_text(&t, JN1_TITLE, "T");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "META"), "a first record that is not META");
    t.n = 3;
    jn_text(&t, 0x40, "x");
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "an unknown record before META");

    /* An unknown record after META is skipped by its length, as decode() skips it. */
    jn_meta(&t, 0, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_text(&t, 0x40, "later formats");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_END, "an unknown record after META is skipped");

    /* Flags against records. */
    jn_meta(&t, 0, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_text(&t, JN1_CHOICE, "Yes");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "question"), "choices without the flag");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 8, 2, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "picture"), "the picture flag with no stripes");
    jn_meta(&t, JN1_FLAG_QUESTION, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "T");
    for (int i = 0; i < 5; i++) jn_text(&t, JN1_CHOICE, i ? "No" : "Yes");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "a fifth choice");

    /* Stripes: whole rows, in order, exactly covered. */
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 480, 60, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 0, 40, 480, 0);
    jn_stripe(&t, 40, 20, 480, 0);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_END, "two stripes covering 480x60");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 480, 60, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 0, 40, 480, 1);
    jn_stripe(&t, 40, 20, 480, 0);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "overflows"), "a run past its stripe's pixels");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 480, 60, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 0, 40, 480, -1);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "short"), "a stripe short of its rows");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 480, 60, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 20, 40, 480, 0);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "a stripe that does not start where the last ended");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 48, 50, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 0, 41, 48, 0);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "more than 40 rows in a stripe");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 480, 60, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 0, 40, 480, 0);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "stripes that stop short of the picture's height");
    jn_meta(&t, JN1_FLAG_PICTURE, 2, 480, 60, 0);
    jn_text(&t, JN1_TITLE, "T");
    jn_stripe(&t, 0, 40, 480, 0);
    jn_stripe(&t, 40, 20, 480, 0);
    const uint8_t pcm[2] = {0};
    jn_rec(&t, JN1_PCM, pcm, 2);
    jn_stripe(&t, 60, 1, 480, 0);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "a STRIPE after PCM");

    /* Sound: even records, and as long as META says. */
    static uint8_t tone[3200];
    jn_meta(&t, JN1_FLAG_SOUND, 0, 0, 0, 100);
    jn_text(&t, JN1_TITLE, "T");
    jn_rec(&t, JN1_PCM, tone, 3200);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_END, "100 ms of sound, as META says");
    jn_meta(&t, JN1_FLAG_SOUND, 0, 0, 0, 100);
    jn_text(&t, JN1_TITLE, "T");
    jn_rec(&t, JN1_PCM, tone, 3199);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "odd"), "an odd PCM record");
    jn_meta(&t, JN1_FLAG_SOUND, 0, 0, 0, 200);
    jn_text(&t, JN1_TITLE, "T");
    jn_rec(&t, JN1_PCM, tone, 3200);
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "sound"), "a sound shorter than META says");

    /* A record longer than 4096, and text that is not UTF-8. */
    jn_meta(&t, 0, 0, 0, 0, 0);
    t.b[t.n] = JN1_TITLE, t.b[t.n + 1] = 0x01, t.b[t.n + 2] = 0x10;
    t.n += 3;
    CHECK(jn_read(&t, &why) == JN1_EV_BAD && strstr(why, "long"), "a 4097-byte record");
    jn_meta(&t, 0, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "\xc3\x28");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "a title that is not UTF-8");
    jn_meta(&t, 0, 0, 0, 0, 0);
    jn_text(&t, JN1_TITLE, "\xed\xa0\x80");
    jn_end(&t);
    CHECK(jn_read(&t, &why) == JN1_EV_BAD, "a surrogate is not UTF-8 either");

    /* What the panel keeps of a longer title: cut before a character, never inside one. */
    jn_meta(&t, 0, 0, 0, 0, 0);
    char long_title[80];
    memset(long_title, 'a', 59);
    memcpy(long_title + 59, "\xc3\xa9xyz", 6);
    jn_text(&t, JN1_TITLE, long_title);
    jn_end(&t);
    static uint8_t rec[JN1_BUF_BYTES];
    jn1_notice_t n;
    jn1_reader_t r;
    jn1_init(&r, rec, &n);
    jn1_ev_t ev;
    for (size_t at = 0; at < t.n;) at += jn1_feed(&r, t.b + at, t.n - at, &ev);
    CHECK(jn1_ended(&r) && strlen(n.title) == 59, "a 66-byte title is kept to 59, before the é");
}

/* ---- the service: files, queue, the box's answers ---- */

static nfetch_t *fetch_bytes(const char *path, const uint8_t *b, size_t n, size_t upto)
{
    nfetch_t *f = svc_notice_fetch_begin(path);
    size_t at = 0;
    while (f && at < upto) {
        uint8_t *dst;
        const size_t room = svc_notice_fetch_room(f, &dst);
        if (!room) break;
        const size_t k = room < upto - at ? room : upto - at;
        memcpy(dst, b + at, k);
        at += k;
        if (!svc_notice_fetch_took(f, k)) break;
    }
    (void)n;
    return f;
}

static bool fetch_fixture(const char *file, size_t cut)
{
    size_t n = 0;
    uint8_t *b = read_fixture(file, &n);
    if (!b) return false;
    nfetch_t *f = fetch_bytes(NOTICE_NEXT_PATH, b, n, cut ? cut : n);
    const bool queued = svc_notice_fetch_end(f, !cut, 200);
    free(b);
    return queued;
}

static unsigned files_in_storage(void)
{
    char name[96];
    unsigned k = 0;
    while (nstore_entry(k, name, sizeof name)) k++;
    return k;
}

static void notice_boot(void)
{
    notice_port_fake_wipe();
    svc_settings_init();
    svc_rtc_init();
    /* A panel that knows the time: a notice's answer window is kept as an instant. */
    svc_rtc_fake_set_date(2026, 10, 13);
    svc_rtc_fake_set(12, 0);
    svc_audio_init();
    svc_notice_start();
    app_events_set_sink(NULL);
}

static void test_the_pickup_reports_the_six_switches_the_box_reads(void)
{
    notice_boot();
    svc_settings_set(SET_NOTIF_ACCEPT, 1);
    svc_settings_set(SET_NOTIF_QUIET, 1);
    svc_settings_set(SET_NOTIF_CHIME, 0);
    svc_settings_set(SET_NIGHT_FROM, 1290);
    svc_settings_set(SET_NIGHT_TO, 390);
    svc_settings_set(SET_TZ, 5);
    char q[64];
    svc_notice_pickup_query(true, q, sizeof q);
    CHECK(!strcmp(q, "&na=1&nq=1&nc=0&nf=1290&nt=390&tz=5"), "the six, as parse_prefs takes them");
    svc_notice_pickup_query(false, q, sizeof q);
    CHECK(!q[0], "no UI, no report: the box says no_notice_surface");
    CHECK(notice_waiting_count("2") == 2 && notice_waiting_count("") == 0 &&
              notice_waiting_count("x") == 0 && notice_waiting_count(NULL) == 0,
          "X-Notice is a count or nothing");
    char path[NOTICE_PATH_BYTES];
    CHECK(!svc_notice_want_fetch(path, sizeof path), "nothing to collect until the pickup says so");
    svc_notice_waiting("1");
    CHECK(svc_notice_want_fetch(path, sizeof path) && !strcmp(path, NOTICE_NEXT_PATH), "then notice/next");
    svc_notice_none_waiting();
    CHECK(!svc_notice_want_fetch(path, sizeof path), "until it answers 204");
}

static void test_a_question_reaches_the_glass_and_its_answer_reaches_the_box(void)
{
    notice_boot();
    CHECK(fetch_fixture("question.jn1", 0), "question.jn1 queued");
    CHECK(svc_notice_count() == 1 && files_in_storage() == 1, "one notice, one file");
    unsigned before = app_events_unsunk();
    svc_notice_service();
    CHECK(app_events_unsunk() == before + 1, "NOTICE_ARRIVED raised once, on the UI task's pass");
    svc_notice_service();
    CHECK(app_events_unsunk() == before + 1, "and not again");
    notice_t n;
    CHECK(svc_notice_head(&n) && !strcmp(n.title, "Run the cleanup now?") &&
              !strcmp(n.body, "Frees about 38 GB.") && !strcmp(n.source, "Jerv") && n.n_choices == 2 &&
              !strcmp(n.choices[1], "No") && n.chime && n.hide_s == 30,
          "the overlay's notice, read from its file");
    char path[NOTICE_PATH_BYTES], json[48];
    CHECK(!svc_notice_post_take(path, sizeof path, json, sizeof json), "nothing owed before it is shown");
    svc_notice_on_glass(n.id);
    svc_notice_on_glass(n.id);
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) &&
              !strcmp(path, "/endpoint/notice/" JN1_ID "/state") && !strcmp(json, "{\"state\":\"shown\"}"),
          "on the glass: `shown`, once");
    svc_notice_answer(1);
    CHECK(svc_notice_count() == 0, "answered, it leaves the glass");
    CHECK(!svc_notice_post_take(path, sizeof path, json, sizeof json), "one POST at a time");
    svc_notice_post_done(-1);
    CHECK(!svc_notice_post_take(path, sizeof path, json, sizeof json), "no answer: not again for 5 s");
    svc_rtc_fake_advance_ms(5000);
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) && !strcmp(json, "{\"state\":\"shown\"}"),
          "then `shown` again, still first");
    svc_notice_post_done(204);
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) &&
              !strcmp(json, "{\"state\":\"answered\",\"choice\":1}"),
          "then the answer with the button's index");
    svc_notice_post_done(204);
    CHECK(!svc_notice_post_take(path, sizeof path, json, sizeof json), "nothing more owed");
    svc_notice_housekeep();
    CHECK(files_in_storage() == 0, "and its file is gone");
}

static void test_a_torn_stream_is_dropped_whole_and_fetched_again(void)
{
    notice_boot();
    size_t n = 0;
    uint8_t *b = read_fixture("banner.jn1", &n);
    CHECK(b != NULL, "banner.jn1");
    if (!b) return;
    nfetch_t *f = fetch_bytes(NOTICE_NEXT_PATH, b, n, n / 2);
    CHECK(!svc_notice_fetch_end(f, false, 200) && svc_notice_count() == 0, "half a stream is no notice");
    CHECK(files_in_storage() == 0, "and leaves no file");
    char path[NOTICE_PATH_BYTES];
    CHECK(!svc_notice_want_fetch(path, sizeof path), "the retry waits");
    svc_rtc_fake_advance_ms(2000);
    CHECK(svc_notice_want_fetch(path, sizeof path) && !strcmp(path, "/endpoint/notice/" JN1_ID "/body"),
          "then its body, by id");
    f = fetch_bytes(path, b, n, n);
    CHECK(svc_notice_fetch_end(f, true, 200) && svc_notice_count() == 1, "whole the second time");
    CHECK(!svc_notice_want_fetch(path, sizeof path), "and not fetched again");
    /* A refused stream is retried the same way, a bounded number of times. */
    notice_boot();
    uint8_t *bad = malloc(n);
    memcpy(bad, b, n);
    bad[n - 4] ^= 1;
    for (int i = 0; i < NOTICE_REFETCH_TRIES + 1; i++) {
        f = fetch_bytes(i ? path : NOTICE_NEXT_PATH, bad, n, n);
        CHECK(!svc_notice_fetch_end(f, true, 200), "a bad END count is never queued");
        svc_rtc_fake_advance_ms(30000);
        const bool again = svc_notice_want_fetch(path, sizeof path);
        CHECK(again == (i < NOTICE_REFETCH_TRIES), "fetched again, three times and no more");
    }
    /* Gone from the box (its window closed): given up at once. */
    notice_boot();
    f = fetch_bytes(NOTICE_NEXT_PATH, b, n, 100);
    svc_notice_fetch_end(f, false, 200);
    svc_rtc_fake_advance_ms(2000);
    CHECK(svc_notice_want_fetch(path, sizeof path), "its body is due");
    for (int i = 0; i < NOTICE_REFETCH_TRIES + 2; i++) {
        svc_notice_fetch_failed(path, -1);
        svc_rtc_fake_advance_ms(30000);
    }
    CHECK(svc_notice_want_fetch(path, sizeof path), "no box at all spends no try: it waits");
    svc_notice_fetch_failed(path, 404);
    CHECK(!svc_notice_want_fetch(path, sizeof path), "a 404 ends the retries");
    /* Torn before META: no id to ask for it by, so it is lost, not retried. */
    notice_boot();
    f = fetch_bytes(NOTICE_NEXT_PATH, b, n, 40);
    svc_notice_fetch_end(f, false, 200);
    svc_rtc_fake_advance_ms(60000);
    CHECK(!svc_notice_want_fetch(path, sizeof path), "a stream torn before its id is lost");
    free(bad);
    free(b);
}

typedef struct {
    sha256_t *s;
    uint32_t bytes;
} pcm_sink_t;

/* The main task's sink, as talk_link.c gives it: every slice, in order. */
static bool pcm_take(const uint8_t *p, uint32_t k, void *c)
{
    pcm_sink_t *ctx = c;
    sha256_update(ctx->s, p, k);
    ctx->bytes += k;
    return true;
}

static void test_a_picture_and_a_sound_come_back_out_of_the_file(void)
{
    notice_boot();
    CHECK(fetch_fixture("full_sound.jn1", 0), "full_sound.jn1 queued");
    notice_t n;
    CHECK(svc_notice_head(&n) && n.img_mode == 1 && n.stripes == 12 && n.audio_ms == 250,
          "a picture and a sound");
    sha256_t s;
    sha256_init(&s);
    uint32_t rows_seen = 0;
    bool ok = true;
    for (uint16_t i = 0; i < n.stripes && ok; i++) {
        uint16_t y0, rows;
        ok = svc_notice_stripe(n.id, i, s_px, 480 * 40, &y0, &rows) && y0 == rows_seen;
        if (ok) sha_px(&s, s_px, (uint32_t)rows * 480);
        rows_seen += rows;
    }
    char px[65];
    sha256_hex(&s, px);
    CHECK(ok && rows_seen == 480 && !strcmp(px, JN1_VECTORS[3].px_sha),
          "every stripe, decoded from the file into one 480x40 buffer, is decode()'s picture");
    uint16_t y0, rows;
    CHECK(svc_notice_stripe(n.id, 3, s_px, 480 * 40, &y0, &rows) && y0 == 120,
          "a stripe out of order too");
    CHECK(!svc_notice_stripe(n.id, 3, s_px, 480 * 39, &y0, &rows), "never into a buffer it overflows");
    CHECK(!svc_notice_stripe(n.id, 12, s_px, 480 * 40, &y0, &rows), "nor past the last");

    /* On the glass: its sound plays once, from its file, through the main task's sink. */
    svc_notice_on_glass(n.id);
    const char *key = svc_audio_fake_last_path();
    CHECK(strstr(key, ".jn1") != NULL && svc_audio_replying(), "the sound is asked for by its file");
    sha256_t pcm;
    sha256_init(&pcm);
    static uint8_t slice[1024];
    pcm_sink_t ctx = {&pcm, 0};
    CHECK(svc_notice_pcm_stream(key, slice, sizeof slice, pcm_take, &ctx), "the sound streams");
    char hex[65];
    sha256_hex(&pcm, hex);
    CHECK(ctx.bytes == 8000 && !strcmp(hex, JN1_VECTORS[3].pcm_sha),
          "250 ms, byte for byte decode()'s");
    svc_notice_dismiss();
    CHECK(!svc_audio_replying(), "hidden, its sound stops");

    /* Never over an alarm. */
    notice_boot();
    fetch_fixture("full_sound.jn1", 0);
    svc_notice_head(&n);
    svc_audio_ring(0);
    svc_notice_on_glass(n.id);
    CHECK(!svc_audio_replying(), "an alarm ringing: the notice's sound does not play");
    svc_audio_ring_stop();
    svc_notice_dismiss();
}

static void test_a_notice_hides_itself_after_hide_s(void)
{
    notice_boot();
    fetch_fixture("question.jn1", 0);
    svc_notice_service();
    notice_t n;
    svc_notice_head(&n);
    svc_rtc_fake_advance_ms(60000);
    svc_notice_service();
    CHECK(svc_notice_count() == 1, "the clock starts when it is shown, not when it arrives");
    svc_notice_on_glass(n.id);
    svc_rtc_fake_advance_ms(29900);
    svc_notice_service();
    CHECK(svc_notice_count() == 1, "29.9 s of 30: still up");
    svc_rtc_fake_advance_ms(100);
    svc_notice_service();
    CHECK(svc_notice_count() == 0, "30 s: hidden");
    char path[NOTICE_PATH_BYTES], json[48];
    svc_notice_post_take(path, sizeof path, json, sizeof json);
    svc_notice_post_done(204);
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) &&
              !strcmp(json, "{\"state\":\"expired\"}"),
          "and the box hears `expired`");

    /* A plain notice's OK is a dismissal, not an answer. */
    notice_boot();
    fetch_fixture("plain.jn1", 0);
    svc_notice_head(&n);
    svc_notice_on_glass(n.id);
    svc_notice_answer(0);
    svc_notice_post_take(path, sizeof path, json, sizeof json);
    svc_notice_post_done(204);
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) &&
              !strcmp(json, "{\"state\":\"dismissed\"}"),
          "OK on a plain notice: `dismissed`");

    /* Past its answer window it goes quietly: the box would 404 anything said. */
    notice_boot();
    fetch_fixture("full_sound.jn1", 0);
    svc_rtc_fake_advance_ms(120 * 1000);
    svc_notice_service();
    CHECK(svc_notice_count() == 0 && !svc_notice_post_take(path, sizeof path, json, sizeof json),
          "120 s after delivery, gone with nothing owed");
    svc_notice_housekeep();
    CHECK(files_in_storage() == 0, "and its file too");
}

static void test_four_open_at_most(void)
{
    notice_boot();
    for (int i = 0; i < 4; i++) {
        size_t n = 0;
        uint8_t *b = read_fixture("plain.jn1", &n);
        if (!b) return;
        b[3 + 3 + 15] = (uint8_t)i; /* a different id each */
        nfetch_t *f = fetch_bytes(NOTICE_NEXT_PATH, b, n, n);
        CHECK(svc_notice_fetch_end(f, true, 200), "queued");
        free(b);
    }
    CHECK(svc_notice_count() == 4, "four open");
    svc_notice_waiting("1");
    char path[NOTICE_PATH_BYTES];
    CHECK(!svc_notice_want_fetch(path, sizeof path), "a fifth is not collected while four are open");
    CHECK(!fetch_fixture("question.jn1", 0) && files_in_storage() == 4, "nor kept if it comes");
    svc_notice_dismiss();
    CHECK(svc_notice_want_fetch(path, sizeof path), "one hidden: room again");
}

/* The review of #1729: a title past the glass's 60 bytes is cut on a character boundary judged
   from the byte that was left out, not from one never read. */
static void test_a_long_title_is_cut_on_a_character(void)
{
    notice_boot();
    static jn_t s;
    jn_meta(&s, 0, 0, 0, 0, 0);
    char t[72];
    memset(t, 'a', 59);
    memcpy(t + 59, "\xc3\xa9tail", 6);
    t[65] = '\0';
    jn_text(&s, JN1_TITLE, t);
    jn_end(&s);
    uint8_t *b = malloc(s.n);
    if (!b) return;
    memcpy(b, s.b, s.n);
    CHECK(svc_notice_fetch_end(fetch_bytes(NOTICE_NEXT_PATH, b, s.n, s.n), true, 200), "taken");
    free(b);
    notice_t n;
    CHECK(svc_notice_head(&n) && strlen(n.title) == 59 && n.title[58] == 'a',
          "the split \"é\" goes whole, the 59 bytes before it stay");
}

/* The review of #1729: with the box unreachable, every end said at the glass must survive, so a
   notice is only collected while the owed list has room for it. */
static void test_an_outage_never_loses_an_answer(void)
{
    notice_boot();
    char path[NOTICE_PATH_BYTES], json[48];
    int taken = 0;
    for (int i = 0; i < 10; i++) {
        svc_notice_waiting("1");
        if (!svc_notice_want_fetch(path, sizeof path)) break;
        size_t n = 0;
        uint8_t *b = read_fixture("question.jn1", &n);
        if (!b) return;
        b[3 + 3 + 15] = (uint8_t)(0x80 + i);
        CHECK(svc_notice_fetch_end(fetch_bytes(NOTICE_NEXT_PATH, b, n, n), true, 200), "collected");
        free(b);
        notice_t h;
        svc_notice_head(&h);
        svc_notice_on_glass(h.id);
        svc_notice_answer(1);
        if (svc_notice_post_take(path, sizeof path, json, sizeof json)) svc_notice_post_done(-1);
        taken++;
    }
    CHECK(taken == NOTICE_POSTS / 2, "collecting stops once the owed list could not hold another");
    int answers = 0, shown = 0;
    svc_rtc_fake_advance_ms(300000);
    while (svc_notice_post_take(path, sizeof path, json, sizeof json)) {
        if (strstr(json, "answered")) answers++;
        else if (strstr(json, "shown")) shown++;
        svc_notice_post_done(204);
    }
    CHECK(answers == taken && shown == taken, "every `shown` and every answer reaches the box");
    svc_notice_waiting("1");
    CHECK(svc_notice_want_fetch(path, sizeof path), "and collecting resumes");
}

static void test_a_reboot_keeps_the_notices_and_what_the_box_is_owed(void)
{
    notice_boot();
    fetch_fixture("question.jn1", 0);
    fetch_fixture("full_sound.jn1", 0);
    CHECK(svc_notice_count() == 1, "the same id twice is one notice");
    notice_t n;
    svc_notice_head(&n);
    svc_notice_on_glass(n.id);
    svc_rtc_fake_advance_ms(3000);
    svc_notice_housekeep();
    CHECK(notice_port_fake_saves() > 0, "what is owed goes to flash");
    /* Half a stream on its way, and a stranger in the directory, when the power goes. */
    nfetch_t *f = svc_notice_fetch_begin(NOTICE_NEXT_PATH);
    char stray[160];
    snprintf(stray, sizeof stray, "%s/stray.bin", notice_port_base());
    FILE *fp = fopen(stray, "wb");
    if (fp) {
        fputs("x", fp);
        fclose(fp);
    }
    CHECK(files_in_storage() == 3, "the notice, the part file and the stray");
    svc_notice_start(); /* the reboot */
    free(f);
    CHECK(svc_notice_count() == 1 && files_in_storage() == 1, "the whole notice back, the rest gone");
    CHECK(svc_notice_head(&n) && !strcmp(n.title, "Run the cleanup now?"), "the same notice");
    char path[NOTICE_PATH_BYTES], json[48];
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) && !strcmp(json, "{\"state\":\"shown\"}"),
          "its `shown` still owed");
    /* Answered before the reboot, the box not yet told, its file not yet deleted: not shown
       again, and the answer still goes. */
    svc_notice_post_done(204);
    svc_notice_answer(0);
    svc_notice_post_take(path, sizeof path, json, sizeof json);
    svc_notice_post_done(-1);
    svc_rtc_fake_advance_ms(3000);
    svc_notice_housekeep();
    char name[NOTICE_FILE_NAME];
    const uint8_t id[16] = {0x6a, 0x1f, 0x2c, 0x3d, 0x4e, 0x5f, 0x4a, 0x6b,
                            0x8c, 0x7d, 0x9e, 0x0f, 0x1a, 0x2b, 0x3c, 0x4d};
    int64_t now_s;
    svc_rtc_epoch(&now_s);
    notice_file_name(99, id, (uint32_t)(now_s + 600), name, sizeof name);
    size_t qn = 0;
    uint8_t *qb = read_fixture("question.jn1", &qn);
    char full[160];
    snprintf(full, sizeof full, "%s/%s", notice_port_base(), name);
    fp = fopen(full, "wb");
    if (fp && qb) fwrite(qb, 1, qn, fp);
    if (fp) fclose(fp);
    free(qb);
    svc_notice_start();
    CHECK(svc_notice_count() == 0 && files_in_storage() == 0, "a notice already answered stays gone");
    CHECK(svc_notice_post_take(path, sizeof path, json, sizeof json) &&
              !strcmp(json, "{\"state\":\"answered\",\"choice\":0}"),
          "and its answer is still owed to the box");

    /* A question whose window closed while the panel was off is not shown again: the box would
       404 its answer. Nor is one that came while the panel had no time to keep its end by. */
    notice_boot();
    fetch_fixture("question.jn1", 0);
    svc_rtc_fake_advance_ms(901 * 1000);
    svc_notice_start();
    CHECK(svc_notice_count() == 0 && files_in_storage() == 0, "past its answer window: gone at boot");
    notice_boot();
    fetch_fixture("question.jn1", 0);
    svc_rtc_fake_advance_ms(600 * 1000);
    svc_notice_start();
    CHECK(svc_notice_count() == 1, "inside it: back");
    CHECK(svc_notice_head(&n) && !svc_notice_post_take(path, sizeof path, json, sizeof json),
          "never shown, nothing owed");
    svc_rtc_fake_advance_ms(301 * 1000);
    svc_notice_service();
    CHECK(svc_notice_count() == 0, "and it lapses when the rest of its window runs out, not 900 s on");

    notice_port_fake_fail_mount(true);
    notice_boot();
    char q[64];
    svc_notice_pickup_query(true, q, sizeof q);
    CHECK(!q[0] && !svc_notice_want_fetch(path, sizeof path), "no storage: no report, no fetch");
    notice_port_fake_fail_mount(false);
}

static void test_the_state_post_list_and_the_file_names(void)
{
    notice_posts_t l = {0};
    for (int i = 0; i < NOTICE_POSTS; i++) CHECK(notice_posts_add(&l, JN1_ID, NOTICE_SHOWN, 0), "kept");
    CHECK(!notice_posts_room(&l, 0), "full: no room for another notice");
    l.p[0].state = NOTICE_ANSWERED;
    CHECK(!notice_posts_add(&l, JN1_ID, NOTICE_EXPIRED, 0) && l.n == NOTICE_POSTS &&
              l.p[0].state == NOTICE_ANSWERED && l.p[NOTICE_POSTS - 1].state == NOTICE_EXPIRED,
          "full, a `shown` gives way and the head (in flight) never does");
    for (int i = 1; i < NOTICE_POSTS; i++) l.p[i].state = NOTICE_DISMISSED;
    CHECK(!notice_posts_add(&l, JN1_ID, NOTICE_SHOWN, 0) && l.n == NOTICE_POSTS &&
              l.p[NOTICE_POSTS - 1].state == NOTICE_DISMISSED,
          "full of ends, a new `shown` is the one lost");
    CHECK(notice_posts_final(&l, JN1_ID), "a final state is seen");
    l.p[0].id[3] = 'X';
    l.p[1].state = 9;
    notice_posts_clean(&l);
    CHECK(l.n == NOTICE_POSTS - 2, "garbage read from flash is dropped");
    CHECK(notice_post_done(204) && notice_post_done(404) && notice_post_done(409) &&
              notice_post_done(422) && !notice_post_done(-1) && !notice_post_done(500) &&
              !notice_post_done(401),
          "done, or tried again");
    CHECK(notice_backoff_ms(1) == 5000 && notice_backoff_ms(3) == 60000 && notice_backoff_ms(9) == 300000,
          "the backoff");
    uint8_t id[16], back[16];
    for (int i = 0; i < 16; i++) id[i] = (uint8_t)(i * 17);
    char name[NOTICE_FILE_NAME];
    uint32_t seq;
    uint32_t deadline;
    notice_file_name(0x2a, id, 0x6a000000u, name, sizeof name);
    CHECK(notice_file_parse(name, &seq, back, &deadline) && seq == 0x2a && !memcmp(id, back, 16) &&
              deadline == 0x6a000000u,
          "a name round-trips, the answer window's end in it");
    CHECK(!notice_file_parse("part.tmp", &seq, back, &deadline) &&
              !notice_file_parse("0000002a-zz.jn1", &seq, back, &deadline),
          "and nothing else is one");
    CHECK(notice_window_left(1000, true, 400) == 600 && !notice_window_left(1000, true, 1000) &&
              !notice_window_left(0, true, 400) && !notice_window_left(1000, false, 400),
          "after a reboot: what is left of the window, and nothing when either end is unknown");
    CHECK(notice_hide_due(1000, 3, 4000) && !notice_hide_due(1000, 3, 3999) && !notice_hide_due(0, 0, 999999),
          "hide_s, and 0 is until tapped");
    CHECK(notice_lapsed(0, 30, 30000) && !notice_lapsed(0, 30, 29999), "the answer window");
}

/* ---- the notice overlay's look (owner, 2026-10-13; mock Main.dc.html) ---- */

static notice_t view_notice(uint8_t choices, uint16_t hide_s, uint8_t mode)
{
    notice_t n = {.id = 7, .n_choices = choices, .hide_s = hide_s, .img_mode = mode};
    snprintf(n.source, sizeof n.source, "Jerv");
    snprintf(n.title, sizeof n.title, "Title");
    if (mode) {
        n.img_w = 480;
        n.img_h = mode == 1 ? 480 : 140;
        n.stripes = 3;
    }
    return n;
}

static void test_the_notice_overlay_reads_like_the_mock(void)
{
    notice_look_t l;
    notice_t n = view_notice(0, 20, 0);
    notice_look(&n, 1, false, NOTICE_NIGHT_RED, false, 2000, &l);
    CHECK(!strcmp(l.hint, "Tap to hide · hides in 18 s") && l.timer && l.timer_permille == 900,
          "a plain notice two seconds in: 18 s left, the line nine tenths full (NotifyInfo)");
    CHECK(l.accent == UI_C_STEEL && l.title == UI_C_TEXT && l.ink_opa == 255 && !l.count[0],
          "by day: steel, the text colours, alone in the queue");
    notice_look(&n, 1, false, NOTICE_NIGHT_RED, false, 20000, &l);
    CHECK(l.timer_permille == 0 && strstr(l.hint, "hides in 0 s"), "and empty when its time is up");

    n = view_notice(2, 0, 0);
    notice_look(&n, 2, false, NOTICE_NIGHT_RED, false, 60000, &l);
    CHECK(!strcmp(l.hint, "Tap an answer · tap elsewhere to hide") && !l.timer &&
              !strcmp(l.count, "1 of 2"),
          "a question: no countdown, a tap elsewhere hides it, 1 of 2 (NotifyQuestion)");
    n.hide_s = 30;
    notice_look(&n, 1, false, NOTICE_NIGHT_RED, false, 10000, &l);
    CHECK(!strcmp(l.hint, "Tap an answer · hides in 20 s"), "a question its sender gave a time");

    n = view_notice(0, 8, 1);
    notice_look(&n, 1, false, NOTICE_NIGHT_RED, false, 3000, &l);
    CHECK(l.full_picture && !l.banner && !l.ask_picture && !strcmp(l.chip_hint, "Tap to hide · 5 s") &&
              l.timer_color == 0xFFFFFF && l.timer_opa == 217,
          "a full picture: the glass, the hint as a chip, a white line (NotifyPicture)");
    n = view_notice(3, 0, 2);
    notice_look(&n, 1, false, NOTICE_NIGHT_RED, false, 0, &l);
    CHECK(l.banner && !l.full_picture, "a banner over the words (NotifyBanner)");

    /* Inside the night clock's hours every notice takes its colour, and a picture waits. */
    uint32_t c;
    uint8_t opa;
    for (uint8_t k = 0; k < 3; k++) {
        notice_night_palette(k, &c, &opa);
        const clock_face_t f = clock_face(true, 0, 1439, k, 600, true);
        CHECK(c == f.color && opa == f.opa, "the night colours are the clock face's own");
    }
    n = view_notice(0, 20, 1);
    notice_look(&n, 1, true, NOTICE_NIGHT_RED, false, 2000, &l);
    notice_night_palette(NOTICE_NIGHT_RED, &c, &opa);
    CHECK(l.night && l.ask_picture && !l.full_picture && l.accent == c && l.title == c &&
              l.body == c && l.muted == c && l.ink_opa == opa && l.timer_color == c,
          "at night: the night palette and Show picture (NotifyNight)");
    notice_look(&n, 1, true, NOTICE_NIGHT_RED, true, 2000, &l);
    CHECK(l.full_picture && !l.ask_picture, "Show picture tapped: the picture");
    n = view_notice(2, 0, 2);
    notice_look(&n, 1, true, NOTICE_NIGHT_AMBER, false, 0, &l);
    notice_night_palette(NOTICE_NIGHT_AMBER, &c, &opa);
    CHECK(l.ask_picture && !l.banner && l.accent == c, "a banner waits too, in the colour chosen");
    n = view_notice(0, 20, 0);
    notice_look(&n, 1, true, NOTICE_NIGHT_GREY, false, 0, &l);
    CHECK(l.night && !l.ask_picture && l.title != UI_C_TEXT, "and words alone are dimmed as well");

    CHECK(notice_night_now(true, 21 * 60 + 30, 6 * 60 + 30, true, 23 * 60) &&
              !notice_night_now(false, 21 * 60 + 30, 6 * 60 + 30, true, 23 * 60) &&
              !notice_night_now(true, 21 * 60 + 30, 6 * 60 + 30, false, 23 * 60) &&
              !notice_night_now(true, 21 * 60 + 30, 6 * 60 + 30, true, 12 * 60),
          "the night clock's hours: on, timed, and inside the window");

    notice_sound_line_t s;
    notice_sound_line(true, 2400, 6000, &s);
    CHECK(s.playing && !strcmp(s.time, "0:02 / 0:06") && s.permille == 400, "playing: 0:02 / 0:06 (NotifyAudio)");
    notice_sound_line(false, 0, 4300, &s);
    CHECK(!s.playing && !strcmp(s.time, "0:05"), "afterwards its length, beside Play again (NotifyQuestionSound)");
}

static void test_key_over_a_notice_s_sound_stops_it(void)
{
    sh_state_t st = {0};
    sh_facts_t f = in_app();
    f.notices = 1;
    f.speaking = true;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_STOP_REPLY && st.key_owner == SH_KEY_FREE,
          "KEY while its sound plays: the sound stops, nothing records (the reply's rule)");
    CHECK(sh_route_button(&st, &f, SH_BTN_BOOT).first == SH_ACT_DISMISS_NOTICE, "BOOT still hides it");
    f.speaking = false;
    CHECK(sh_route_button(&st, &f, SH_BTN_KEY_DOWN).first == SH_ACT_NONE, "silent: KEY waits");
}

static void test_the_picture_comes_out_of_its_file_a_few_rows_at_a_time(void)
{
    notice_boot();
    CHECK(fetch_fixture("full_sound.jn1", 0), "full_sound.jn1 queued");
    notice_t n;
    svc_notice_head(&n);
    notice_pic_t p;
    CHECK(svc_notice_pic_open(n.id, &p), "opened");
    sha256_t s;
    sha256_init(&s);
    bool ok = true;
    for (uint16_t y = 0; y < 480 && ok; y += 8) {
        ok = svc_notice_pic_rows(&p, y, 8, s_px);
        if (ok) sha_px(&s, s_px, 8 * 480);
    }
    char hex[65];
    sha256_hex(&s, hex);
    CHECK(ok && !strcmp(hex, JN1_VECTORS[3].px_sha), "eight rows at a time, top to bottom: decode()'s picture");
    static uint16_t stripe[480 * 40];
    uint16_t y0, rows;
    svc_notice_stripe(n.id, 5, stripe, 480 * 40, &y0, &rows);
    CHECK(svc_notice_pic_rows(&p, (uint16_t)(y0 + 3), 8, s_px) &&
              !memcmp(s_px, stripe + 3 * 480, 8 * 480 * 2),
          "asked above the last row read, it starts over and agrees with the stripe");
    CHECK(!svc_notice_pic_rows(&p, 476, 8, s_px), "never past the bottom");
    svc_notice_pic_close(&p);
    notice_boot();
    fetch_fixture("plain.jn1", 0);
    svc_notice_head(&n);
    CHECK(!svc_notice_pic_open(n.id, &p), "a notice with no picture has none to open");
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
    
    test_boot_short_is_back_on_release_and_long_is_the_clock_once();
    test_boot_held_opens_the_clock_from_anywhere_but_the_clock();
    test_the_pickup_waits_three_seconds_backs_off_and_never_jumps_the_manifest();
    test_the_relay_reports_itself_in_one_token();
    test_a_request_id_is_uuid_text_or_it_is_dropped();
    test_a_commands_answer_is_its_own_phase_for_the_heap_low();
    test_a_registered_secret_is_scrubbed_from_a_log_line_even_cut_short();
    test_the_stream_is_byte_for_byte_what_the_box_decodes();
    test_the_ring_never_overwrites_and_wraps();
    test_a_play_rides_a_smaller_ring_than_a_talk();
    test_a_turn_and_its_touch_inverse_agree_everywhere();
    test_turned_strips_build_the_turned_screen();
    test_down_needs_a_clear_margin_and_a_dwell();
    test_the_service_reads_only_while_lit_and_on();
    test_the_fit_finds_a_panel_s_error_and_refuses_nonsense();
    test_calibration_lives_in_the_glass_frame_and_rotation_follows();
    test_touch_and_calibration_follow_the_glass_not_the_request();
    test_two_holds_settle_the_sensor_s_axes();
    test_the_calibration_screen_counts_taps_that_agree();
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
    test_emoji_are_drawn_skipped_or_marked_and_text_is_left_alone();
    test_an_agent_answer_is_kept_whole_on_the_glass();
    test_the_menu_opens_only_over_a_clear_conversation();
    test_clear_chat_takes_two_taps_within_three_seconds();
    test_buttons_over_the_menu_only_close_it();
    test_jerv_and_dave_are_two_threads_and_two_conversations();
    test_step_records_read_in_any_split_and_an_old_panel_skips_them();
    test_the_steps_keep_the_last_four_and_count_the_rest();
    test_key_inside_a_conversation_continues_it_and_anywhere_else_starts_jerv_anew();
    test_the_working_view_lists_steps_then_folds_into_the_answer_s_line();
    test_a_status_change_redraws_only_the_working_view();
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
    test_the_box_s_longest_alarms_answer_fits_the_fetch_buffer();
    test_switches_queue_for_the_box_and_the_last_one_wins();
    test_the_alarm_sounds();
    test_a_ringing_alarm_beats_everything();
    test_the_alarms_service_end_to_end();
    test_other_tasks_read_the_alarms_the_ui_task_published();
    test_an_agent_s_menu_is_new_recent_and_cancel();
    test_new_conversation_is_one_tap_and_does_nothing_when_already_new();
    test_a_row_s_time_reads_like_the_mock();
    test_recent_opens_lists_switches_and_closes_as_the_mock_does();
    test_the_box_s_recent_json_is_read_as_the_box_writes_it();
    test_a_switched_thread_s_json_is_read_as_the_box_writes_it();
    test_the_conversation_service_end_to_end();
    test_post_paths_take_only_box_ids();
    test_post_upload_is_framed_and_capped();
    test_post_thread_json_reads_as_the_box_writes_it();
    test_post_sent_answer_and_bubbles();
    test_post_unread_badge_and_chime_rules();
    test_tk_listen_claims_without_a_hold();
    test_a_person_thread_keeps_the_panels_own_bubbles();
    test_the_people_s_threads_end_to_end();
    test_played_stamps_survive_failures_reboots_and_many_plays();
    test_jn1_reads_every_fixture_as_the_box_decodes_it();
    test_jn1_refuses_what_decode_refuses();
    test_the_pickup_reports_the_six_switches_the_box_reads();
    test_a_question_reaches_the_glass_and_its_answer_reaches_the_box();
    test_a_torn_stream_is_dropped_whole_and_fetched_again();
    test_a_picture_and_a_sound_come_back_out_of_the_file();
    test_a_notice_hides_itself_after_hide_s();
    test_four_open_at_most();
    test_an_outage_never_loses_an_answer();
    test_a_long_title_is_cut_on_a_character();
    test_a_reboot_keeps_the_notices_and_what_the_box_is_owed();
    test_the_state_post_list_and_the_file_names();
    test_the_notice_overlay_reads_like_the_mock();
    test_key_over_a_notice_s_sound_stops_it();
    test_the_picture_comes_out_of_its_file_a_few_rows_at_a_time();
    printf("%d checks, %d failed\n", s_checks, s_fails);
    return s_fails ? 1 : 0;
}
