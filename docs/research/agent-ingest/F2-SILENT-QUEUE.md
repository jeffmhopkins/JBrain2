# F2 — The silent queue

> **Status:** Research · **Last verified:** 2026-09-08

Research dossier for the agent-ingest redesign: the deterministic note-analysis pipeline is
deleted, a note becomes turn 0 of an agent conversation, the agent commits the graph changes
it is confident about and **asks the owner** about the rest. Those questions accumulate in a
**silent queue — no push notifications** (binding owner decision). The owner is on a phone,
remote, with no terminal.

Nothing here is built. Mocks that carry the argument: `docs/mocks/silent-queue/` (A ambient ·
B deck · C stacks + README). All examples synthetic. Every claim is either a `path:line`
citation (**verified**) or flagged **assumed** in §11.

---

## 1. Recommendation, first

**A question is not a request. It is a decision the agent has already made, shown to the
owner with a window to overrule it.**

That single reframing is the whole design, and everything below falls out of it:

1. **No question may be filed without a `default_action` and a `default_at`.** The agent must
   have a position. If it cannot form one, it must not ask — it commits raw (tier-2 doctrine)
   or does nothing.
2. **The queue is an index, not a destination.** Questions are delivered **in context** — on
   the entity page, in the note's Analysis tab — where the owner already is. The list screen
   exists, but it is reached from the launcher tile, never from home.
3. **Unanswered ≠ lost.** At `default_at` the agent commits its default and marks the fact
   `assumed`, visibly, reversibly, at the point of use. Owner silence is a legitimate answer
   ("go with your guess"), not debt.
4. **The working set is capped at 40 open questions, globally.** Filing #41 forces the
   lowest-regret open question to settle immediately. A 400-item backlog is not unlikely — it
   is *structurally impossible*.
5. **Ordering is by regret, not recency**: `P(default wrong) × blast radius × reach`,
   with imminence and cheapness as tie-breaks.
6. **The follow-up is silent too.** Answering re-enters the conversation as an answer-only
   transcript turn (no fake owner bubble, no ping), on the continuation runner's shape.

**Why this and not "an inbox that's just nicer".** The repo has already run the experiment.
The `new_predicate` card filed **one open card per distinct raw predicate spelling** — the
canonicalization design admitted the auto-merge band effectively never fires, so *every*
unknown predicate filed a card: *"Review noise, forever"*
(`docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md:31-38`). The fix was not a better inbox. It was
a **one-shot retire sweep that mass-deleted every open card** and marked itself done so it
never ran twice (`backend/src/jbrain/analysis/predicates.py:225-265`). That is what an
unread-inbox of shame looks like when it is finally admitted: a `DELETE ... RETURNING` and a
settings marker. The design below is built so that sweep is never needed, because the queue
drains itself continuously by construction.

---

## 2. Audit — what the shipped review inbox does, and what survives

The unified review inbox is the split-inbox redesign settled in `docs/reference/DESIGN.md:899-951`
(lanes, browsable list, detail with prev/next, bulk, undo) and `:983-1010` (the block registry).
Inline approvals is the parallel in-conversation surface
(`docs/archive/INLINE_APPROVALS_PLAN.md`, migration 0130, `frontend/src/agent/InlineProposal.tsx`).

### 2.1 Reusable **verbatim**

