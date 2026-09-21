#include "font.h"

/* Column-major, bit 0 is the top row. The classic 5x7 cell, one blank column of spacing
   added by the renderer rather than baked in. */
typedef struct {
    char ch;
    uint8_t col[FONT_W];
} glyph_t;

static const glyph_t GLYPHS[] = {
    {'0', {0x3E, 0x51, 0x49, 0x45, 0x3E}}, {'1', {0x00, 0x42, 0x7F, 0x40, 0x00}},
    {'2', {0x42, 0x61, 0x51, 0x49, 0x46}}, {'3', {0x21, 0x41, 0x45, 0x4B, 0x31}},
    {'4', {0x18, 0x14, 0x12, 0x7F, 0x10}}, {'5', {0x27, 0x45, 0x45, 0x45, 0x39}},
    {'6', {0x3C, 0x4A, 0x49, 0x49, 0x30}}, {'7', {0x01, 0x71, 0x09, 0x05, 0x03}},
    {'8', {0x36, 0x49, 0x49, 0x49, 0x36}}, {'9', {0x06, 0x49, 0x49, 0x29, 0x1E}},
    {'.', {0x00, 0x60, 0x60, 0x00, 0x00}}, {'v', {0x1C, 0x20, 0x40, 0x20, 0x1C}},
    {'-', {0x08, 0x08, 0x08, 0x08, 0x08}}, {' ', {0x00, 0x00, 0x00, 0x00, 0x00}},
};

static const glyph_t *find(char c)
{
    for (unsigned i = 0; i < sizeof(GLYPHS) / sizeof(GLYPHS[0]); i++) {
        if (GLYPHS[i].ch == c) return &GLYPHS[i];
    }
    return 0;
}

int font_text_w(const char *s, int scale)
{
    int n = 0;
    for (const char *p = s; *p; p++) n++;
    if (n == 0) return 0;
    /* Glyph cells plus one blank column between each pair. */
    return (n * FONT_W + (n - 1)) * scale;
}

void font_draw(uint16_t *fb, int fbw, int fbh, int x, int y, int scale, const char *s,
               uint16_t colour)
{
    if (scale < 1) scale = 1;
    int pen = x;
    for (const char *p = *s ? s : ""; *p; p++) {
        const glyph_t *g = find(*p);
        for (int cx = 0; cx < FONT_W; cx++) {
            const uint8_t bits = g ? g->col[cx] : 0;
            for (int cy = 0; cy < FONT_H; cy++) {
                if (!(bits & (1u << cy))) continue;
                for (int sy = 0; sy < scale; sy++) {
                    for (int sx = 0; sx < scale; sx++) {
                        const int px = pen + cx * scale + sx;
                        const int py = y + cy * scale + sy;
                        if (px < 0 || py < 0 || px >= fbw || py >= fbh) continue;
                        fb[py * fbw + px] = colour;
                    }
                }
            }
        }
        pen += (FONT_W + 1) * scale;
    }
}
