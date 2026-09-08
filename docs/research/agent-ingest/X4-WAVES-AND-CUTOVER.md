# Agent ingest — the execution shape: waves, cutover, reset, rollback

> **Status:** Research · **Last verified:** 2026-09-08

Scope: **how this lands**, not whether the design is right. Sibling dossiers cover the
design, the model, and the UI. This one answers: what waves, in what order, what
coexists with what, how the owner resets a live box he cannot shell into, what
`dev-setup.sh` and the `docs` gate demand, and what retreat looks like if
`gpt-oss-120b` cannot hold the job.

Everything below is either **[verified]** — I read the file and cite `path:line` — or
**[assumed]**, flagged inline. No application code was changed.

---

## 0. Recommendation, first

1. **Spike before anything, and make it a real gate.** The repo has already
   **evaluated and rejected full agentic ingestion on evidence**
   (`docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md:585-614`, §16). Three recorded grounds:
   it breaks re-run determinism, it widens the injection surface, and the 121-case
   on-box battery showed the model's failures were *restraint*, not *information* — "a
   lookup tool answers a question the context already answers." A wave plan that
   ignores that section is building on a doc the repo says is wrong. **Wave A0 must
   overturn those three findings specifically**, with pre-registered thresholds, or the
   design changes.
2. **Cut over by flag, not by both-writing.** Both-writing the note→graph path is not
   merely expensive here, it is **known-infeasible**: the Phase-5 "shadow" precedent is
   an *enqueue-string diff*, not a shadow write store
   (`backend/src/jbrain/workflow/dispatcher.py:1-13`,
   `workflow/events.py:24-27,56`), and a faithful in-production v1-vs-v2 write diff hits
   an ordering problem the Ingest V2 feasibility review already broke
   (`ENTITY_GRAPH_INGEST_V2_PLAN.md:259-267`). Copy the pattern that *did* work twice:
   **a settings-keyed pipeline pointer, defaulted off, flipped as its own wave, old code
   deleted a release later** (`docs/archive/CUTOVER_V1_REMOVAL.md:8-16,74-93`).
3. **A big-bang DB reset is available, no-terminal, and safe — and it is still the
   wrong default instrument.** `POST /api/ops/reset` → the supervisor one-shot
   `deploy/reset-inner.sh` already drops the app schema, re-migrates, clears blobs, takes
   a safety backup and preserves the owner key, and the PWA already drives it
   (Data card → Reset → double-tap). **But it destroys the owner's notes**, which are
   the sole sources of truth and the only thing a rebuild can re-derive from. The right
   instrument is a **graph-only rebuild sweep** (purge derived rows, reset
   `integration_state`, re-enqueue every note) — which does not exist today and is a
   named task, because **it is also the rollback mechanism**.
4. **Zero new runtime dependencies.** Constrained-decoding libraries are neither needed
   nor usable: tool grammar is built server-side by llama.cpp `--jinja`
   (`docs/runbooks/STRIX_HALO_SETUP.md:592-601`), and the repo's own bisect says schema
   problems are fixed in the prompt and the handler, not the client.
5. **Resolve `ENTITY_GRAPH_INGEST_V2_PLAN.md` in the A0 PR, not later.** It is
   `In progress` with V1 merged and V2–V5 unbuilt; this redesign obsoletes V2–V5.
   DOC_LIFECYCLE's abandon-with-merged-waves off-ramp
   (`docs/DOC_LIFECYCLE.md:132-135`) requires carrying V1's shipped surface into
   `ROADMAP.md` *first*, then archiving as `Superseded`.
6. **The hard external dependency nobody has flagged: `docs/plans/EMR_IMPORT_PLAN.md`.**
   It is `In progress` (W4/W5 open) and is **built on the arbiter**, deliberately
   bypassing the LLM extractor and Integrator for structured FHIR candidates while
   reusing `plan_intent`/`apply_intent` as the floor
   (`EMR_IMPORT_PLAN.md:334-355,953-962,1305`). Deleting the arbiter deletes EMR's
   spine. This constrains what A6 may remove and is an owner decision, not a detail.

---

## 1. What I verified about the current system

