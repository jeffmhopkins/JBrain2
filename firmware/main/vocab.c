#include "vocab.h"

#include <stddef.h>

/* Deliberately SHORT. Every phrase here is always listening (no wake word), so each one is a
   chance to fire at the room; a vocabulary that covers everything the twins might say would
   spend most of its day misfiring. These are the things they asked for, plus the gags that
   are worth asking for by name. */
static const vocab_t VOCAB[] = {
    /* The ask that started this: one of the twins wanted the robot to be M.E.R.C. Two ways
       to say it, because a four-year-old will say the one you did not think of. */
    {"change into merc", VOCAB_FORM, FORM_OSTRICH},
    {"be an ostrich", VOCAB_FORM, FORM_OSTRICH},
    {"change into robot", VOCAB_FORM, FORM_ROBOT},
    {"be a robot", VOCAB_FORM, FORM_ROBOT},

    {"do a dance", VOCAB_ACTION, ACT_DANCE},
    {"dance with me", VOCAB_ACTION, ACT_BOP},
    {"shake your body", VOCAB_ACTION, ACT_SHIMMY},
    {"wave hello", VOCAB_ACTION, ACT_WAVE},
    {"say hello", VOCAB_ACTION, ACT_WAVE},
    {"jump up", VOCAB_ACTION, ACT_JUMP},
    {"bounce around", VOCAB_ACTION, ACT_BOING},
    {"nod your head", VOCAB_ACTION, ACT_NOD},
    {"be silly", VOCAB_ACTION, ACT_WIGGLE},
    {"make me laugh", VOCAB_ACTION, ACT_GIGGLE},
    /* Peekaboo by name. It is the action most worth asking for and the hardest to discover by
       poking, since `hide` looks like sitting down until the hands come up. */
    {"play peekaboo", VOCAB_ACTION, ACT_HIDE},
    {"hide your eyes", VOCAB_ACTION, ACT_HIDE},
    {"go to sleep", VOCAB_ACTION, ACT_SLEEP},
    {"wake up now", VOCAB_ACTION, ACT_BOING},
    /* The gags. A four-year-old will ask for these more than everything above combined, and
       the long bewildered hold after them is the joke (`rig.c`). */
    {"do a burp", VOCAB_ACTION, ACT_BURP},
    {"make a rude noise", VOCAB_ACTION, ACT_FART},

    /* "color", not "colour", and this is the one place in the repo that spells it that way.
       The phrase is not prose — it is fed to MultiNet's English grapheme-to-phoneme pass,
       whose lexicon is American. The two spellings are the same sound; only one is looked
       up rather than guessed at. */
    {"change your color", VOCAB_COLOUR, 0},
    {"pick a new color", VOCAB_COLOUR, 0},
};

const vocab_t *vocab_all(void)
{
    return VOCAB;
}

int vocab_count(void)
{
    return (int)(sizeof(VOCAB) / sizeof(VOCAB[0]));
}

const vocab_t *vocab_get(int id)
{
    if (id < 0 || id >= vocab_count()) return NULL;
    return &VOCAB[id];
}
