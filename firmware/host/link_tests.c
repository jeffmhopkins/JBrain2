/* The deadline arithmetic every `link.c` wait runs on (plan §7.5a, §13). `link.c` itself only
   runs on a panel, so these helpers are the part of its clamping CI can hold to account: if a
   step can outlast what is left, or a clock wrap reads as forever, it shows up here. */

#include <stdio.h>
#include <stdlib.h>

#include "link_types.h"
#include "wsproto.h"

static int checks;

#define CHECK(c, msg)                                                   \
    do {                                                                \
        checks++;                                                       \
        if (!(c)) {                                                     \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg)); \
            exit(1);                                                    \
        }                                                               \
    } while (0)

static void test_link_left_counts_down_to_zero_and_stays_there(void)
{
    CHECK(link_left(1000, 1500) == 500, "half a second left");
    CHECK(link_left(1500, 1500) == 0, "due at the deadline itself");
    CHECK(link_left(1501, 1500) == 0, "and after it, never a huge unsigned remainder");
    CHECK(link_left(0, 0x80000000u) == 0, "a deadline 2^31 away reads as due, not as forever");
}

static void test_link_left_survives_the_wrap(void)
{
    CHECK(link_left(0xFFFFFF00u, 0x00000100u) == 0x200, "a deadline past the wrap is near");
    CHECK(link_left(0x00000010u, 0xFFFFFFF0u) == 0, "a deadline just before the wrap is past");
}

static void test_a_deadline_is_never_zero(void)
{
    /* 0 means "legacy timeout_ms" in a request; a sum that lands there must not be misread. */
    CHECK(link_deadline_in(1000, 500) == 1500, "now plus the budget");
    CHECK(link_deadline_in(0xFFFFFF00u, 0x100) == 1, "moved a millisecond off zero at the wrap");
    CHECK(link_left(0xFFFFFF00u, link_deadline_in(0xFFFFFF00u, 0x100)) == 0x101,
          "and that millisecond is all it costs");
}

static void test_a_legacy_request_runs_on_its_timeout(void)
{
    CHECK(link_req_deadline(1000, 0, 15000) == 16000, "deadline 0 takes timeout_ms");
    CHECK(link_req_deadline(1000, 7000, 15000) == 7000, "a real deadline wins over timeout_ms");
    CHECK(link_left(1000, link_req_deadline(1000, 0, 0)) == 0,
          "a legacy request with no timeout is due at once, not unbounded");
    CHECK(link_left(1000, link_req_deadline(1000, 0, -5)) == 0, "nor with a negative one");
}

static void test_wsp_wait_step_never_outlasts_what_is_left(void)
{
    CHECK(wsp_wait_step(10000, LINK_STEP_MS) == LINK_STEP_MS, "a long wait is cut into steps");
    CHECK(wsp_wait_step(100, LINK_STEP_MS) == 100, "the last step is only what is left");
    CHECK(wsp_wait_step(0, LINK_STEP_MS) == 0, "and a due deadline waits not at all");
}

/* A wait loop the way `link.c` runs one, on a fake clock where every step blocks for its whole
   allowance: the loop must end exactly at the deadline, never past it, and must tick and poll
   its cancel at least every LINK_STEP_MS. */
static void run_wait(uint32_t start, uint32_t budget)
{
    const uint32_t deadline = link_deadline_in(start, budget);
    uint32_t now = start;
    uint32_t last_beat = start;
    int steps = 0;
    while (true) {
        const uint32_t left = link_left(now, deadline);
        if (left == 0) break;
        const uint32_t step = wsp_wait_step(left, LINK_STEP_MS);
        CHECK(step > 0 && step <= LINK_STEP_MS, "every step is bounded");
        now += step;
        steps++;
        CHECK((uint32_t)(now - last_beat) <= LINK_STEP_MS, "no gap between polls exceeds a step");
        if (link_tick_due(now, last_beat)) last_beat = now;
    }
    const uint32_t span = deadline - start;
    CHECK(now == deadline, "the loop ends exactly at the deadline, never past it");
    CHECK(steps == (int)((span + LINK_STEP_MS - 1) / LINK_STEP_MS),
          "in no more steps than the budget needs");
}

