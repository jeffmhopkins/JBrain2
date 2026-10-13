/* The host's stand-in for `storage` and the owed POSTs' NVS blob (notice_port_fake.c). */
#pragma once

#include <stdbool.h>

/* A first boot: no files, nothing owed. */
void notice_port_fake_wipe(void);
/* How many times the owed POSTs were written. */
unsigned notice_port_fake_saves(void);
/* A partition that will not mount (a board with a bad flash region). */
void notice_port_fake_fail_mount(bool fail);
