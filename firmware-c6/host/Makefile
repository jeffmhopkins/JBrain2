# The panel's pure-C half, built and run on the host: every decision the shell makes, every
# app model, the notice policy, and the debug surface (redaction, commands, log ring, the
# screenshot codec). None of it may include LVGL or ESP-IDF — this link is what checks that.
CC ?= cc
# gnu11, not c11: ESP-IDF builds this code as gnu17, and a stricter host dialect would test a
# language the device never compiles (firmware/host/Makefile has the same rule and the reason).
C := ../components
INC := $(addprefix -I,$(wildcard $(C)/*/include)) -I$(C)/app_clock -I$(C)/app_settings \
       -I$(C)/app_thread
CFLAGS ?= -std=gnu11 -O1 -g -Wall -Wextra -Werror $(INC)

SRC = $(C)/app_api/dbg_json.c $(C)/app_api/app_event.c \
      $(C)/shell/shell_core.c $(C)/shell/shell_evq.c \
      $(C)/app_clock/clock_model.c $(C)/app_settings/settings_model.c \
      $(C)/app_thread/thread_model.c $(C)/convo/convo_model.c $(C)/ui_kit/ui_pin.c \
      $(C)/svc_siblings/sib_logic.c $(C)/svc_siblings/svc_siblings.c \
      $(C)/svc_siblings/sib_port_fake.c \
      $(C)/svc_notice/notice_policy.c $(C)/svc_notice/svc_notice_fake.c \
      $(C)/svc_audio/talk_policy.c $(C)/svc_audio/talk_core.c \
      $(C)/svc_audio/svc_audio_talk.c $(C)/svc_audio/svc_audio_fake.c \
      $(C)/svc_settings/svc_settings_fake.c \
      $(C)/svc_settings/settings_store.c $(C)/svc_rtc/time_logic.c \
      $(C)/svc_rtc/svc_rtc_fake.c $(C)/svc_net/svc_net_fake.c $(C)/svc_box/svc_box_fake.c \
      $(C)/svc_box/box_logic.c $(C)/svc_messages/svc_messages_fake.c \
      $(wildcard $(C)/svc_debug/*.c) \
      $(C)/svc_display/disp_logic.c $(C)/svc_display/disp_guard.c \
      $(C)/svc_power/axp_logic.c $(C)/svc_buttons/btn_logic.c \
      $(C)/svc_touch/tp_logic.c
# Headers are prerequisites: a suite that reports OK against a stale binary is worse than none.
HDR = $(wildcard $(C)/*/include/*.h $(C)/*/*.h)

.PHONY: test clean
test: run_tests
	./run_tests

run_tests: tests.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ tests.c $(SRC)

clean:
	rm -f run_tests
