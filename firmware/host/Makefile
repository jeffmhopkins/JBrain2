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
# `ui.c` is in this list for the reason the header comment gives: it is the panel's interaction
# state machine, it is supposed to be free of ESP-IDF, and the link is what checks that. It is
# built into BOTH binaries so the claim is tested even by the suite that does not use it.
SRC = ../main/cue.c ../main/orient.c ../main/screen.c ../main/cadence.c ../main/confirm.c ../main/ring.c ../main/face.c ../main/font.c ../main/emotion.c ../main/rig.c ../main/variants.c ../main/gesture.c ../main/calib.c ../main/caption.c ../main/vocab.c ../main/ui.c
# HEADERS ARE PREREQUISITES TOO. They were not, and the cost was a green run against a stale
# binary: a constant moved into `face.h`, `make` saw no newer .c file and re-ran the previous
# build. A suite that can report OK without having compiled the change is worse than no suite.
HDR = $(wildcard ../main/*.h)

# TWO SUITES, BOTH RUN BY `make test`, and the split is by what they need rather than by taste.
# `run_tests` measures pure functions: given these numbers, what comes out. `run_ui_tests` drives
# a STATE MACHINE across frames — a press, a clock advanced, an overlay arbitrated — so it needs a
# harness (a fake speaker, a fake queue, a framebuffer) that the first suite would only be
# slowed down by. Sequential, not parallel: a suite whose failure output interleaves with
# another's is a suite nobody reads.
.PHONY: test clean
test: run_tests run_ui_tests
	./run_tests
	./run_ui_tests

run_tests: tests.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ $(filter-out %.h,$^) $(LDLIBS)

run_ui_tests: ui_tests.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ $(filter-out %.h,$^) $(LDLIBS)

clean:
	rm -f run_tests run_ui_tests