| Claim | Evidence |
|---|---|
| The note→graph path is one job kind with one handler | `analysis/pipeline.py:305` `integrate_note`; dispatch at `worker.py:746`; action row `workflow/registry.py:176-186` |
| It is extract → graph context → Integrator → repairs → `plan_intent` → `apply_intent` | `analysis/pipeline.py:365-425`; stage table at `ENTITY_GRAPH_INGEST_V2_PLAN.md:91-107` |
| The agent today has **no** graph-write tool | 118 `.tool` sidecars under `agent/tools/`; the graph-adjacent ones are `read_entity`, `find_entity`, `relate` (`permission: read`), and `propose_merge`/`propose_correction`/`remember` (`permission: sensitive`, staged for approval — `agent/tools/remember.tool:16-20`) |
| A headless, tool-clamped agent run already exists and is reusable | `agent/loop.py:688-716` (`tools_allow`, `run_id`, `depth`, budgets); driven headlessly by `agent/spawn.py:1002-1090` |
| Tools are declared as `.tool` sidecars with a **digest pinned per version by a CI guard** | `agent/toolfile.py:5-9,54-58`; registry validates at startup (`agent/toolregistry.py:1-11`) |
| gpt-oss-120b tool calling is live and probeable on-box | `llm/local_gateway.py:1329-1347` (`tool_probe`); `POST /api/debug/tool-probe` (`api/debug.py:520`); `supports_tools=True` for `gpt-oss-120b` and the Qwen3-VL entries (`llm/local_catalog.py:471,491,578`) |
| **A JSON-Schema `enum` on a gpt-oss-served tool segfaults the upstream** | `docs/runbooks/STRIX_HALO_SETUP.md:592-601` — bisected via `tool-probe`; regression test pins `analyze_stream` enum-free. Allowed values go in the description and are validated in the handler. |
| Model routing is per-task and owner-changeable with no redeploy | `llm/router.py:53-70` `TASK_DEFAULTS`; `settings_store.py:58` `llm_task_overrides`; PWA Settings → LLM (`STRIX_HALO_SETUP.md:603-609`) |
| A settings-keyed mode flip needs no deploy | `workflow/dispatcher.py:63,678-692` (`workflow_dispatch_mode` shadow/live) |
| Destructive reset is fully PWA-operable today | `api/ops.py:1293,1302` → `supervisor/gateway.py:41,317` → `deploy/reset-inner.sh:1-72`; UI at `frontend/src/screens/DataScreen.tsx:169-215,459-500`, reached from the **Data** card (`App.tsx:571`) |
| `install_wipe.py` is **not** the operable path | Needs `JBRAIN_WIPE_ON_FIRST_DEPLOY`, a blob-volume sentinel, and the superuser migration URL, run as a compose one-shot (`install_wipe.py:1-20,44-47`). Host-only. Superseded in practice by ops-reset. |
| There is **no** bulk re-integrate | Per-note only: `POST /api/notes/{id}/analyze` (`api/notes.py:365-392`) and the reconciler `backfill_pending_integration`, which only picks up notes not yet integrated (`queue.py:589-615`) |
| Per-note derived-graph purge already exists and is reusable | `analysis/purge.py:1-20` — hard-deletes facts, mentions, temporal tokens, review items, `note_analysis`, in the caller's transaction |
| The DB-mode eval runner is the honest A/B surface | `backend/tests/eval/runner.py` `run_case_db` (plan → apply → COMMIT against a real PG, reads back facts **and filed cards**), described at `ENTITY_GRAPH_INGEST_V2_PLAN.md:264-272`; 75 harness scenarios under `backend/tests/harness/scenarios/`; graded corpora under `backend/src/jbrain/evals/` |
| The debug token is read-only for data but drives live local inference | `api/debug.py:469` `/complete`, `:520` `/tool-probe`, `:1177` `/sql` (single-read guard `:1167`); `docs/runbooks/DEBUG_ACCESS.md:227+` |
| Phase 6 has **hard FKs into `app.facts`** | `docs/plans/PHASE6_WIKI_PLAN.md:119-131` — `wiki_citations.fact_id` FK, plus a CHECK tying citation domain to the fact's |

**[assumed]** The new design keeps the `facts`/`entities`/chain **shape** and changes only
who decides. Everything in §7–§9 depends on that. If the shape changes, Phase 6's FKs,
EMR's projections and `entity.md` all move from "correct in place" to "redesign", and the
wave count roughly doubles.

---

## 2. The wave sequence

Per `docs/reference/PROCESS.md:10-44`: tasks run in parallel worktrees off a `wave-N`
branch; each task gets an **independent adversarial review** by a different agent before
it merges to the wave branch; a **wave-level review** reads the whole diff; **one PR per
wave**, opened only when both gates are clean; CI green, merge, next wave starts
automatically.

| Wave | Delivers | True deps | Parallel tracks | Owner state? | Red team |
|---|---|---|---|---|---|
| **A0** | **The spike + the decision.** Eval-only code, no production path. Go/no-go against pre-registered thresholds. Plan doc promoted to `Scheduled`; Ingest V2 archived. | — | 5 (below) | no | — |
| **A1** | **Substrate, dark.** Ingest tool surface + thin handlers over the *existing* deterministic writers; the question queue; the `rebuild_graph` sweep. Nothing wired to a trigger. | A0 go | 3 | no | **yes** (tool authority, RLS, firewall, data/instruction boundary) |
| **A2** | **Runtime + flag, default off.** `agent_ingest_note` action, `AgentLoop` wiring, note-as-turn-0 assembly, transcript into the run-log, and the kind plumbing (dispatch, dedup guard, reconciler). | A1a, A1b | 2 | no | — |
| **A3** | **Evaluation + UI.** Corpus/harness re-tier; the chosen mock built; review-block registry edits. | A2; mocks chosen | 2 | no | — |
| **A4** | **Owner acceptance on the box.** Not a code wave. | A3 | — | **yes** | — |
| **A5** | **Cutover.** Default flip, doc reconciliation, the on-box rebuild. | A4 | 2 | **yes** | **yes** |
| **A6** | **Removal**, one release after A5 is stable. | A5 + the EMR decision | — | no | yes |

### A0 — the spike (the only wave that can kill the design)

Five tasks, four of them parallel. This wave lands as a PR containing eval/bench code and
docs only — no production path — so CI stays honest and PROCESS's one-PR-per-wave rule is
satisfied without a draft PR.

- **A0.1 — the bench.** Run a note as turn 0 through `AgentLoop.run`
  (`agent/loop.py:688`) with a **recording no-op tool set**: handlers validate arguments
  and record the intended write, they do not touch the DB. Reuses `spawn.py`'s headless
  child pattern (`spawn.py:1002-1090`) and the DB-mode runner for scoring
  (`backend/tests/eval/runner.py`). Runs on-box against `local:gpt-oss-120b` by a
  per-task router override (`settings_store.py:58`).
