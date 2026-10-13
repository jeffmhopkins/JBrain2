/* What each Settings card says, apart from drawing it. Pure C, host-tested. The clamps
 * themselves are svc_settings' (the one authority for every writer); this is the glass's half:
 * the slider's step, which control is live when, and the copy. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_box.h"
#include "svc_calib.h"
#include "svc_net.h"
#include "svc_power.h"

#define BRIGHTNESS_STEP 5

/* The slider's raw value to a setting: snapped to the step, never below the floor. */
int32_t settings_brightness_from_slider(int32_t raw);
#define VOLUME_STEP 5
int32_t settings_volume_from_slider(int32_t raw);

/* The Jerv card's line, from the read-aloud switch and svc_audio_state(): what a reply will do,
 * or plainly why there is no sound. `speaker` false when replies cannot be spoken at all, so the
 * card hides the switch's volume slider. */
const char *settings_jerv_note(bool tts, const char *audio_state, bool *speaker);

/* Disconnect takes two taps no more than this apart: going offline stops messages, Jerv and
 * updates, so one brush of a finger must not do it (owner, 2026-10-11). */
#define WIFI_ARM_MS 3000

typedef enum { WIFI_TAP_NONE, WIFI_TAP_ARM, WIFI_TAP_DISCONNECT, WIFI_TAP_CONNECT } wifi_tap_t;
/* `armed` with `armed_ms` is the first tap; it lapses after WIFI_ARM_MS. */
wifi_tap_t settings_wifi_tap(net_state_t state, bool armed, uint32_t armed_ms, uint32_t now_ms);
bool settings_wifi_armed(net_state_t state, bool armed, uint32_t armed_ms, uint32_t now_ms);

typedef enum { WIFI_BTN_SECONDARY, WIFI_BTN_PRIMARY, WIFI_BTN_ARMED } wifi_btn_t;

typedef struct {
    const char *state;  /* "Connected" */
    const char *button; /* "Disconnect" */
    bool button_live;
    wifi_btn_t kind;
    const char *note;
    uint8_t bars;       /* 0-4 */
} settings_wifi_t;
void settings_wifi(const net_info_t *n, bool armed, settings_wifi_t *out);

typedef struct {
    const char *note;
    const char *button;
    bool button_live;
    bool show_bar;
    bool done; /* green note */
} settings_fw_t;
/* Update now is dead while offline and while a check or download is running. */
void settings_fw(const fw_info_t *fw, bool online, settings_fw_t *out, char *note, size_t note_len);

typedef struct {
    const char *title; /* "Battery" or "Power" */
    char big[8];       /* "87%" or "USB" */
    char line[48];
    bool bar;
    bool low;          /* 15% and under turns amber */
} settings_power_t;
void settings_power(const power_info_t *p, settings_power_t *out);

/* The Notifications card: what refusing means for the sender, in the owner's words. */
void settings_notif_note(bool accept, bool quiet, uint16_t from, uint16_t to, char *buf, size_t len);
/* With messages refused, the other two switches are moot and are drawn dead. */
bool settings_notif_row_live(int row, bool accept);

/* The Auto-rotate row's line (0.0.18): what the switch does, or, with no tilt sensor, why it is
 * dead. */
const char *settings_rotate_note(bool on, bool available);

/* ---- touch calibration (0.0.18, mock Calib.dc.html) ----
 * A cross at each corner, tapped until three taps agree, BOOT cancelling. The four corners are the
 * mock's outer knots (44 and 436 px in from the glass's edges): far enough apart to fit a scale
 * and a shear, inside the bezel's reach. The fit itself is svc_calib's. */
#define CAL_TARGETS 4
#define CAL_TAPS 3
#define CAL_NEAR 44
#define CAL_FAR 436
/* A tap further than this from its cross is not counted, and says so: a finger that slipped, not
 * a panel that is off (the 0.0.7 mapping is within ~20 px at the corners). */
#define CAL_MISS_PX 64
/* A cross's three taps must agree within this of their mean, or that cross starts again. */
#define CAL_AGREE_PX 24
/* The green cross holds this long before the next one appears. */
#define CAL_NEXT_MS 350

typedef struct {
    uint8_t target, taps;
    int16_t x[CAL_TAPS], y[CAL_TAPS];
    calib_pair_t done[CAL_TARGETS];
} calib_flow_t;

typedef enum {
    CALF_COUNTED,   /* a tap on this cross, more wanted */
    CALF_MISSED,    /* too far from the cross: not counted */
    CALF_DISAGREED, /* the cross's taps did not agree: this cross again */
    CALF_NEXT,      /* this cross done: the next one */
    CALF_DONE,      /* every cross done: `done` holds the pairs for svc_calib_finish */
} calib_step_t;

void calib_flow_start(calib_flow_t *f);
/* Cross `i`, clockwise from the top left. */
void calib_target(uint8_t i, int16_t *x, int16_t *y);
calib_step_t calib_flow_tap(calib_flow_t *f, int16_t x, int16_t y);
/* The prompt over the cross after a step: "Tap the cross", or what went wrong with the last tap. */
const char *calib_flow_title(calib_step_t last);
/* The line under it for cross `i`: "2 of 4 · BOOT cancels". */
void calib_flow_line(uint8_t i, char *out, size_t cap);
/* The Touch screen card's corner: "Never", "Today", or the day it was done ("12 Oct"). */
void settings_calib_when(uint32_t saved_ymd, uint32_t today_ymd, char *out, size_t cap);
/* What the toast says once the fit is done. */
const char *settings_calib_result(calib_result_t r);
