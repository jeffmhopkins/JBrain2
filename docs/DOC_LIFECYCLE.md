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

Living docs: `README` and `ROADMAP` (the map + the phase status), `ARCHITECTURE`,
`DEVELOPMENT`, `PROCESS`, `DESIGN`, `ANALYSIS`, `entity`, `ASSISTANT`,
`MODEL_PROMPTING`, `OPERATIONS`, the access/runbook set, `WIKI_TYPE_GUIDES`, and
this doc. They do not move. Plan docs are the `*_PLAN.md` family and any doc that
names waves (e.g. a `*_CONTRACT.md` that sequences work).

**Default rule:** if a doc doesn't name waves and doesn't describe unbuilt work,
it's Living. Two directories are special cases of the Living kind: `mocks/` holds
binding UI spec as **HTML**, so it carries no Markdown freshness header — its
currency is governed by `DESIGN.md`; `research/` holds design dossiers feeding a
plan (see Homes). Both are kept current, neither climbs the plan ladder.

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
| `Superseded` | Replaced by a named successor doc. | `docs/archive/`, **unless** other docs still cite it — then it stays in `docs/` with a top banner (as `PREDICATE_CANONICALIZATION` does). |

**Living-doc states** (a Living doc doesn't "ship," but it can end):

| State | Meaning | Home |
|---|---|---|
| `Living` | Binding reference/runbook. Kept true continuously. Carries `Last verified`. | `docs/` |
| `Superseded` | Replaced by a named successor doc. Same rule as the plan-doc off-ramp above. | `archive/` or in place with a banner if cited |
| `Retired` | Its subject was removed (a runbook for a decommissioned feature). | `docs/archive/` |

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
- **Exactly one** status block, at the top. Never contradict it lower down — a
  plan that reads "planned" up top while its body already carries "shipped"
  annotations sends readers in circles.
- **Active vs terminal.** An active doc (`Living`/`Scheduled`/`In progress`/
  `Parked`) lives outside `archive/` and carries `Last verified`. A terminal doc
  (`Shipped`/`Rejected`/`Superseded`/`Retired`) lives under `archive/` (or in
  place with a banner, for cited Superseded) and its banner names the month, not
  a `Last verified` date — nothing re-verifies a frozen doc.
- **Wave labels.** Waves use a single-letter or single-letter+digit scheme
  (`A B C`, `W1 W2`, `P0 P1`, `S1 S4`, `G1 G3`), each with a status glyph in the
  header: `✅` done, `◻️` not done. `Waves:` mirrors the body's wave sections;
  when a body wave flips to ✅, the header marker flips in the same edit.
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
6. **Off-ramps (from any state):**
   - *Park* — flip to `Parked`, add a one-line "why parked / what would revive
     it" banner. Doc stays put (e.g. `JCODE_SESSION_ISOLATION_PLAN`, parked after
     its spike with the shipped substrate reverted).
   - *Reject / abandon* — flip to `Rejected`, record the reason, `git mv` to
     `archive/`. A killed design is not icebox; it does not belong in `proposed/`.
     **If some waves had already merged** before the abandon, carry their
     shipped surface + any residual into `ROADMAP.md` first (as Transition 4
     does) so the shipped half isn't orphaned — then archive the plan.
   - *Supersede* — flip to `Superseded`, name the successor, `git mv` to
     `archive/` (or keep in place with a top banner if other docs still cite it,
     as `PREDICATE_CANONICALIZATION` does). Applies to a Living doc too.
   - *Retire* (Living only) — the doc's subject was removed; flip to `Retired`,
     `git mv` to `archive/`.

## Homes — one state per directory

| Directory | Holds | Never holds |
|---|---|---|
| `docs/` | Living docs + active plans (`Scheduled`, `In progress`, `Parked`) + Superseded-but-still-cited docs (with a banner). | `Shipped`, `Rejected`, `Retired`, or Superseded-and-uncited. |
| `docs/proposed/` | `Proposed` only — the icebox. Nothing built, nothing killed. | Built or rejected designs. |
| `docs/archive/` | Terminal states: `Shipped`, `Rejected`, `Retired`, uncited `Superseded`, and completed research. | Anything still active. |
| `docs/research/` | Live research feeding a not-yet-shipped plan. | Research whose plan has shipped — that moves to `archive/research/`. |
| `docs/mocks/` | Binding UI spec as HTML (Living, per `DESIGN.md`). | — |

Every directory that has a `README.md` index must name **every** file in the
folder; an index that omits a sibling is itself stale, and the freshness check
flags it. (It only checks under-listing — a README also cross-references other
docs in prose, so a named-but-absent file can't be told from a legitimate
mention; catch those at review. `research/`, `mocks/`, and `archive/research/`
may have no index; if one exists, it's held to the same rule.)

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

1. **`scripts/docs-freshness.sh`** — a check that scans `docs/` and flags, with
   a nonzero exit on errors:
   - *(error)* volatile migration-head prose outside `archive/` (R1);
   - *(error)* a plan doc in `docs/` whose header `Waves:` are all ✅ but whose
     status is not `Shipped`/archived (R4);
   - *(warn)* a non-archived doc missing a freshness header (R2/R3);
   - *(warn)* a `README.md` index that under- or over-lists its folder (homes);
   - *(warn)* an **active plan** (`Scheduled`/`In progress`) whose `Last verified`
     is older than 90 days. Living runbooks that stay true for years are exempt —
     the age warn is for work in flight, not stable reference.
   **Sequencing:** it stays advisory (run locally, `make` target) until the
   `DOC_CLEANUP_PLAN.md` waves land the freshness headers repo-wide; wiring it as
   a binding CI gate is that plan's final wave, and adds it to the gate list in
   `PROCESS.md`. Turning the gate on before then would fail CI on every not-yet-
   migrated doc.
2. **Definition of done.** The canonical checklist line lives in `DEVELOPMENT.md`
   and the PR template — *"Docs reconciled: plan status flipped or archived,
   Living docs corrected, `Last verified` bumped."* — not restated here, so the
   two can't drift. Reviewers hold the PR to it.

## Naming

This process is **the Doc Lifecycle**; its status line is **the freshness
header**; its slogan is **docs ship with the code**. (Alternate brandings
considered and open to change: *Doc Ledger*, *Freshness Protocol*,
*Docs-as-Code*. The mechanics matter more than the label — rename freely, the
state vocabulary and the one rule stay.)
