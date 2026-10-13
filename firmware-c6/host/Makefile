# The panel's pure-C half, built and run on the host: every decision the shell makes, every
# app model, the notice policy and the JN1 reader (against the box's own fixtures), and the
# debug surface (redaction, commands, log ring, the screenshot codec). None of it may include
# LVGL or ESP-IDF — this link is what checks that.
CC ?= cc
# gnu11, not c11: ESP-IDF builds this code as gnu17, and a stricter host dialect would test a
# language the device never compiles (firmware/host/Makefile has the same rule and the reason).
C := ../components
INC := $(addprefix -I,$(wildcard $(C)/*/include)) -I$(C)/app_clock -I$(C)/app_settings \
       -I$(C)/app_thread
CFLAGS ?= -std=gnu11 -O1 -g -Wall -Wextra -Werror $(INC)

SRC = $(C)/app_api/dbg_json.c $(C)/app_api/app_event.c \
      $(C)/shell/shell_core.c $(C)/shell/shell_evq.c $(C)/shell/notice_view.c \
      $(C)/app_clock/clock_model.c $(C)/app_settings/settings_model.c \
      $(C)/app_thread/thread_model.c $(C)/convo/convo_model.c $(C)/ui_kit/ui_pin.c $(C)/ui_kit/ui_hold.c \
      $(C)/ui_kit/emoji_logic.c $(C)/ui_kit/emoji/emoji_set.c \
      $(C)/svc_siblings/sib_logic.c $(C)/svc_siblings/svc_siblings.c \
      $(C)/svc_siblings/sib_port_fake.c \
      $(C)/svc_alarms/alarm_logic.c $(C)/svc_alarms/svc_alarms.c $(C)/svc_alarms/alarm_port_fake.c \
      $(C)/svc_convo/convo_wire.c $(C)/svc_convo/svc_convo.c $(C)/svc_convo/convo_port_fake.c \
      $(C)/svc_post/post_logic.c $(C)/svc_post/svc_post.c $(C)/svc_post/post_port_fake.c \
      $(C)/svc_notice/notice_policy.c $(C)/svc_notice/svc_notice.c $(C)/svc_notice/jn1.c \
      $(C)/svc_notice/notice_logic.c $(C)/svc_notice/notice_store.c \
      $(C)/svc_notice/notice_port_fake.c \
      $(C)/svc_audio/talk_policy.c $(C)/svc_audio/talk_core.c \
      $(C)/svc_audio/svc_audio_talk.c $(C)/svc_audio/svc_audio_fake.c $(C)/svc_audio/alarm_sound.c \
      $(C)/svc_settings/svc_settings_fake.c \
      $(C)/svc_settings/settings_store.c $(C)/svc_settings/settings_sync.c $(C)/svc_rtc/time_logic.c \
      $(C)/svc_rtc/svc_rtc_fake.c $(C)/svc_net/svc_net_fake.c $(C)/svc_box/svc_box_fake.c \
      $(C)/svc_box/box_logic.c $(C)/svc_messages/svc_messages_fake.c $(C)/svc_messages/hist_logic.c \
      $(wildcard $(C)/svc_debug/*.c) \
      $(C)/svc_display/disp_logic.c $(C)/svc_display/disp_guard.c \
      $(C)/svc_power/axp_logic.c $(C)/svc_buttons/btn_logic.c \
      $(C)/svc_touch/tp_logic.c $(C)/svc_touch/svc_calib.c $(C)/svc_touch/tp_cal_port_fake.c \
      $(C)/svc_orient/orient_logic.c $(C)/svc_orient/svc_orient.c $(C)/svc_orient/orient_port_fake.c \
      $(C)/svc_display/svc_display_fake.c
# Headers are prerequisites: a suite that reports OK against a stale binary is worse than none.
HDR = $(wildcard $(C)/*/include/*.h $(C)/*/*.h)

.PHONY: test clean
test: run_tests
	./run_tests

# sha256.c is the suite's own: the box's notice fixtures name their pixels and sound by digest.
run_tests: tests.c sha256.c sha256.h $(SRC) $(HDR)
	$(CC) $(CFLAGS) -DEMOJI_HOST_TEST -o $@ tests.c sha256.c $(SRC) -lm

clean:
	rm -f run_tests