| Thing | Where | Why it survives |
|---|---|---|
| `review_items` row shape + RLS | `backend/migrations/versions/0006_analysis_schema.py:206-225` | `id · kind · payload jsonb · status · resolution jsonb · domain_code · created_at · resolved_at` is exactly a question. Additive columns only (§10) — a new table would need a new RLS isolation surface; migration 0024 makes precisely this argument for reusing the row. |
| The **block registry** | `frontend/src/review/blocks/registry.ts:18-66`, `types.ts:48-79` | "Adding a review kind is *declare a sequence*, not *add a screen branch*" (`registry.ts:1-5`). Question kinds are open-ended by nature in an agent redesign; this is the only part of the inbox that was designed for that. Blocks **self-gate** (render null when data is absent) so a generous sequence is safe. |
| Data-driven proposals | `frontend/src/review/payload.ts:234-252`, rendered `blocks/Action.tsx:171-207` | Pick-one is already "the card advertises its own choices"; the backend display contract emits `choices [{action,label,detail,destructive}]` with the invariant that **every advertised action is exactly an action the resolve endpoint accepts** (`backend/src/jbrain/analysis/display.py:6-13`). That invariant is worth more than the code. |
| Armed tap-again | `frontend/src/review/useArmed.ts:9-31` | Shared key-space, 3s disarm, arming one disarms the rest. The settled destructive-confirm doctrine. |
| Batch resolve | `backend/src/jbrain/api/analysis.py:242-263` (`max_length=200`), repo `analysis/repo.py:1278-1340` | One RLS-scoped transaction, good ones commit, bad ones return in `errors`. Exactly what a stack answer needs. |
| Optimistic move + rollback + **one** undo per batch | `frontend/src/review/useReviewQueue.ts:123-174`, snackbar `ReviewScreen.tsx:557-572` | The undo label is per-batch, not per-item — already the right granularity for "answer 12". |
| **Reopen = full unwind** | `analysis/repo.py:1366-1430`, `_reverse_effects:1827` | Load-bearing for the whole recommendation: decay-to-assumption is only defensible because an assumption is reversible, and the reversal is the server's own unwind, not a hand-edit. |
| Entity grouping | `frontend/src/review/grouping.ts:37-83` | Reads the subject out of four different payload keys and title-cases refs; sinks a no-subject "Other" bucket. This is fiddly, correct, and directly reusable. |
| Correction = a **note**, never a hand-written fact | `useReviewQueue.ts:106-121` → `api/analysis.py:194-240` | `provenance='owner_correction'`, owner-gated, force-supersedes and pins so a correction *applies* instead of colliding with what it corrects. Non-negotiable #7 stays intact by construction. |
| Filing-time dedupe | `analysis/pipeline.py:670-686` (note+entity+predicate+qualifier), `:1249-1259` | Re-analysis must not multiply identical open cards. In an agent world where turn 0 may be re-run, this is not optional. |
| Server-authored outcome from **DB truth, never model text** | `backend/src/jbrain/agent/proposals.py:192-233` | "honest by construction" (`:195`). The single best idea in the inline-approvals work. |
| Data-framed, not-instruction turn wrapper | `backend/src/jbrain/api/agent.py:605-620` | Invariant #1 holds; the agent "must not re-stage anything the owner declined". |

### 2.2 Reusable **in spirit** (the shape is right, the mechanism must change)

- **Correct-in-place.** `blocks/types.ts:21-46` hoists predicate + value + modality edit state
  to the detail so the fact panel and the action button share it; an edit flips the primary to
  *approve correction* (`Action.tsx:146-154`) which files the correction note. Keep the
  *concept* — the answer to "is this right?" is often "no, it's X" — but it needs a keyboard
  and a screen, so in the queue it lives one level down behind "Say more…", not on the card.
- **The enact→agent outcome loop.** Reusable in spirit only, because its stated premise
  breaks — see §9.
- **"Approve N high-confidence"** (`ReviewScreen.tsx:392-400`, threshold ≥0.75 at `:330-333`).
  The instinct is right (clear the easy volume in one tap); the implementation is wrong for
  questions, because it asks the owner to trust a number they cannot see. Replaced by the
  **stack**, which shows *which twelve* and lets them flick out exceptions (mock C).
- **Every leaf starts approved; you decline the exceptions**
  (`InlineProposal.tsx:78-84`). This default-in inversion is the single most thumb-efficient
  idea in the shipped code. It becomes the stack's chip behaviour.
- **Trace / evidence blocks** (`blocks/Trace.tsx`, `Evidence.tsx`). Keep evidence — the cited
  snippet with `<mark>` (`display.py:1-5`) is what makes a question answerable without opening
  the note. The pipeline-stage **trace** dies with the pipeline; its replacement is the
  agent's own one-sentence reasoning ("I'd keep Ozone Labs because…").

### 2.3 What should **die**

