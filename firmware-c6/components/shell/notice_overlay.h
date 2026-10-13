/* The notice overlay (shell-private): the LVGL half of notice_view.h. Built over every app by
 * the shell's one overlay decision (sync_overlays), dropped when it no longer wins. UI task. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"
#include "svc_notice.h"

/* Registers the picture decoder: once, at shell_start. */
void notice_overlay_init(void);
/* (Re)draws `n` as the head of `queued`, on `parent` (the top layer). */
void notice_overlay_build(lv_obj_t *parent, const notice_t *n, uint8_t queued);
void notice_overlay_drop(void);
/* The notice drawn now (0: none) and the count it was drawn with. */
uint32_t notice_overlay_id(void);
uint8_t notice_overlay_count(void);
/* `notice=` details for the shell's snapshot: what is drawn. */
const char *notice_overlay_kind(void);
