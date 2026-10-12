/*******************************************************************************
 * Size: 26 px
 * Bpp: 4
 * Opts: --font <lvgl-v9.2.2>/scripts/built_in_font/Montserrat-Medium.ttf -r 0xB7,0x2013,0x2014,0x2026,0x2192,0x2212 --size 26 --bpp 4 --no-compress --format lvgl --lv-include lvgl.h --lv-font-name ui_glyphs_26 -o ui_glyphs_26.c
 ******************************************************************************/

#ifdef LV_LVGL_H_INCLUDE_SIMPLE
#include "lvgl.h"
#else
#include "lvgl.h"
#endif

#ifndef UI_GLYPHS_26
#define UI_GLYPHS_26 1
#endif

#if UI_GLYPHS_26

/*-----------------
 *    BITMAPS
 *----------------*/

/*Store the image of the glyphs*/
static LV_ATTRIBUTE_LARGE_CONST const uint8_t glyph_bitmap[] = {
    /* U+00B7 "·" */
    0x4, 0x83, 0x1, 0xff, 0xf1, 0x3f, 0xff, 0x20,
    0xaf, 0x90,

    /* U+2013 "–" */
    0xcc, 0xcc, 0xcc, 0xcc, 0xcc, 0xcc, 0xcf, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff,

    /* U+2014 "—" */
    0xcc, 0xcc, 0xcc, 0xcc, 0xcc, 0xcc, 0xcc, 0xcc,
    0xcc, 0xcc, 0xcc, 0xcc, 0xcc, 0xff, 0xff, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff,

    /* U+2026 "…" */
    0x7, 0x70, 0x0, 0x7, 0x70, 0x0, 0x7, 0x70,
    0x9f, 0xf8, 0x0, 0x9f, 0xf8, 0x0, 0x8f, 0xf9,
    0xbf, 0xf9, 0x0, 0xbf, 0xfa, 0x0, 0xaf, 0xfa,
    0x4e, 0xd2, 0x0, 0x3e, 0xe3, 0x0, 0x3d, 0xe3,

    /* U+2192 "→" */
    0x0, 0x0, 0x0, 0x47, 0x40, 0x0, 0x0, 0x0,
    0x0, 0x2, 0xff, 0x20, 0x0, 0x0, 0x0, 0x0,
    0x8, 0xfc, 0x0, 0x0, 0x0, 0x0, 0x0, 0xd,
    0xf7, 0x0, 0x0, 0x0, 0x0, 0x0, 0x4f, 0xf2,
    0x4, 0xff, 0xff, 0xff, 0xff, 0xff, 0xd0, 0x3d,
    0xdd, 0xdd, 0xdd, 0xdf, 0xfb, 0x0, 0x0, 0x0,
    0x0, 0x6, 0xfe, 0x10, 0x0, 0x0, 0x0, 0x1,
    0xef, 0x50, 0x0, 0x0, 0x0, 0x0, 0xaf, 0xa0,
    0x0, 0x0, 0x0, 0x0, 0x4f, 0xe1, 0x0, 0x0,
    0x0, 0x0, 0x3, 0x42, 0x0, 0x0,

    /* U+2212 "−" */
    0x1, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1, 0xff,
    0xff, 0xff, 0xff, 0xff, 0xf9, 0x1f, 0xff, 0xff,
    0xff, 0xff, 0xff, 0x80
};


/*---------------------
 *  GLYPH DESCRIPTION
 *--------------------*/

static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc[] = {
    {.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, .ofs_x = 0, .ofs_y = 0} /* id = 0 reserved */,
    {.bitmap_index = 0, .adv_w = 111, .box_w = 5, .box_h = 4, .ofs_x = 1, .ofs_y = 5},
    {.bitmap_index = 10, .adv_w = 208, .box_w = 13, .box_h = 2, .ofs_x = 0, .ofs_y = 6},
    {.bitmap_index = 23, .adv_w = 416, .box_w = 26, .box_h = 2, .ofs_x = 0, .ofs_y = 6},
    {.bitmap_index = 49, .adv_w = 287, .box_w = 16, .box_h = 4, .ofs_x = 1, .ofs_y = 0},
    {.bitmap_index = 81, .adv_w = 250, .box_w = 13, .box_h = 12, .ofs_x = 1, .ofs_y = 1},
    {.bitmap_index = 159, .adv_w = 242, .box_w = 13, .box_h = 3, .ofs_x = 1, .ofs_y = 8}
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
    1, -3, -3, 0
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
const lv_font_t ui_glyphs_26 = {
#else
lv_font_t ui_glyphs_26 = {
#endif
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,    /*Function pointer to get glyph's data*/
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,    /*Function pointer to get glyph's bitmap*/
    .line_height = 13,          /*The maximum line height required by the font*/
    .base_line = 0,             /*Baseline measured from the bottom of the line*/
#if !(LVGL_VERSION_MAJOR == 6 && LVGL_VERSION_MINOR == 0)
    .subpx = LV_FONT_SUBPX_NONE,
#endif
#if LV_VERSION_CHECK(7, 4, 0) || LVGL_VERSION_MAJOR >= 8
    .underline_position = -2,
    .underline_thickness = 1,
#endif
    .dsc = &font_dsc,          /*The custom font data. Will be accessed by `get_glyph_bitmap/dsc` */
#if LV_VERSION_CHECK(8, 2, 0) || LVGL_VERSION_MAJOR >= 9
    .fallback = NULL,
#endif
    .user_data = NULL,
};



#endif /*#if UI_GLYPHS_26*/

