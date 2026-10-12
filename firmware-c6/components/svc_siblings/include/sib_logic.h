/* The siblings list's rules, apart from storing or fetching it. Pure C, host-tested: the sim and
 * the host suite feed it the box's own JSON, so they exercise exactly what the panel does. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_siblings.h"

/* The list as the box sent it, in its order. */
typedef struct {
    sibling_t s[SIB_MAX];
    uint8_t n;
} sib_list_t;

/* Who sits in which slot. A slot keeps its sibling for as long as the box lists that device. */
typedef struct {
    sibling_t s[SIB_MAX];
    bool used[SIB_MAX];
} sib_slots_t;

/* `{"siblings":[{"device_id":"…","name":"…"},…]}` → at most SIB_MAX entries. Unknown keys are
 * skipped; an entry with no device id is dropped. False for anything that is not that shape —
 * the slots then stay as they are. */
bool sib_parse(const char *json, size_t len, sib_list_t *out);

/* A name as the panel can draw it: printable ASCII kept, every other character (one code point,
 * however many bytes) a '?', runs of space folded, ends trimmed, at most SIB_NAME_BYTES. */
void sib_clean_name(const char *in, char *out, size_t cap);
/* The tile's letter: the first letter or digit, upper-cased; '?' when there is none. */
char sib_initial(const char *name);
/* One of the four people's colours, from the name, avoiding the ones in `taken` (a bit each)
 * while any is free — two siblings never share a colour unless there are more than four. */
uint8_t sib_accent_for(const char *name, uint8_t taken);

/* Brings `slots` to `list`: a listed device keeps its slot (its name and colour follow the box),
 * a device no longer listed leaves its slot, and a new one takes the lowest free slot. Returns a
 * bit per slot whose device changed — that thread's history belonged to someone else and is
 * cleared — and sets `*renamed` when only names or colours moved. */
uint8_t sib_merge(sib_slots_t *slots, const sib_list_t *list, bool *renamed);
