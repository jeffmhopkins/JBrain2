#include "vocab.h"

#include <stddef.h>

/* Deliberately SHORT. Every phrase here is always listening (no wake word), so each one is a
   chance to fire at the room; a vocabulary that covers everything the twins might say would
   spend most of its day misfiring. These are the things they asked for, plus the gags that
   are worth asking for by name. */
static const vocab_t VOCAB[] = {
    /* THE PANEL'S NAME, and the one phrase here whose cost is not an animation.
     *
     * The owner: *"both of these panels will have a wake word that will allow the same
     * interaction as if I held the panel and it was listening"* — and the twins named this one
     * **fish**.
     *
     * TWO WORDS, AND THE SECOND ONE IS THE NAME. Every other phrase in this table costs a
     * wiggle when it misfires. This one opens the microphone, uploads six seconds of a child's
     * bedroom, calls a language model and makes the pet talk to an empty room — so it is the
     * one entry where `vocab.h`'s warning that "a one-word always-on vocabulary fires at the
     * television" is not a style note. A carrier word in front is what every always-on device
     * does and it is why they do it.
     *
     * It also happens to be the only form that WORKS here: the twins' first choice was
     * "robot", and rule 3 forbids a phrase being a prefix of another — "change into robot" and
     * "be a robot" are already in this table. "hey fish" collides with nothing.
     *
     * COMPILED IN FOR NOW, WHICH IS A GAP. §10.4ab's argument applies: the owner has no
     * terminal, so a name only a rebuild can change is a name they cannot change, and the two
     * panels will want different ones. It belongs on `endpoint_settings` beside the other
     * knobs; `esp_mn_commands_update()` already supports re-registering at runtime. */
    {"hey fish", VOCAB_LISTEN, 0},

    /* THE WAY OUT, and the owner found it the way these things get found: *"now that it auto
     * continues for six turns, it wants to keep going even if I say stop."*
     *
     * Saying "stop" used to be a sentence like any other — transcribed, sent, answered, and
     * followed by the microphone opening again. A conversation that continues itself needs a
     * word that ends it, or the only way out is to stop talking and wait the loop out.
     *
     * ON THE PANEL, NOT THE BOX, because that is where the loop lives: MultiNet resolves it
     * on-chip in under half a second, so it takes effect before a recording is even uploaded,
     * and it works when the box is slow or unreachable — which is exactly when a child would
     * most want to give up.
     *
     * THE SAFEST SINGLE WORD IN THE TABLE, and the only one whose one-word case is easy. Every
     * other entry here costs something when the television says it — a wiggle, or six seconds
     * of a bedroom uploaded. A false "stop" ends a conversation that was not happening. */
    {"stop", VOCAB_STOP, 0},

    /* The ask that started this: one of the twins wanted the robot to be M.E.R.C. Two ways
       to say it, because a four-year-old will say the one you did not think of. */
    {"change into merc", VOCAB_FORM, FORM_OSTRICH},
    {"be an ostrich", VOCAB_FORM, FORM_OSTRICH},
    {"change into robot", VOCAB_FORM, FORM_ROBOT},
    {"be a robot", VOCAB_FORM, FORM_ROBOT},

    /* THE SHORT FORMS, asked for by name: "burp", "fart", "dance", "jump". They break the
       two-word rule in `vocab.h` and the reason that rule exists has not gone away — these
       four are live in a room with a television in it. They are here because they are what a
       four-year-old actually says, which beats a vocabulary that is safe and unused.

       Each one forces a collision, because rule 3 forbids a phrase being a PREFIX of another:
       "jump" would have made "jump up" unreachable and "dance" would have done the same to
       "dance with me". So the long forms that collide are gone or reworded, and the ones that
       merely CONTAIN the word ("do a dance", "do a burp") are untouched — those start with
       "do" and collide with nothing. */
    {"dance", VOCAB_ACTION, ACT_DANCE},
    {"jump", VOCAB_ACTION, ACT_JUMP},
    {"burp", VOCAB_ACTION, ACT_BURP},
    {"fart", VOCAB_ACTION, ACT_FART},
    {"wave", VOCAB_ACTION, ACT_WAVE},
    {"shake", VOCAB_ACTION, ACT_SHIMMY},
    {"laugh", VOCAB_ACTION, ACT_GIGGLE},
    {"eat", VOCAB_ACTION, ACT_EAT},
    {"kick", VOCAB_ACTION, ACT_KICK},
    {"spin", VOCAB_ACTION, ACT_SPIN},

    {"do a dance", VOCAB_ACTION, ACT_DANCE},
    /* Was "dance with me", which "dance" is now a prefix of. Reworded rather than dropped so
       bop keeps a voice — since 0.2.45 dance, bop and shimmy are three different animations
       on the bird, not three names for one. */
    {"come and boogie", VOCAB_ACTION, ACT_BOP},
    /* Was "shake your body" and "wave hello", both of which the new one-word forms are a
       prefix of. Reworded rather than dropped: two ways to ask is the point of having long
       forms at all, since a four-year-old will say the one you did not think of. */
    {"wiggle your body", VOCAB_ACTION, ACT_SHIMMY},
    {"say hello", VOCAB_ACTION, ACT_WAVE},
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
    {"change your color", VOCAB_COLOUR, -1},
    {"pick a new color", VOCAB_COLOUR, -1},

    /* NAMED COLOURS. `arg` is a palette index into `face.c`'s PALETTE, and the two at the end
       (red, blue) exist because this palette had no colour a child would give those names to:
       its nearest red was a rose and its nearest blue a periwinkle. The rest map onto entries
       that were already there and already look like their name.

       "turn X" rather than bare "X": two words, so these obey the rule the four short action
       words break, and no colour word is left live on its own in a room where someone might
       simply say "white" or "orange" in conversation. */
    {"turn red", VOCAB_COLOUR, 11},
    {"turn blue", VOCAB_COLOUR, 12},
    {"turn green", VOCAB_COLOUR, 7},
    {"turn yellow", VOCAB_COLOUR, 3},
    {"turn orange", VOCAB_COLOUR, 4},
    {"turn pink", VOCAB_COLOUR, 8},
    {"turn purple", VOCAB_COLOUR, 9},
    {"turn white", VOCAB_COLOUR, 10},
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

const char *vocab_name(void)
{
    for (int i = 0; i < (int)(sizeof(VOCAB) / sizeof(VOCAB[0])); i++) {
        if (VOCAB[i].kind != VOCAB_LISTEN) continue;
        const char *p = VOCAB[i].phrase, *last = VOCAB[i].phrase;
        while (*p != '\0') {
            if (*p == ' ' && *(p + 1) != '\0') last = p + 1;
            p++;
        }
        return last;
    }
    return NULL;
}