| Thing | Where | Why |
|---|---|---|
| The **`deferred` lane / snooze** | migration `0024_review_deferred.py`, `analysis/repo.py:52` (`REVIEW_STATUSES`), `repo.py:1160-1163` | **It is already dead and nobody noticed.** `DESIGN.md:900-902` still specifies a three-lane *pending · deferred · decided* filter with count pills; the shipped screen renders **two** (`ReviewScreen.tsx:467-470, 523-537`), and the footer's *defer* / *talk it over* escape hatches described at `DESIGN.md:934-938` are absent from `blocks/Footer.tsx:11-33`. The backend status, the index and the `REVIEW_STATUSES` tuple are still there, serving nothing. **Doc drift worth reconciling regardless of this redesign.** And the lesson stands: a park-it button is a way to feel productive without deciding, and it re-surfaces the item — which is nagging with extra steps. Decay-to-assumption replaces it: "later" is the default state of *every* question, so it needs no button. |
| **Selection mode** (checkboxes + bulk bar) | `ReviewScreen.tsx:304-306, 322-327, 432-447` | Two-handed, and it makes the owner do the grouping (read 12 rows, agree they match, tick 12 boxes). The stack does the grouping. Keep the endpoint, drop the mode. |
| The **pending · decided segmented header** as primary IA | `ReviewScreen.tsx:523-537` | Lanes exist to navigate a backlog. A working set capped at 40 doesn't have one. The decided log stays reachable; it stops being half the header. |
| **`new_predicate` as a question kind** | `registry.ts:48`, `blocks/NewPredicateCard.tsx`, retire sweep `predicates.py:225-265` | Already retired once for volume. The tier-2 doctrine (`ENTITY_GRAPH_REFOCUS_PLAN.md:88-93`) is that a long-tail predicate **commits raw with no review card**. Do not resurrect it as an agent question. |
| **Confidence-threshold bulk approve** | `ReviewScreen.tsx:329-333, 392-400` | See above — replaced by the stack. |
| The **detail carousel** (swipe/chevrons, N of M) | `ReviewScreen.tsx:190-206, 254-278` | Built to page a long lane one card at a time. The deck (mock B) is the same gesture with a *finite, visible* end, which is the difference between triage and a chore. |

---

## 3. Ordering — rank by regret, not recency

Recency ordering is what makes a queue an inbox. Today the open lane is **oldest-first**
(`analysis/repo.py:1155, 1160`) — defensible for FIFO triage, wrong when the owner will only
ever answer the top few.

```
regret = P(default is wrong) × blast_radius × reach × imminence
```

Every factor is computable from things that already exist:

| Factor | Signal | Source |
|---|---|---|
| `P(default wrong)` | `1 − confidence` | already on review payloads; `confidenceBadge` at `frontend/src/review/payload.ts:270-277` |
| `blast_radius` | **tier-1 predicate → high, tier-2 → low.** A tier-1 predicate is an arbiter of current truth (functional supersession, firewall floor, display projection, ref-edge traversal); a tier-2 one is a leaf nobody navigates by. | `SchemaRegistry.declares_predicate` (`backend/src/jbrain/schema/models.py:167-172`) — "declared-in-registry IS tier-1" (`ENTITY_GRAPH_REFOCUS_PLAN.md:71-77`). Firewall domains (health/finance/location) float to the top on their own. |
| `reach` | subject entity degree — how many facts and notes hang off it. A question about *Me* or a hub person outranks one about a one-mention Thing. | `SqlAnalysisRepo.ego_graph:439`, `neighborhood:589` |
| `imminence` | `default_at − now`, normalized | §6 |
| tie-break | **cheapness**: yes/no > pick-one > correct-a-value | the question's own `answer_shape` (§5) |

Two consequences worth stating:

- **The score is cached on the row** (`priority real`) and recomputed by the settle sweep, not
  per-request. The list is a plain `ORDER BY priority DESC` — the same shape as today's
  ordered read.
- **The list never shows more than the top 5** on the proactive surface. Everything below the
  fold is not hidden, it is *delivered elsewhere* (§7). This is the point at which the design
  stops being a queue and starts being an index.

---

## 4. Grouping and batching

**Group by entity, not by note.** The owner thinks "what's going on with Celine", never "what
did note #4821 want". This is also the shipped default — pending triage groups by subject
entity, with `time` as the alternative (`ReviewScreen.tsx:308, 357, 371-401`) — and
`grouping.ts:37-52` already resolves the subject across the four payload key conventions.
A note *is* still a grouping, but it earns its place in one specific spot: the note's own
Analysis tab (§7a), where the frame is "here's what I took from this and what I wasn't sure
of", not "here are your chores".

