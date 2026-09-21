# Eggs, hatching and growing up — the pet gets a life stage (proposed)

> **Status:** Proposed (icebox) · **Last verified:** 2026-09-21

**Status: proposed / icebox. Not on the roadmap, nothing built.** The owner's idea, recorded
2026-09-21 while the panel firmware was in flight:

> *"Eventually what I want to do is present these as eggs and they have to interact with it x
> amount of times over x amount of minutes and then it'll hatch into a baby version that kind
> of grows up over the course of 2 days. I'm not sure if that would mean that we need
> different variations of a baby form of an ostrich — but it kind of makes sense to me that we
> might."*

Renders on both pet surfaces: the panels (`ROOM_ENDPOINT_PLAN.md`, `firmware/main/face.c`) and
the PWA (`PET_ENDPOINT_PWA_PLAN.md`, `frontend/src/pet/`).

## The short answer to the question asked

**Mostly no, and specifically yes for the ostrich.**

A baby is not new artwork; it is **proportion**. Neoteny is a short list — bigger head, bigger
eyes, shorter limbs, rounder body — and `face.c` is already parameterised for exactly that.
The figure has per-form constants and a whole-figure transform (`SX()`/`SY()` scale
coordinates as they are computed rather than resampling a finished bitmap), and the eyes are
one shared call with a scale argument. So a **single `growth` float, 0..1, threaded through
the existing constants** buys a baby of every form at once.

The exception is the one the owner guessed at. **An ostrich's silhouette IS its neck and its
legs** — that was the finding that made the mock read as a bird rather than a duck, and
`OS_LEG_L` is 92 px of a 428 px figure. A chick has neither. Scaling those two down does not
produce a chick, it produces a small ostrich, and the difference is the whole charm of the
idea. The robot has no such problem: a baby robot really is a big head on short legs.

So the shape to build is:

- **One growth parameter, shared by every form.** Not a second form per stage.
- **A per-form baby silhouette only where proportion alone fails** — which today is the
  ostrich, and is one extra draw function in the place `face.c` already keeps
  `draw_robot()`/`draw_ostrich()` behind one `face_draw()`. Not a second animation system.
- **The rig, the emotions and the gags DO NOT FORK.** A baby's wave is the adult's wave with
  shorter arms. A baby that needs its own action table doubles the content budget forever, and
  `rig.h` already states the governing rule: *a form decides shapes, never behaviour.*

Concretely for the ostrich chick, using numbers that exist: the neck is a 7-segment loop
(drop it to 2-3), `OS_LEG_L 92` → ~34, `OS_HEAD_W 128` → ~112 against a much smaller body, the
eye scale `1.10` → ~1.35, and the crest becomes down rather than three plumes. Every one of
those is an existing primitive with a different number in it.

## What this actually costs, and it is not the drawing

The drawing is the cheap half. Three things are not:

**1. It is new durable pet state, and `jpet/` owns it.** `PetStateInfo` carries name, mood,
emotion, the command script and the room objects — there is **no age, no stage, no interaction
count** anywhere in it. `PET_ENDPOINT_PWA_PLAN.md` §8 is explicit that the PWA plan *"never
invents pet state"*, so this cannot be smuggled in as a rendering feature on either surface. It
is a `jpet/` change first: new columns, a migration, an RLS isolation test per the root
`CLAUDE.md` non-negotiables, and a tick that advances growth on the server's clock rather than
on whichever surface happens to be awake.

**2. One pet per principal, and there are two twins.** `pet_state` is keyed by
`principal_id` + `domain_code` — a single pet row, which is right for a wall display and wrong
for an egg. *Two children each hatching their own egg is the entire point of the idea*, and
one shared pet would make it a race rather than a gift. The good news is that the seam already
planned may deliver it for free: `PET_ENDPOINT_PWA_PLAN.md` P0 mints a scoped `pet_endpoint`
principal, so **a principal per panel gives a `pet_state` row per panel with no schema
reshape** — but that has to be a deliberate decision at P0, not discovered afterwards.

**3. The growth clock runs mostly while he is asleep.** Two days of growth means most of the
state changes happen at nursery or overnight, and both of the interesting moments — the hatch
and the last day of babyhood — are easy to miss entirely. See below; this is the part most
likely to turn a lovely idea into a disappointment.

## Four design constraints this repo already knows

Each of these is a rule the project arrived at the hard way, and each one bites here.

**"Nothing happened" reads as broken.** `variants.c` states it and the render defects of
0.2.36 proved it. "X taps over X minutes" is a *rate limit*, and a rate limit that answers a
touch with nothing is the exact failure. So: **the egg always reacts** — it wobbles, it
thunks, it rocks toward the finger — and only the *progress toward hatching* is rate-limited.
The child never gets told "not yet"; he gets an egg that is fun to poke and that happens to
crack a little when it is due.

**Preschoolers love repetition, and a one-way ladder fights that.** The same constraint that
shaped the variant pools ("let him get the burp a hundred times, make the burp different each
time") applies to a life stage. A pet that grows up *once, forever* is a toy that gets better
and then stops. Two cheap answers, and they compose: `lay an egg` is **already a shipped
chicken trick** (`jpet/intents.py`), which is a natural, in-character way to get a new egg;
and an adult pet that can be asked to be a baby again costs nothing when growth is a float.
Recommend both — irreversibility buys nothing here that anticipation does not buy better.

**The hatch must wait for him.** Making the hatch fire on a timer guarantees that the best
moment in the whole feature happens to an empty room. Split it: **"ready to hatch" is a
state**, reached by interaction, and the panel then *shows* it (rocking, a crack, a sound) and
**holds there until he touches it**. Nothing is lost by waiting and the payoff is his.

**Quiet hours cut luminance, not just volume.** An egg that wobbles invitingly at 3 a.m. in a
four-year-old's bedroom is a worse bug than any of the ones fixed this week. Growth is a
server-side clock; *display* of growth obeys the existing quiet-hours rule.

## The cheapest first slice, if it is ever picked up

**The egg alone, no hatching.** An egg is an ellipse, a shadow and a crack that advances with
a counter — the single cheapest thing this panel can draw, and the progress indicator *is* the
artwork. It needs one new integer of pet state and no new form, no growth parameter and no
per-form work. It would say within a day whether the twins poke an egg for a week, which is
the only question that decides whether the rest is worth building.

## Open questions, deliberately unanswered

- **What counts as an interaction?** Taps only, or does talking to it count? The panel can now
  hear 23 phrases (0.2.37) and "wake up" to an egg is a lovely thing for a child to try.
- **Does the egg know which twin?** Two panels, two eggs, one box. If the pets are distinct,
  are they aware of each other — and is that a feature or a rivalry?
- **Does the form survive the hatch, or does the egg decide it?** An egg that could hatch into
  either body makes the hatch a surprise; a chosen form makes it a promise. Four-year-olds are
  badly served by surprise when they have already picked.
- **Two days of what, exactly?** Wall-clock, or accumulated interaction? Wall-clock is simpler
  and grows a neglected pet anyway; interaction-based rewards attention but can strand a pet
  half-grown for a month.
