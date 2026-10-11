/* Included by LVGL's lv_assert.h (CONFIG_LV_ASSERT_HANDLER_INCLUDE), after lv_conf_internal.h has
 * set the default handler, `while(1);`. A failed allocation would then spin the UI task forever,
 * sharing the CPU with the update loop. A trap is a panic instead, and the boot after a panic
 * leaves the display off (svc_display/disp_guard.c), so a fault in the display path costs one
 * reboot, never the panel's ability to take the next image. */
#pragma once

#undef LV_ASSERT_HANDLER
#define LV_ASSERT_HANDLER __builtin_trap();
