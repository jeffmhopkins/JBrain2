/*******************************************************************************
 * Size: 20 px
 * Bpp: 4
 * Opts: --font <lvgl-v9.2.2>/scripts/built_in_font/Montserrat-Medium.ttf -r 0xB7,0x2013,0x2014,0x2026,0x2192,0x2212 --size 20 --bpp 4 --no-compress --format lvgl --lv-include lvgl.h --lv-font-name ui_glyphs_20 -o ui_glyphs_20.c
 ******************************************************************************/

#ifdef LV_LVGL_H_INCLUDE_SIMPLE
#include "lvgl.h"
#else
#include "lvgl.h"
#endif

#ifndef UI_GLYPHS_20
#define UI_GLYPHS_20 1
#endif

#if UI_GLYPHS_20

/*-----------------
 *    BITMAPS
 *----------------*/

/*Store the image of the glyphs*/
static LV_ATTRIBUTE_LARGE_CONST const uint8_t glyph_bitmap[] = {
    /* U+00B7 "·" */
    0x3c, 0x6a, 0xff, 0x5f, 0x90,

    /* U+2013 "–" */
    0x11, 0x11, 0x11, 0x11, 0x11, 0xff, 0xff, 0xff,
    0xff, 0xff, 0x44, 0x44, 0x44, 0x44, 0x44,

    /* U+2014 "—" */
    0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x11,
    0x11, 0x11, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0x44, 0x44, 0x44, 0x44,
    0x44, 0x44, 0x44, 0x44, 0x44, 0x44,

    /* U+2026 "…" */
    0x7, 0xb2, 0x1, 0xb9, 0x0, 0x4c, 0x50, 0xff,
    0x80, 0x6f, 0xf3, 0xc, 0xfd, 0xa, 0xe4, 0x2,
    0xec, 0x0, 0x6f, 0x70,

    /* U+2192 "→" */
    0x0, 0x0, 0x29, 0x40, 0x0, 0x0, 0x0, 0xb,
    0xe1, 0x0, 0x0, 0x0, 0x2, 0xfb, 0x0, 0x23,
    0x33, 0x33, 0xaf, 0x60, 0xaf, 0xff, 0xff, 0xff,
    0xe0, 0x24, 0x44, 0x44, 0xaf, 0x60, 0x0, 0x0,
    0x1, 0xfb, 0x0, 0x0, 0x0, 0xb, 0xf1, 0x0,
    0x0, 0x0, 0x2a, 0x50, 0x0,

    /* U+2212 "−" */
    0x8a, 0xaa, 0xaa, 0xaa, 0xa0, 0xdf, 0xff, 0xff,
    0xff, 0xf1
};


/*---------------------
 *  GLYPH DESCRIPTION
 *--------------------*/

static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc[] = {
    {.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, .ofs_x = 0, .ofs_y = 0} /* id = 0 reserved */,
    {.bitmap_index = 0, .adv_w = 85, .box_w = 3, .box_h = 3, .ofs_x = 1, .ofs_y = 4},
    {.bitmap_index = 5, .adv_w = 160, .box_w = 10, .box_h = 3, .ofs_x = 0, .ofs_y = 4},
    {.bitmap_index = 20, .adv_w = 320, .box_w = 20, .box_h = 3, .ofs_x = 0, .ofs_y = 4},
    {.bitmap_index = 50, .adv_w = 221, .box_w = 13, .box_h = 3, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 70, .adv_w = 192, .box_w = 10, .box_h = 9, .ofs_x = 1, .ofs_y = 1},
    {.bitmap_index = 115, .adv_w = 186, .box_w = 10, .box_h = 2, .ofs_x = 1, .ofs_y = 6}
};

/*---------------------
 *  CHARACTER MAPPING
 *--------------------*/

static const uint16_t unicode_list_0[] = {
    0x0, 0x1f5c, 0x1f5d, 0x1f6f, 0x20db, 0x215b
};

/*Collect the unicode lists and glyph_id offsets*/
static const lv_font_fmt_txt_cmap_t cmaps[] =
{
    {
        .range_start = 183, .range_length = 8540, .glyph_id_start = 1,
        .unicode_list = unicode_list_0, .glyph_id_ofs_list = NULL, .list_length = 6, .type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY
    }
};

/*-----------------
 *    KERNING
 *----------------*/


/*Map glyph_ids to kern left classes*/
static const uint8_t kern_left_class_mapping[] =
{
    0, 1, 0, 0, 2, 0, 1
};

/*Map glyph_ids to kern right classes*/
static const uint8_t kern_right_class_mapping[] =
{
    0, 1, 0, 0, 2, 0, 1
};

/*Kern values between classes*/
static const int8_t kern_class_values[] =
{
    1, -2, -2, 0
};


/*Collect the kern class' data in one place*/
static const lv_font_fmt_txt_kern_classes_t kern_classes =
{
    .class_pair_values   = kern_class_values,
    .left_class_mapping  = kern_left_class_mapping,
    .right_class_mapping = kern_right_class_mapping,
    .left_class_cnt      = 2,
    .right_class_cnt     = 2,
};

/*--------------------
 *  ALL CUSTOM DATA
 *--------------------*/

#if LVGL_VERSION_MAJOR == 8
/*Store all the custom data of the font*/
static  lv_font_fmt_txt_glyph_cache_t cache;
#endif

#if LVGL_VERSION_MAJOR >= 8
static const lv_font_fmt_txt_dsc_t font_dsc = {
#else
static lv_font_fmt_txt_dsc_t font_dsc = {
#endif
    .glyph_bitmap = glyph_bitmap,
    .glyph_dsc = glyph_dsc,
    .cmaps = cmaps,
    .kern_dsc = &kern_classes,
    .kern_scale = 16,
    .cmap_num = 1,
    .bpp = 4,
    .kern_classes = 1,
    .bitmap_format = 0,
#if LVGL_VERSION_MAJOR == 8
    .cache = &cache
#endif
};



/*-----------------
 *  PUBLIC FONT
 *----------------*/

/*Initialize a public general font descriptor*/
#if LVGL_VERSION_MAJOR >= 8
const lv_font_t ui_glyphs_20 = {
#else
lv_font_t ui_glyphs_20 = {
#endif
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,    /*Function pointer to get glyph's data*/
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,    /*Function pointer to get glyph's bitmap*/
    .line_height = 10,          /*The maximum line height required by the font*/
    .base_line = 0,             /*Baseline measured from the bottom of the line*/
#if !(LVGL_VERSION_MAJOR == 6 && LVGL_VERSION_MINOR == 0)
    .subpx = LV_FONT_SUBPX_NONE,
#endif
#if LV_VERSION_CHECK(7, 4, 0) || LVGL_VERSION_MAJOR >= 8
    .underline_position = -1,
    .underline_thickness = 1,
#endif
    .dsc = &font_dsc,          /*The custom font data. Will be accessed by `get_glyph_bitmap/dsc` */
#if LV_VERSION_CHECK(8, 2, 0) || LVGL_VERSION_MAJOR >= 9
    .fallback = NULL,
#endif
    .user_data = NULL,
};



#endif /*#if UI_GLYPHS_20*/

