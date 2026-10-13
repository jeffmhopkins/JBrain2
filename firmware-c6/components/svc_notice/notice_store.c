#include "notice_store.h"

#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "jn1.h"
#include "notice_logic.h"
#include "notice_port.h"

#define PATH_BYTES 128

static void path_of(const char *name, char *out)
{
    snprintf(out, PATH_BYTES, "%s/%s", notice_port_base(), name);
}

int nstore_open(const char *name)
{
    char p[PATH_BYTES];
    path_of(name, p);
    return open(p, O_RDONLY);
}

int nstore_part_open(void)
{
    char p[PATH_BYTES];
    path_of(NSTORE_PART, p);
    return open(p, O_WRONLY | O_CREAT | O_TRUNC, 0644);
}

bool nstore_part_write(int fd, const void *b, size_t n)
{
    const uint8_t *p = b;
    while (n) {
        const ssize_t k = write(fd, p, n);
        if (k <= 0) return false;
        p += k;
        n -= (size_t)k;
    }
    return true;
}

bool nstore_part_commit(int fd, const char *name)
{
    char from[PATH_BYTES], to[PATH_BYTES];
    path_of(NSTORE_PART, from);
    path_of(name, to);
    if (close(fd) != 0) {
        unlink(from);
        return false;
    }
    unlink(to);
    if (rename(from, to) != 0) {
        unlink(from);
        return false;
    }
    return true;
}

void nstore_part_discard(int fd)
{
    char p[PATH_BYTES];
    if (fd >= 0) close(fd);
    path_of(NSTORE_PART, p);
    unlink(p);
}

bool nstore_remove(const char *name)
{
    char p[PATH_BYTES];
    path_of(name, p);
    return unlink(p) == 0;
}

static bool read_all(int fd, void *b, size_t n)
{
    uint8_t *p = b;
    while (n) {
        const ssize_t k = read(fd, p, n);
        if (k <= 0) return false;
        p += k;
        n -= (size_t)k;
    }
    return true;
}

static bool skip(int fd, uint32_t n)
{
    return !n || lseek(fd, (off_t)n, SEEK_CUR) >= 0;
}

/* `len` bytes of text into a field of `cap` with its terminator: what does not fit is skipped,
   and the cut falls before a character, never inside one. */
static bool text(int fd, uint16_t len, char *dst, size_t cap)
{
    size_t k = len < cap - 1 ? len : cap - 1;
    if (!read_all(fd, dst, k)) return false;
    if (k < len) {
        /* The first byte left out says whether the cut split a character: back off to its start. */
        uint8_t c;
        if (!read_all(fd, &c, 1) || !skip(fd, (uint32_t)(len - k - 1))) return false;
        while (k && (c & 0xC0) == 0x80) c = (uint8_t)dst[--k];
    }
    dst[k] = '\0';
    return true;
}

bool nstore_load_text(const char *name, notice_t *out)
{
    const int fd = nstore_open(name);
    if (fd < 0) return false;
    uint8_t h[JN1_HEAD];
    bool ok = read_all(fd, h, 3) && !memcmp(h, "JN1", 3);
    out->n_choices = 0;
    out->source[0] = out->title[0] = out->body[0] = '\0';
    while (ok) {
        if (!read_all(fd, h, JN1_HEAD)) {
            ok = false;
            break;
        }
        const uint16_t len = (uint16_t)(h[1] | h[2] << 8);
        if (h[0] == JN1_STRIPE || h[0] == JN1_PCM || h[0] == JN1_END) break;
        if (h[0] == JN1_META) {
            ok = len >= JN1_META_FIXED && skip(fd, JN1_META_FIXED) &&
                 text(fd, (uint16_t)(len - JN1_META_FIXED), out->source, sizeof out->source);
        } else if (h[0] == JN1_TITLE) {
            ok = text(fd, len, out->title, sizeof out->title);
        } else if (h[0] == JN1_BODY) {
            ok = text(fd, len, out->body, sizeof out->body);
        } else if (h[0] == JN1_CHOICE && out->n_choices < NOTICE_CHOICES_MAX) {
            ok = text(fd, len, out->choices[out->n_choices], sizeof out->choices[0]);
            out->n_choices++;
        } else {
            ok = skip(fd, len);
        }
    }
    close(fd);
    return ok;
}

