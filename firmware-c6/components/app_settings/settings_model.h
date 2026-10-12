/* What each Settings card says, apart from drawing it. Pure C, host-tested. The clamps
 * themselves are svc_settings' (the one authority for every writer); this is the glass's half:
 * the slider's step, which control is live when, and the copy. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_box.h"
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

/* The Auto-rotate row's line. The switch is stored now; turning the content arrives with the
 * auto-rotate wave (plan §2b), and until then the line says so. */
const char *settings_rotate_note(bool on);
