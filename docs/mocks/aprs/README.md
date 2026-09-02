# APRS surface — mock rounds and decisions

> **Status:** Living · **Last verified:** 2026-09-02

The GUI gate for `../../plans/APRS_CONTROL_PLAN.md` P3. Sibling of `../sdr-tuner/`,
which holds the interactive tuner's rounds — and whether these two stay siblings or
become one screen is precisely what the first round asks.

## Round 1 — the launcher's shape · `a-launcher-shape.html` · **decided**

Three distinct shapes, not three skins. The round carries a **scoping** question as
much as a layout one, because answering it by argument would have been guessing:

| | Shape | Costs |
|---|---|---|
| **A** | A **tab of the Radio launcher** — Tuner / APRS / Recordings | APRS gets a third of a screen; the feed is short and the map has nowhere to go |
| **B** | **Its own screen, feed first** — the heard log as the spine | Triggers move behind a button; the map needs a home elsewhere |
| **C** | **Its own screen, map first** — geofences drawn, packets as pins, log in a drawer | The most work by a distance, and it demotes the log |

The argument for A is that Tuner, APRS and Recordings share one dongle, one lease and
one mental model, so they plausibly share a screen — and it is one mock round for the
whole radio family rather than two. The argument for C is that if geo triggers are the
point, it is the only shape where a trigger and its meaning are the same object. B is
the middle: the log is what you open it for.

> **Decided (owner, 2026-09-02): A — a tab of the Radio launcher.** With it: the trigger
> list needs *"handling similar to existing tasks that are time based"*.

### What that second half changed

It changed what A draws. The mock's first pass gave the tab a **bespoke armed-list of
switches**, which was a small parallel system nobody needed — the same mistake, one
layer up, that P2 avoids by feeding the existing location core instead of writing a
second geofence evaluator.

An APRS trigger is an `EventTrigger`: the shape `workflow/contracts.py` already models
beside `ScheduleTrigger`, and which the Ops Workflow screen already renders. So the tab
shows the **same automation cards** — same switch, same run history, same run-now — and
an action lands in the section just by declaring a `category`, which the reader is
explicit about ("never a hardcoded id list").

Arming then reuses the **task schedule spec** (`on_demand | once | repeat`), which
`AutomationsScreen` already reuses once. The vocabulary transfers exactly, answering
*when is it listening* rather than *when does it run* — and `once` turns out to be the
one-time-command semantic for free, while `repeat` makes a gate command not exist
outside the hours it would be used. That is a security control, not a convenience.

The plan records the trap this opens: arming must be evaluated at **verify time**, never
as an `ActionSpec.precondition`, because a precondition *defers* rather than refuses —
and a deferred gate command is a gate that opens hours late.

### What else the round decides

**Where "is the receiver actually alive" lives.** Load-bearing, not decoration: a watch
that silently died is worse than no watch, and this family already deleted a control for
the related reason — the tuner's signal meter measured post-demodulation audio and so
read *high* on a dead channel full of hiss (`../sdr-tuner/README.md`). The health
reading here is therefore **last decode time and decode rate**, never a signal bar,
and each variant places it differently: a strip under the tabs (A), a feed header that
ages (B), a map overlay (C).

**How an armed trigger reads at a glance**, and how arming is reached.

### States deliberately in the mock

Per `../../reference/DESIGN.md` (realistic, varied data — empty, long, error, offline):

- **A stale receiver** (nothing for 41 minutes) as its own state, so a dead receiver
  never reads as a quiet channel.
- **A rejected command attempt**, as prominent as an accepted one. The box cannot answer
  over the air, so this screen plus a push is the operator's *only* feedback, and
  "heard you, code wrong" must not look like "never heard you".
- **An untrusted-text packet**, badged. Heard text may never reach a model as
  instructions — a packet becoming an LLM prompt is prompt injection with an antenna —
  and the badge is where that rule becomes visible to the owner rather than staying an
  invariant in a plan.
- A long third-party comment that wraps, and the empty state before anything is heard.

### Verified, not assumed

Driven in Chromium before being offered: all nine shape × state combinations render, no
console errors, the arm switch toggles, and the page does not scroll horizontally.

## Not decided here

Frequency choice (commands want a private simplex channel; position wants the
digipeated network), sender hardware, and packet retention. Those are the plan's §7.