**Add a third mode the shipped inbox lacks: by shape.** Two questions stack when they share
`(kind, predicate, default_action)` — the same question about different subjects. Twelve
"is this a place you go, or just a mention?" questions render as **one card, asked once**,
with the twelve subjects as chips (mock C). Default every chip **in** (the
`InlineProposal.tsx:78-84` inversion); the owner flicks out the exceptions and taps one armed
**Answer 11**. That is one `POST /review/resolve-batch` (`api/analysis.py:248-263`) and one
undo (`useReviewQueue.ts:142`).

Stacking matters more than any other batching move because **shape-volume is the exact failure
mode the repo already hit**: one card per distinct spelling of the same idea. Stacking converts
that volume from *retirable* to *answerable*.

**Do not batch across shapes.** "Approve everything above 0.75 confidence" asks the owner to
trust a number they cannot audit; the stack shows them exactly what they are agreeing to.

---

## 5. Answer affordances — what must exist, and what each costs

Ranked by how often it will be the answer, and constrained by one thumb, bottom third of a
phone screen, ≥44px targets (`DESIGN.md:11-12`).

| Affordance | Must exist? | Where it lives | Backend cost |
|---|---|---|---|
| **yes / no** | **Yes — the load-bearing one.** ~70% of questions reduce to "is my guess right?" | Two 52px buttons, side by side, bottom of the card | One `POST /review/{id}/resolve` — **shipped**, `api/analysis.py:173-192`. Free. |
| **"not important — drop it"** | **Yes — the pressure valve.** Must be one tap and always present. Without it, a question the owner doesn't care about has no cheap exit and becomes debt. | A ghost button under the pair | `resolve(action='dismiss')` — **shipped** (`useReviewQueue.ts:84`). Free. *Plus* one thing that isn't: the drop must **teach** (§8.4). |
| **pick-one (≤4)** | **Yes.** Identity/disambiguation questions are natively pick-one. | Stacked full-width option buttons; the agent's guess carries a "my guess" tag | The card advertises `payload.choices`; the renderer is `Action.tsx:171-207` — **shipped, verbatim**. Free. |
| **correct-a-value** | **Yes, but one level down.** It needs a keyboard, so it belongs on the entity page / detail, not on a deck card. | Behind "Say more…", opening the composer at `Action.tsx:92-118` | **The expensive one**: mints an `owner_correction` note (`api/analysis.py:194-240`) → emits `note.created` → full re-ingest → extract → integrate → force-supersede + pin. One tap costs a pipeline run. That is the right price for the right answer, and the wrong price to pay by accident — hence one level down. |
| **free text** | **Yes, same slot as above.** It is the "you've misunderstood the situation" escape. | Same composer | Same as above. |
| **ask me later** | **No — recommend cutting it.** | — | Already built (`0024_review_deferred.py`) and already silently dropped from the UI (§2.3). Every question is *already* "later" by default; a snooze button only re-surfaces the item, which is the nagging we're avoiding. |
| **pick-many** | **No.** | — | It decomposes: "which of these 4 is Celine" is pick-one; "which of these 6 facts are wrong" is a stack. Building a real multi-select means a new payload shape and a per-kind backend action for no case that doesn't already have a cheaper form. |

**The card's answer bar, concretely:** `[✗ no] [✓ yes]` as a 2-up pair, then a 2-up ghost row
`[drop it] [say more…]`. Four targets, two rows, all in the bottom third. A pick-one card
swaps the pair for a stack of options and keeps the ghost row.

---

## 6. Expiry and self-resolution — **decay to a marked assumption**

Of the three options posed:

- ❌ **It rots visibly.** This is the unread-inbox of shame with a timestamp. It converts the
  owner's reasonable disinterest into a permanent accusation, and — worse — the knowledge is
  *still* missing.
- ❌ **Silently dropped, the fact never exists.** The most dangerous option, precisely because
  it is invisible. The owner never learns the graph is wrong about Celine's employer; they
  learn it when the wiki tells them something false. Notes stay the sources of truth, but the
  graph is the spine the agent navigates by (`ENTITY_GRAPH_REFOCUS_PLAN.md:41-47`) — a
  silently-absent spine fact is a silently-wrong answer later.
