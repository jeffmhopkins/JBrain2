#pragma once

#include <stdbool.h>

/* Bring up the AMOLED and draw the test pattern. NEVER fails fatally: a display fault must
   not stop this unit reaching the box, because reaching the box is how the fault gets
   fixed. Returns whether the panel came up; the caller logs it and carries on either way. */
bool display_start(void);

/* Start the render loop: the robot, and a tap that changes its colour. Its own task, so the
   OTA loop's timing is not something a frame rate can disturb. */
void display_run_face(void);

/* The loudest microphone sample since this was last called, 0..32767; reading clears it.
   Reported in telemetry so the microphone can be confirmed from the box rather than by
   asking someone to describe a sound. */
int display_mic_peak(void);

/* The panel's 0x51 brightness, 0..255. Also becomes what the periodic re-assert re-sends, so
   a setting cannot be quietly undone thirty seconds later by the recovery write. */
void display_set_brightness(int level);

/* The face task's smallest observed stack headroom, in words. Reported in telemetry so a
   near-overflow shows up as a shrinking number rather than as a panic. */
int display_stack_free(void);

/* The render loop stage reached just before the last restart, or -1 on a cold boot.
   1 loop top, 2 touch, 3 beep, 4 stack probe, 5 IMU, 6 face_draw, 7 label, 8 flip,
   9 full blit, 10 mic read, 11 meter blit, 12 panel re-assert, 13 PMU sample. */
/* Whether the panel got a real HARDWARE reset at boot (`pmu_reset_panel`). False means the
   expander did not answer and the CO5300 was initialised with only a software reset — the
   state every build before this one shipped in, and the one the post-OTA black screen lives
   in. Reported so the box can tell those two boots apart. */
bool display_panel_reset(void);

int display_crash_phase(void);

/* THE MICROPHONE METER IS A DEBUG TOOL NOW, not furniture. It earned its place during
   bring-up — a microphone has no symptom, so a bar that is always running answers "is it
   hearing anything" at a glance, and it is how the first real decode was confirmed to be a
   voice rather than a number. Bring-up is over, and what it buys now is a green bar down the
   edge of a pet in a four-year-old's bedroom. Switched from the box, so the next time the
   microphone goes quiet the fastest answer in the building is one setting away rather than a
   rebuild. */
void display_set_debug_overlay(bool on);

/* Frames that reached the glass and frames that did not, since the last recovery. The render
   heartbeat says this on the console every ten seconds; this is the same two numbers for a
   panel with no console, which is the only kind there will be from now on. */
void display_blit_counts(int *ok, int *fail);

/* The same story WITHOUT the amnesia. The pair above is "since the last recovery", so a panel
   that failed 249 blits in a row, self-healed and has drawn cleanly since reports exactly what
   a panel that never faltered reports. These three are monotonic: total failures, how many
   times it recovered, and the meter's own failures — which reached no counter at all until
   now, on the transfer that happens FIVE TIMES more often than the face's. */
void display_blit_totals(int *fail_total, int *recoveries, int *meter_fail);

/* WHY THE PANEL RESTARTED ITSELF, across the restart.
 *
 * Three unrelated callers reach `esp_restart()` and all three arrive at the box as
 * `reset_reason: "sw(3)"`, with two of them also sharing `PHASE(9)`. So a self-heal after 250
 * failed blits — a real fault — is indistinguishable from a four-year-old doing the reboot
 * gesture. Each caller now says which it was, into an RTC word that was already there and
 * already dead, and the next boot reports it once and forgets it. "" when the panel did not
 * restart itself (a power cycle, a crash, a fresh flash). */
typedef enum {
    DISPLAY_RESTART_NONE = 0,
    DISPLAY_RESTART_BLIT_HEAL,
    DISPLAY_RESTART_GESTURE,
    DISPLAY_RESTART_OTA_PARK,
} display_restart_t;
void display_note_restart(display_restart_t why);
const char *display_restart_reason(void);

/* PARK THE RENDERER, THEN RESTART — the reboot an OTA must use.
 *
 * An `esp_restart()` from any other task cuts a QSPI pixel transfer in half, and the CO5300
 * keeps the half it got: it is still waiting for the rest of a memory-write when the chip
 * comes back, so the next boot's init bytes are consumed as pixel data and the panel never
 * initialises. That is the black screen after every over-the-air update, and it is why the
 * driver's software reset does not rescue it — 0x01 is eaten as a parameter like everything
 * else. The renderer meanwhile queues frames into a controller that is not listening, which
 * is why telemetry reads `blit_ok` climbing with `blit_fail` at zero.
 *
 * The reboot GESTURE never had the fault, and that is the whole proof: it restarts from
 * inside the render loop, after the frame has gone out, with nothing in flight.
 *
 * So this asks the render loop to restart at that same point. It returns immediately; the
 * caller waits, and must keep its own timeout, because a renderer that has died cannot be
 * allowed to strand a panel on an image it has already installed. */
void display_request_restart(void);

/* HOW MANY TIMES THE BOOT BUTTON HAS BEEN PRESSED SINCE BOOT, and why that is a question.
 *
 * The owner: *"there are two switches on this board, one labeled power, one labeled boot. Can
 * we utilize those to basically turn off the microphone with one of them?"*
 *
 * A mute switch is worth having and this firmware has never read either button, so the first
 * thing needed is which of them it CAN read. BOOT is GPIO0 on every ESP32-S3 board there is —
 * but "every board there is" is not this board, and the plan's own history is full of pin maps
 * that were obvious and wrong (`audio.c`'s header opens with two pins named from opposite ends
 * of the link, where guessing gives silence AND a dead microphone with no error from either).
 *
 * So this counts edges rather than assuming them. Press the button a few times, read the count
 * out of telemetry, and the question is answered by the panel instead of by me. A count rather
 * than a level because the button is momentary and telemetry is every fifteen minutes.
 *
 * PWR is almost certainly not a GPIO at all — it goes to the AXP2101, whose latched PWRON bits
 * are in registers `pmu.c` does not sample. That is the next probe if this one comes back
 * alive and one button is not enough. */
int display_boot_presses(void);

/* Where the last tap landed and which zone it was classified as. Reported so the touch
   controller's orientation is a measurement rather than an assumption — see display.c. */
void display_last_tap(int *x, int *y, int *zone);
