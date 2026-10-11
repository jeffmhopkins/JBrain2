/* The CO5300 driver's device-side controls, beside the app-facing svc_display.h. Every call
 * here that touches the panel runs on the UI task, the ONE owner of the panel io handle
 * (firmware/README.md "One task owns the codec, one owns the panel"). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* UI task, after lv_init. Resets the panel through ALDO3, opens the QSPI bus, sends the vendor
 * init table and registers the LVGL display. False with `*failed_step` naming the step that
 * failed; the panel is then left alone for the rest of the boot. */
bool svc_display_start(const char **failed_step);

/* UI task, between frames: applies a recorded brightness. Never while a pixel transfer is in
 * flight — on the S3's CO5300, a framed command arriving mid-frame took the renderer down. */
void svc_display_service(void);

/* False once a flush failed: the UI task stops drawing rather than queue onto a stuck bus. */
bool svc_display_ok(void);

/* This boot leaves the panel off (disp_guard.h); telemetry says so. */
void svc_display_mark_skipped(void);

/* `display=… frames=… flush_ms=… flush_err=… bri=req/sent/state lcdrst=…` — telemetry-safe. */
void svc_display_note(char *out, size_t len);
