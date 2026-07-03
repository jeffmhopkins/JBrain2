# JBrain2 — Doc Lifecycle

> **Status:** Living · **Last verified:** 2026-07-03

Binding process for how a document is born, changes, and dies alongside the
feature it describes. It exists because the `docs/` folder drifted into a
graveyard of shipped plans still labelled "in progress," status blocks stamped
with a month that had passed, and hardcoded counters (`migrations run through
0044` when the head was `0114`) that rotted the moment the next migration
landed. This doc is the rule that stops that from recurring: **a doc travels
with its feature, and a feature is not done until its doc is filed.**

Peer to `DEVELOPMENT.md` (binding standards) and `PROCESS.md` (multi-wave
execution). Where `PROCESS.md` governs *how the code lands*, this governs *how
the doc that describes it stays true*.

## The one rule

**Docs ship with the code.** Every PR that changes what a doc asserts — a wave
lands, a plan completes, a behaviour changes, a decision is made — updates that
doc *in the same PR*. Doc reconciliation is part of the definition of done, not
a someday-cleanup. Everything below is the mechanics of that one rule.

## Two kinds of doc

Every Markdown file under `docs/` is exactly one of these. The kind determines
whether it has a lifecycle at all.

| Kind | Describes | Lifecycle? |
|---|---|---|
| **Living** | How the system *is* — architecture, standards, runbooks, the entity/analysis/agent models, the design system, UI mocks. | No ladder. Corrected continuously; never "ships." Carries a `Last verified` date. |
| **Plan** | Work to *build* — a feature, decomposed into waves. | Yes. Climbs the ladder below, then is archived. |

Living docs: `ARCHITECTURE`, `DEVELOPMENT`, `PROCESS`, `DESIGN`, `ANALYSIS`,
`entity`, `ASSISTANT`, `MODEL_PROMPTING`, `OPERATIONS`, the access/runbook set,
`WIKI_TYPE_GUIDES`, `mocks/`, this doc, and `README` (the map). They do not move.
Plan docs are the `*_PLAN.md` family and any doc naming waves.

## State values — the vocabulary

The status a doc carries. Plan docs climb a ladder; Living docs have one state.

**Plan-doc ladder** (normal path top-to-bottom, off-ramps at the end):

| State | Meaning | Home |
|---|---|---|
| `Proposed` | On the record as an idea. Not committed to the roadmap. **Nothing built.** | `docs/proposed/` |
| `Scheduled` | Committed to the roadmap. Waves defined. Build not started. | `docs/` |
| `In progress` | At least one wave merged; not all. | `docs/` |
| `Shipped` | All waves merged. Terminal — the plan's job is done. | `docs/archive/` |
| `Parked` | Deliberately paused after a spike or decision; may revive. | stays put, parked banner |
| `Rejected` | Evaluated and killed (e.g. red-teamed non-viable). | `docs/archive/` |
| `Superseded` | Replaced by a named successor doc. | `docs/archive/` (or in place with a banner if still cited) |

**Living-doc state:**

| State | Meaning |
|---|---|
| `Living` | Binding reference/runbook. Kept true continuously. Carries `Last verified`. |

## The freshness header

Every non-archived doc opens with a single greppable status line, immediately
under the H1:

```
> **Status:** <state> · **Last verified:** YYYY-MM-DD[ · **Waves:** A✅ B✅ C◻️]
```

Examples:

```
> **Status:** Living · **Last verified:** 2026-07-03
> **Status:** In progress · **Last verified:** 2026-07-03 · **Waves:** A✅ B✅ C◻️
> **Status:** Scheduled · **Last verified:** 2026-07-03 · **Waves:** W1◻️ W2◻️ W3◻️
> **Status:** Parked (after Wave P1 spike) · **Last verified:** 2026-07-03
```

Archived docs carry a terminal banner instead and live under `archive/`:

```
> **Status:** Shipped 2026-07 · migrations 0107–0113 · **Superseded-by:** — (if any)
```

Rules for the header:
- **Exactly one** status block, at the top. Never contradict it lower down —
  the wiki plan once said "planned" at the top and "shipped" at the bottom.
- `Waves:` mirrors the wave sections in the body. When a wave section flips to
  ✅, the header marker flips in the same edit.
- `Last verified` is bumped by whoever last confirmed the doc matches reality,
  in the PR that made it match.

## Transitions — the doc travels with the feature

Each transition is an edit that lands *in the PR that triggers it*.

