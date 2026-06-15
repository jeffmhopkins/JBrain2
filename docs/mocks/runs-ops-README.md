# Runs — Ops surface (Phase 5, Wave 1, Track D) — three directions

Three interactive mockups for the run-log **"Runs"** Ops surface, the front end of the
workflow engine (`docs/WORKFLOW_ENGINE_PLAN.md` §5 Track D). All three convey the same
data — a list of recent runs (kind `agent`/`integration`/`pipeline`, status
`running`/`done`/`failed`, trigger/pipeline name, start, duration, step count, token
cost, and a failed run's `last_error`), a drill into each run's **step tree** (model
turn / tool call / enqueued job, with ok/error state, name, per-step cost, and the
failing step's error), and a **re-run / emergency-trigger** control to fire a sweep
"now" from Ops. Each is single-file, dark-first with a working theme toggle, phone-
framed, tokens-only (no raw hex outside the token sheet), outline icons, no emoji.

They differ on three axes the brief called out: **list-first vs timeline-first vs
ops-dashboard**, **how the step tree is disclosed** (inline accordion vs full-screen
drill vs split panel), and **how prominently the emergency-trigger sits** (footer vs
top-bar bolt vs first-class control row).

## A — list-first, inline step accordion · `runs-ops-a-list-accordion.html`

A scannable run list grouped by recency (Active / Earlier today / Yesterday) with a
horizontal kind/status filter chip row. Each row carries kind icon, name, status badge
(running pulses), and a tabular meta line (trigger · start · duration · steps · cost);
a failed run shows its `last_error` inline in a rose strip. Tapping a row **expands its
step tree inline** — a vertical rail with ok/error/running nodes, per-step kind tags
and cost, a failing step's error in a mono inset, and Re-run / View-log buttons. The
**sweep "now"** control is a persistent footer bar that opens a sheet of fireable
triggers.

- **Strengths:** lowest-friction scanning; never leaves context; the closest reuse of
  the settled review/proposals list paradigm; the persistent sweep footer is always
  reachable.
- **Tradeoffs:** deep step trees push siblings down (accordion can get long); the
  step timeline shares vertical space with the list rather than owning the screen.

## B — timeline-first, full-screen drill · `runs-ops-b-timeline-drill.html`

A **live activity timeline** down a single spine: a green "N runs active" strip pins
the live state up top (per DESIGN.md "honest status, always visible"), running runs
carry a progress meter and pulse, and finished/failed runs flow below by time. Tapping
any run **opens a full-screen step-drill layer** (rises from the bottom like the card-
launcher tree levels) showing the run's own step timeline with per-step sub-lines and
errors, plus a footer Re-run / View-log. Emergency-trigger lives behind the top-bar
**bolt**.

- **Strengths:** strongest "live feel" and the most honest at-a-glance running state;
  the full-screen drill gives the step tree the whole screen, best for long/agent runs;
  matches the navigation-tree slide-up convention exactly.
- **Tradeoffs:** an extra full-screen transition to read steps; the emergency-trigger
  is one tap less discoverable behind the bolt; a single spine is denser to skim than
  grouped list rows.

## C — ops-dashboard, split-panel drill · `runs-ops-c-dashboard-split.html`

An **operator dashboard**: a 2-column status-tile grid up top (active now / failed
today / jobs queued / tokens today) using the settled half-width status-card pattern,
then a **prominent sweep-control row** (the two most-common sweeps as one-tap amber
buttons + a "More…" sheet) as a first-class control, then a compact run log. Tapping a
run raises a **split panel** — a 62%-height sheet of the step tree over the still-
visible (dimmed) list, so the operator keeps the dashboard in view while inspecting.

- **Strengths:** best for the "is everything healthy?" operator glance; emergency
  triggers are the most prominent and fastest to fire; the split keeps context while
  drilling; richest top-level system summary.
- **Tradeoffs:** most chrome / least minimal — pushes against the "fewer things per
  screen" principle; the split sheet gives the step tree less room than B's full drill;
  status tiles duplicate signal that the run rows already carry.

## Recommendation

**Direction A (list-first, inline accordion)** as the primary, with **one borrow from
B**: keep the green "N runs active" live strip from B at the top of A so the running
state is honest at a glance without a dashboard's worth of tiles.

Rationale: A is the most faithful reuse of paradigms the design system has already
settled (the proposals/review browsable list, inline row-level disclosure for "detail
that doesn't warrant navigation" per the surface-paradigms table), it keeps the
operator in one scannable context, and its persistent sweep footer makes the
emergency-trigger always reachable without hiding it behind a bolt. C's dashboard tiles
are appealing but lean against "comfortable density / fewer things per screen," and B's
full-screen drill, while best for long agent runs, adds a transition for the common
case (a 3–5 step integration run reads fine inline). If runs routinely grow to many
steps, promote B's full-screen drill as the disclosure for `agent`-kind runs
specifically while integration/pipeline runs stay inline — a per-kind disclosure rule
the implementation can carry.
