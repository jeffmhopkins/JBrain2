# Build plan: past / "legacy" relationship links

Buildable, wave-sequenced plan derived from `docs/research/legacy-links-handling.md`
(the design dossier; read it first). Governed by `docs/PROCESS.md` (multi-wave
execution), `docs/DEVELOPMENT.md` (standards), and the `CLAUDE.md`
non-negotiables. **Status: plan + red-team complete; awaiting owner go.**

## Goal

"I used to do X" records X as **former**, never current — *even when there is no
current value*. Two co-stated past values with no order are **equally past**.

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
6. **`active` ≠ `current` is a READ-PATH refinement, scoped surgically.** Most
   `status='active'` filters mean "live/non-retracted" and a closed past fact IS
   live — they must keep showing it. Only computations of *the current value of a
   predicate* gain `AND valid_to IS NULL`. Hiding history is a regression.

---

## Wave 1 — Deterministic core (backend; fully CI-testable; no prompt bump, no migration)

Pure logic + a scoped read refinement. This is the safety net everything else
leans on, so it ships first (PROCESS.md deterministic-first).

### T1.1 — Supersession: closed-on-arrival skips the contest
- **Files:** `analysis/supersession.py` (`decide`, `_interval_close`,
  `_validity`/`key`); `tests/unit/test_supersession.py`.
- **Change:** a candidate with `valid_to` set (closed) on a `state`/functional-
  `relationship` address must (a) **not** be flattened to active+open by the "no
  actives → insert active" branch (`supersession.py:446-447`) — it inserts
  `active` but **closed** (keeps its `valid_to`); (b) **not** supersede an
  existing *closed* value, and **not** be superseded by one — supersession is
  open-vs-open only; (c) coexist with other closed values on the same address
  (no `superseded_by` link between two closed facts). An existing **open** value
  is still closed+chained by a newer **open** value (unchanged sequential path).
- **Decide matrix to encode:** open-new vs open-current → supersede (today);
  closed-new (older validity) vs open-current → retrospective superseded history
  (today, keep); closed-new vs **no active** → active+closed (NEW); closed-new vs
  closed-existing → both coexist, no link (NEW); open-new vs closed-existing →
  open becomes current head, closed stays history, no false supersede (verify).
- **Tests:** first-and-only past job (active, closed, current=none); two
  co-stated past jobs + no current (both active+closed, neither linked); two past
  + one current (current open+active; two closed); sequential open→open still
  chains; pinned closed fact untouched; re-extraction of the same closed fact is
  an idempotent refresh, not a duplicate (anchor is stable capture time, so
  `valid_to=anchor` is deterministic across re-runs).

### T1.2 — `normalize_past_assertion` deterministic guard
- **Files:** `analysis/extraction.py` (new pure fn beside
  `normalize_future_assertion`); wire at `pipeline.py:1613` and `:1815` (same
  per-fact sites); `tests/unit/test_analysis_extraction.py`.
- **Change:** for a `state`/functional-`relationship` fact with **no** `valid_to`
  whose statement/phrase matches a **closed past-marker set** — *used to, used to
  be, former(ly), ex-, no longer, previously, back when, "left <Org>"* — stamp
  `temporal.resolved_end = anchor` at `precision = era` (keep/lower precision; do
  not raise). Mirrors `normalize_future_assertion`'s shape: relax only the
  temporal, never rewrite kind/object. Skip if a `valid_to` already present
  (model or upstream supplied one).
- **Guard rails:** never fire on `event`/`measurement`/`attribute` (timeless);
  never on `assertion ∈ {hypothetical, question}`; emit
  `analysis.temporal_closed_past` log (prompt-tuning signal, mirrors
  `temporal_repaired`).
- **Tests:** phrase matrix (each marker → closed); negative cases ("I work for X"
  stays open; "I will leave X" stays open/expected); attribute never closed;
  already-closed left alone; precision floor.

### T1.3 — "current = active AND valid_to IS NULL" (scoped read refinement)
- **Files:** `analysis/repo.py:619` (the entity-page current-head pick — the
  linchpin); audit each `status='active'` read for *current-value* semantics vs
  *live-fact* semantics; any wiki/assistant "current value of predicate" reader.
- **Change (surgical, per invariant 6):** at `repo.py:619`, `current` becomes the
  first row with `status='active' AND valid_to IS NULL`; closed actives fall to
  `history` (visible as former, T-Wave-3). **Do NOT touch** filters that mean
  "live/non-retracted": `graph_context.py:214/220/275` (integrator resolution —
  must still see closed facts? see open-decision below), entity edges
  (`entities.py:154/176/182`, `repo.py:384/390/477/566`), history rails
  (`repo.py:722-729`), appointment projection (scheduled-time bindings, different
  semantics). Each left-alone site gets a one-line "why unchanged" note in the PR.