- ✅ **The agent commits its best guess and records that it guessed.** Recommended, with three
  refinements that do most of the work:

**(a) The default is declared at filing time, not invented at expiry.** `default_action` +
`default_payload` are **NOT NULL** on the row. A question the agent cannot default is a
question it may not ask. This is a filing-time gate, not an expiry-time scramble, and it is
also the anti-pattern control (§8) — it forces the agent to have a position, which is most of
what "commit what you're confident about" means.

**(b) A ladder, not a 60-day cliff.** The window scales with regret:

| Class | Window | Default at expiry |
|---|---|---|
| Low-regret tier-2 leaf (a car's colour) | **7 days** | commit the guess, mark `assumed` |
| Ordinary tier-1 fact (an employer) | **30 days** | commit the guess, mark `assumed` |
| Destructive / irreversible (an entity **merge**) | **60 days** | the conservative branch — **do not merge**, record `declined by default`, stop asking |
| Firewall domain (health · finance · location) | **never** | stays open — see the open question in §12 |

60 days uniform is wrong in both directions: a week is plenty for "is the Civic yours", and
*no* amount of time makes it OK to merge two people on a guess. The conservative branch is
always available, because "do nothing" is a valid default — it is just a default that must be
*recorded* ("I asked, you didn't answer, I kept them separate") rather than forgotten.

**(c) The assumption is visible at the point of use, and one tap to overturn.** The committed
fact carries `provenance='agent_assumption'` and renders on the entity page with an amber
`assumed 12 Aug` chip (mock A), tapping which reopens the question. This is what makes the
whole scheme honest: the graph never claims to know something the owner told it. It rides two
shipped facilities — `provenance` is already the axis that distinguishes `agent` / `human` /
`owner_correction` (`INLINE_APPROVALS_PLAN.md` Decision #2), and **reopen is already a full
unwind** of a resolution's recorded effects (`analysis/repo.py:1366-1430`).

**The sweep.** `settle_open_questions` — a registered `ActionSpec`, `cost_class="cheap"`,
`mutating=True`, in-code-only (not in the `app.actions` seed), with a migration-seeded nightly
schedule + pipeline, idempotent on re-fire. This is a straight copy of
`EXPIRE_RESEARCH_REPORTS_ACTION` (`backend/src/jbrain/workflow/scheduler.py:155-170`, handler
`:581-595`, wired at `worker.py:839`), which does exactly this job for research reports whose
opt-in TTL has passed. One bounded query over a partial index on `default_at`.

**What this buys.** The number of open questions is now self-limiting without anyone
intervening, unanswered knowledge is committed rather than lost, the owner's silence is a real
answer with a real effect, and nothing is unrecoverable.

---

## 7. Discoverability without nagging — the argument

**This is the key insight, and it is not a UI trick.** Every inbox design fails at the same
place: it asks the owner to *go somewhere and do chores*, and the only lever it has for
prioritization is a model of what it thinks they care about. Both problems have the same
solution.

> **Deliver the question where its subject already lives, and the owner's own navigation
> becomes the ranking function.**

The entities the owner opens are, by definition, the entities they care about right now. A
question rendered on Celine's page is answered because they were already looking at Celine —
it costs one extra tap on a screen they chose to open, not a trip to a chore list. No ranking
model can beat that signal, because the signal *is* the owner's attention, measured directly.

Three surfaces, in strict order of importance:

**(a) In context — the spine.** (mock A)
- **The entity page** renders an open question as an unresolved predicate row, inline among
  the settled ones. `EntityScreen.tsx:257-273` already lays out "Current" as one row per
  predicate; an unresolved row is the same row with a question and two buttons.
- **The note's Analysis tab** renders that note's questions above its committed facts — and
  the note view **already opens on Analysis by default** (`NoteScreen.tsx:295-297`: *"Analysis
  is the most useful surface once a note exists, so it opens first"*). The frame is "here's
  what I took, and here's what I wasn't sure of", which is the honest frame for turn-0
  ingestion.
- Critically: **the commits are not gated on the questions.** The graph already moved. The
  question is an amendment offer, not a blocker.

**(b) A count that cannot become a shame number.** The launcher tile badge already exists and
is already the right kind of quiet: it polls only while the launcher is open and the app is
foregrounded, and it hides itself at zero (`frontend/src/components/Launcher.tsx:246-272,
370-372`, `REVIEW_POLL_MS = 10_000` at `:190`). Keep it — but **change what it counts.**
Counting all open questions recreates "137". Count only the **top band**: questions whose
default the agent would rather not take. That number is 0–5 in normal operation, and reaching
zero is achievable, which is the only thing that makes a badge honest. Everything below the
band is not a badge-able state; it is a clock.

**(c) One offered moment, never a pushed one.** A single dashed, dismissible line at the foot
of the home stream: *"3 things I'd rather not guess at · about a minute"*, shown at most once
per day and only when the top band is non-empty. It opens the deck (mock B). Dismissing it
marks nothing. The register is the one the owner already ratified for exactly this kind of
signal: the research-expiry GUI gate chose the **quiet footer** over a pill and over a
full-width urgency strip, on the reasoning that *"the quiet footer is the right default for a
library you mostly browse, not act on"* (`docs/mocks/research-expiry/README.md`).

**Explicitly rejected:** push notifications (owner's binding decision — and the SSE notify
stream at `backend/src/jbrain/api/notifications.py:1-8` must simply not be used for this), an
app-icon dot, a top-bar count, a digest email, and any interstitial.

**And the deck must end.** Mock B's end card is load-bearing: five answered, a green ring, and
then the truth said out loud — *"there are 132 smaller things I was unsure about; none of them
are waiting on you"*. Without that sentence the deck is a lie about a backlog. With it, the
deck is a complete unit of work.

---

## 8. Anti-patterns — four layered controls

**8.1 Per-note question budget: ≤3 questions, ≤1 per subject entity.**
The agent picks its top three by the same regret score; everything else commits with its
default, silently. The lesson is directly available: the extraction prompt's `min_facts: 12`
**floor** on the per-note fact budget (`analysis/prompt.py:36-47`, cited at
`ENTITY_GRAPH_REFOCUS_PLAN.md:26-28`) is what produced maximalism. A floor manufactures volume.
Use a ceiling.

**8.2 Global open cap: 40.** Enforced *at filing*, not by a cleanup job. Filing question 41
settles the lowest-regret open question immediately (commit its default, mark `assumed`). The
open queue is a **fixed-size working set**, not a log. This is the single most important
control in the document, because it converts "we hope it doesn't grow" into "it cannot".

**8.3 Tier gate: tier-2 predicates may not ask.** A question may only be spent on a **tier-1
(registry-declared) predicate**, an **identity/merge** decision, or a **firewall-domain** fact.
Everything else commits raw. This is not new policy — it is the shipped refocus doctrine
("Tier-2 semantics. Any undeclared predicate: stored raw, searchable, traversable, **no embed
round-trip, no `new_predicate` card, no unknown-predicate weight penalty**",
`ENTITY_GRAPH_REFOCUS_PLAN.md:88-93`) restated as the agent's asking rule.

**8.4 Silence and dismissal are scored — the agent chooses silence.** Three dismissals of the
same `(kind, predicate)` shape within 30 days and the agent **stops asking that shape** and
defaults it forever. There is a shipped precedent for exactly this mechanism: the Gmail
archivist keeps a `TRIAGE CLARIFICATIONS` block in its memory, harvested from the owner's
corrections and injected into the classifier prompt as *"Owner corrections … They take
priority over the general rules above"* (`backend/src/jbrain/gmail/triage.py:137-157`).
Answers — and refusals to answer — become durable policy, not one-off resolutions.

**Plus, free:** filing-time dedupe on `(note, entity, predicate, qualifier)` already exists
(`analysis/pipeline.py:670-686`) and matters more in an agent world, where turn 0 may be
re-run against the same note.

---

## 9. Answering → back into the conversation

The inline-approvals loop is the right pattern, and its **mechanism does not transfer**. That
is worth being precise about, because it is the one place a naive reuse would break.

`INLINE_APPROVALS_PLAN.md §3.1` chose a **frontend-initiated** follow-up and explicitly
rejected server-side turn injection — *"the frontend is already attached to that session and
`fb.send` reuses it. Rejected as scope the loop doesn't need."* That reasoning is sound and its
premise is exactly what the silent queue removes: the owner answers from an **entity page**, a
**note**, or the **deck** — not attached to the originating chat, and possibly months after
turn 0. There is no `fb.send` to call.

**Recommendation: answers are drained, not sent. Two parts.**

**(i) The graph effect is synchronous.** An answer resolves through the shipped path
(`analysis/repo.py:1216-1275` → `_apply_resolution:1431`, which already emits its
`resolution.changed` event *after* commit in its own best-effort session so a failed emit can
never abort the resolution, `:1265-1271`). The owner sees the fact change now. **No agent turn
is required for the common case**, and this matters enormously: the majority of answers are
"yes, commit what you guessed", which is a DB write, not a conversation. Spinning a turn for
each would be both slow and a source of pings.

**(ii) A follow-up turn fires only when the answer changes the agent's picture** — a *no*, a
correction, or a batch — and it is **debounced and consolidated**: one follow-up per
originating conversation per ~10 minutes, carrying a single server-authored outcome covering
every question answered in that window.

- The **outcome string** is `enact_outcome_summary`'s pattern applied to questions: built from
  DB truth — which questions were answered, which were corrected and to what, which were
  dropped, which self-settled — *"never model text … honest by construction"*
  (`agent/proposals.py:192-233`). Reuse verbatim in spirit, and copy the consolidation shape:
  *"Enacted 3 of 4 — 2 approved, 1 corrected (HCTZ → 25 mg) · declined 1 …"* becomes *"You
  answered 7 of the things I asked: 5 as I'd guessed, 1 corrected (employer → Ozone Labs),
  1 dropped — I'll stop asking about vehicle colours."*
- The **framing** is the shipped data-not-instruction wrapper (`api/agent.py:605-620`), with a
  third variant beside `proposal_outcome` / `deferred_outcome`. It carries the same two
  obligations: it is data for the agent to acknowledge and continue from, and the agent **must
  not re-ask anything the owner dropped**.
- The **delivery mechanism** is the **plan-continuation runner's shape**, not the chat
  endpoint: a due-time on a row + a periodic sweep + `LoopTurnExecutor` (*"the same engine
  /chat and tasks use"*, `agent/continuation.py:15-17`; the executor itself at
  `tasks/runner.py:98-107`), persisted **answer-only via `record_answer`** — *"no fake owner
  bubble"* (`continuation.py:16`, call site `:329-334`) — while registering a real `_LiveTurn`
  so a client that happens to be attached streams it (`continuation.py:129-132, 239-247`).
- **Routing** needs the originating conversation id on the question row. The precedent is
  `ProposalRow.session_id`, added for precisely this — *"Surfaced so an enact can route its
  outcome back to the originating chat"* (`agent/proposals.py:158-160`).

**And the follow-up must itself be silent.** No notification, no unread marker on the chat.
It lands as an answer-only transcript entry, found when the owner next opens that
conversation. If answering a question produced a ping, the queue would become a nag generator
through the back door — which would defeat the owner's decision by accident.

---

## 10. Data model sketch (additive, no new table)

Reuse `app.review_items` (`0006_analysis_schema.py:206-225`); a new table would open a new RLS
isolation surface, and migration 0024 already makes exactly this argument for reusing the row
(*"the lane is a status, not a new table"*).

```
ALTER TABLE app.review_items
  ADD COLUMN default_action  text,          -- NOT NULL for new question kinds (filing gate)
  ADD COLUMN default_payload jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN default_at      timestamptz,   -- when it self-answers; NULL = never (firewall only)
  ADD COLUMN priority        real,          -- cached regret; recomputed by the sweep
  ADD COLUMN session_id      uuid,          -- originating conversation, for the follow-up (§9)
  ADD COLUMN answer_shape    text;          -- 'yesno' | 'pickone' | 'value'  (drives the card)

CREATE INDEX review_items_due_idx  ON app.review_items (default_at) WHERE status = 'open';
CREATE INDEX review_items_rank_idx ON app.review_items (priority DESC) WHERE status = 'open';
```

- **Status ladder** becomes `open · answered · assumed · dismissed`; drop `deferred` from the
  CHECK and from `REVIEW_STATUSES` (`analysis/repo.py:52`).
- **Every new column needs an RLS isolation test** (non-negotiable #3). No new table, so the
  existing `review_items` policy covers the rows — same posture as `proposal_nodes.decision_note`
  in migration 0130, which added a column and took an isolation test rather than a policy change.
- **`display.py`'s invariant carries forward** unchanged: every advertised choice action is
  exactly an action the resolve endpoint accepts (`analysis/display.py:6-13`).
- The **producers** change completely (the pipeline's `_file_ambiguous_review` and friends are
  deleted with it); the **row** and its whole resolve/reopen/batch surface survive.

---

## 11. Verified vs assumed

**Verified** (read in this repo at the cited lines): every `path:line` above — the review
screen, blocks, registry, grouping, payload, queue controller, armed hook, the analysis API
and repo, migrations 0006 / 0024 / 0130, `InlineProposal`, `enact_outcome_summary`,
`_model_message`, the continuation runner, `LoopTurnExecutor`, the scheduler's expiry action
and handler, the predicate retire sweep, the Gmail clarifications block, `declares_predicate`,
the launcher badge, `NoteScreen`'s default tab, and DESIGN.md's review-inbox and UI-process
sections.

**Also verified — a drift finding worth reconciling independently of this redesign:**
`DESIGN.md:899-951` specifies a **three-lane** *pending · deferred · decided* inbox with
footer *defer* and *talk it over* escape hatches; the shipped screen has **two** lanes
(`ReviewScreen.tsx:467-470`) and a footer with neither (`blocks/Footer.tsx:11-33`), while the
`deferred` status, its partial index and its `REVIEW_STATUSES` entry remain in the backend
serving nothing.

**Assumed** (not verified; flagged so nobody builds on them unchecked):
1. **The shape of turn 0.** How the agent decides what it is confident about vs what it asks,
   and whether it can reliably produce a `default_action` per question, is another dossier's
   territory. This design *depends* on it: the filing gate in §6(a) is only enforceable if the
   agent can be made to always have a position.
2. **Volume.** "≤3 per note", "40 open", "top band 0–5" are engineering judgements, not
   measurements. They should be tunable settings on first build, and the caps are the thing to
   instrument first.
3. **Regret weights.** The four factors are all computable, but their relative weighting is
   untested; the first version should be crude and observable rather than tuned.
4. **`LoopTurnExecutor` from a non-chat context.** Verified that `continuation.py` drives it
   with no client attached, but that path is jerv+plan specific; whether a queue-answer
   follow-up can reuse it without generalizing the runner is unconfirmed.
5. **Cross-device count consistency.** The launcher badge is a poll while foregrounded; whether
   the top-band count needs anything stronger is untested.

---

## 12. Open questions for the owner

1. **Does an unanswered question get to write to the graph at all?** The recommendation is yes
   — the agent commits its guess and marks it `assumed`, visibly and reversibly. The
   alternative is that unanswered questions rot open forever, which is honest but is exactly
   the unread-inbox failure. **This is the decision the rest of the design hangs on.**
2. **Health, finance and location: do those questions ever self-answer?** The recommendation
   says **never** — a firewall-domain fact is never committed on a guess. But "never" means
   they are the one class that *can* accumulate. Is a permanently-open health question
   acceptable, or should it expire to the conservative branch ("don't record it") like a merge
   does?
3. **What does the launcher badge count** — the top band (0–5, reachable zero) or every open
   question (the shame number)? The recommendation is the top band.
4. **Is the daily home line acceptable at all**, or is even one dashed row per day too much
   proaction? The fully-silent alternative is that the launcher badge is the *only* ambient
   signal and the deck is reached by choice.
5. **Formally retire `defer`?** It is already gone from the UI and still present in the
   backend. Recommendation: delete it and let decay-to-assumption be "later". If you disagree,
   the drift in DESIGN.md should be reconciled the other way instead — the doc currently
   describes a screen that does not exist.
6. **Should the agent be allowed to ask a question that has no default?** Recommendation: no,
   ever. It is the single rule that makes the whole scheme work, and it means the agent
   sometimes stays quiet about something it genuinely doesn't know.
7. **Three mocks, one pick** (`docs/mocks/silent-queue/`) — the recommendation is a
   **composition**: A ambient as the spine, B deck as the offered moment, C stacks as the
   fallback and the batching home. If you'd rather pick one, the argument in §7 says A is the
   one that cannot be dropped.
