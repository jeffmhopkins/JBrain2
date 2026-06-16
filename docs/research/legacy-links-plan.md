# Build plan: past / "legacy" relationship links

Buildable, wave-sequenced plan derived from `docs/research/legacy-links-handling.md`
(the design dossier; read it first). Governed by `docs/PROCESS.md` (multi-wave
execution), `docs/DEVELOPMENT.md` (standards), and the `CLAUDE.md`
non-negotiables. **Status: ALL THREE WAVES SHIPPED on the feature branch
(deterministic core + prompt/eval + variant-C frontend). Plan + 3-way pre-build
red-team and the Wave-1 independent review in the § ledger / below. Pending: one
PR + CI (incl. integration & live eval, which don't run locally).**

- **Wave 1** (deterministic core): supersession + `normalize_past_assertion` +
  scoped read refinement + inverse gate + graph_context former rendering.
- **Wave 2** (extraction teaching + eval): note-extract-v18 (past closure +
  always-emit-current), digest re-pinned; eval DSL `ExpectFact.former` +
  `legacy-past-employment` corpus case (advisory).
- **Wave 3** (frontend, variant C): `FactTenure` interval track — a former
  (closed) edge renders a faded "former" span and dims one step; current stays
  calm. GUI gate cleared (variant C).

**Wave 1 review (independent, reviewer ≠ builder) — resolved:** the one real gap
was a missing test that a CLOSED edge mints no inverse through the full pipeline
(added `test_used_to_relationship_is_closed_and_mints_no_inverse`,
test_extraction_pg.py); the `left` marker was tightened to the "left <a job/org>"
shape so "left-justified"/"on the left" never match (+ negative tests). Remaining
review items were comment nits (added) or non-issues the reviewer downgraded.

## Goal

"I used to do X" records X as **former**, never current — *even when there is no
current value*. Two co-stated past values with no order are **equally past**.

## Scope note: fresh database [owner-confirmed 2026-06-15]

The DB will be **reset**, so this is **purely go-forward**: no stored facts to
repair → **no backfill task**, and Wave 2's prompt bump is **not** a corpus
re-extraction migration (no corpus). This removes the single biggest process
hazard the red-team raised. Wave 1's deterministic logic is therefore the whole
correctness story.

## Binding invariants (the model, from the dossier §3.1–§3.2)

1. **Two axes.** `status` (active/superseded = chain position) is independent of
   `valid_to` (null=open/ongoing, set=closed/past). A fact can be `active` **and**
   closed — it's the live truth *about a past interval*.
2. **"Current" = `active AND valid_to IS NULL`.** Never `active` alone.
3. **Functional = at most one *open* value.** Closed values accumulate as history.
4. **Supersession fires only between OPEN values.** A closed-on-arrival fact
   ("used to") never supersedes and is never superseded; it lands straight into
   history. The `superseded_by` chain records *sequential replacement* only.
5. **No invented order/dates.** Co-stated past values are parallel history unless
   the note states a sequence.
6. **`active` ≠ `current` is a READ refinement, scoped surgically.** Most
   `status='active'` filters mean "live/non-retracted" and a closed past fact IS
   live — they must keep showing it (history rails, entity-graph edges, **and the
   integrator's candidate/supersession retrieval, which must still see closed
   facts to compare against**). Only computations of *the current value of a
   predicate* gain `AND valid_to IS NULL`. Hiding history — or hiding closed facts
   from supersession comparison — is a regression.
7. **A closed past relationship has NO derived inverse.** Inverse materialization
   is a "current value" computation; it must gate on `valid_to IS NULL`, not just
   `status='active'` (the §ledger F1 defect).

---

## Wave 1 — Deterministic core (backend; fully CI-testable; no prompt bump, no migration)

Pure logic + a scoped read refinement + the inverse gate. The safety net
everything else leans on, so it ships first (PROCESS.md deterministic-first).
**T1.1 and T1.2 are interdependent and land together** (without T1.2 setting the
end, T1.1 has no closed fact to route) — review them as one unit.

### T1.1 — Supersession: closed-on-arrival skips the open-value contest
- **Files:** `analysis/supersession.py` (`decide`, the no-actives branch
  `445-447`, the functional newest-wins branch `457-490`, the retrospective
  branch `491-498`, `_interval_close`); `tests/unit/test_supersession.py`.
- **Precise changes:**
  1. **Current = OPEN actives only.** The functional/state contest must compare
     against `open_actives = [e for e in actives if e.valid_to is None]`, not all
     actives. A *closed* existing peer is history, never "the current value" to
     supersede or be superseded by.
  2. **Closed-on-arrival never supersedes.** If `candidate.valid_to is not None`
     (and it isn't a retrospective-vs-open case the `491-498` branch already
     owns), it inserts as history and links **nothing**: with an open current it
     is retrospective (existing `491-498` path, keep); with no open current it is
     `active` + **closed**; against a closed peer it **coexists, no
     `superseded_by`**.
  3. **No-actives branch keeps `valid_to`.** `445-447` must return
     `Decision(insert=True, insert_valid_to=candidate.valid_to)` — do **not**
     rely on the `pipeline.py:1980` `or valid_to` fallback (fragile; §ledger F3).
- **Decide matrix (encode as tests):** open-new vs open-current → supersede
  (unchanged); open-new vs closed-existing → open becomes current, closed stays
  history, **no false supersede/refresh** (§ledger F6 — verify the refresh path
  can't swallow it); closed-new vs open-current → retrospective superseded
  history (unchanged); **closed-new vs no open current → active+closed (NEW)**;
  **closed-new vs closed-existing (two co-stated past) → both active+closed,
  neither linked (NEW)**; sequential open→open still chains; pinned untouched.
- **Tests:** all matrix cells; first-and-only past job (active, closed,
  current=none); two co-stated past + no current (both active+closed, neither
  `superseded_by` the other — the §ledger F2 regression); two past + one current;
  pinned closed fact re-flagged not edited; **refresh idempotency: re-applying
  the same closed edge refreshes in place and does NOT alter `valid_to`** (pins
  the `pipeline.py:1954` rendering-only refresh; §ledger F8).

### T1.2 — `normalize_past_assertion` deterministic guard
- **Files:** `analysis/extraction.py` (new pure fn beside
  `normalize_future_assertion`); wire at `pipeline.py:1613` and `:1815` (the held
  + upserted per-fact normalize sites, same as the future guard);
  `tests/unit/test_analysis_extraction.py`.
- **Fires only when ALL hold** (tightened per §ledger F7):
  `kind ∈ {state, relationship}`; `assertion == "asserted"` (so "no longer X" is
  asserted+closed, never a `negated` double-up; never `hypothetical`/`question`);
  the fact has **no temporal at all** (null start AND null end — avoids the
  single-`temporal_precision` conflict a half-dated fact would create); and the
  statement/phrase matches the closed **past-marker set** — *used to, used to be,
  former(ly), ex-, no longer, previously, back when, "left <Org>"*.
- **Effect:** set `temporal = {resolved_start: null, resolved_end: anchor,
  precision: "era", phrase: <marker>}`. Emit `analysis.temporal_closed_past` log
  (prompt-tuning signal, mirrors `temporal_repaired`).
- **Tests:** each marker → closed; negatives ("I work for X" open; "I'll leave X"
  open/expected; an `attribute` never closed; a `negated`/`question` skipped; an
  already-dated fact left alone); precision is `era`, start stays null.

### T1.3 — "current = active AND valid_to IS NULL" (scoped read refinement)
- **Change these (current-value computations — the corrected/expanded list from
  the read-path audit, §ledger F4):**
  - `repo.py:619` — entity-page current-head pick (the linchpin).
  - `repo.py:722` — `note_currency` "current value" of a superseded fact.
  - `canonical.py:115` — entity name projection (a closed former name must not
    re-project the display name).
  - `canonical.py:191` — corroboration count (a past+present same-note reference
    must not double-count toward auto-confirm).
  - `consolidation.py:72` — "live canonical twin" block (a closed twin must not
    block drift-healing).
  - `appointment_projection.py:293` & `:448` — appointment facet + lifecycle
    status projection (show *current* details, not closed history).
  - `pipeline.py:2019` — **inverse-edge gate** (§ledger F1): add
    `and decision.insert_valid_to is None`.
- **Leave alone (live-fact / resolution semantics — confirmed correct), with a
  one-line "why" in the PR:** `graph_context.py:214/220/275` (integrator must
  still see closed facts; see T1.3a); `entities.py:153-158` `_VALID_EDGE`
  (already interval-scoped — `valid_to > :at` correctly excludes past at note
  time; no change); `repo.py:344/384/390/477/566` (graph-view + inbound edges
  show all live edges); `repo.py:722-729` history rails; `arbiter.py:79` (in-pass
  active set).
- **T1.3a — expose `valid_to`/former in the integrator FactLine** (resolves
  open-decision 5; §ledger F5): render the closed/former marker in
  `graph_context.py`'s `FactLine` so the integrator can tell a former relationship
  from a current one **without** gating closed facts out (keeping past entities
  resolvable). Do **not** add `valid_to IS NULL` to the graph_context reads.
- **Tests:** entity-page payload (active+closed → `history`, active+open →
  `current`, mixed → one current + N history); name projection ignores a closed
  former name; inverse not materialized for a closed fact; appointment projection
  shows current facet/status only.

**Wave 1 gate:** per-task adversarial review (≠ builder) + a wave-level review
(touches read paths + firewall-adjacent reads + the inverse/derived path → red-team
mandatory). One PR. Local `ruff`+`pyright`+unit before merge; CI integration green.

---

## Wave 2 — Extraction teaching + eval (no migration — fresh DB)

### T2.1 — Prompt: teach past relationships + always-emit current
- **Files:** `analysis/prompts/note_extract.prompt`; `analysis/prompt.py`
  (`PROMPT_VERSION` bump); **the `.prompt` digest pin in
  `tests/unit/test_promptfile.py` updated in lockstep** (it runs under
  `pytest --cov` in CI, so a stale pin fails CI — §ledger F-pin).
- **Change:** teach the model to (a) set `resolved_end = capture anchor` at era
  precision for a state/relationship stated as ended ("used to / former / no
  longer / left"); (b) **always** emit the stated *current* employer/residence as
  its own edge, never demote it to only a tag (fixes Failure B — SpaceX-as-tag).
  Worked example = the screenshot note. T1.2 is the deterministic net behind any
  lapse.
- **Verification:** out-of-CI live-model eval (the harness cannot test prompts)
  as the pre-merge gate.

### T2.2 — Eval: corpus case + harness assertion DSL extension
- **Files:** `tests/eval/assertions.py` (extend the DSL — §ledger F-eval),
  `tests/eval/corpus/lifecycle.json` (new case), the DB-mode harness.
- **DSL gap to close first:** the current `Expect`/`ExpectFact` cannot assert
  "**no** derived inverse on this edge" or "these two facts are **not**
  `superseded_by`-linked". Add `forbidden_inverse`/`forbidden_fact` and a
  no-supersede-link check, or the eval can't verify the core invariants.
- **Cases:** screenshot note → SpaceX active+**open** `worksFor`; US army +
  Oregon Lithoprint **active+closed**, neither linked; **no** inverse on the past
  edges; `current` resolves to SpaceX only. No-current variant ("used to work for
  US army and Oregon Lithoprint" alone) → both closed, current=none. The
  **deterministic** (hand-authored extraction) version of these lands in Wave 1's
  DB-mode integration tests; the **live**-extraction version rides here.

**Wave 2 gate:** per-task + wave review; live-eval pre-merge; one PR.

---

## Wave 3 — Display legibility (frontend; GUI-GATED)

### T3.1 — "Former / ended" affordance — **variant C, interval timeline** [GUI gate CLEARED 2026-06-15]
- **GUI GATE — DONE.** Three interactive mocks presented; owner chose **C, the
  interval timeline** (`docs/mocks/legacy-links-c-interval-timeline.html`,
  binding spec; decision recorded in `DESIGN.md` "Former / past relationships").
  A (inline chip) and B (current/previously split) rejected.
- **Bound pattern:** a current value (active + open) renders a `--green` open
  span to **now**; a former value (closed `valid_to`) renders a faded/dashed
  `--slate` span and **stays on the default view** (not hidden behind the
  `N earlier →` rail). Undated "used to" reads `former`/`ended ≤ <capture>` at
  era precision — no invented date. Tapping the row opens the property's
  **revision rail** in the shared `Sheet` (source citations live there).
- **Files:** `frontend/src/components/AnalysisTab.tsx` (its `VISIBLE_STATUSES`
  excludes nothing new — closed facts are `active` — but the row must render the
  validity track and route tap → rail; today the inline `FactCitation` is the tap
  target, so the tap behavior changes); `analysis/format.ts:136-137` (`factSpan`
  already yields `from → to` — reuse for the track + rail); `bits.tsx` (the track
  component + reuse the rail); the shared `Sheet`; tests + mock fixtures (default,
  former-undated, mixed current+former).
- **Tests:** an active+open `worksFor` renders the open "now" span; an
  active+closed one renders the former span and is NOT shown as current; tap
  opens the rail with both co-equal former values cited; no inverse rendered for
  a former edge.

**Wave 3 gate:** GUI gate first; then per-task + wave review; one PR.

---

## Sequencing & dependencies

```
Wave 1 (deterministic core + inverse gate) ──► ship first; no migration, full CI
        │ (data now carries correct valid_to + current semantics)
        ├──► Wave 2 (prompt + eval DSL + cases)   — no corpus migration (fresh DB)
        └──► Wave 3 (display)                      — GUI-gated; independent of Wave 2
```

Waves 2 and 3 depend only on Wave 1's data semantics and are mutually
independent; kept as separate single-PR waves (Wave 3 carries the GUI gate).

## CLAUDE.md alignment

- **#1/#2/#3:** Waves 1–2 add no LLM-adapter bypass, no raw file I/O, no new
  table → no new RLS test (no schema change). T1.3 is firewall-adjacent (read
  scoping) and touches the derived-inverse path → wave-level red-team mandatory.
- **#4 comments why-not-what; #5 tests same PR (80% / security-100%, real PG via
  testcontainers, LLM faked):** each wave's tests land with it; Wave 1 is
  pure-logic + DB-mode integration.
- **#6 Conventional Commits, branch+PR, CI green; #7 wiki machine-only; #8
  dev-setup currency:** no new dependency expected.

---

## Red-team findings & iterations (3 independent reviewers)

Each finding below was raised by an independent adversarial reviewer and is now
**resolved in the plan above** (or dismissed with reason).

| # | Severity | Finding | Resolution |
|---|---|---|---|
| **F1** | CRITICAL (×2 reviewers) | Inverse-edge gate (`pipeline.py:2019`) checks only `insert_status=='active'`; a closed-on-arrival fact is active, so it would materialize "US army employs Me" — and that inverse would answer "who works for US army?" with the owner, smuggling the past edge back as current. | **Folded into T1.3 + invariant 7:** gate gains `and decision.insert_valid_to is None`. Dedicated test. |
| **F2** | CRITICAL | Two co-stated past jobs in one note: with no dates both share `reported_at`, so the functional newest-wins branch fires and the 2nd `superseded_by` the 1st — violates "equally past". | **T1.1 change 1+2:** functional contest compares **open** actives only; a closed candidate never supersedes a closed peer. Test added. |
| **F3** | CRITICAL | No-actives branch (`445-447`) returns `insert_valid_to=None`; relies on a fragile pipeline fallback to keep the end. | **T1.1 change 3:** return `insert_valid_to=candidate.valid_to` explicitly. |
| **F4** | MAJOR | T1.3's change-list named only `repo.py:619`; the audit found 7 more current-value sites. | **T1.3 expanded** to the full list (`repo.py:619/722`, `canonical.py:115/191`, `consolidation.py:72`, `appointment_projection.py:293/448`, `pipeline.py:2019`) + confirmed leave-alone set with reasons. |
| **F5** | MAJOR | With closed facts now active, the integrator (graph_context) sees them; open-decision 5 unresolved; and supersession candidate-retrieval **must** keep seeing closed facts. | **T1.3a:** expose `valid_to`/former in the FactLine; do **not** gate graph_context. Resolves decision 5 (past relationships are visible to the integrator, labelled former). Invariant 6 updated. |
| **F6** | MAJOR | open-new vs closed-existing: could the refresh path swallow a closed existing row (losing `valid_to`)? Today refresh writes rendering only (`1954`) — protective but fragile. | **T1.1 matrix + F8 test** pin that refresh never alters `valid_to`. |
| **F7** | MAJOR | Guard could fire on `negated` ("no longer") and on half-dated facts (single `temporal_precision` conflict). | **T1.2 tightened:** `asserted`-only, **no-temporal-only**, era precision. |
| **F8** | MAJOR | Refresh-path idempotency for closed facts untested → latent regression. | **T1.1 test added** (re-apply closed edge → refresh in place, `valid_to` unchanged). |
| **F-eval** | CRITICAL | Eval DSL can't assert "no inverse" / "not superseded-linked", so T2.2 couldn't verify the core invariants. | **T2.2 adds the DSL extension** before the cases. |
| **F-pin** | MAJOR | `.prompt` digest pin (`test_promptfile.py`) must change in lockstep with `PROMPT_VERSION` or CI fails. | **T2.1 lists the pin update** explicitly. |
| **F-gui** | MAJOR | Wave-3 GUI mocks don't exist; the gate hasn't run. | **Correct — by design.** T3.1 marks the gate NOT-yet-executed and blocking; Wave 3 cannot start until mocks are chosen. |
| **F-backfill** | (was CRITICAL) | Existing facts stored open/current need a repair; no backfill task; re-extraction ordering hazard. | **MOOT — DB reset** (scope note). No backfill; Wave 2 carries no corpus migration. |
| **F-misc** | NON-ISSUE | Several findings flagged "T1.1/T1.2 not implemented yet" as defects. | Dismissed — this is a plan; unwritten code is the work it describes, not a defect. |

### Net effect of the red-team
Two genuine CRITICAL correctness holes (F1 inverse gate, F2 co-stated-past
supersession) that would each have *silently re-presented a past job as current*
— the exact bug we set out to kill — are now closed in Wave 1 with tests. The
read refinement grew from 1 to 8 precise sites with a justified leave-alone set,
the guard got three correctness fences, the eval got the assertions it was
missing, and the DB reset erased the backfill/migration risk class entirely.
