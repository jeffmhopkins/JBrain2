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
| `c-filter-first.html` | a **filter field** at the top; recents when it is empty | a keyboard on a surface that had none, and typing is slower than tapping when the band is already visible |

**Not built, deliberately:** manual favourites. It is another thing to maintain, and the
history is already the answer to the same question without anyone curating it.

**What every shape needs underneath**, whichever wins: the box has to REMEMBER the picks.
Nothing does today — a band choice goes to the sidecar as a range and is forgotten. That
is a small store keyed by section id with a count and a last-picked time, written when a
band is chosen for either purpose (listen or spectrum).
