#include "face.h"
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    uint16_t *fb = malloc((size_t)FACE_W * FACE_H * 2);
    face_draw(fb, argc > 1 ? atoi(argv[1]) : 0);
    FILE *f = fopen(argv[2], "wb");
    fprintf(f, "P6\n%d %d\n255\n", FACE_W, FACE_H);
    for (int i = 0; i < FACE_W * FACE_H; i++) {
        /* face.c stores byte-swapped for the panel; swap back to read it. */
        uint16_t c = (uint16_t)((fb[i] >> 8) | (fb[i] << 8));
        unsigned char rgb[3] = {(unsigned char)(((c >> 11) & 0x1F) << 3),
                                (unsigned char)(((c >> 5) & 0x3F) << 2),
                                (unsigned char)((c & 0x1F) << 3)};
        fwrite(rgb, 1, 3, f);
    }
    fclose(f);
    return 0;
}
