#include "settings_model.h"

#include <stdio.h>
#include <string.h>

#include "svc_settings.h"

int32_t settings_volume_from_slider(int32_t raw)
{
    int32_t v = (raw + VOLUME_STEP / 2) / VOLUME_STEP * VOLUME_STEP;
    return v < VOLUME_FLOOR ? VOLUME_FLOOR : v > VOLUME_MAX ? VOLUME_MAX : v;
}

static bool starts(const char *s, const char *p)
{
    return strncmp(s, p, strlen(p)) == 0;
}

const char *settings_jerv_note(bool tts, const char *audio_state, bool *speaker)
{
    *speaker = false;
    if (starts(audio_state, "failed:"))
        return "Sound did not start, so holding KEY cannot record. The box's snapshot says which "
               "step failed.";
    if (starts(audio_state, "off")) return "Sound is off this boot: the last one ended in a fault.";
    if (starts(audio_state, "speaker-only:"))
        return "The microphone did not start, so holding KEY cannot record.";
    if (starts(audio_state, "mic-only:")) return "The speaker did not start: replies are text only.";
    *speaker = true;
    return tts ? "Replies are spoken and shown as text." : "Replies are text only.";
}

int32_t settings_brightness_from_slider(int32_t raw)
{
    int32_t snapped = (raw + BRIGHTNESS_STEP / 2) / BRIGHTNESS_STEP * BRIGHTNESS_STEP;
    return svc_settings_clamp(SET_BRIGHTNESS, snapped);
}

bool settings_wifi_armed(net_state_t state, bool armed, uint32_t armed_ms, uint32_t now_ms)
{
    return state == NET_CONNECTED && armed && (uint32_t)(now_ms - armed_ms) < WIFI_ARM_MS;
}

wifi_tap_t settings_wifi_tap(net_state_t state, bool armed, uint32_t armed_ms, uint32_t now_ms)
{
    switch (state) {
    case NET_CONNECTED:
        return settings_wifi_armed(state, armed, armed_ms, now_ms) ? WIFI_TAP_DISCONNECT
                                                                   : WIFI_TAP_ARM;
    case NET_OFF: return WIFI_TAP_CONNECT;
    default: return WIFI_TAP_NONE;
    }
}

static const char *const OFF_NOTE =
    "Off until you connect or restart. No messages, Jerv or updates while off.";

void settings_wifi(const net_info_t *n, bool armed, settings_wifi_t *out)
{
    memset(out, 0, sizeof *out);
    switch (n->state) {
    case NET_CONNECTED:
        out->state = "Connected";
        out->button = armed ? "Tap again to disconnect" : "Disconnect";
        out->kind = armed ? WIFI_BTN_ARMED : WIFI_BTN_SECONDARY;
        out->button_live = true;
        out->note = "Saved when the panel was flashed from the box. Change the network there.";
        out->bars = n->rssi >= -50 ? 4 : n->rssi >= -60 ? 3 : n->rssi >= -70 ? 2 : 1;
        break;
    case NET_CONNECTING:
        out->state = "Connecting…";
        out->button = "Connecting…";
        out->note = OFF_NOTE;
        break;
    default:
        out->state = "Off";
        out->button = "Connect";
        out->button_live = true;
        out->kind = WIFI_BTN_PRIMARY;
        out->note = OFF_NOTE;
        break;
    }
}

void settings_fw(const fw_info_t *fw, bool online, settings_fw_t *out, char *note, size_t note_len)
{
    memset(out, 0, sizeof *out);
    bool busy = fw->phase == FW_CHECKING || fw->phase == FW_DOWNLOADING || fw->phase == FW_RESTARTING;
    out->button_live = online && !busy;
    out->button = fw->phase == FW_CHECKING      ? "Checking…"
                  : fw->phase == FW_DOWNLOADING ? "Downloading…"
                                                : "Update now";
    out->show_bar = fw->phase == FW_DOWNLOADING;
    out->done = fw->phase == FW_DONE;
    out->note = note;
    /* A download already under way finishes whatever the radio does next; say what it is. */
    if (!online && !busy) {
        snprintf(note, note_len, "Connect Wi-Fi to check for updates.");
        return;
    }
    switch (fw->phase) {
    case FW_CHECKING: snprintf(note, note_len, "Asking the box…"); break;
    case FW_DOWNLOADING: snprintf(note, note_len, "Downloading %s · %u%%", fw->next, fw->pct); break;
    case FW_RESTARTING: snprintf(note, note_len, "Restarting into %s…", fw->next); break;
    case FW_DONE:
        snprintf(note, note_len, "Updated. A version that fails to start rolls back by itself.");
        break;
    case FW_UPTODATE: snprintf(note, note_len, "Already on the newest version."); break;
    case FW_FAILED:
        snprintf(note, note_len, "Update failed (%s). The panel stays on %s.",
                 fw->err[0] ? fw->err : "unknown", fw->version);
        break;
    default:
        snprintf(note, note_len,
                 "Asks the box for newer firmware now, instead of waiting for the next check.");
    }
}

