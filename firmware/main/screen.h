#pragma once

#include <stdbool.h>
#include <stdint.h>

/* WHEN THE SCREEN GOES TO SLEEP, AND HOW FAR.
 *
 * These panels sit on bedside tables. A face at full brightness all night is the reason a
 * child switches the toy off, and a toy that has been switched off is one that misses the
 * morning's message. So the screen fades ITSELF out and comes back on anything that looks
 * like a person: five minutes idle dims it, fifteen puts it dark.
 *
 * DARK IS BRIGHTNESS 0 AND A SKIPPED BLIT — DELIBERATELY NOT `esp_lcd_panel_disp_on_off()`.
 * Switching the display off is one command; switching it back on is the same class of
 * command this controller has already been caught dropping — `reassert_panel()` exists, on a
 * timer, because of exactly that. A sleep whose wake path is the operation most likely to
 * fail turns a power saving into a panel that is dead every morning, and the owner has no
 * terminal to revive it with. Brightness 0 leaves the display initialised and the io handle
 * untouched, so waking is one 0x51 and one frame — both of which the render loop already
 * does thousands of times an hour.
 *
 * The policy lives here rather than in `display.c` for the reason `orient.c` does: a rule
 * about time and thresholds that is only ever reasoned about is how the first orientation
 * shipped backwards (§10.4). It is pure arithmetic, so the host suite can hold it to the
 * properties the comments claim.
 */

typedef enum {
    SCREEN_AWAKE = 0,
    SCREEN_DIM,  /* still visible, no longer lighting the room */
    SCREEN_DARK, /* brightness 0, and the render loop stops blitting */
} screen_stage_t;

#define SCREEN_DIM_MS (5u * 60u * 1000u)
#define SCREEN_DARK_MS (15u * 60u * 1000u)

/* One poll in three while dark. Wake latency is the whole cost of the slower cadence, and
   120 ms between a finger and a face is under what a hand notices. The render loop's
   accumulators all run on the interval this returns rather than on a constant, so gesture
   timings, the panel reassert and the PMU sample stay in real time at either rate. */
#define SCREEN_POLL_MS 120

/* Raw accelerometer counts, summed over three axes, sample to sample, where 1 g is about
   8192 (`imu.h`) — so roughly 0.11 g: far above the tens of counts a resting panel jitters
   by, far below a hand lifting it. A TUNING KNOB, and an honest one: this number has never
   met a bedside table. Every wake it causes is logged WITH its magnitude, so correcting it
   means reading the box's log rather than guessing a second time. */
#define SCREEN_MOVE_COUNTS 900

/* Dim is a quarter of whatever the box configured, floored so it cannot round away to off:
   the point of the first stage is that the pet is still VISIBLE. */
#define SCREEN_DIM_SHIFT 2
#define SCREEN_DIM_FLOOR 8

/* How far gone the screen should be after this long with nothing happening. */
screen_stage_t screen_stage(uint32_t idle_ms);

/* Should the pet be ASLEEP on screen — eyes shut, zzz — rather than merely dimmer?
 *
 * Dim was only ever a backlight level, so the pet carried on with its idle loop at a quarter
 * brightness and read as neither awake nor asleep. The owner asked for the pose.
 *
 * DIM ONLY. At DARK the render loop stops blitting, so there is nothing to draw and no frames
 * to spend; the zzz lives in the five-to-fifteen-minute window and the night is simply dark.
 *
 * It yields to a live conversation, and that is not a detail: voice no longer resets the idle
 * timer, so the screen CAN be dim while the pet answers something asked of it — and a pet that
 * replied with its eyes shut would look broken rather than sleepy.
 *
 * Here rather than in `display.c` because this file is the sleep policy and is host-tested,
 * which is the whole reason it was split out. */
bool screen_dozing(screen_stage_t stage, bool conversing, bool speaking);

/* What the panel should be showing, as against the brightness the BOX asked for — which has
   to survive a night's sleep unchanged, because it is a setting and this is a mood. */
uint8_t screen_level(uint8_t configured, screen_stage_t stage);

/* MOVEMENT IS THE CHANGE, NOT THE TILT. A panel lying at an angle is still a panel nobody is
   touching, so this is a sample-to-sample difference — which is also why it cannot reuse
   `orient_quarter()`, whose whole job is to be STABLE under exactly the small changes being
   looked for here. Returns the magnitude in raw counts; compare against SCREEN_MOVE_COUNTS
   with `screen_moved()` so the log can print what it nearly was. */
int screen_motion(const int16_t prev[3], const int16_t cur[3]);
bool screen_moved(int magnitude);
