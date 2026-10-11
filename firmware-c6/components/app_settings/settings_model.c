#include "settings_model.h"

#include <stdio.h>
#include <string.h>

#include "svc_settings.h"

int32_t settings_brightness_from_slider(int32_t raw)
{
    int32_t snapped = (raw + BRIGHTNESS_STEP / 2) / BRIGHTNESS_STEP * BRIGHTNESS_STEP;
    return svc_settings_clamp(SET_BRIGHTNESS, snapped);
}

void settings_wifi(const net_info_t *n, settings_wifi_t *out)
{
    memset(out, 0, sizeof *out);
    switch (n->state) {
    case NET_CONNECTED:
        out->state = "Connected";
        out->button = "Disconnect";
        out->button_live = true;
        out->note = "Saved when the panel was flashed from the box. Change the network there.";
        out->bars = n->rssi >= -50 ? 4 : n->rssi >= -60 ? 3 : n->rssi >= -70 ? 2 : 1;
        break;
    case NET_CONNECTING:
        out->state = "Connecting...";
        out->button = "Connecting...";
        out->note = "Off until you connect or restart. No messages, Jerv or updates while off.";
        break;
    default:
        out->state = "Off";
        out->button = "Connect";
        out->button_live = true;
        out->primary = true;
        out->note = "Off until you connect or restart. No messages, Jerv or updates while off.";
        break;
    }
}

void settings_fw(const fw_info_t *fw, bool online, settings_fw_t *out, char *note, size_t note_len)
{
    memset(out, 0, sizeof *out);
    bool busy = fw->phase == FW_CHECKING || fw->phase == FW_DOWNLOADING || fw->phase == FW_RESTARTING;
    out->button_live = online && !busy;
    out->button = fw->phase == FW_CHECKING      ? "Checking..."
                  : fw->phase == FW_DOWNLOADING ? "Downloading..."
                                                : "Update now";
    out->show_bar = fw->phase == FW_DOWNLOADING;
    out->done = fw->phase == FW_DONE;
    out->note = note;
    if (!online) {
        snprintf(note, note_len, "Connect Wi-Fi to check for updates.");
        return;
    }
    switch (fw->phase) {
    case FW_CHECKING: snprintf(note, note_len, "Asking the box..."); break;
    case FW_DOWNLOADING: snprintf(note, note_len, "Downloading %s - %u%%", fw->next, fw->pct); break;
    case FW_RESTARTING: snprintf(note, note_len, "Restarting into %s...", fw->next); break;
    case FW_DONE:
        snprintf(note, note_len, "Updated. A version that fails to start rolls back by itself.");
        break;
    case FW_UPTODATE: snprintf(note, note_len, "Already on the newest version."); break;
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
        snprintf(out->line, sizeof out->line, "No battery fitted - USB %u.%02u V", p->usb_mv / 1000,
                 p->usb_mv % 1000 / 10);
        return;
    }
    out->title = "Battery";
    out->bar = true;
    out->low = p->pct <= 15;
    snprintf(out->big, sizeof out->big, "%u%%", p->pct);
    const char *how = p->charging ? "Charging from USB" : out->low ? "On battery - plug in soon" : "On battery";
    snprintf(out->line, sizeof out->line, "%u.%02u V - %s", p->mv / 1000, p->mv % 1000 / 10, how);
}

void settings_notif_note(bool accept, bool quiet, uint16_t from, uint16_t to, char *buf, size_t len)
{
    if (!accept)
        snprintf(buf, len, "Off: every message is refused at the box. The sender is told this "
                           "panel is not accepting messages.");
    else if (quiet)
        snprintf(buf, len,
                 "Refused %02u:%02u-%02u:%02u - the sender is told quiet hours, and until when. "
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
