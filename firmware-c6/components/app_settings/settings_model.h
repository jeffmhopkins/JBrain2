/* What each Settings card says, apart from drawing it. Pure C, host-tested. The clamps
 * themselves are svc_settings' (the one authority for every writer); this is the glass's half:
 * the slider's step, and which control is live when. */
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

typedef struct {
    const char *state;  /* "Connected" */
    const char *button; /* "Disconnect" */
    bool button_live;
    bool primary;       /* Connect is the action; Disconnect is secondary */
    const char *note;
    uint8_t bars;       /* 0-4 */
} settings_wifi_t;
void settings_wifi(const net_info_t *n, settings_wifi_t *out);

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