static void test_a_wait_loop_ends_at_its_deadline(void)
{
    run_wait(1000, 15000);
    run_wait(1000, 55000);
    run_wait(1000, 75100); /* a SEND head: 60 s + 15 s, plus a remainder smaller than a step */
    run_wait(0xFFFFF000u, 20000); /* across the wrap */
    run_wait(5000, 0);
}

static void test_the_open_split_fits_three_phases(void)
{
    /* esp-tls applies the open's timeout to the TCP connect and to each handshake read, not in
       total; a third each keeps three phases inside what is left. */
    for (uint32_t left = 0; left < 100000; left += 997) {
        const int t = link_open_timeout_ms(left);
        CHECK(t >= 1, "never 0, which esp_http_client reads as its own default");
        if (left >= 3) CHECK((uint32_t)t * 3 <= left, "three phases fit in what is left");
    }
    CHECK(link_open_timeout_ms(15000) == 5000, "a 15 s request opens in 5 s phases");
}

static void test_progress_mode_moves_the_deadline_with_the_upload(void)
{
    const uint32_t first = 1000 + LINK_UP_WAIT_MS + LINK_STALL_MS;
    CHECK(link_after_frame(LINK_DEADLINE_PROGRESS, 5000, first) == 5000 + LINK_STALL_MS,
          "each frame buys LINK_STALL_MS from now");
    CHECK(link_after_frame(LINK_DEADLINE_PROGRESS, 60000, 5000 + LINK_STALL_MS) ==
              60000 + LINK_STALL_MS,
          "so a slow upload that keeps moving is never cut");
    CHECK(link_after_upload(LINK_DEADLINE_PROGRESS, 70000, 90000, 75000) == 145000,
          "the body out, the head gets its own budget");
}

static void test_absolute_mode_leaves_the_deadline_alone(void)
{
    CHECK(link_after_frame(LINK_DEADLINE_ABSOLUTE, 5000, 16000) == 16000, "a frame changes nothing");
    CHECK(link_after_upload(LINK_DEADLINE_ABSOLUTE, 9000, 16000, 75000) == 16000,
          "nor does the end of the upload: talk's 55 s is from the start of the turn");
}

static void test_the_body_stall_bound_moves_with_each_byte(void)
{
    const uint32_t last_byte = 0xFFFFC000u;
    const uint32_t bound = link_deadline_in(last_byte, LINK_STALL_MS);
    CHECK(link_left(last_byte + LINK_STALL_MS - 1, bound) == 1, "not stalled a ms early");
    CHECK(link_left(last_byte + LINK_STALL_MS, bound) == 0, "stalled at LINK_STALL_MS, wrap or not");
}

static void test_a_tick_is_owed_every_step(void)
{
    CHECK(!link_tick_due(1000 + LINK_STEP_MS - 1, 1000), "not before a step has passed");
    CHECK(link_tick_due(1000 + LINK_STEP_MS, 1000), "once a step has");
    CHECK(link_tick_due(100, 0xFFFFFF00u), "across the wrap");
}

static void test_the_clock_constants_hold_together(void)
{
    CHECK(LINK_STEP_MS < LINK_STALL_MS, "a stall spans many steps");
    CHECK(LINK_STEP_MS * 4 <= 1000, "a cancel is seen within a quarter second");
}

int main(void)
{
    test_link_left_counts_down_to_zero_and_stays_there();
    test_link_left_survives_the_wrap();
    test_a_deadline_is_never_zero();
    test_a_legacy_request_runs_on_its_timeout();
    test_wsp_wait_step_never_outlasts_what_is_left();
    test_a_wait_loop_ends_at_its_deadline();
    test_the_open_split_fits_three_phases();
    test_progress_mode_moves_the_deadline_with_the_upload();
    test_absolute_mode_leaves_the_deadline_alone();
    test_the_body_stall_bound_moves_with_each_byte();
    test_a_tick_is_owed_every_step();
    test_the_clock_constants_hold_together();
    printf("link: %d checks OK\n", checks);
    return 0;
}
