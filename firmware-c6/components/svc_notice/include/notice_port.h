/* What each build supplies to svc_notice: the device's esp/notice_port_esp.c (littlefs on the
 * `storage` partition, NVS, a critical section), or notice_port_fake.c on the host and in the
 * sim (a temporary directory, RAM, no lock). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "notice_logic.h"

void notice_port_lock(void);
void notice_port_unlock(void);
/* Mounts the files' home, formatting it the first time. `heap_cost` is what the mount took from
 * the internal heap (0 where that means nothing). */
bool notice_port_mount(uint32_t *heap_cost);
/* Where the files are: a path prefix for POSIX calls. */
const char *notice_port_base(void);
/* The partition's size and what is used, in bytes. */
bool notice_port_fs_info(uint32_t *total, uint32_t *used);
/* The unconfirmed state POSTs (namespace `jpanel`, key `notice_posts`). False with nothing. */
bool notice_port_load(notice_posts_t *out);
void notice_port_save(const notice_posts_t *in);