/* Where the last stripe read ended, so the next one in order costs one header. */
static struct {
    char name[NOTICE_FILE_NAME];
    uint16_t index;
    uint32_t at;
} s_cursor;

bool nstore_stripe(const char *name, uint32_t first_at, uint16_t index, uint16_t img_w,
                   uint16_t *px, uint32_t cap_px, uint16_t *y0, uint16_t *rows)
{
    if (!first_at) return false;
    uint16_t i = 0;
    uint32_t at = first_at;
    if (!strcmp(s_cursor.name, name) && s_cursor.index <= index) {
        i = s_cursor.index;
        at = s_cursor.at;
    }
    const int fd = nstore_open(name);
    if (fd < 0) return false;
    uint8_t h[JN1_HEAD + 4];
    bool ok = true;
    for (; ok && i < index; i++) {
        ok = lseek(fd, (off_t)at, SEEK_SET) >= 0 && read_all(fd, h, JN1_HEAD) && h[0] == JN1_STRIPE;
        at += JN1_HEAD + (uint32_t)(h[1] | h[2] << 8);
    }
    ok = ok && lseek(fd, (off_t)at, SEEK_SET) >= 0 && read_all(fd, h, sizeof h) &&
         h[0] == JN1_STRIPE;
    const uint32_t len = ok ? (uint32_t)(h[1] | h[2] << 8) : 0;
    ok = ok && len >= 4 && (len - 4) % 3 == 0;
    if (ok) {
        *y0 = (uint16_t)(h[3] | h[4] << 8);
        *rows = (uint16_t)(h[5] | h[6] << 8);
        ok = (uint32_t)*rows * img_w <= cap_px;
    }
    /* The runs a slice at a time (85 runs, 255 bytes): the payload never sits in RAM whole. */
    const uint32_t want = ok ? (uint32_t)*rows * img_w : 0;
    uint32_t got = 0, left = len >= 4 ? len - 4 : 0;
    uint8_t run[255];
    while (ok && left) {
        const uint32_t k = left < sizeof run ? left : sizeof run;
        ok = read_all(fd, run, k);
        for (uint32_t r = 0; ok && r < k; r += 3) {
            const uint32_t n = (uint32_t)run[r] + 1;
            if (n > want - got) {
                ok = false;
                break;
            }
            const uint16_t c = (uint16_t)(run[r + 1] | run[r + 2] << 8);
            for (uint32_t j = 0; j < n; j++) px[got + j] = c;
            got += n;
        }
        left -= k;
    }
    close(fd);
    ok = ok && got == want;
    if (ok) {
        snprintf(s_cursor.name, sizeof s_cursor.name, "%s", name);
        s_cursor.index = (uint16_t)(index + 1);
        s_cursor.at = at + JN1_HEAD + len;
    } else {
        s_cursor.name[0] = '\0';
    }
    return ok;
}

bool nstore_pcm(const char *name, uint32_t pcm_at, uint8_t *buf, size_t cap, nstore_sink_t sink,
                void *ctx)
{
    if (!pcm_at || cap < 2) return false;
    const int fd = nstore_open(name);
    if (fd < 0) return false;
    bool ok = lseek(fd, (off_t)pcm_at, SEEK_SET) >= 0;
    bool more = true;
    while (ok && more) {
        uint8_t h[JN1_HEAD];
        ok = read_all(fd, h, JN1_HEAD);
        if (!ok || h[0] != JN1_PCM) break;
        uint32_t left = (uint32_t)(h[1] | h[2] << 8);
        while (ok && more && left) {
            /* Even slices: a sample is never split between two hand-offs. */
            const uint32_t k = left < cap ? left : (uint32_t)(cap & ~(size_t)1);
            ok = read_all(fd, buf, k);
            if (ok) more = sink(buf, k, ctx);
            left -= k;
        }
    }
    close(fd);
    return ok;
}

