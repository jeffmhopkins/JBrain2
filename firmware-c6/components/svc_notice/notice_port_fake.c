/* The notice service's port on the host and in the sim: one thread needs no lock, a temporary
 * directory stands in for the `storage` partition (removed at exit), and RAM for the NVS blob,
 * so the host suite can show a reboot keeps both. */
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "notice_port.h"
#include "notice_port_fake.h"

static char s_dir[64];
static notice_posts_t s_flash;
static bool s_written;
static unsigned s_saves;
static bool s_fail_mount;

void notice_port_lock(void) {}

void notice_port_unlock(void) {}

static void wipe_dir(void)
{
    if (!s_dir[0]) return;
    DIR *d = opendir(s_dir);
    if (d) {
        const struct dirent *e;
        char p[340];
        while ((e = readdir(d)) != NULL) {
            if (e->d_name[0] == '.') continue;
            snprintf(p, sizeof p, "%s/%s", s_dir, e->d_name);
            unlink(p);
        }
        closedir(d);
    }
}

static void gone(void)
{
    wipe_dir();
    if (s_dir[0]) rmdir(s_dir);
}

bool notice_port_mount(uint32_t *heap_cost)
{
    *heap_cost = 0;
    if (s_fail_mount) return false;
    if (s_dir[0]) return true;
    const char *tmp = getenv("TMPDIR");
    snprintf(s_dir, sizeof s_dir, "%s/jpanel-notice-XXXXXX", tmp && tmp[0] && strlen(tmp) < 40 ? tmp : "/tmp");
    if (!mkdtemp(s_dir)) {
        s_dir[0] = '\0';
        return false;
    }
    atexit(gone);
    return true;
}

const char *notice_port_base(void)
{
    return s_dir;
}

bool notice_port_fs_info(uint32_t *total, uint32_t *used)
{
    *total = 0x3C0000;
    *used = 0;
    DIR *d = opendir(s_dir);
    if (!d) return false;
    const struct dirent *e;
    char p[340];
    struct stat st;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        snprintf(p, sizeof p, "%s/%s", s_dir, e->d_name);
        if (stat(p, &st) == 0) *used += (uint32_t)st.st_size;
    }
    closedir(d);
    return true;
}

bool notice_port_load(notice_posts_t *out)
{
    if (!s_written) return false;
    *out = s_flash;
    return true;
}

void notice_port_save(const notice_posts_t *in)
{
    s_flash = *in;
    s_written = true;
    s_saves++;
}

void notice_port_fake_wipe(void)
{
    memset(&s_flash, 0, sizeof s_flash);
    s_written = false;
    wipe_dir();
}

unsigned notice_port_fake_saves(void)
{
    return s_saves;
}

void notice_port_fake_fail_mount(bool fail)
{
    s_fail_mount = fail;
}
