/* LVGL for the host sim. Only what differs from LVGL's defaults (lv_conf_internal.h fills in
 * the rest), and chosen to match the device where it matters: RGB565, partial strip
 * rendering, Montserrat. The device's own configuration arrives with the display driver (C2). */
#ifndef LV_CONF_H
#define LV_CONF_H

#define LV_COLOR_DEPTH 16
#define LV_USE_OS LV_OS_NONE
#define LV_MEM_SIZE (512 * 1024)
#define LV_DEF_REFR_PERIOD 33

/* Every widget styles itself from ui_kit's tokens; a theme would be a second source of look. */
#define LV_USE_THEME_DEFAULT 0
#define LV_USE_THEME_SIMPLE 0

#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_MONTSERRAT_18 1
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_MONTSERRAT_22 1
#define LV_FONT_MONTSERRAT_24 1
#define LV_FONT_MONTSERRAT_26 1
#define LV_FONT_MONTSERRAT_28 1
#define LV_FONT_MONTSERRAT_30 1
#define LV_FONT_MONTSERRAT_48 1
#define LV_FONT_DEFAULT &lv_font_montserrat_20

#define LV_USE_LOG 1
#define LV_LOG_LEVEL LV_LOG_LEVEL_WARN
#define LV_LOG_PRINTF 1

#endif
