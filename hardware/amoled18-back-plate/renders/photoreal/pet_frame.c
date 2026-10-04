/* One frame of the pet, drawn by the firmware's own face.c, as a PPM for the screen texture.
 * Usage: pet_frame <colour> <out.ppm> <form: 0 ostrich, 1 robot> [side]
 * With "side" the figure is fitted into the 368 px square the firmware uses on its side
 * (display.c's landscape blit), still upright in the frame; the caller turns it. */
#include "face.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    if (argc < 4) return 2;
    uint16_t *fb = malloc((size_t)FACE_W * FACE_H * 2);
    face_state_t st;
    face_rest(&st);
    st.form = (face_form_t)atoi(argv[3]);
    if (argc > 4) face_set_fit(368.0f / (float)FACE_H, (FACE_H - 368) / 2 + (int)(368 * 0.545f));
    face_draw(fb, atoi(argv[1]), &st);
    FILE *f = fopen(argv[2], "wb");
    if (f == NULL) return 1;
    fprintf(f, "P6\n%d %d\n255\n", FACE_W, FACE_H);
    for (int i = 0; i < FACE_W * FACE_H; i++) {
        /* face.c stores RGB565 byte-swapped for the panel. */
        const uint16_t c = (uint16_t)((fb[i] >> 8) | (fb[i] << 8));
        const unsigned char rgb[3] = {(unsigned char)(((c >> 11) & 0x1F) << 3),
                                      (unsigned char)(((c >> 5) & 0x3F) << 2),
                                      (unsigned char)((c & 0x1F) << 3)};
        fwrite(rgb, 1, 3, f);
    }
    fclose(f);
    free(fb);
    return 0;
}
