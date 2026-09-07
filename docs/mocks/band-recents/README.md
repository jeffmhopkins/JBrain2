# Reordering the band picker by what you actually tune

> **Status:** Living · **Last verified:** 2026-09-07

**Why now.** The table went from 32 curated sections to **57** in one change — every
reachable HF utility, broadcast and amateur band, plus everything the tuner reaches
between 24 and 50 MHz. That is the coverage the owner asked for and it makes the
picker long enough that the band you want is usually off-screen. The picker's order has
always been the TABLE's order (`bands.py` browsing order, grouped by band), which knows
nothing about this box or this owner.

**The ask, verbatim:** "maybe because there are so many bands, we can maybe reorganize
the band list by which band we've picked in the past?"

Three shapes were built and are interactive — tap rows and the list rearranges, so the
behaviour can be felt rather than imagined. Open them side by side:

| | shape | what it costs |
|---|---|---|
| `a-recent-on-top.html` | a pinned **Recent** group above an otherwise unchanged list | one new section; the same band appears twice (once recent, once in its group) |
| `b-sorted-by-use.html` | the same groups, **reordered by how often** each is picked, rows and groups both | the list stops being in a fixed place — muscle memory has nothing to hold on to |
| **`c-filter-first.html`** | a **filter field** at the top; recents when it is empty | **CHOSEN 2026-09-07** |

**C is the binding spec.** At 57 sections the fastest path is not scrolling: the field
matches a band's name, its group, its note, or the frequency range as written on the row
— so `27` finds CB and `air` finds en-route control, which is how a band is actually
remembered. While the field is empty the list is exactly the one that was always there,
with what this device tuned lately on top of it.

**Its costs, accepted with the choice:** a keyboard on a surface that had none, and A's
duplicate row — a recent band appears both above and in its own group. The second is
deliberate rather than tolerated: the groups are how someone browses when they do not
already know what they want, and a band that vanished from its group when it became
recent would be worse than seeing it twice.

**Not built, deliberately:** manual favourites. It is another thing to maintain, and the
history is already the answer to the same question without anyone curating it.

**What every shape needed underneath**, and what shipped: `frontend/src/sdrBandPicks.ts`,
a store keyed by section id holding when it was last picked. **Device-local**, like the
theme and the tasks/viewed markers — what an owner reaches for is a property of how they
use this phone, it is worth nothing to anyone else, and a lost history costs one scroll
rather than a broken screen. Capped at 40 entries so a key that only grows cannot, and
every read filtered rather than trusted: one non-date value reaching `Date.parse` would
sort the Recent group into an order nothing explains.

**Only the last four are shown.** Enough for a session's habits, few enough that the
groups below stay visible without scrolling — the point of the shape is that the list
underneath is unchanged.
