/* The notices' files on the `storage` partition: one per notice, the JN1 stream exactly as it
 * came, written record by record while it arrives and renamed into place only once END has been
 * checked, so a file under a notice's name is always a whole stream. POSIX calls on the port's
 * mount point: littlefs through the VFS on the device, a temporary directory on the host. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_notice.h"

/* The stream being written; never a notice's name. */
#define NSTORE_PART "part.tmp"

/* A notice's file, read-only (close it with close()). */
int nstore_open(const char *name);
int nstore_part_open(void);
bool nstore_part_write(int fd, const void *b, size_t n);
/* Closes it and renames it to `name` (replacing a stale file of that name). */
bool nstore_part_commit(int fd, const char *name);
void nstore_part_discard(int fd);
bool nstore_remove(const char *name);

/* The sender, title, body and choices into `out` (each cut on a character to its field), read
 * record by record with no buffer bigger than the body. Every other field is left alone. */
bool nstore_load_text(const char *name, notice_t *out);

/* Stripe `index` of the picture whose first STRIPE record is at `first_at`, decoded into `px`.
 * Sequential reads (0, 1, 2, …) cost one header each: the last position is remembered. */
bool nstore_stripe(const char *name, uint32_t first_at, uint16_t index, uint16_t img_w,
                   uint16_t *px, uint32_t cap_px, uint16_t *y0, uint16_t *rows);

/* The picture by rows (svc_notice.h's notice_pic_t). */
bool nstore_pic_open(notice_pic_t *p, const char *name, uint32_t first_at, uint16_t w, uint16_t h);
bool nstore_pic_rows(notice_pic_t *p, uint16_t y0, uint16_t rows, uint16_t *px);
void nstore_pic_close(notice_pic_t *p);

/* The PCM records from `pcm_at` on, through `buf` (`cap` bytes) into `sink` until END. Stops
 * early, true, when the sink says false (the sound stopped); false when the file cannot be
 * read. */
typedef bool (*nstore_sink_t)(const uint8_t *pcm, uint32_t n, void *ctx);
bool nstore_pcm(const char *name, uint32_t pcm_at, uint8_t *buf, size_t cap, nstore_sink_t sink,
                void *ctx);

/* The directory's first entry `skip` past, by name: false once there are no more. */
bool nstore_entry(unsigned skip, char *name, size_t cap);
