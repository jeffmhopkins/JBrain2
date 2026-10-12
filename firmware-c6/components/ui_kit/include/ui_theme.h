/* The look, as tokens, from the approved mock (docs/mocks/jpanel-c6/). Mock pixels are panel
 * pixels, so every number here is the mock's. No LVGL types: the tokens and the tile/icon
 * types are shared with the pure models and the host suite. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define UI_W 480
#define UI_H 480

/* Ground is pure black: an unlit AMOLED pixel is off. */
#define UI_C_BG 0x000000
#define UI_C_SURFACE 0x17181B  /* cards, tiles */
#define UI_C_RAISED 0x1E2024   /* chips, their bubble, secondary buttons */
#define UI_C_LINE 0x26282C
#define UI_C_LINE_STRONG 0x3A3D42
#define UI_C_TRACK 0x2C2F34
#define UI_C_THUMB 0x4A4D53
#define UI_C_TEXT 0xE6E7E9
#define UI_C_TEXT_SOFT 0xC4C7CC
#define UI_C_MUTED 0x9A9DA3
#define UI_C_FAINT 0x5C5F66
#define UI_C_MINE 0x1D2733     /* my bubble */

/* DESIGN.md accents. */
#define UI_C_JERV 0xA493C9
/* The people's threads: four colours, one per sibling panel, chosen from its name (svc_siblings). */
#define UI_C_PERSON_A 0xC9A36A
#define UI_C_PERSON_B 0x6FB6B1
#define UI_C_PERSON_C 0xB79AD6
#define UI_C_PERSON_D 0xD69A8A
#define UI_C_MINECRAFT 0x8FBC9A
#define UI_C_STEEL 0x7FA7C9
#define UI_C_ROSE 0xCF8A8F      /* Jerv listening ring */
#define UI_C_OK 0x8FBC9A
#define UI_C_WARN 0xC9A36A

/* An accent "tint" is the accent at 16% over black, the mock's rgba(…, 0.16). */
#define UI_OPA_TINT 41
#define UI_OPA_TRACK_ON 56     /* switch track, 22% */
#define UI_OPA_ACTION 46       /* primary action fill, 18% */

#define UI_R_TILE 28
#define UI_R_CARD 24
#define UI_R_BUTTON 18
#define UI_R_BUBBLE 22
#define UI_R_BUBBLE_TAIL 6
#define UI_R_CHOICE 20

#define UI_HEADER_H 76
#define UI_TOUCH_MIN 44         /* nothing tappable is smaller */
#define UI_BUTTON_H 56
#define UI_CHOICE_H 64
#define UI_BIG_BUTTON_H 72
#define UI_SWITCH_W 68
#define UI_SWITCH_H 40
#define UI_PAD_PAGE 20
#define UI_PAD_CARD 18
#define UI_GAP 14

#define UI_TILE_W 196
#define UI_TILE_H 196
#define UI_TILE_GAP 16
#define UI_HOME_X 36
#define UI_HOME_Y 28

typedef enum {
    UI_ACCENT_JERV,
    UI_ACCENT_PERSON_A,
    UI_ACCENT_PERSON_B,
    UI_ACCENT_PERSON_C,
    UI_ACCENT_PERSON_D,
    UI_ACCENT_MINECRAFT,
    UI_ACCENT_STEEL,
    UI_ACCENT_NEUTRAL, /* Clock, Settings: grey glyph on a raised disc */
} ui_accent_t;

typedef enum {
    UI_GLYPH_STAR,
    UI_GLYPH_CUBE,
    UI_GLYPH_INITIAL,
    UI_GLYPH_CLOCK,
    UI_GLYPH_GEAR,
    UI_GLYPH_COMPASS,
} ui_glyph_t;

typedef struct {
    ui_glyph_t glyph;
    char initial; /* UI_GLYPH_INITIAL */
} ui_icon_t;

typedef struct {
    uint16_t badge;      /* amber count; 0 hides it */
    bool dimmed;         /* not built yet */
    bool soon;           /* the "Soon" chip */
    char live_label[16]; /* replaces the title, e.g. the Clock tile shows the time */
} ui_tile_state_t;

uint32_t ui_accent_color(ui_accent_t a);
