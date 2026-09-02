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


## Round 2 — the command task · `b-trigger-editor.html` · **awaiting decision**

Reframed 2026-09-02 before any decision, because the scope changed under it. The first
pass asked how to edit a *general* APRS trigger — any event, any pipeline from the
action registry. The owner then cut it to what is actually being built:

- **the gate only** — the authenticated command path; no geofence, no position
- **one action type — an agent task**, not a pipeline picker
- *"duplicate the time based system"*

That cut dissolves most of the original question. If the only thing a verified command
can do is run an agent task, then **an APRS task is a task**: same name, prompt, agent,
scopes, push, enabled (`TasksScreen.Draft`). Only the trigger differs — a verified radio
command instead of a clock. Container and form language stop being interesting, because
Tasks already answers both.

What is left is one structural decision:

> **Is "on command" a fourth trigger kind of the task system, or a parallel task
> system living in the Radio tab?**

| | Shape | Costs |
|---|---|---|
| **A** | **Fourth kind** — `ScheduleKind` gains `on_command`; one list, one editor, one runs history | Command tasks sit in a general Tasks list, so the radio screen is not where you manage them |
| **B** | **Parallel list** — its own collection in Radio → APRS, same components | **A second place tasks live**: two editors to keep in step, and every later task feature built twice or silently diverging |
| **C** | **Two doors** — Tasks own the data; the Radio tab is a filtered view opening the same editor | Two entry points to one object need care so they cannot disagree; more work than A for a navigational benefit |

### A note on "duplicate"

Taken literally, that is **B** — and it is worth weighing before choosing, because this
plan has already faced the same shape of decision twice and gone the other way both
times: P2 feeds the existing location core rather than writing a second geofence
evaluator, and round 1 dropped a bespoke armed-list for the automation cards that
already exist. A parallel task system would be the third instance and the first taken
the other way. **A is that instinct applied once more**; C is the compromise that keeps
the radio screen as the place you manage radio tasks.

### What the narrower scope changed about the safety story

Dropping the pipeline picker removes the permission-class cap with it, so **the task's
scopes are now the cap**. The editor says so: selecting Medical, Financial or Location
raises a warning, because a radio-triggered task reaching a firewalled domain on a
command sent over the air is the thing to be deliberate about, and there is no longer a
pipeline class behind it. The prompt is fixed and the mock says plainly that nothing
heard over the air is ever put into it.

The trust tier also stops being a choice: only a verified command reaches a task at all,
so the editor states the guarantee rather than offering tiers.

### Verified, not assumed

Driven in Chromium: all six shape × view combinations render, A's editor offers the
fourth kind while B and C are command-only, A's list correctly shows command *and*
scheduled tasks together while B and C show command only, the scope warning appears only
once a firewalled domain is selected, no console errors, no horizontal scroll.
