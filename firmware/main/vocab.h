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
 *  2. **Two words minimum.** WakeNet is DISABLED — the owner asked it to just listen — so
 *     every phrase here is always live. A one-word always-on vocabulary fires at the
 *     television, and a robot that reacts to the room reads as broken rather than as clever.
 *  3. **No phrase is a prefix of another.** Two commands where one contains the other make
 *     the shorter one unreachable and the longer one unreliable.
 *
 * Pure C: the table and its rules are testable on the host, which is where they are checked.
 */

typedef enum {
    VOCAB_ACTION = 0, /* play an action */
    VOCAB_FORM,       /* become a body */
    VOCAB_COLOUR,     /* next colour in the palette */
} vocab_kind_t;

typedef struct {
    const char *phrase;  /* what is said, and what the ticker shows */
    vocab_kind_t kind;
    int arg;             /* action_t, face_form_t, or unused */
} vocab_t;

/* The table and its length. */
const vocab_t *vocab_all(void);
int vocab_count(void);

/* The entry for a command id, or NULL. Ids are indices — MultiNet hands back the id it was
   given, so this is the whole lookup. */
const vocab_t *vocab_get(int id);
