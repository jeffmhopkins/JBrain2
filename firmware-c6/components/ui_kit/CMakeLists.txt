idf_component_register(
    SRCS "ui_kit.c" "ui_pin.c" "ui_hold.c" "ui_jerv_mark.c"
         "ui_emoji.c" "emoji_logic.c" "emoji/emoji_set.c" "emoji/emoji_img.c"
         "fonts/ui_font_clock_92.c" "fonts/ui_font_clock_124.c"
         "fonts/ui_glyphs_14.c" "fonts/ui_glyphs_16.c" "fonts/ui_glyphs_18.c"
         "fonts/ui_glyphs_20.c" "fonts/ui_glyphs_22.c" "fonts/ui_glyphs_24.c"
         "fonts/ui_glyphs_26.c" "fonts/ui_glyphs_28.c" "fonts/ui_glyphs_30.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl__lvgl
)
