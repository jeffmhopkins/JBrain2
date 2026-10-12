/* Minecraft: a second-level menu (owner, 2026-10-12: "when you click Minecraft it should go to a
 * next layer of a menu, options will be Dave or Compass"). Dave is the box's real Minecraft_Dave
 * in the shared conversation view — his own instance and thread, never Jerv's; Compass is a page
 * saying it is a work in progress. BOOT, or the back arrow, climbs one level: Dave or Compass back
 * to the menu, the menu home. */
#include "app_minecraft.h"
#include "convo.h"
#include "ui_kit.h"

static const ui_icon_t ICON = {UI_GLYPH_CUBE, 0};
static const ui_icon_t DAVE_ICON = {UI_GLYPH_INITIAL, 'D'};
static const ui_icon_t COMPASS_ICON = {UI_GLYPH_COMPASS, 0};
static const convo_config_t DAVE = {
    .thread = "dave",
    .title = "Dave",
    .icon = &DAVE_ICON,
    .accent = UI_ACCENT_MINECRAFT,
    .empty = "Nothing said yet. Hold KEY and ask Dave something.",
};

typedef enum { MC_MENU, MC_DAVE, MC_COMPASS } mc_view_t;

typedef struct {
    app_t *self;
    lv_obj_t *root, *view;
    mc_view_t at;
    convo_t dave;
} mc_t;

static mc_t s_mc;

static void show(mc_t *m, mc_view_t at);

static void home(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->go_home();
}

static void up(lv_event_t *e)
{
    show(lv_event_get_user_data(e), MC_MENU);
}

static void open_dave(lv_event_t *e)
{
    show(lv_event_get_user_data(e), MC_DAVE);
}

static void open_compass(lv_event_t *e)
{
    show(lv_event_get_user_data(e), MC_COMPASS);
}

/* Each view is built under a child of the app's root, so leaving one frees all of it. */
static lv_obj_t *fresh_view(mc_t *m)
{
    if (m->at == MC_DAVE) convo_close(&m->dave);
    if (m->view) lv_obj_delete(m->view);
    m->view = lv_obj_create(m->root);
    lv_obj_remove_style_all(m->view);
    lv_obj_set_size(m->view, UI_W, UI_H);
    lv_obj_remove_flag(m->view, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    return m->view;
}

static void build_menu(mc_t *m, lv_obj_t *v)
{
    ui_header(v, &ICON, UI_ACCENT_MINECRAFT, "Minecraft", home, m->self);
    /* The home grid's own geometry, one row down: the same tiles, the same reach. */
    const int y = UI_HOME_Y + UI_HEADER_H;
    ui_tile(v, UI_HOME_X, y, "Dave", &DAVE_ICON, UI_ACCENT_MINECRAFT, NULL, open_dave, m);
    ui_tile(v, UI_HOME_X + UI_TILE_W + UI_TILE_GAP, y, "Compass", &COMPASS_ICON,
            UI_ACCENT_MINECRAFT, &(ui_tile_state_t){.soon = true}, open_compass, m);
}

static void build_compass(mc_t *m, lv_obj_t *v)
{
    ui_header(v, &COMPASS_ICON, UI_ACCENT_MINECRAFT, "Compass", up, m);
    lv_obj_t *col = ui_box(v, LV_FLEX_FLOW_COLUMN, 18);
    lv_obj_set_width(col, UI_W - 96);
    lv_obj_align(col, LV_ALIGN_CENTER, 0, UI_HEADER_H / 2);
    ui_icon(col, &COMPASS_ICON, UI_ACCENT_MINECRAFT, 120, true);
    ui_label(col, "Work in progress", 26, UI_C_TEXT);
    lv_obj_t *l = ui_label_wrap(col, "This page is not built yet. Press BOOT to go back.", 20,
                                UI_C_MUTED);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
}

static void show(mc_t *m, mc_view_t at)
{
    lv_obj_t *v = fresh_view(m);
    m->at = at;
    switch (at) {
    case MC_MENU: build_menu(m, v); break;
    case MC_DAVE: convo_open(&m->dave, &DAVE, m->self->host, v, up, m); break;
    case MC_COMPASS: build_compass(m, v); break;
    }
}

static void mc_open(app_t *self, lv_obj_t *root)
{
    mc_t *m = self->state;
    *m = (mc_t){.self = self, .root = root, .at = MC_MENU};
    show(m, MC_MENU);
}

static void mc_close(app_t *self)
{
    mc_t *m = self->state;
    if (m->at == MC_DAVE) convo_close(&m->dave);
    /* The root, and the view under it, are the shell's to delete. */
    m->view = NULL;
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    mc_t *m = self->state;
    if (m->at == MC_DAVE && convo_event(&m->dave, ev)) return true;
    /* BOOT climbs a level; on the menu it is the shell's (home). KEY outside Dave's thread is
       not consumed, so the shell's own talk to Jerv takes it (plan "KEY routing"). */
    if (ev->type == APP_EV_BOOT && m->at != MC_MENU) {
        show(m, MC_MENU);
        return true;
    }
    return false;
}

/* Dave's thread has the conversation menu (New and Recent, 0.0.15); the Minecraft menu and
   Compass have none. */
static bool menu(app_t *self, char *sub, size_t cap, ui_accent_t *accent)
{
    mc_t *m = self->state;
    return m->at == MC_DAVE && convo_menu(&m->dave, sub, cap, accent);
}

static const char *start_new(app_t *self)
{
    mc_t *m = self->state;
    return m->at == MC_DAVE ? convo_new(&m->dave) : NULL;
}

static void recent(app_t *self)
{
    mc_t *m = self->state;
    if (m->at == MC_DAVE) convo_recent(&m->dave);
}

static void describe(app_t *self, dbg_json_t *w)
{
    static const char *const AT[] = {"menu", "dave", "compass"};
    mc_t *m = self->state;
    dbg_json_str(w, "view", AT[m->at]);
    if (m->at == MC_DAVE) convo_describe(&m->dave, w);
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
    .on_event = on_event,
    .describe = describe,
    .convo_menu = menu,
    .convo_new = start_new,
    .convo_recent = recent,
    .state = &s_mc,
};
