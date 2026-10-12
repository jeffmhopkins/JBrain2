/* The twins' panels, by the names the owner gave them on the box (`GET /endpoint/siblings`):
 * who the two message threads are with. A thread is bound to a sibling's device id, not to a
 * name compiled in, so a renamed panel keeps its thread and a different panel never inherits
 * someone else's. Until the box has answered once (and with nothing cached from before a
 * reboot) a thread is "Panel 1" / "Panel 2". */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ui_theme.h"

/* At most this many siblings are kept, each name at most this many UTF-8 bytes. The home pages
 * have two thread tiles: slots 0 and 1. */
#define SIB_MAX 4
#define SIB_NAME_BYTES 24
#define SIB_ID_LEN 37
#define SIB_THREADS 2

typedef struct {
    char device_id[SIB_ID_LEN];
    char name[SIB_NAME_BYTES + 1]; /* already reduced to what the panel's fonts can draw */
    uint8_t accent;                /* 0..3: which of the four people's colours */
} sibling_t;

/* The sibling bound to `slot`; false when nobody is (the thread shows "Panel <slot+1>"). */
bool svc_siblings_slot(uint8_t slot, sibling_t *out);
/* What the thread at `slot` is called, and its tile's letter and colour. */
void svc_siblings_name(uint8_t slot, char *out, size_t cap);
char svc_siblings_initial(uint8_t slot);
ui_accent_t svc_siblings_accent(uint8_t slot);
/* The svc_messages thread a slot's sibling talks in (SVC_MESSAGES_THREAD_1/2), NULL past the
 * two threads the home pages have. */
const char *svc_siblings_thread(uint8_t slot);