bool nstore_entry(unsigned skip_n, char *name, size_t cap)
{
    DIR *d = opendir(notice_port_base());
    if (!d) return false;
    const struct dirent *e;
    bool found = false;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        if (skip_n) {
            skip_n--;
            continue;
        }
        snprintf(name, cap, "%s", e->d_name);
        found = true;
        break;
    }
    closedir(d);
    return found;
}

/* ---- the picture by rows ---- */

/* `n` bytes of the stream through the struct's buffer. */
static bool pic_bytes(notice_pic_t *p, uint8_t *out, uint32_t n)
{
    while (n) {
        if (p->buf_at == p->buf_n) {
            const ssize_t k = read(p->fd, p->buf, sizeof p->buf);
            if (k <= 0) return false;
            p->buf_n = (uint16_t)k;
            p->buf_at = 0;
        }
        const uint32_t k = n < (uint32_t)(p->buf_n - p->buf_at) ? n : (uint32_t)(p->buf_n - p->buf_at);
        memcpy(out, p->buf + p->buf_at, k);
        p->buf_at = (uint16_t)(p->buf_at + k);
        out += k;
        n -= k;
    }
    return true;
}

static bool pic_rewind(notice_pic_t *p)
{
    p->row = 0;
    p->run_left = p->stripe_left = 0;
    p->buf_n = p->buf_at = 0;
    return lseek(p->fd, (off_t)p->first_at, SEEK_SET) >= 0;
}

bool nstore_pic_open(notice_pic_t *p, const char *name, uint32_t first_at, uint16_t w, uint16_t h)
{
    memset(p, 0, sizeof *p);
    p->fd = -1;
    if (!first_at || !w || !h) return false;
    p->fd = nstore_open(name);
    if (p->fd < 0) return false;
    p->w = w;
    p->h = h;
    p->first_at = first_at;
    if (pic_rewind(p)) return true;
    nstore_pic_close(p);
    return false;
}

/* The next `n` pixels of the stream, into `px` or (NULL) skipped. Runs never cross a stripe, so
   the stream is the stripes' runs end to end; a stripe's header is read where its runs end. */
static bool pic_pixels(notice_pic_t *p, uint32_t n, uint16_t *px)
{
    while (n) {
        if (!p->run_left) {
            if (!p->stripe_left) {
                uint8_t h[JN1_HEAD + 4];
                if (!pic_bytes(p, h, sizeof h) || h[0] != JN1_STRIPE) return false;
                const uint32_t len = (uint32_t)(h[1] | h[2] << 8);
                if (len < 4 || (len - 4) % 3) return false;
                p->stripe_left = len - 4;
                if (!p->stripe_left) continue;
            }
            uint8_t r[3];
            if (!pic_bytes(p, r, 3)) return false;
            p->stripe_left -= 3;
            p->run_left = (uint32_t)r[0] + 1;
            p->color = (uint16_t)(r[1] | r[2] << 8);
        }
        const uint32_t k = n < p->run_left ? n : p->run_left;
        if (px) {
            for (uint32_t j = 0; j < k; j++) px[j] = p->color;
            px += k;
        }
        p->run_left -= k;
        n -= k;
    }
    return true;
}

bool nstore_pic_rows(notice_pic_t *p, uint16_t y0, uint16_t rows, uint16_t *px)
{
    if (p->fd < 0 || !rows || (uint32_t)y0 + rows > p->h) return false;
    if (y0 < p->row && !pic_rewind(p)) return false;
    if (y0 > p->row && !pic_pixels(p, (uint32_t)(y0 - p->row) * p->w, NULL)) return false;
    p->row = y0;
    if (!pic_pixels(p, (uint32_t)rows * p->w, px)) {
        /* Whatever was read is unknown now: the next ask starts again from the top. */
        p->row = UINT16_MAX;
        return false;
    }
    p->row = (uint16_t)(y0 + rows);
    return true;
}

void nstore_pic_close(notice_pic_t *p)
{
    if (p->fd >= 0) close(p->fd);
    p->fd = -1;
}
