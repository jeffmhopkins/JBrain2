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


## Round 2 — add and edit a trigger · `b-trigger-editor.html` · **awaiting decision**

Round 1 settled the list. This settles **creating and editing** one, which is a new
surface and so takes its own round — an editor is not "a chip state or a button on an
existing card" (`../../reference/DESIGN.md`'s exemption).

### Why this needed a round rather than a decision

The owner asked for it "like tasks … in a new modal", and two established paradigms
point in opposite directions:

- **Tasks, the thing being copied, is not a modal.** `TasksScreen.Editor` is a
  **full-screen layer** over the list with a back chevron (`useBackLayer`), which is
  where DESIGN.md's paradigm table sends *primary tasks*.
- **DESIGN.md sends "contextual quick forms & actions" to the bottom sheet**, "the
  workhorse modal on phone" — and a trigger is squarely that, far smaller than a task's
  prompt + scopes + schedule.

So the round decides the container as much as the form language.

| | Shape | Costs |
|---|---|---|
| **A** | **Task-editor layer** — full screen, back chevron, stacked sections | Heaviest container for a four-field object; hides the list you may be copying from |
| **B** | **Sentence sheet** — the trigger reads as one sentence, every value an inline picker | Least conventional; long values must wrap gracefully |
| **C** | **Sectioned sheet** — bottom sheet, conventional numbered field groups | Least distinctive; a tall sheet scrolls about as much as a full screen |

B's argument is that the sentence **is** the card headline (`When ‹ev› → run
‹pipeline›`), so the editor and the row become one object seen twice, with nothing to
translate between reading and editing. C's numbering is not decoration: what you listen
for decides which fields and which trust tier apply, so the order is a real dependency
chain — which is the test for whether numbering earns its place.

### Why add/edit is new at all

Automations are **seeded system config**: the Ops screen can enable, retime and run
them, but never *create* one. An APRS trigger is owner-created — precisely the
capability the automations paradigm lacks. Hence the split settled across the two
rounds: **list like Automations, edit like Tasks.**

### Load-bearing content in the mock

- **The trust tier is shown, not implied.** Switch *Trigger type* between Command and
  Geofence: a command trigger says it is authenticated by HMAC and counter; a geofence
  trigger says it is *not*, and that it may start a chat only from a fixed prompt,
  never from anything heard over the air. The editor is where the owner meets that
  rule, so it cannot live only in a plan.
- **The permission-class cap is visible.** Each action carries its class, and
  `sensitive` renders unavailable rather than being silently filtered out — a cap you
  cannot see is a cap that surprises you.
- **Arming is the task schedule spec**, on demand / once / repeat.

### Verified, not assumed

Driven in Chromium: all six shape × trigger-type combinations render, the trust-tier
panel switches with the type, the `sensitive` action stays unselectable even when the
click is forced past its disabled state, no console errors, no horizontal scroll.
