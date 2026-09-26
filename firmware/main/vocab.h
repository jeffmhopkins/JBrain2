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

/* THE PANEL'S NAME, taken from the wake phrase rather than stored twice.
 *
 * `VOCAB_LISTEN`'s phrase is "hey fish": a carrier word and then the name the twins chose. The
 * name is what the label on the glass shows, and deriving it from the phrase means there is
 * one place to change it when the phrase becomes a per-panel setting — a second constant
 * holding "fish" would be a second thing to forget.
 *
 * Returns the word after the last space, or the whole phrase if it has none. NULL only if no
 * `VOCAB_LISTEN` entry exists, which the host suite forbids. */
const char *vocab_name(void);

/* Rename the pet: rewrites the `VOCAB_LISTEN` phrase to "hey <name>", and stores the box's
   phonemes for it. Returns true when either actually CHANGED, which is the caller's signal to
   re-register the vocabulary with MultiNet — the settings poll runs every three seconds and
   re-registering on each one would rebuild the model's command list twenty times a minute for
   nothing.
   Refuses anything rule 1 refuses (a-z and spaces, case-folded here) and anything too long to
   hold, rather than storing a phrase the model will silently decline: a name that cannot be
   heard leaves a child saying it and getting nothing, which reads as a broken panel.

   `phonemes` IS THE NAME'S ALONE — the carrier ("hey") is this file's word, so this file
   supplies its phonemes; the box knows only what the pet is called. NULL or "" is normal and
   means the box could not pronounce it (an invented name is in no dictionary), and this entry
   then goes back to being converted on-chip, which is where it was for its whole life. A
   string carrying anything outside the phoneme alphabet is treated the same way: see
   `compose_phonemes`. */
bool vocab_set_name(const char *name, const char *phonemes);

typedef enum {
    VOCAB_ACTION = 0, /* play an action */
    VOCAB_FORM,       /* become a body */
    VOCAB_COLOUR,     /* `arg` < 0: next colour in the palette; otherwise that palette index */
    VOCAB_LISTEN,     /* the panel's NAME: start a conversation turn, hands-free */
    VOCAB_STOP,       /* end the conversation: no reply, no follow-up */
    VOCAB_SEND,       /* record a message for someone else; `arg` is a `jpanel_to_t` */
} vocab_kind_t;

typedef struct {
    const char *phrase;  /* what is said, and what the ticker shows */
    /* THE PHRASE AS PHONEMES, precomputed, in the single-letter classes Espressif's
       `tool/multinet_g2p.py` emits. NULL means "convert it at runtime" and is correct for
       exactly one entry, the wake phrase, whose name the owner can change.
     *
       MultiNet7 English decodes PHONEMES, not words — the shipped model's own `vocab` file is
       a language model over these classes — and Espressif's documentation says to run that
       tool, warning that skipping it calls an internal converter at runtime "with potential
       accuracy reduction". This firmware skipped it for its whole life. `speech.c` hands these
       to `esp_mn_commands_phoneme_add`, which is the API whose doc points at the tool.
     *
       SECOND IN THE STRUCT ON PURPOSE: an entry that forgets it puts an enum where a
       `const char *` belongs and fails to build, which is the only way a table of 48 stays
       honest. */
    const char *phonemes;
    vocab_kind_t kind;
    int arg;             /* action_t, face_form_t, jpanel_to_t, or a palette index (VOCAB_COLOUR) */
} vocab_t;

/* The table and its length. */
const vocab_t *vocab_all(void);
int vocab_count(void);

/* The entry for a command id, or NULL. Ids are indices — MultiNet hands back the id it was
   given, so this is the whole lookup. */
const vocab_t *vocab_get(int id);