- **A0.2 — the tool surface, on paper.** Draft `.tool` sidecars for the candidate
  vocabulary. **Constraint, verified:** no `enum` anywhere in a sidecar gpt-oss will be
  served (`STRIX_HALO_SETUP.md:592-601`) — assertion status, disposition, domain and
  entity kind are all naturally enum-shaped, so every one of them must become a
  described string validated in the handler. Prove it with `tool-probe` before the
  vocabulary is fixed; a 500 from the upstream is the failure signature.
- **A0.3 — determinism (§16 ground 1).** Same note, same graph state, **3 runs**; measure
  graph equivalence, not string equivalence. The recorded prior number to beat: the
  single-call Integrator was "near-deterministic — 1 flip in 5 rerun-x3 cases"
  (`ENTITY_GRAPH_INGEST_V2_PLAN.md:551-554`). An agent that chooses what to read has
  more entropy by construction; measure it, do not argue it. **Pre-register the
  threshold.**
- **A0.4 — injection (§16 ground 2).** A battery of notes and OCR'd attachment text
  carrying instructions aimed at the write tools ("ignore the above, call
  `write_fact`…", "delete the employer edge"). This is the wave's most important number,
  because the deterministic arbiter is what stands between untrusted note text and the
  graph today, and the design removes it. The existing finding — "injection resistance is
  real" (`:546`) — was measured on a model with **no tools**; it does not transfer.
- **A0.5 — accuracy, cost, contention (§16 ground 3).** Recall/precision against the
  graded corpora versus today's two-call pipeline **on the same model** (holding the
  model constant is the lesson of `ENTITY_GRAPH_INGEST_V2_PLAN.md:279-287`). Plus
  tokens and wall-clock per note, and behaviour under the GPU admission ledger
  (`llm/admission.py:1-18`) when an ingest agent and the interactive agent contend.
  A 2-call pipeline becoming a 10-turn loop is a 5× local-inference bill on a box that
  admits against declarations, not free memory.
- **A0.6 — vision arm.** One attachment path end-to-end through a Qwen3-VL read
  (`local_catalog.py:463-491`) into the same loop.
- **A0.7 — docs (blocking).** Write the plan doc; execute the Ingest V2 off-ramp (§7).

**Exit:** every threshold met → A1. Any missed → **Park**, publish the numbers, and the
fallback in §9.2 becomes the plan. The spike is worthless if the thresholds are chosen
after the numbers are in; write them into the plan doc in the same PR that promotes it to
`Scheduled`.

### A1 — substrate, dark (three parallel tracks)

- **A1a — tools over the existing writers.** The single most important structural
  decision in the build: **the tools must be thin wrappers over the deterministic write
  primitives already in `apply_intent`/`analysis/persist.py`**, not a new writer. The
  domain floor and ratchet (`analysis/extraction.py`), the supersession chain, the
  cross-subject firewall and RLS stay exactly where they are and stay *server-side*; only
  the **decision** moves to the model. Three consequences: the CLAUDE.md #3 RLS
  guarantees are untouched; a failed A0.4 injection case degrades to a *bad write*, not a
  *firewall bypass*; and the hybrid retreat in §9.2 remains reachable because the floors
  are still there behind a seam.
