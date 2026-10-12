/* Jerv: the shared conversation (convo) with Jerv's thread, title and colour. Its own instance,
 * so nothing here is Dave's (app_minecraft holds his). */
#include "app_jerv.h"
#include "convo.h"

static const ui_icon_t ICON = {UI_GLYPH_JERV, 0};
static const convo_config_t CFG = {
    .thread = "jerv",
    .title = "Jerv",
    .icon = &ICON,
    .accent = UI_ACCENT_JERV,
    .empty = "Nothing said yet. Hold KEY and ask Jerv something.",
};
static convo_t s_convo;

static void back(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->go_home();
}

static void jerv_open(app_t *self, lv_obj_t *root)
{
    convo_open(self->state, &CFG, self->host, root, back, self);
}

static void jerv_close(app_t *self)
{
    convo_close(self->state);
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    return convo_event(self->state, ev);
}

static void describe(app_t *self, dbg_json_t *w)
{
    convo_describe(self->state, w);
}

/* The long press's menu is the conversation's own: New and Recent (0.0.15). */
static bool menu(app_t *self, char *sub, size_t cap, ui_accent_t *accent)
{
    return convo_menu(self->state, sub, cap, accent);
}

static const char *start_new(app_t *self)
{
    return convo_new(self->state);
}

static void recent(app_t *self)
{
    convo_recent(self->state);
}

app_t app_jerv = {
    .id = "jerv",
    .title = "Jerv",
    .icon = &ICON,
    .accent = UI_ACCENT_JERV,
    .page = 0,
    .slot = 0,
    .open = jerv_open,
    .close = jerv_close,
    .on_event = on_event,
    .describe = describe,
    .convo_menu = menu,
    .convo_new = start_new,
    .convo_recent = recent,
    .state = &s_convo,
};