1. **Ideate** → write a `Proposed` doc in `docs/proposed/`. No code.
2. **Commit to the roadmap** → flip to `Scheduled`, `git mv` into `docs/`, add
   waves + a `ROADMAP.md` entry.
3. **A wave merges** → flip to `In progress`, tick that wave's marker (header +
   body), bump `Last verified`. Same PR as the wave.
4. **The last wave merges** → flip to `Shipped`, `git mv` to `docs/archive/`,
   carry any residual/open/deferred items into `ROADMAP.md` so nothing is lost,
   update `archive/README.md` and the `README.md` map. Same PR. **A feature is
   not done until its plan is archived.**
5. **Behaviour changes under a Living doc** → update that doc + bump its
   `Last verified`, same PR. This is the definition-of-done clause for any
   change to product behaviour a Living doc asserts.
6. **Off-ramps:**
   - *Park* — flip to `Parked`, add a one-line "why parked / what would revive
     it" banner. Doc stays put (e.g. `JCODE_SESSION_ISOLATION_PLAN`).
   - *Reject* — flip to `Rejected`, record the reason, `git mv` to `archive/`.
     A killed design is not icebox; it does not belong in `proposed/`.
   - *Supersede* — flip to `Superseded`, name the successor, `git mv` to
     `archive/` (or keep in place with a top banner if other docs still cite it,
     as `PREDICATE_CANONICALIZATION` does).

## Homes — one state per directory

| Directory | Holds | Never holds |
|---|---|---|
| `docs/` | Living docs + active plans (`Scheduled`, `In progress`, `Parked`). | `Shipped`, `Rejected`, `Superseded`. |
| `docs/proposed/` | `Proposed` only — the icebox. Nothing built, nothing killed. | Built or rejected designs. |
| `docs/archive/` | Terminal states: `Shipped`, `Rejected`, `Superseded`, and completed research. | Anything still active. |
| `docs/research/` | Live research feeding a not-yet-shipped plan. | Research whose plan has shipped — that moves to `archive/research/`. |
| `docs/mocks/` | Binding UI spec (Living, per `DESIGN.md`). | — |

Every directory has a `README.md` index. The index lists **every** file in the
directory — an index that under-lists its own folder is itself stale and is the
first thing the freshness check flags.

## Anti-rot rules — why we got stale, and the fix

Each rule targets a failure this cleanup actually found.

- **R1 — No volatile counters in prose.** Migration heads, table counts, "N
  tables," "head is 0044" rot the instant the next one lands. State them only
  under `Last verified` as a dated snapshot, or point at the source of truth
  (`backend/migrations/versions/`). *Never* assert a live counter as timeless
  prose. This single pattern produced the most stale lines in the repo.
- **R2 — One status block, at the top.** A doc that self-reports its status in
  two places will eventually disagree with itself.
- **R3 — Dates are `Last verified`, never identity.** A section titled
  "Where the project is (2026-06)" is a lie by July. "Last verified 2026-06-29"
  is honest forever — it says when, not that it's current now.
- **R4 — Archive at merge, not "later."** The graveyard forms entirely from
  deferred archiving. Moving a `Shipped` plan is a merge step, not backlog.
- **R5 — One home per state (see table).** `Shipped` code left in `proposed/`
  (jcode) or `docs/` root (image-gen, location, subagents) is how readers lose
  the thread between "planned" and "done."

## Enforcement

Rules without teeth rot too. Two mechanisms:

1. **`scripts/docs-freshness.sh`** — an advisory check (CI-gateable) that scans
   `docs/` and flags, with a nonzero exit:
   - volatile migration-number prose outside `archive/` (R1);
   - a plan doc in `docs/` whose wave markers are all ✅ but whose status is not
     `Shipped`/archived (R4);
   - a non-archived doc missing a freshness header (R2/R3);
   - a directory `README.md` that under- or over-lists its folder (homes);
   - *(warn)* a Living doc whose `Last verified` is older than 90 days.
   Run it locally before a docs PR; wire it into CI to make the gate binding.
2. **Definition of done.** `DEVELOPMENT.md` and the PR template carry the line:
   *"Docs reconciled — plan status flipped or archived, Living docs corrected,
   `Last verified` bumped."* Reviewers hold the PR to it.

## Naming

This process is **the Doc Lifecycle**; its status line is **the freshness
header**; its slogan is **docs ship with the code**. (Alternate brandings
considered and open to change: *Doc Ledger*, *Freshness Protocol*,
*Docs-as-Code*. The mechanics matter more than the label — rename freely, the
state vocabulary and the one rule stay.)
