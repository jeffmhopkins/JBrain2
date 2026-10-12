/* A PNG writer with no library: stored (uncompressed) deflate blocks, so the sim builds with
 * nothing but a C compiler and LVGL. The files are large and nobody cares. */
#include "sim_png.h"

#include <stdio.h>
#include <stdlib.h>

static uint32_t crc_table[256];

static void crc_init(void)
{
    for (uint32_t n = 0; n < 256; n++) {
        uint32_t c = n;
        for (int k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320u ^ (c >> 1) : c >> 1;
        crc_table[n] = c;
    }
}

static uint32_t crc(uint32_t c, const uint8_t *b, size_t n)
{
    c = ~c;
    for (size_t i = 0; i < n; i++) c = crc_table[(c ^ b[i]) & 0xFF] ^ (c >> 8);
    return ~c;
}

static void be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24), p[1] = (uint8_t)(v >> 16), p[2] = (uint8_t)(v >> 8), p[3] = (uint8_t)v;
}

static void chunk(FILE *f, const char *type, const uint8_t *data, uint32_t n)
{
    uint8_t h[8];
    be32(h, n);
    for (int i = 0; i < 4; i++) h[4 + i] = (uint8_t)type[i];
    fwrite(h, 1, 8, f);
    if (n) fwrite(data, 1, n, f);
    uint32_t c = crc(crc(0, h + 4, 4), data, n);
    be32(h, c);
    fwrite(h, 1, 4, f);
}

bool png_write_rgb565(const char *path, const uint16_t *px, int w, int h)
{
    crc_init();
    size_t raw_n = (size_t)h * (1 + (size_t)w * 3);
    uint8_t *raw = malloc(raw_n);
    if (!raw) return false;
    size_t at = 0;
    for (int y = 0; y < h; y++) {
        raw[at++] = 0;
        for (int x = 0; x < w; x++) {
            uint16_t c = px[y * w + x];
            raw[at++] = (uint8_t)((c >> 11 & 0x1F) * 255 / 31);
            raw[at++] = (uint8_t)((c >> 5 & 0x3F) * 255 / 63);
            raw[at++] = (uint8_t)((c & 0x1F) * 255 / 31);
        }
    }
    size_t blocks = (raw_n + 65534) / 65535;
    size_t z_n = 2 + raw_n + blocks * 5 + 4;
    uint8_t *z = malloc(z_n);
    if (!z) {
        free(raw);
        return false;
    }
    size_t o = 0;
    z[o++] = 0x78, z[o++] = 0x01;
    uint32_t a = 1, b = 0;
    for (size_t i = 0; i < raw_n; i++) {
        a = (a + raw[i]) % 65521;
        b = (b + a) % 65521;
    }
    for (size_t i = 0; i < raw_n; i += 65535) {
        size_t len = raw_n - i < 65535 ? raw_n - i : 65535;
        z[o++] = i + len == raw_n;
        z[o++] = (uint8_t)len, z[o++] = (uint8_t)(len >> 8);
        z[o++] = (uint8_t)~len, z[o++] = (uint8_t)(~len >> 8);
        for (size_t k = 0; k < len; k++) z[o++] = raw[i + k];
    }
    be32(z + o, b << 16 | a);
    o += 4;
    FILE *f = fopen(path, "wb");
    bool ok = f != NULL;
    if (ok) {
        static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};
        uint8_t ihdr[13];
        be32(ihdr, (uint32_t)w);
        be32(ihdr + 4, (uint32_t)h);
        ihdr[8] = 8, ihdr[9] = 2, ihdr[10] = 0, ihdr[11] = 0, ihdr[12] = 0;
        fwrite(sig, 1, 8, f);
        chunk(f, "IHDR", ihdr, 13);
        chunk(f, "IDAT", z, (uint32_t)o);
        chunk(f, "IEND", NULL, 0);
        ok = fclose(f) == 0;
    }
    free(raw);
    free(z);
    return ok;
}
