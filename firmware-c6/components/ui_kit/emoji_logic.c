#include "emoji_logic.h"

#include <stddef.h>

typedef struct {
    uint32_t lo, hi;
} range_t;

/* Draw nothing and take no room. LVGL's draw path asks for a glyph with no neighbour (it passes
 * '\0' as the next letter), so a sequence is handled one code point at a time: the selectors
 * vanish, a skin tone leaves its base emoji in the default yellow, a ZWJ sequence shows its
 * parts side by side (Unicode's own fallback for one a font lacks), and a flag's two regional
 * indicators leave nothing. */
static const range_t INVISIBLE[] = {
    {0x200D, 0x200D},   /* zero-width joiner */
    {0x20E3, 0x20E3},   /* combining keycap: "1️⃣" is the plain digit */
    {0xFE00, 0xFE0F},   /* variation selectors, VS16 among them */
    {0x1F1E6, 0x1F1FF}, /* regional indicators: flags are not carried */
    {0x1F3FB, 0x1F3FF}, /* skin-tone modifiers */
    {0xE0020, 0xE007F}, /* tag characters (subdivision flags) */
};

/* Unicode's Extended_Pictographic (emoji-data.txt), the code points that may be an emoji: one
 * the set lacks is drawn as the placeholder rather than LVGL's missing-glyph box. Everything
 * else is plain text and reaches Montserrat exactly as before. */
static const range_t PICTOGRAPHIC[] = {
    {0x00A9, 0x00A9},   {0x00AE, 0x00AE},   {0x203C, 0x203C},   {0x2049, 0x2049},
    {0x2122, 0x2122},   {0x2139, 0x2139},   {0x2194, 0x2199},   {0x21A9, 0x21AA},
    {0x231A, 0x231B},   {0x2328, 0x2328},   {0x2388, 0x2388},   {0x23CF, 0x23CF},
    {0x23E9, 0x23F3},   {0x23F8, 0x23FA},   {0x24C2, 0x24C2},   {0x25AA, 0x25AB},
    {0x25B6, 0x25B6},   {0x25C0, 0x25C0},   {0x25FB, 0x25FE},   {0x2600, 0x2605},
    {0x2607, 0x2612},   {0x2614, 0x2685},   {0x2690, 0x2705},   {0x2708, 0x2712},
    {0x2714, 0x2714},   {0x2716, 0x2716},   {0x271D, 0x271D},   {0x2721, 0x2721},
    {0x2728, 0x2728},   {0x2733, 0x2734},   {0x2744, 0x2744},   {0x2747, 0x2747},
    {0x274C, 0x274C},   {0x274E, 0x274E},   {0x2753, 0x2755},   {0x2757, 0x2757},
    {0x2763, 0x2767},   {0x2795, 0x2797},   {0x27A1, 0x27A1},   {0x27B0, 0x27B0},
    {0x27BF, 0x27BF},   {0x2934, 0x2935},   {0x2B05, 0x2B07},   {0x2B1B, 0x2B1C},
    {0x2B50, 0x2B50},   {0x2B55, 0x2B55},   {0x3030, 0x3030},   {0x303D, 0x303D},
    {0x3297, 0x3297},   {0x3299, 0x3299},   {0x1F000, 0x1F0FF}, {0x1F10D, 0x1F10F},
    {0x1F12F, 0x1F12F}, {0x1F16C, 0x1F171}, {0x1F17E, 0x1F17F}, {0x1F18E, 0x1F18E},
    {0x1F191, 0x1F19A}, {0x1F1AD, 0x1F1E5}, {0x1F201, 0x1F20F}, {0x1F21A, 0x1F21A},
    {0x1F22F, 0x1F22F}, {0x1F232, 0x1F23A}, {0x1F23C, 0x1F23F}, {0x1F249, 0x1F3FA},
    {0x1F400, 0x1F53D}, {0x1F546, 0x1F64F}, {0x1F680, 0x1F6FF}, {0x1F774, 0x1F77F},
    {0x1F7D5, 0x1F7FF}, {0x1F80C, 0x1F80F}, {0x1F848, 0x1F84F}, {0x1F85A, 0x1F85F},
    {0x1F888, 0x1F88F}, {0x1F8AE, 0x1F8FF}, {0x1F90C, 0x1F93A}, {0x1F93C, 0x1F945},
    {0x1F947, 0x1FAFF}, {0x1FC00, 0x1FFFD},
};

static int in(const range_t *r, size_t n, uint32_t cp)
{
    size_t lo = 0, hi = n;
    while (lo < hi) {
        const size_t mid = (lo + hi) / 2;
        if (cp < r[mid].lo)
            hi = mid;
        else if (cp > r[mid].hi)
            lo = mid + 1;
        else
            return 1;
    }
    return 0;
}

emoji_kind_t emoji_kind(uint32_t cp, uint16_t *index)
{
    /* Everything under U+00A9 is ASCII or Latin-1 text: the common case leaves at once. */
    if (cp < 0xA9) return EMOJI_NOT;
    size_t lo = 0, hi = EMOJI_SET_N;
    while (lo < hi) {
        const size_t mid = (lo + hi) / 2;
        if (cp < EMOJI_SET[mid]) {
            hi = mid;
        } else if (cp > EMOJI_SET[mid]) {
            lo = mid + 1;
        } else {
            if (index) *index = (uint16_t)mid;
            return EMOJI_IN_SET;
        }
    }
    if (in(INVISIBLE, sizeof INVISIBLE / sizeof *INVISIBLE, cp)) return EMOJI_INVISIBLE;
    if (in(PICTOGRAPHIC, sizeof PICTOGRAPHIC / sizeof *PICTOGRAPHIC, cp)) return EMOJI_UNKNOWN;
    return EMOJI_NOT;
}
