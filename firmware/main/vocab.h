#pragma once

#include <stdbool.h>

#include "face.h"
#include "rig.h"

/* WHAT THE PANEL CAN BE TOLD. One table, in flash, and the whole vocabulary of the thing.
 *
 * MultiNet resolves a list you hand it in advance (see `speech.h` for why that is not a
 * limitation we chose), so this file IS the feature: a phrase that is not here cannot be
 * heard, and a phrase that is here is heard in under half a second without a network.
 *
 * THREE RULES THE PHRASES OBEY, and the host suite enforces all three:
 *
 *  1. **Lowercase a-z and spaces only.** MultiNet's English grapheme-to-phoneme pass takes
 *     words; a digit or an apostrophe is silently refused and the panel is then deaf to
 *     exactly that one thing, with nothing on screen to say so.
 *  2. **Two words minimum, EXCEPT for four the owner asked for by name.** WakeNet is
 *     DISABLED — the owner asked it to just listen — so every phrase here is always live, and
 *     a one-word always-on vocabulary fires at the television. That rule still holds for
 *     everything a phrase might be added for in future. It is relaxed for exactly `burp`,
 *     `fart`, `dance`, `jump`, `wave`, `shake`, `laugh`, `eat`, `kick` and `spin`, which the
 *     owner asked for in those words because they are what a four-year-old actually says; the
 *     allowance is a NAMED LIST in the host suite rather than a loosened check, so the next
 *     single word has to be argued for too. The list has already grown once, which is the
 *     argument for it being a list.
 *     The cost is real and is paid in false triggers, which makes the confidence floor in
 *     `speech.c` more urgent rather than less.
 *  3. **No phrase is a prefix of another.** Two commands where one contains the other make
 *     the shorter one unreachable and the longer one unreliable.
 *
 * Pure C: the table and its rules are testable on the host, which is where they are checked.
 */

typedef enum {
    VOCAB_ACTION = 0, /* play an action */
    VOCAB_FORM,       /* become a body */
    VOCAB_COLOUR,     /* `arg` < 0: next colour in the palette; otherwise that palette index */
    VOCAB_LISTEN,     /* the panel's NAME: start a conversation turn, hands-free */
} vocab_kind_t;

typedef struct {
    const char *phrase;  /* what is said, and what the ticker shows */
    vocab_kind_t kind;
    int arg;             /* action_t, face_form_t, or a palette index (see VOCAB_COLOUR) */
} vocab_t;

/* The table and its length. */
const vocab_t *vocab_all(void);
int vocab_count(void);

/* The entry for a command id, or NULL. Ids are indices — MultiNet hands back the id it was
   given, so this is the whole lookup. */
const vocab_t *vocab_get(int id);