void settings_power(const power_info_t *p, settings_power_t *out)
{
    memset(out, 0, sizeof *out);
    if (!p->fitted) {
        out->title = "Power";
        snprintf(out->big, sizeof out->big, "USB");
        snprintf(out->line, sizeof out->line, "No battery fitted · USB %u.%02u V", p->usb_mv / 1000,
                 p->usb_mv % 1000 / 10);
        return;
    }
    out->title = "Battery";
    out->bar = true;
    out->low = p->pct <= 15;
    snprintf(out->big, sizeof out->big, "%u%%", p->pct);
    const char *how = p->charging ? "Charging from USB"
                      : out->low  ? "On battery — plug in soon"
                      : p->usb    ? "On USB, not charging"
                                  : "On battery";
    snprintf(out->line, sizeof out->line, "%u.%02u V · %s", p->mv / 1000, p->mv % 1000 / 10, how);
}

void settings_notif_note(bool accept, bool quiet, uint16_t from, uint16_t to, char *buf, size_t len)
{
    if (!accept)
        snprintf(buf, len, "Off: every message is refused at the box. The sender is told this "
                           "panel is not accepting messages.");
    else if (quiet)
        snprintf(buf, len,
                 "Refused %02u:%02u–%02u:%02u — the sender is told quiet hours, and until when. "
                 "Same hours as the night clock.",
                 from / 60, from % 60, to / 60, to % 60);
    else
        snprintf(buf, len, "Jerv, Ops and workflows can pop up a message here and ask a question "
                           "you answer with a tap.");
}

bool settings_notif_row_live(int row, bool accept)
{
    return row == 0 || accept;
}

const char *settings_rotate_note(bool on, bool available)
{
    if (!available) return "The tilt sensor did not start, so the screen stays upright";
    return on ? "Follows which way is down" : "Locked upright";
}

void calib_flow_start(calib_flow_t *f)
{
    *f = (calib_flow_t){0};
}

void calib_target(uint8_t i, int16_t *x, int16_t *y)
{
    static const int16_t T[CAL_TARGETS][2] = {
        {CAL_NEAR, CAL_NEAR}, {CAL_FAR, CAL_NEAR}, {CAL_FAR, CAL_FAR}, {CAL_NEAR, CAL_FAR}};
    if (i >= CAL_TARGETS) i = CAL_TARGETS - 1;
    *x = T[i][0];
    *y = T[i][1];
}

static int32_t d2(int32_t ax, int32_t ay, int32_t bx, int32_t by)
{
    return (ax - bx) * (ax - bx) + (ay - by) * (ay - by);
}

calib_step_t calib_flow_tap(calib_flow_t *f, int16_t x, int16_t y)
{
    if (f->target >= CAL_TARGETS) return CALF_DONE;
    int16_t tx, ty;
    calib_target(f->target, &tx, &ty);
    if (d2(x, y, tx, ty) > CAL_MISS_PX * CAL_MISS_PX) return CALF_MISSED;
    f->x[f->taps] = x;
    f->y[f->taps] = y;
    if (++f->taps < CAL_TAPS) return CALF_COUNTED;
    int32_t sx = 0, sy = 0;
    for (int i = 0; i < CAL_TAPS; i++) {
        sx += f->x[i];
        sy += f->y[i];
    }
    const int16_t mx = (int16_t)((sx + CAL_TAPS / 2) / CAL_TAPS), my = (int16_t)((sy + CAL_TAPS / 2) / CAL_TAPS);
    f->taps = 0;
    for (int i = 0; i < CAL_TAPS; i++)
        if (d2(f->x[i], f->y[i], mx, my) > CAL_AGREE_PX * CAL_AGREE_PX) return CALF_DISAGREED;
    f->done[f->target] = (calib_pair_t){tx, ty, mx, my};
    return ++f->target < CAL_TARGETS ? CALF_NEXT : CALF_DONE;
}

const char *calib_flow_title(calib_step_t last)
{
    switch (last) {
    case CALF_MISSED: return "Tap the centre of the cross";
    case CALF_DISAGREED: return "Those taps disagreed: again";
    default: return "Tap the cross";
    }
}

void calib_flow_line(uint8_t i, char *out, size_t cap)
{
    const unsigned at = i < CAL_TARGETS ? i + 1u : CAL_TARGETS;
    snprintf(out, cap, "%u of %u · BOOT cancels", at, (unsigned)CAL_TARGETS);
}

void settings_calib_when(uint32_t saved, uint32_t today, char *out, size_t cap)
{
    static const char *const MON[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    const unsigned mon = saved / 100u % 100u, day = saved % 100u;
    if (!saved || mon < 1 || mon > 12) {
        snprintf(out, cap, "%s", saved ? "Calibrated" : "Never");
        return;
    }
    if (saved == today) {
        snprintf(out, cap, "Today");
        return;
    }
    snprintf(out, cap, "%u %s", day, MON[mon - 1]);
}

const char *settings_calib_result(calib_result_t r)
{
    switch (r) {
    case CALIB_SAVED: return "Calibration saved";
    case CALIB_NOT_SAVED: return "Calibrated, but not saved: it lasts until a restart";
    default: return "Not saved: those taps did not agree. Try again";
    }
}
