#include "vocab.h"

#include <stdio.h>
#include <stddef.h>
#include <string.h>

/* Deliberately SHORT. Every phrase here is always listening (no wake word), so each one is a
   chance to fire at the room; a vocabulary that covers everything the twins might say would
   spend most of its day misfiring. These are the things they asked for, plus the gags that
   are worth asking for by name. */
/* THE ONE PHRASE THAT IS NOT IN FLASH, because it is the one the owner has to be able to
   change. Everything else here is fixed vocabulary; this is the pet's NAME, and the note below
   the table has asked since it was written for it to stop being a rebuild away.
   Sized for "hey " plus a name the box caps well under this. */
static char s_listen[24] = "hey fish";
/* AND ITS PRONUNCIATION, which arrives from the box beside the name.
 *
   Every other phrase in this table was converted to phonemes when this firmware was built and
   sits in flash beside its words. This one could not be: it carries the NAME, and the owner
   changes that from the PWA, so there was nothing to convert at build time. It was therefore
   the only phrase still going through the runtime converter Espressif's own documentation
   warns about — and it is the phrase that starts every conversation.

   The box has the name and a dictionary, so the box converts it and sends the result on the
   settings poll it already answers (`pet_name_phonemes`; `backend/src/jbrain/g2p.py`).

   EMPTY IS A REAL AND EXPECTED STATE, not a failure: an invented name is not a word any
   dictionary holds, and the box sends "" rather than a guess. `speech.c` reads empty as
   "convert this one at runtime", which is exactly where this phrase already was — so a name
   the box cannot pronounce is no worse off than before, and a name it can is better. */
static char s_listen_phon[48] = "";
/* The carrier word's own phonemes. "hey" is spelled here rather than sent, because the carrier
   belongs to this file: the box sends the phonemes of the NAME and knows nothing about what
   this firmware puts in front of it. Keep the two in step — `hd` is `HH EY1` through the
   alphabet in `tool/multinet_g2p.py`, and the host suite pins it against the phrase. */
#define LISTEN_CARRIER_PHON "hd"