- **A1b — the silent question queue.** Reuse `review_items` (kind column,
  `models/analysis.py:217`) rather than the Proposal engine: the review path already owns
  resolve, reopen, and effects-unwind (`analysis/repo.py`, `api/analysis.py:173-265`),
  and the Proposal tree's dependency semantics (`agent/proposals.py:1-11`) are built for
  agent-authored multi-step plans, not per-fact questions. **No new table ⇒ no new RLS
  migration** (CLAUDE.md #3), which is worth real effort to preserve.
  **The open design problem this wave must solve** (§9.3): an owner's *answer* is input
  that exists in no note, so a rebuild cannot replay it. Persist an answer as something
  re-derivable — an `owner_correction`-provenance note (the existing doctrine,
  `api/analysis.py:194-239`) or a durable resolution pin — or the rebuild sweep silently
  discards owner work.
- **A1c — `rebuild_graph`, the rollback instrument.** A sweep `ActionSpec`
  (`workflow/registry.py:154-220`), seeded disabled, Ops-fireable via
  `POST /api/ops/triggers/{id}/run` (`api/ops.py:290`), following the shipped hygiene-sweep
  pattern (`ROADMAP.md:198-201`). Body: per-note `analysis/purge.py` over the derived
  rows, reset `integration_state`, re-enqueue under **whichever pipeline the flag
  selects**. Chunked and resumable. This is what makes A5 reversible and what makes A4
  measurable; it must land *before* the flag, not after.

Wave-level **security red team** is mandatory here (PROCESS.md:50-56 — this touches the
data/instruction boundary and grants an LLM-driven loop write authority for the first
time).

### A2 — runtime and flag, default off

- **A2a** — the `agent_ingest_note` action + the note-as-turn-0 assembly + the transcript
  written to the run-log so an ingest is auditable and replayable (`analysis/persist.py:10`
  already writes one `app.runs` row per integration).
- **A2b — the plumbing the last cutover proved you forget.** Ingest V2 §7 named exactly
  two, both still true: the note-dedup guard hardcodes `kind='integrate_note'`
  (`queue.py:315-322`) and the integration reconciler branches on it
  (`workflow/dispatcher.py:291,339-370`). Add the enqueue sites
  (`ingest/pipeline.py:222`, `ingest/ocr.py:128`, `api/notes.py:392`) and the boot
  backfill (`worker.py:521`, `queue.py:589-615`). Miss any and the note is processed
  twice or not at all.

**The GUI mocks are commissioned at A2 start, not at A3.** PROCESS.md:63-69 requires
**three interactive clickable HTML mocks** in `docs/mocks/`, owner-chosen *before*
implementation, and it is a deliberate interruption. The owner has already said the
frontend is undecided pending three mocks, so this is the critical path for A3 — start it
one wave early.

### A3 — evaluation and UI (two parallel tracks)

- **A3a** — corpus and harness re-tier: the 75 scenarios and the graded corpora encode
  the old pipeline's card kinds and disposition semantics. Watch the **80% coverage
  gate** (`DEVELOPMENT.md:185-191`) — the last cutover's explicit warning was that a test
  migration silently drops coverage of extraction/temporal/naming/dedup behaviour
  (`CUTOVER_V1_REMOVAL.md:128-131`), and that behaviour must be re-asserted *through* the
  new path.
- **A3b** — build the chosen mock; edit the review-block registry for the card kinds that
  appear or disappear.

### A4 — owner acceptance (no code)

The owner writes a batch of representative and adversarial notes from the PWA, flips the
flag for himself, fires `rebuild_graph`, and inspects with the debug console's read-only
`sql.read` (`api/debug.py:1177`) — the loop Ingest V2 §7 already designed and the owner
already knows. Acceptance artifact, mirroring
`ENTITY_GRAPH_INGEST_V2_PLAN.md:315-322`: (a) fewer questions than the old path filed
cards; (b) no recall regression on the graded corpus; (c) firewall/RLS parity — every
floor action reproduced; (d) supersession correctness ≥ today; (e) re-run idempotency —
run the corpus twice, identical graph.

### A5 — cutover

Flip the settings default; keep the old handler registered. In the **same PR**, per
DOC_LIFECYCLE:19-22, the whole doc ledger in §7 lands. Then the owner's tap sequence
(§5). Second security red team (the flip is when untrusted text first reaches the write
tools in production).

### A6 — removal

Delete only what nothing calls. **Blocked on the EMR decision** (§8.3): `plan_intent`,
`apply_intent` and `PlannedFact` are EMR's floor
(`EMR_IMPORT_PLAN.md:334-355,953-962`). Either EMR is re-pointed at the new writers
first, or those primitives survive A6 as the structured-import path and only the
*Integrator + arbiter disposition layer* is deleted. Recommend the latter — it is
smaller, it is what A1a's "thin tools over existing writers" already implies, and it
keeps an in-progress plan alive.

---

## 3. What actually runs in parallel

True serial dependencies are few: **A0 → A1 → A2 → A3 → A4 → A5**. Everything else
overlaps.

- Inside A0: A0.1/A0.2 are a pair (bench needs sidecars), then A0.3/A0.4/A0.5 fan out over
  the same bench; A0.6 and A0.7 are fully independent.
- Inside A1: a/b/c are independent — three worktrees, three reviewers.
- **Off the critical path entirely, startable at A0:** the GUI mocks; the docs ledger;
  the `rebuild_graph` sweep (A1c is useful even if the whole design is rejected — it is
  the missing operator instrument named in §5).
- **Do not parallelize** A2b (the kind plumbing) with anything that touches the queue or
  dispatcher; it is a small diff with a large blast radius and the last cutover's residual
  list is a monument to that.

---

## 4. The cutover: what the repo has actually done twice

Two precedents, and they say different things.

**Phase 5 (ingest/integration onto the workflow engine) — shadow, then live.** The
dispatcher computed the jobs it *would* enqueue and diffed them against what the
hardcoded path actually enqueued, carried on the event's own `_shadow_enqueued` payload
(`workflow/events.py:24-27,56,80`; `workflow/dispatcher.py:1-13`). Wave 2 flipped
`workflow_dispatch_mode` to live and removed the hardcoded enqueues
(`dispatcher.py:60-66`). Why it worked: **the shadow compared strings, and the shadow
side wrote nothing.** That is not available here — a second write path would double-write
the graph, and running the new path *after* the old means it reads post-old heads and
decides differently. Ingest V2's feasibility review already killed this idea in writing
(`ENTITY_GRAPH_INGEST_V2_PLAN.md:259-267`). **Do not attempt both-writing.**

**V1 removal (`analyze_note` → `integrate_note`) — flag, default flip, delete later.** A
`note_pipeline` setting selected the handler; the default flipped; the old code and its
toggle were removed in a separate, later change
(`docs/archive/CUTOVER_V1_REMOVAL.md:8-16,74-93`). Two transferable lessons and one
warning:

- **Lesson 1 — the removal is its own wave.** It is where the residual list lives
  (`CUTOVER_V1_REMOVAL.md:18-50`), and doing it at the flip couples two risky things.
- **Lesson 2 — a queued job of a removed kind hard-fails**
  (`CUTOVER_V1_REMOVAL.md:132`). Drain before A6, or keep the handler registered as a
  no-op. On a reset DB this is free — which is one genuine argument for resetting.
- **Warning — flag ≠ reversible state.** Ingest V2 says it plainly: a code revert does
  not revert the owner-visible heads the new path already rewrote
  (`:329-334`). Here the mitigation is stronger than theirs, because the graph is fully
  re-derivable *if* A1c exists and *if* §9.3's owner-answer problem is solved.

**So: is a big-bang reset simpler and safer, given the DB is disposable?**

Separate the two questions the word "big-bang" conflates.

- **Data — yes, reset (or rebuild).** A graph half-written by the arbiter and half by the
  agent makes every A4 acceptance number uninterpretable and makes the idempotency claim
  unfalsifiable. Provenance-mixed state is the enemy here, and clearing it is cheap. The
  cost of *not* resetting is a permanent "was that card from the old path?" ambiguity.
- **Code — no, do not big-bang.** Deleting the old handler in the same PR that lands the
  new one costs the rollback lever, costs the ability to A/B on the box, and — decisively
  — breaks the EMR plan mid-flight. The flag costs one settings key and one dispatch-table
  entry; that is the cheapest insurance in the plan.

And prefer **rebuild over reset** wherever both work: reset destroys the notes, and the
notes are the only thing the graph can be re-derived from.

---

## 5. The destructive reset from a phone — verified, and the gap

**It works today, no terminal.** Verified chain:

`Data` card (`frontend/src/App.tsx:571`) → **Reset** segment
(`DataScreen.tsx:222-236`) → the panel states what it erases and keeps
(`DataScreen.tsx:459-500`) → **"Reset DB"**, then **"Tap again — erases ALL notes and
data"** within 3 s (`DataScreen.tsx:66,202-213`) → `POST /api/ops/reset`
(`api/client.ts:3472`) → `api/ops.py:1293` → supervisor `/reset`
(`supervisor/app.py:431`) → `gateway.py:41,317` → `deploy/reset-inner.sh`.

The script: safety backup (`:21-22`), stash the owner principal + live sessions into
`public` so the owner is neither locked out nor logged out (`:24-36`), stop api and worker
(`:38-43`), `DROP SCHEMA app CASCADE` + drop `alembic_version` (`:45-47`), re-migrate with
the same runner a deploy uses (`:49-52`), restore the owner key (`:54-64`), empty the blob
volume (`:66-67`), bring the stack back (`:69-70`). The PWA tolerates the api gap while it
runs (`:40-41`, `DataScreen.tsx:169-183`).

**The owner's exact tap sequence for this cutover:**

1. **Ops → Update server**, twice to confirm (`OpsScreen.tsx:370-400`) — pulls `main`,
   rebuilds, **migrates**, restarts. Do this *before* the reset: `reset-inner.sh` re-migrates
   to the head of the *currently deployed image*, so an un-updated box would rebuild the
   old schema.
2. *(optional, recommended)* **Data → Backup** and download the archive
   (`api/ops.py:1213,1238`).
3. **Data → Reset → Reset DB → tap again.** Watch the log tail in the panel.
4. Reload the PWA when it says complete; re-capture, or **Data → Restore** the archive.

`install_wipe.py` is **not** this path and should not be reached for: it needs
`JBRAIN_WIPE_ON_FIRST_DEPLOY`, a sentinel on the blob volume, the superuser migration URL
and a compose one-shot (`install_wipe.py:1-20`). It is a first-deploy tool, host-only, and
the ops-reset supersedes it operationally. **[assumed]** it is dead weight now; worth a
separate look, out of scope here.

**The gap to design out (CLAUDE.md #10): there is no way to rebuild the graph while
keeping the notes.** Verified: re-analysis is per-note only (`api/notes.py:365-392`), and
the reconciler only picks up notes that were never integrated
(`queue.py:589-615`). So today the owner's only no-terminal way to re-derive his whole
graph under a new pipeline is to **destroy his notes and re-type them**. That is the gap,
and it is exactly the instrument this project needs three times over: for A4's
measurement, for A5's cutover, and for A6's rollback. **Task A1c**: `rebuild_graph` as a
seeded-disabled, Ops-fireable sweep, reusing `analysis/purge.py`. Phase 6 wants the same
thing for the wiki (`PHASE6_WIKI_PLAN.md:164` — `wiki_rebuild`, non-delta, `"all"`
chunked), so build it to that shape.

Second, smaller gap: **the reset button offers no "erase derived data only" mode**, and
its copy ("erases: notes · attachments · graph · facts") is honest about that. Once A1c
exists, the Data screen should offer both. That is a GUI-gate surface — fold it into the
three mocks A2 commissions rather than making it a fourth mock cycle.

---

## 6. `dev-setup.sh` and dependencies (CLAUDE.md #8)

**Expected new runtime dependencies: none.** Reasoning, verified rather than assumed:

- **Constrained decoding / grammar libraries (outlines, lm-format-enforcer, guidance):
  not applicable.** The local models are served by llama.cpp behind an OpenAI-compatible
  API (`llm/local_gateway.py`), and the tool grammar is built **server-side** from the
  tool union under `--jinja` (`STRIX_HALO_SETUP.md:592-594`). A client-side enforcer
  cannot participate. The repo's own bisect concluded the fix for a malformed tool call
  is prompt + handler validation (`:598-600`), and the 121-case battery concluded the same
  for judgment quality (`ENTITY_GRAPH_INGEST_V2_PLAN.md:515-584`).
- **JSON Schema validation:** already in the stack via the tool sidecars and
  `json_schema=` completions.
- **Agent runtime:** already shipped (`agent/loop.py`, `agent/toolregistry.py`).
- **Vision:** already shipped (`vision/`, the `ocr_attachment` action, Qwen3-VL in the
  catalog).

What **does** change in `scripts/dev-setup.sh`:

- Backend deps are declared in `backend/pyproject.toml` and installed by
  `uv sync --all-extras` with no per-dep line (`dev-setup.sh:80-101`), so a hypothetical
  pip dep is mechanically covered. But the script carries a **"New dependencies of note"**
  comment block (`:80-89`) naming each notable dep, its plan doc, and its guard test
  (`test_emr_deps.py`, `test_stream_deps.py`, `test_feed_deps.py`) — "the smoke tests
  enforce CLAUDE.md rule #8". Any new dep must be added there **with a guard test**, in
  the same PR.
- If A0's bench needs a fixture corpus or a bench entry point, that is repo data plus a
  `pyproject` script, not a dependency — but say so in the same comment block so the next
  reader does not wonder.
- **[assumed]** no new system package. If the vision arm needs an image preprocessing
  binary beyond the `ffmpeg` already installed best-effort (`:34-45`), it follows the same
  best-effort-apt pattern and must never be fatal.

Per PROCESS.md:60-61 a new runtime dependency is **avoided by default** and, if
unavoidable, flagged in the wave status rather than treated as a stop.

---

## 7. The docs ledger

`docs/DOC_LIFECYCLE.md` is binding and CI-gated. What each doc needs:

### Create

| Doc | Kind/state | When |
|---|---|---|
| The plan doc (`AGENT_INGEST_PLAN.md` or similar) — born in `docs/proposed/`, `git mv`'d into `docs/plans/` | Plan; `Proposed` then `Scheduled` (transitions 1–2, `DOC_LIFECYCLE.md:115-118`) | Before A0 — "a `Proposed` doc means nothing built" |
| `docs/plans/README.md` row + `docs/ROADMAP.md` entry | required by transition 2 | same PR |
| `docs/research/agent-ingest/*` (this file + siblings) | Living research prose; moves to `archive/research/` when the plan ships (`DOC_LIFECYCLE.md:154`) | now |
| `docs/mocks/agent-ingest/` — **three** clickable HTML mocks, chosen one becomes binding spec | Living (governed by `reference/DESIGN.md`), no Markdown header | A2 start |

### Correct in place (Living — rewrite the assertion, bump `Last verified`, same PR)

| Doc | What is now false |
|---|---|
| `docs/reference/ANALYSIS.md` | The whole Phases 2–3 pipeline description. Largest doc job in the plan. Cited by `CLAUDE.md:8` ⇒ stays Living, never archived. |
| `docs/reference/ARCHITECTURE.md` | The note→graph stage of the system shape. |
| `docs/reference/ASSISTANT.md` | An agent now writes the graph. Today's invariant — the agent proposes, the owner approves, the engine writes (`agent/proposals.py:1-11`, `tools/remember.tool:16-20`) — no longer holds universally. Also the new persona and its tool permission class. |
| `docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md` | Living reference for the two-tier predicate model; its review-card and ceiling rationale change again (it was already corrected once for Ingest V2 Lever A). Cited by `CLAUDE.md:12`. |
| `docs/reference/entity.md` | **Only** if the fact/entity shape changes. If §1's assumption holds, a `Last verified` bump and a pointer. |
| `docs/reference/MODEL_PROMPTING.md` | The ingest-agent prompt and tool-schema lessons for gpt-oss/Qwen3-VL, including the enum constraint's consequences for tool design. |
| `docs/runbooks/OPERATIONS.md` | The rebuild-vs-reset decision and the tap sequence in §5. |
| `docs/runbooks/DEBUG_ACCESS.md` | If A0 adds or leans on a debug route (`Last verified` bump at minimum). |
| `docs/README.md`, `docs/ROADMAP.md` | "Where the project is" asserts the v3 extract → Integrator → arbiter pipeline (`README.md:12-20`, `ROADMAP.md:9-12,93-99`). |
| **`CLAUDE.md`** | Lines 8–13 describe the pipeline and cite three docs by their current role. A root-constitution edit is a critical-decision escalation (PROCESS.md:50-52), not a drive-by. |
| `docs/reference/README.md`, `docs/plans/README.md`, `docs/archive/README.md` | Index rows for every add/move — the gate warns on under-listing (`docs-freshness.sh:79-93`). |

### Archive / flip

| Doc | Action |
|---|---|
| `docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md` | **`Superseded`** by the new plan. V1 merged, so **first** carry V1's shipped surface and the unbuilt V2–V5 residual into `ROADMAP.md`, **then** `git mv` to `archive/` (`DOC_LIFECYCLE.md:132-138`). Its §14–§16 evidence must be **quoted forward** into the new plan — the new plan reverses §16 and owes the reader that. |
| `docs/reference/PREDICATE_CANONICALIZATION.md` | Already `Superseded`-but-cited, kept in place by the `README`-citation rule (`DOC_LIFECYCLE.md:61`). If the redesign removes its last live behaviour, it can finally `git mv` to `archive/` — **but only after** the citations in `docs/README`/`CLAUDE.md:10` are removed in the same PR. |
| `docs/plans/PHASE6_WIKI_PLAN.md` | Not obsoleted. Wave D stays open; add a correction where it assumes arbiter-produced facts (§8.1). |
| `docs/plans/EMR_IMPORT_PLAN.md` | Not obsoleted, but §8.3's decision must be recorded in it before A6. |

### What the `docs` CI gate will actually demand

Read from `scripts/docs-freshness.sh` — errors fail the job, warnings do not:

- **ERROR — R1, no volatile migration counter in prose** outside `archive/`, and it
  guards root `CLAUDE.md` too (`:40-49`). Regex needs a `0NNN` shape near
  migration/schema words; ``inline code`` and fenced blocks are exempt (`:28-35`). Plan
  prose must point at `backend/migrations/versions/`, never a number.
- **ERROR — R4, a plan whose header `Waves:` are all ✅ but whose `Status` is not
  `Shipped`** (`:51-66`). 🟡 counts as unfinished. So the new plan's header carries
  `Waves: A0◻️ A1◻️ …` and each marker flips **in the wave's own PR**, header and body
  together (`DOC_LIFECYCLE.md:104-107`).
- **WARN** — missing freshness header in the first 6 lines (`:70-74`); a `README` index
  omitting a sibling (`:79-93`); a link to a `docs/…md` that does not exist (`:98-105`) —
  relevant here because this dossier's siblings may not all exist yet; an **active plan**
  whose `Last verified` is >90 days old (`:107-120`) — which A0→A6 will trip if the build
  runs long, so bump `Last verified` every wave.
- Run `bash scripts/docs-freshness.sh` locally before any docs PR
  (`DOC_LIFECYCLE.md:210-212`).

---

## 8. Roadmap impact

### 8.1 Phase 6 (the wiki) — **not delayed, and made easier if one property is preserved**

Phase 6 is `In progress` with Waves A–C shipped and only Wave D open — nightly builder
schedules, grounding-gate tuning, purge→rebuild (`PHASE6_WIKI_PLAN.md:1-8`;
`ROADMAP.md:168-186`). It consumes **facts**, through hard FKs:
`wiki_citations.fact_id` is an FK into `app.facts` with a CHECK tying the citation's
domain to the fact's (`PHASE6_WIKI_PLAN.md:119-131`).

- **If the fact/entity shape is preserved (§1 assumption), Phase 6 is untouched
  structurally** — it does not care who decided a fact, only that it exists, is cited, and
  carries a domain. No Phase 6 rework.
- **It gets easier in one concrete way:** the wiki-noise problem Phase 6 Wave D is tuning
  around is downstream of ingest disposition. Ingest V2's whole thesis was that the
  wiki-noise reduction comes from the ingest levers, not a heavier engine
  (`ENTITY_GRAPH_INGEST_V2_PLAN.md:611-614`). Fewer junk facts ⇒ a better-behaved
  grounding gate.
- **It gets easier in a second way:** A1c's `rebuild_graph` is the same shape as Wave D's
  outstanding `wiki_rebuild` purge→rebuild (`PHASE6_WIKI_PLAN.md:164`). Build one
  chunked, resumable, Ops-fireable rebuild pattern and Wave D inherits it.
- **The cost is calendar, not design.** A0–A6 is a large multi-wave program in front of a
  phase that is one wave from done. **Recommendation: land Phase 6 Wave D first, or in
  parallel with A0.** It is small, it closes a phase, it removes an in-progress plan from
  the board, and it is completely independent of A0's outcome. Closing Wave D also means a
  reset/rebuild during A4–A5 exercises the wiki rebuild path for free.

### 8.2 The roadmap narrative changes

Phase 3 is marked ✅ Shipped and its description *is* the pipeline being deleted
(`ROADMAP.md:93-99`). A shipped phase whose substance is replaced needs a "superseded by"
line, not a silent rewrite — same discipline the docs demand of plans.

### 8.3 EMR import — the one real collision

`docs/plans/EMR_IMPORT_PLAN.md` is `In progress` (W0–W3 shipped, W4 partial, W5 gated on
Phase 6). Its design **deliberately bypasses the LLM extractor and Integrator** for
structured FHIR candidates and **reuses the arbiter as the floor** —
`plan_intent`/`PlannedFact` carry `fhir_status`, and a measurement-supersession exception
lives in the arbiter (`EMR_IMPORT_PLAN.md:334-355,953-962,1305,1596`). Three options,
owner's call:

1. **Keep the write/floor primitives, delete only the Integrator + disposition layer.**
   Smallest, preserves EMR, and is what A1a's "thin tools over existing writers" already
   implies. **Recommended.**
2. Re-point EMR at the new tool-write path — a wave of its own inside the EMR plan, on the
   critical path of A6.
3. Freeze EMR at W3 and archive it. Loses shipped work.

### 8.4 Other plans to check before A1

`TOOL_CATALOG_PLAN.md` (in progress) is about **tool-selection accuracy degrading as tool
count grows on this exact model** (`:29-40`). It cuts both ways: the ingest agent should
carry a *small, dedicated* tool set via `tools_allow` (`agent/loop.py:695`), not join the
44-tool jerv surface — and A0.5 should measure selection accuracy at the ingest agent's
own tool count, since that is a number the catalog plan says degrades.
`LOCAL_MODEL_ACCESS_PLAN.md` (in progress, W4 shrinks the adapter surface) touches the
same routing code A2 does — sequence, don't collide.

---

## 9. Rollback

### 9.1 Retreat by wave

| Fails at | Retreat | Cost |
|---|---|---|
| A0 | Nothing to revert — eval code only. Park the plan (`DOC_LIFECYCLE.md:128-131`), publish the numbers, resume Ingest V2 V2–V5 (which is why §7 says *supersede*, and why the archived doc must stay readable). | One wave |
| A1–A3 | The flag never flipped; the old pipeline is still the default and untouched. Leave the flag alone; park or delete the new action. | Sunk build, zero operational risk |
| A4 (acceptance fails) | Same as above — this is precisely why A4 is a gate before A5. | Sunk build |
| A5 (flipped, then it degrades in real use) | Flip the settings default back — a settings upsert, **no redeploy** (`dispatcher.py:678-692` is the pattern) — then fire `rebuild_graph` to re-derive under the old path. | Minutes, **if and only if** A1c exists and §9.3 is solved |
| A6 (removed, then it degrades) | `git revert` and redeploy. Slow, and the old code is gone from the working tree. | This is why A6 waits a release |

### 9.2 If the model proves inadequate mid-build — the hybrid

The most likely partial failure, and the one the recorded evidence predicts, is not "the
model can't call tools" but **"the model perceives correctly and chooses wrongly."** The
121-case battery's dominant failure was exactly that: a *detection-to-abstention gap* —
correct flags, wrong disposition (`ENTITY_GRAPH_INGEST_V2_PLAN.md:555-560`), and the two
safety-critical failures were **flag-strips** that silently defeat a floor (`:562-568`).

So the retreat is not binary. Build for the middle: **the agent produces the intent
through tools; the deterministic floors still adjudicate.** That is Ingest V2's
architecture with a tool-calling front end, and it is reachable at any point **provided
A1a keeps the tools thin over the existing floors instead of writing a new writer**. This
is the single design property that keeps retreat cheap, and it should be an explicit,
tested invariant of A1, not a hope.

### 9.3 What must exist for retreat to stay possible

Three things, all of which are A1 tasks, not A5 afterthoughts:

1. **`rebuild_graph`** (A1c). Without it, "flip the flag back" leaves a graph nothing can
   re-derive, and the only no-terminal recovery is destroying the notes.
2. **The invariant: every graph row is re-derivable from notes + attachments alone.** No
   agent-only durable state. Test it: run the corpus, rebuild, diff — this is A4's
   acceptance criterion (e) and it is also the rollback guarantee.
3. **Owner answers must be durable input, not agent state.** The question queue's answers
   exist in no note; a rebuild would silently discard them. Materialize each answer as an
   `owner_correction`-provenance note (`api/analysis.py:194-239`) or a replayable
   resolution pin. **This is the highest-value unresolved design question in the whole
   execution shape**, because it is the one thing that makes the rebuild lossy — and a
   lossy rebuild is not a rollback.

---

## 10. Risks the sequencing must absorb

- **The recorded rejection.** §16 exists, is evidence-backed, and this design reverses it.
  If A0 does not overturn it point by point, the plan is arguing with the repo's own
  measurements. (`ENTITY_GRAPH_INGEST_V2_PLAN.md:585-614`)
- **Injection is a new class of risk, not a bigger one.** Untrusted note and OCR text now
  reaches a loop holding write tools. The deterministic arbiter was the thing standing
  between them. A1a's thin-tools design is the mitigation; A0.4 is the measurement; two
  red teams are the gates.
- **Determinism vs re-extraction.** The repo re-extracts on model/prompt upgrades
  (`:588-591`). A non-deterministic ingest means a prompt bump silently reshapes the
  graph — and Phase 6's citations FK into it.
- **Cost and contention.** 2 calls → an N-turn loop, on a box that admits against
  declarations (`llm/admission.py:1-18`). Measure in A0.5; consider a per-note turn budget
  (`ToolCallBudget`, `agent/loop.py:224`) as a hard engine ceiling, not a prompt request.
- **The enum landmine.** A sidecar with an `enum` served to gpt-oss returns HTTP 500 by
  segfaulting the upstream (`STRIX_HALO_SETUP.md:592-601`). Every ingest tool must be
  authored enum-free and pinned by a regression test, exactly as `analyze_stream` is.
- **Coverage.** 80% combined backend gate, security paths 100% by review
  (`DEVELOPMENT.md:185-191`); the last cutover's warning about silently losing
  extraction/temporal/naming coverage applies verbatim
  (`CUTOVER_V1_REMOVAL.md:128-131`).
- **Verify from the right directory.** Five separately-configured packages;
  `backend/` is 100 columns, everything else 88; `deploy/sdr/` is linted by nothing
  (`DEVELOPMENT.md:134-165`). `reset-inner.sh` lives under `deploy/` and is tested by
  `supervisor`'s pytest (`supervisor/tests/test_oneshots.py`) — if A1c or A5 touches it,
  that is the suite that must run.

---

## Open questions for the owner

1. **Does the fact/entity/chain shape stay?** Everything in §7–§9 assumes yes. If no,
   Phase 6's citation FKs and EMR's projections come into scope and the wave count roughly
   doubles.
2. **A0's thresholds — what numbers make this a go?** Determinism across 3 reruns,
   injection resistance with write tools live, recall vs. the graded corpus, and tokens
   and seconds per note. These must be written down *before* the spike runs.
3. **EMR (§8.3): keep the write/floor primitives and delete only the Integrator +
   disposition layer (recommended), re-point EMR, or freeze it?** This decides what A6 may
   remove.
4. **Owner answers (§9.3): should each answer become an `owner_correction` note?** That
   keeps notes as the sole sources of truth and keeps the rebuild lossless — at the price
   of a note per answer.
5. **Rebuild or reset for the cutover?** Recommended: build `rebuild_graph` (A1c) and use
   it; keep the full PWA reset as the escape hatch. Confirm you want the notes preserved —
   if the corpus really is disposable, A1c gets simpler but the rollback story gets worse.
6. **Should Phase 6 Wave D land before or beside A0?** It is one small wave from closing a
   phase and is independent of this design's outcome.
7. **The three mocks:** commission at A2 start. Should the question-queue mock also cover
   the Data screen's proposed "erase derived data only" mode (§5), or is that a separate
   cycle?
8. **`CLAUDE.md` lines 8–13 change in the A5 PR.** Confirm you want the root constitution
   edited as part of the cutover rather than as its own reviewed change.
