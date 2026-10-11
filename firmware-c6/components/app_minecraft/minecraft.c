#include "app_minecraft.h"
#include "ui_kit.h"

static const ui_icon_t ICON = {UI_GLYPH_CUBE, 0};

static void back(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->go_home();
}

static void mc_open(app_t *self, lv_obj_t *root)
{
    ui_header(root, &ICON, UI_ACCENT_MINECRAFT, "Minecraft", back, self);
    lv_obj_t *col = ui_box(root, LV_FLEX_FLOW_COLUMN, 18);
    lv_obj_set_width(col, UI_W - 96);
    lv_obj_align(col, LV_ALIGN_CENTER, 0, UI_HEADER_H / 2);
    ui_icon(col, &ICON, UI_ACCENT_MINECRAFT, 120, true);
    ui_label(col, "Not built yet", 26, UI_C_TEXT);
    lv_obj_t *l = ui_label_wrap(col, "This page is reserved. Press BOOT to go home.", 20, UI_C_MUTED);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
}

static void mc_close(app_t *self)
{
    (void)self;
}

static void tile_state(app_t *self, ui_tile_state_t *out)
{
    (void)self;
    out->dimmed = true;
    out->soon = true;
}

app_t app_minecraft = {
    .id = "minecraft",
    .title = "Minecraft",
    .icon = &ICON,
    .accent = UI_ACCENT_MINECRAFT,
    .page = 0,
    .slot = 1,
    .open = mc_open,
    .close = mc_close,
    .tile_state = tile_state,
};