static vocab_t VOCAB[] = {
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
     * NO LONGER COMPILED IN: §10.4ab's gap is closed. The name lives on `endpoint_settings`,
     * arrives on the settings poll and is re-registered live by `vocab_set_name` — the owner
     * has no terminal, so a name only a rebuild could change was a name they could not change.
     * What is compiled in is the FALLBACK, which is what a panel answers to until the box
     * tells it otherwise. */
    /* THE ONE ENTRY WHOSE PHONEMES ARE NOT IN FLASH. Every other line here was converted when
       this firmware was built; this one carries the pet's NAME, which the owner changes from
       the PWA, so it has no build-time value. The box converts it and sends it on the settings
       poll, and `s_listen_phon` above says what happens when it cannot. */
    {s_listen, s_listen_phon, VOCAB_LISTEN, 0},

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
     * IT IS THE WORD SAID TWICE, and that is the whole reason it is two words. Bare "stop"
     * was the safest single word in the table — the others cost a wiggle or a recording when
     * the television says one, while a false "stop" only ends a conversation that was not
     * happening — but a one-word entry that can be said another way should be, and this one
     * can.
     *
     * It was "fish stop" first, on the argument that ending a conversation with the pet's
     * name mirrors starting one with it ("hey fish"). The owner changed it, and the new one is
     * better for a reason the symmetry argument missed: A CHILD WHO WANTS IT TO STOP IS NOT
     * COMPOSING A PHRASE. Repetition is what escalation sounds like at four — "stop stop" is
     * already what they say, where "fish stop" is a thing they have to remember to say. The
     * phrase most likely to be uttered in the moment it is needed wins, and that is not
     * always the tidiest one.
     *
     * It still costs nothing in false triggers that bare "stop" did not already cost less of:
     * a television saying "stop" twice in a row ends a conversation that was not happening. */
    {"stop stop", "STnP STnP", VOCAB_STOP, 0},

    /* VOICE POST, AND THE TWO PHRASES ARE THE WHOLE INTERFACE TO IT.
     *
     * The owner: *"cross panel or panel to pwa messaging… 'send message' / 'send Dad a
     * message'."* A four-year-old cannot pick a recipient off a list, so the recipient is
     * part of the sentence — which is also why there are exactly two of these and not a
     * general "send a message to X": the name of the person would have to be in this table,
     * and the twins' names are not (JPANEL_PLAN.md §5).
     *
     * THEY WERE "send a message" AND "send dad a message", AND NEITHER EVER FIRED. Measured on
     * two panels on 0.3.00: `do a dance` fires, `dance` does not, `spin` does, and neither send
     * phrase does. The old comment here called the pair's margin "the entire margin" — they
     * differ only at their second word, `a` against `dad` — and that now looks less like a rule
     * 3 near-miss than like the reason both failed: two phrases that sound alike appear to split
     * the confidence between them, and §10.4bf already records how thin it is to begin with
     * (`spin` fired at p=13/100).
     *
     * The owner's call, and the shape of it is his: *"let's change it up to 'tell Dad' and
     * 'tell sister'."* Short, distinctive, no shared carrier with each other or with the wake
     * phrase. "hey dad" was considered and rejected for two reasons — it is among the most-said
     * sentences in a house with a dad in it, and this vocabulary is ALWAYS LIVE (rule 2), so it
     * would record a message every time a child spoke to him; and it would have shared the `hey`
     * carrier with the wake phrase, which is the very collision suspected of killing the pair
     * being replaced.
     *
     * "sister" HARDCODES A RELATIONSHIP, which is a known cost rather than an oversight. The box
     * already knows each panel's sibling by name — it is what the pop-up says out loud — and
     * `endpoint_panel` can now carry per-panel strings to the firmware, so "tell Lydian" on one
     * unit and "tell Elora" on the other is the better shape once there is evidence this one
     * fires at all. That is the next step, not this one.
     *
     * THE ARGUMENT IS A `jpanel_to_t`, not a face or an action, and it is the first entry in
     * this table whose `arg` means something outside `face.h` and `rig.h`. Spelled as the
     * literal rather than by including `jpanel.h`: this file is pure C and built on the host,
     * and `jpanel.h` drags in `cfg.h` and the ESP headers behind it. The host suite pins the
     * two values together so the spelling cannot drift. */
    {"tell sister", "TfL SgSTk", VOCAB_SEND, 0}, /* JPANEL_TO_PANEL — the twin's unit */
    {"tell dad", "TfL DaD", VOCAB_SEND, 1},    /* JPANEL_TO_DAD — the owner's PWA */

    /* The ask that started this: one of the twins wanted the robot to be M.E.R.C. Two ways
       to say it, because a four-year-old will say the one you did not think of. */
    {"change into merc", "pdNq gNTo MkK", VOCAB_FORM, FORM_OSTRICH},
    {"be an ostrich", "Bm aN eSTRgp", VOCAB_FORM, FORM_OSTRICH},
    {"change into robot", "pdNq gNTo RbBnT", VOCAB_FORM, FORM_ROBOT},
    {"be a robot", "Bm c RbBnT", VOCAB_FORM, FORM_ROBOT},

    /* THE SHORT FORMS, asked for by name: "burp", "fart", "dance", "jump". They break the
       two-word rule in `vocab.h` and the reason that rule exists has not gone away — these
       four are live in a room with a television in it. They are here because they are what a
       four-year-old actually says, which beats a vocabulary that is safe and unused.

       Each one forces a collision, because rule 3 forbids a phrase being a PREFIX of another:
       "jump" would have made "jump up" unreachable and "dance" would have done the same to
       "dance with me". So the long forms that collide are gone or reworded, and the ones that
       merely CONTAIN the word ("do a dance", "do a burp") are untouched — those start with
       "do" and collide with nothing. */
    {"dance", "DaNS", VOCAB_ACTION, ACT_DANCE},
    {"jump", "qcMP", VOCAB_ACTION, ACT_JUMP},
    {"burp", "BkP", VOCAB_ACTION, ACT_BURP},
    {"fart", "FnRT", VOCAB_ACTION, ACT_FART},
    {"wave", "WdV", VOCAB_ACTION, ACT_WAVE},
    {"shake", "sdK", VOCAB_ACTION, ACT_SHIMMY},
    {"laugh", "LaF", VOCAB_ACTION, ACT_GIGGLE},
    {"eat", "mT", VOCAB_ACTION, ACT_EAT},
    {"kick", "KgK", VOCAB_ACTION, ACT_KICK},
    {"spin", "SPgN", VOCAB_ACTION, ACT_SPIN},

    {"do a dance", "Do c DaNS", VOCAB_ACTION, ACT_DANCE},
    /* Was "dance with me", which "dance" is now a prefix of. Reworded rather than dropped so
       bop keeps a voice — since 0.2.45 dance, bop and shimmy are three different animations
       on the bird, not three names for one. */
    {"come and boogie", "KcM cND BoGm", VOCAB_ACTION, ACT_BOP},
    /* Was "shake your body" and "wave hello", both of which the new one-word forms are a
       prefix of. Reworded rather than dropped: two ways to ask is the point of having long
       forms at all, since a four-year-old will say the one you did not think of. */
    {"wiggle your body", "WgGcL YeR BnDm", VOCAB_ACTION, ACT_SHIMMY},
    {"say hello", "Sd hcLb", VOCAB_ACTION, ACT_WAVE},
    {"bounce around", "BtNS ktND", VOCAB_ACTION, ACT_BOING},
    {"nod your head", "NnD YeR hfD", VOCAB_ACTION, ACT_NOD},
    {"be silly", "Bm SgLm", VOCAB_ACTION, ACT_WIGGLE},
    {"make me laugh", "MdK Mm LaF", VOCAB_ACTION, ACT_GIGGLE},
    /* Peekaboo by name. It is the action most worth asking for and the hardest to discover by
       poking, since `hide` looks like sitting down until the hands come up. */
    {"play peekaboo", "PLd PmKcBo", VOCAB_ACTION, ACT_HIDE},
    {"hide your eyes", "hiD YeR iZ", VOCAB_ACTION, ACT_HIDE},
    {"go to sleep", "Gb To SLmP", VOCAB_ACTION, ACT_SLEEP},
    {"wake up now", "WdK cP Nt", VOCAB_ACTION, ACT_BOING},
    /* The gags. A four-year-old will ask for these more than everything above combined, and
       the long bewildered hold after them is the joke (`rig.c`). */
    {"do a burp", "Do c BkP", VOCAB_ACTION, ACT_BURP},
    {"make a rude noise", "MdK c RoD NuZ", VOCAB_ACTION, ACT_FART},

    /* A WAY IN THAT IS NOT ONE WORD, FOR EVERY ACTION.
     *
     * The owner: *"burp has been on there. It never actually activates them. The kids say the
     * word — like the code word is wrong."* Four actions had a single word as their ONLY
     * phrasing (eat, jump, kick, spin) and two more had alternates no four-year-old would
     * ever say ("make a rude noise"). A one word phrase is the FRAGILE form here — three
     * phonemes for "burp" against ten for "come and boogie" — so it must never be the only
     * way to ask for something. `tests.c` now holds that.
     *
     * None of these may start with the single word it backs up: "jump up high" would make
     * "jump" a prefix of it, which rule 3 forbids and which MultiNet refuses outright. */
    {"can you burp", "KaN Yo BkP", VOCAB_ACTION, ACT_BURP},
    {"can you fart", "KaN Yo FnRT", VOCAB_ACTION, ACT_FART},
    {"do a big jump", "Do c BgG qcMP", VOCAB_ACTION, ACT_JUMP},
    {"have a snack", "haV c SNaK", VOCAB_ACTION, ACT_EAT},
    {"give it a kick", "GgV gT c KgK", VOCAB_ACTION, ACT_KICK},
    {"do a spin", "Do c SPgN", VOCAB_ACTION, ACT_SPIN},

    /* "color", not "colour", and this is the one place in the repo that spells it that way.
       The phrase is not prose — it is fed to MultiNet's English grapheme-to-phoneme pass,
       whose lexicon is American. The two spellings are the same sound; only one is looked
       up rather than guessed at. */
    {"change your color", "pdNq YeR KcLk", VOCAB_COLOUR, -1},
    {"pick a new color", "PgK c No KcLk", VOCAB_COLOUR, -1},

    /* NAMED COLOURS. `arg` is a palette index into `face.c`'s PALETTE, and the two at the end
       (red, blue) exist because this palette had no colour a child would give those names to:
       its nearest red was a rose and its nearest blue a periwinkle. The rest map onto entries
       that were already there and already look like their name.

       "turn X" rather than bare "X": two words, so these obey the rule the four short action
       words break, and no colour word is left live on its own in a room where someone might
       simply say "white" or "orange" in conversation. */
    {"turn red", "TkN RfD", VOCAB_COLOUR, 11},
    {"turn blue", "TkN BLo", VOCAB_COLOUR, 12},
    {"turn green", "TkN GRmN", VOCAB_COLOUR, 7},
    {"turn yellow", "TkN YfLb", VOCAB_COLOUR, 3},
    {"turn orange", "TkN eRcNq", VOCAB_COLOUR, 4},
    {"turn pink", "TkN PglK", VOCAB_COLOUR, 8},
    {"turn purple", "TkN PkPcL", VOCAB_COLOUR, 9},
    {"turn white", "TkN WiT", VOCAB_COLOUR, 10},
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

/* The box's phonemes for the name, as "<carrier> <name>", or "" if they cannot be used.
 *
   REFUSES RATHER THAN REPAIRS. A phoneme string is not readable by the person who would have
   to notice it is wrong: it is single letters whose case is significant, and a stray character
   is invisible in a log line. So anything outside the alphabet — or too long to hold whole —
   yields "", which puts this phrase back on the runtime converter. Being converted on-chip is
   a known, working state; being registered with a pronunciation nobody says is a panel that
   has gone deaf for no visible reason. */
static void compose_phonemes(const char *phon, char *out, size_t cap)
{
    out[0] = '\0';
    if (phon == NULL || phon[0] == '\0') return;
    for (const char *p = phon; *p != '\0'; p++) {
        const char c = *p;
        /* The classes `tool/multinet_g2p.py` emits, plus the space between words. */
        if (c != ' ' && (c < 'a' || c > 'z') && (c < 'A' || c > 'Z')) return;
    }
    const int n = snprintf(out, cap, "%s %s", LISTEN_CARRIER_PHON, phon);
    if (n < 0 || (size_t)n >= cap) out[0] = '\0'; /* truncated is wrong, not short */
}

bool vocab_set_name(const char *name, const char *phonemes)
{
    /* RULE 1 OF THIS FILE, ENFORCED RATHER THAN ASSUMED: MultiNet's grapheme-to-phoneme pass
       takes lowercase words and spaces, and silently refuses anything else — leaving the panel
       deaf to exactly that one phrase with nothing on screen to say so. The box validates too,
       but a panel that trusted it would be deaf on the say-so of a route it cannot see. */
    if (name == NULL || name[0] == '\0') return false;
    char phrase[sizeof(s_listen)];
    int n = snprintf(phrase, sizeof(phrase), "hey ");
    for (const char *p = name; *p != '\0'; p++) {
        char c = *p;
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
        if (c != ' ' && (c < 'a' || c > 'z')) return false;
        if (n >= (int)sizeof(phrase) - 1) return false; /* longer than we can hold: refuse whole */
        phrase[n++] = c;
    }
    phrase[n] = '\0';
    /* BOTH HALVES DECIDE. The pronunciation can change while the name does not — the box
       learns a name it could not convert before, or stops being able to — and re-registering
       is the only way that reaches the model. Checking the name alone would leave a panel
       listening for the old sound of a name it had already accepted. */
    char phon[sizeof(s_listen_phon)];
    compose_phonemes(phonemes, phon, sizeof(phon));
    if (strcmp(phrase, s_listen) == 0 && strcmp(phon, s_listen_phon) == 0) return false;
    snprintf(s_listen, sizeof(s_listen), "%s", phrase);
    snprintf(s_listen_phon, sizeof(s_listen_phon), "%s", phon);
    return true;
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