- **Tests:** entity-page payload: an active+closed `worksFor` lands in `history`
  with `current=None`; an active+open lands in `current`; mixed (one open, two
  closed) → one current, two history.

**Wave 1 gate:** per-task adversarial review (different agent than builder) + a
wave-level review (this wave touches read paths/firewall-adjacent reads → include
red-team). One PR. Local `ruff`+`pyright`+unit before merge; CI integration green.

---

## Wave 2 — Extraction teaching + eval (triggers ONE re-extraction migration)

### T2.1 — Prompt: teach past relationships + always-emit current
- **Files:** `analysis/prompts/note_extract.prompt`; `analysis/prompt.py`
  (`PROMPT_VERSION` bump + the `.prompt` digest pin); the live-eval set.
- **Change:** teach the model to (a) set `resolved_end = capture anchor` at
  era/unknown precision for a state/relationship stated as ended ("used to",
  "former", "no longer", "left"); (b) **always** emit the stated *current*
  employer/residence/etc. as its own edge, never demote it to only a tag (fixes
  Failure B — the SpaceX-as-tag drop). Add a worked example = the screenshot note.
- **Discipline:** `PROMPT_VERSION` bump → budgeted corpus re-extraction
  (`docs/ANALYSIS.md` "Reprocessing", ≈$0.01/note); update the `.prompt` digest
  pin; T1.2 is the deterministic net behind any model lapse.
- **Verification:** out-of-CI live-model eval (the harness cannot test prompts)
  as the pre-merge gate.

### T2.2 — Eval corpus case
- **Files:** `tests/eval/corpus/lifecycle.json` (or a new case file); DB-mode
  harness assertions.
- **Change:** the screenshot note → SpaceX active+**open** `worksFor`; US army +
  Oregon Lithoprint **active+closed**, neither `superseded_by` the other; **no**
  derived inverse on the past edges; `current` resolves to SpaceX only. Plus a
  no-current variant ("used to work for US army and Oregon Lithoprint" alone) →
  both closed, current=none. A DB-mode deterministic case (hand-authored
  extraction) belongs in Wave 1's integration tests; the **live**-extraction
  assertion rides here behind the prompt.

**Wave 2 gate:** per-task + wave review; live-eval pre-merge; one PR.

---

## Wave 3 — Display legibility (frontend; GUI-GATED)

### T3.1 — "Former / ended" affordance
- **GUI GATE (PROCESS.md §GUI):** three interactive mock HTML artifacts for the
  Analysis-tab + entity-page "former" treatment, **presented to the owner to pick
  before any code**. The chosen mock lands in `docs/mocks/` as the binding spec.
  This is a blocking critical-decision interruption.
- **Files (after mock chosen):** `frontend/src/components/AnalysisTab.tsx`
  (currently filters to `{active, pending_review}` — closed-active facts would
  vanish; must show them as *former*); `analysis/format.ts:136-137` (`factSpan`
  already renders `from → to`); `bits.tsx` (a "former" chip vs the muted
  `superseded` one); tests.
- **Change:** surface a closed interval as *former* in the note's own analysis
  (not only entity history rails); render `valid_to` / a "former" chip; keep the
  calm "active+open = no chip" baseline.
- **Tests:** AnalysisTab renders an active+closed `worksFor` as former with its
  end; active+open unchanged.

**Wave 3 gate:** GUI gate first; then per-task + wave review; one PR.

---

## Sequencing & dependencies

```
Wave 1 (deterministic core) ──► ship first; no migration, full CI
        │ (data now carries correct valid_to + current semantics)
        ├──► Wave 2 (prompt + eval)   — one PROMPT_VERSION bump / re-extraction
        └──► Wave 3 (display)         — GUI-gated; independent of Wave 2
```

Waves 2 and 3 are mutually independent (both depend only on Wave 1's data
semantics) but are kept separate waves: Wave 2 carries a migration, Wave 3
carries the GUI gate — distinct single-PR concerns.

## CLAUDE.md alignment

- **#1/#2/#3:** Waves 1–2 add no LLM-adapter bypass, no raw file I/O, no new
  table → no new RLS test required (no schema change). T1.3 is firewall-adjacent
  (read scoping) → wave-level red-team mandatory.
- **#4 comments why-not-what; #5 tests same PR (80%/security-100%, real PG via
  testcontainers, LLM faked):** each wave's tests land with it; Wave 1 is
  pure-logic + DB-mode integration.
- **#6 Conventional Commits, branch+PR, CI green; #7 wiki machine-only; #8
  dev-setup currency:** no new dependency expected.

---

## Red-team findings & iterations

*(populated by the independent red-team pass — see next section)*
