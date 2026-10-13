# The pure-C half of the firmware, built and run on the host.
#
# `face.c`, `font.c`, `emotion.c`, `rig.c` and `variants.c` have no ESP dependencies — that was
# always a stated property and nothing ever checked it. This Makefile is the check: if someone
# includes `esp_log.h` in one of them, the link fails here before CI ever reaches the panel.
#
# It is also the only place the firmware has assertions at all. The device has no test harness
# (no hardware in CI), so anything that CAN be tested on a host should be, and the rig's
# selection logic — cooldowns, weights, the repetition penalty — is exactly that kind of code.

CC ?= cc
# gnu11, not c11: ESP-IDF builds this code with -std=gnu17, and a host build in a STRICTER
# dialect tests a language the device never compiles. (Concretely, M_PI is a GNU extension —
# under -std=c11 every trig call here fails to build while the firmware is fine.)
CFLAGS ?= -std=gnu11 -O1 -g -Wall -Wextra -Werror -I../main
LDLIBS = -lm
# `panel_sm.c` is in this list for the reason the header comment gives: it is the panel's state
# machine, it is supposed to be free of ESP-IDF, and the link is what checks that. It is built
# into BOTH binaries so the claim is tested even by the suite that does not use it.
SRC = ../main/cue.c ../main/orient.c ../main/screen.c ../main/poop.c ../main/tickle.c ../main/cadence.c ../main/reach.c ../main/confirm.c ../main/ring.c ../main/face.c ../main/font.c ../main/emotion.c ../main/rig.c ../main/variants.c ../main/gesture.c ../main/calib.c ../main/caption.c ../main/overlay.c ../main/vocab.c ../main/wsproto.c ../main/panel_sm.c ../main/stream.c
# HEADERS ARE PREREQUISITES TOO. They were not, and the cost was a green run against a stale
# binary: a constant moved into `face.h`, `make` saw no newer .c file and re-ran the previous
# build. A suite that can report OK without having compiled the change is worse than no suite.
HDR = $(wildcard ../main/*.h)

# THE SUITES, ALL RUN BY `make test`, and the split is by what they need rather than by taste.
# `run_tests` measures pure functions: given these numbers, what comes out. `run_sm_tests` drives
# a STATE MACHINE across frames — a press, a clock advanced, an overlay arbitrated — so it needs a
# harness (a fake speaker, a fake queue, a framebuffer) that the first suite would only be
# slowed down by. `run_stream_tests` steps the speaker's arbiter (`stream.c`) one 40 ms tick at a
# time against a fake clock. Sequential, not parallel: a suite whose failure output interleaves with
# another's is a suite nobody reads.
.PHONY: test clean check_no_logic_in_audio check_no_logic_in_display test_update_check_precedes_task_starts test_wake_press_starts_no_swipe test_codec_written_only_by_audio_task test_stack_sampled_before_first_report
test: run_tests run_sm_tests run_link_tests run_stream_tests run_jpanel_tests run_e2e_tests check_no_logic_in_audio check_no_logic_in_display test_update_check_precedes_task_starts test_wake_press_starts_no_swipe test_codec_written_only_by_audio_task test_stack_sampled_before_first_report
	./run_tests
	./run_sm_tests
	./run_link_tests
	./run_stream_tests
	./run_jpanel_tests
	./run_e2e_tests

run_tests: tests.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ $(filter-out %.h,$^) $(LDLIBS)

run_sm_tests: sm_tests.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ $(filter-out %.h,$^) $(LDLIBS)

# `link.c`'s deadline arithmetic (plan §7.5a): its own binary, since it needs only `wsproto.c`.
run_link_tests: link_tests.c ../main/wsproto.c $(HDR)
	$(CC) $(CFLAGS) -o $@ $(filter-out %.h,$^) $(LDLIBS)

# The arbiter's suite builds `stream.c` with its interleave hooks, so the seqlock test can stop
# render half-way through a slot write; the device build never defines them.
run_stream_tests: stream_tests.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -DSTREAM_TEST_HOOKS -o $@ $(filter-out %.h,$^) $(LDLIBS)

# `audio.c` IS CODEC I/O AND NOTHING ELSE (plan §7.4): every decision about what the speaker says
# lives in `stream.c`, where this suite can reach it. A grep, because the failure it guards is a
# quiet one — a shortcut added on the audio task works on the device and is invisible here. No
# ring arithmetic, no clip buffer, no cue rendering, no slot or render-side stream call, and no
# field of the speaker state; comments included, so nobody argues about what a mention means.
AUDIO_FORBIDDEN = ring_|s_play|cue_render|\b(stream_cue|stream_cue_play|stream_silence|stream_stop|stream_reply|stream_pause_req|stream_begin|stream_write|stream_end)\(|STREAM_SLOT_(PRESS|SEND_RESULT|FAIL|REFUSAL|CHIME)|(\.|->)(stop|silence|slot|buf|pause_req|render_seq|variant|w|word|classified_gen|reply_[a-z]+|r|seen_w|out|arb_hb|busy|done|cue_expired|arbiter|a_[a-z_]+|primed|deaf|out_samples|snd[a-z_]*|elig_[a-z]+|lease|ring|play|play_pos|play_len)\b
check_no_logic_in_audio:
	@if grep -nE '$(AUDIO_FORBIDDEN)' ../main/audio.c; then \
		echo "audio.c: the lines above belong in stream.c (check_no_logic_in_audio)"; exit 1; \
	else echo "check_no_logic_in_audio: OK"; fi

# THE ENGINE AGAINST A SCRIPTED BOX (plan §10). Its own source list, not SRC: the fake arbiter
# stands in for `stream.c`, and both would define the same symbols. The end-to-end suite links the
# real one. -pthread for the seqlock's concurrent-reader test.
JPANEL_SRC = ../main/jpanel_core.c ../main/reach.c fake_stream.c
run_jpanel_tests: jpanel_tests.c $(JPANEL_SRC) fake_stream.h $(HDR)
	$(CC) $(CFLAGS) -pthread -o $@ $(filter-out %.h,$^) $(LDLIBS)

# THE THREE TOGETHER (plan §8.5): the machine, the real engine and the real arbiter, wired the way
# `display.c` wires them, against `jpanel_tests.c`'s scripted box (included whole, minus its
# `main`). Its own source list for the reason `run_jpanel_tests` has one: this links the real
# `stream.c` where that one links the fake.
E2E_SRC = ../main/jpanel_core.c ../main/stream.c ../main/panel_sm.c ../main/cue.c ../main/ring.c ../main/reach.c ../main/confirm.c ../main/face.c ../main/font.c ../main/emotion.c ../main/rig.c ../main/gesture.c
run_e2e_tests: e2e_tests.c jpanel_tests.c $(E2E_SRC) fake_stream.h $(HDR)
	$(CC) $(CFLAGS) -pthread -o $@ e2e_tests.c $(E2E_SRC) $(LDLIBS)

# `display.c` SAMPLES, EXECUTES AND DRAWS (plan §8.1): no static that duplicates the machine's
# state, no engine or speaker call outside the executor's list. The script says how it reads both.
check_no_logic_in_display:
	@./check_no_logic_in_display.sh ../main

# `app_main` checks for an update before it starts a single task (plan §6a, gate 4).
test_update_check_precedes_task_starts:
	@./test_update_check_precedes_task_starts.sh ../main

# A press that wakes a dark screen forgets the last press's point, so it cannot start a swipe.
test_wake_press_starts_no_swipe:
	@./test_wake_press_starts_no_swipe.sh ../main

# The boot report's stack high-water mark is sampled before the render loop, not at its first beat.
test_stack_sampled_before_first_report:
	@./test_stack_sampled_before_first_report.sh ../main

# Only `audio_task` writes the codec's control registers; the setters other tasks call record.
test_codec_written_only_by_audio_task:
	@./test_codec_written_only_by_audio_task.sh ../main

clean:
	rm -f run_tests run_sm_tests run_link_tests run_stream_tests run_jpanel_tests run_e2e_tests
