> **Status:** Research · **Last verified:** 2026-09-08

# X8 — Downstream consumers of the entity/predicate graph, under agent ingest

**Question answered.** The proposed change deletes the deterministic
`extract → Integrator → arbiter (plan_intent) → apply (apply_intent/_apply)` pipeline and
replaces it with a tool-using LOCAL agent that writes the graph directly, asking the owner
when unsure. `app.facts` / `app.entities` survive in *some* form. This doc maps **every
consumer downstream of that producer** and states, per consumer,
**UNAFFECTED / NEEDS ADAPTER / BREAKS** with the specific fix.

**Method.** Read `CLAUDE.md`, `docs/plans/PHASE6_WIKI_PLAN.md`,
`docs/reference/WIKI_TYPE_GUIDES.md`, `docs/reference/ARCHITECTURE.md`, `docs/ROADMAP.md`,
`docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md`, `docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md`,
then `backend/src/jbrain/{wiki,search,agent,appointments,lists,locations,family,analysis,
ingest/emr,workflow}/**`, migration `0046_wiki_graph_coupling.py`, and the frontend
`analysis/bits.tsx` / `components/AnalysisTab.tsx` / `screens/EntityHistorySheet.tsx`.
Everything marked **[V]** was read in code; **[A]** is inference stated as inference.

---

## 0. What `_apply` actually is (the seam being cut)

`AnalysisPipeline._apply` (`backend/src/jbrain/analysis/pipeline.py:825-993`) is not "write
facts". It is a **thirteen-effect transaction** and every one of them is somebody's input **[V]**:

| # | Effect | Code | Downstream owner |
|---|---|---|---|
| 1 | Resolve mention→entity (4-layer: alias / embedding / LLM / hard-link) | `pipeline.py:820` → `analysis/entities.py:374-475` | entity identity, merge machinery |
| 2 | Rebuild `entity_mentions` **with `char_start`/`char_end` spans** | `pipeline.py:1275-1316` | the `<mark>` citation snippet + wiki mention sourcing |
| 3 | Upsert `temporal_tokens` (incl. `rrule`) | `pipeline.py:1511-1552` | appointments recurrence |
| 4 | Upsert facts (supersession, shape check, inverse materialization) | `pipeline.py:1909-2262` | everything |
| 5 | Mint **per-domain `derived` chunks** for ratcheted facts | `pipeline.py:1645-1688`, called at `:1818,:2097,:2128,:2142` | the wiki's entire firewalled-domain citability |
| 6 | Register declared aliases | `pipeline.py:1384` | entity resolution |
| 7 | **Retraction sweep** of facts the re-extraction no longer asserts + chain repair | `pipeline.py:900-946` (`purge.repair_chains:138`, `purge.delete_review_items:190`) | supersession history integrity |
| 8 | `_sweep_stale_ambiguous` + `_sync_truncation_review` | `pipeline.py:995,1019` | review inbox hygiene |
| 9 | `_reproject_entities` (canonical_name from `name.*` facts) | `pipeline.py:1318-1330` | entity display name everywhere |
| 10 | `_promote_corroborated` (provisional→confirmed, or a `confirm_entity` card) | `pipeline.py:1331-1353` | entity lifecycle |
| 11 | `NoteAnalysis` upsert (`extractor`, `prompt_version`, `analyzed_at`, `tags`) | `pipeline.py:953-975` | the PWA "analyzed" chip, tag sweep, reprocessing watermark |
| 12 | **Four typed projections**: `project_appointments`, `project_emr`, `project_place_geofences`, `reconcile_device_bindings` | `pipeline.py:986-992` | calendar/ICS, lab results, geofences, device binding |
| 13 | Return `held_ids` so the arbiter can link each held fact to its review card | `pipeline.py:993` | the review inbox |

**`_apply` and `analysis/purge.py:110-120` are the ONLY two callers of the four
projections** (`grep` over `backend/src/jbrain`, plus one drift backstop at
`locations/geofence.py:276-279`) **[V]**. That is the single most under-appreciated fact in
this redesign: *delete `_apply` and the calendar, the lab read-model, the PostGIS geofence
mirror and the person⇄device binding all stop updating*, silently, with no error.

---

## 1. Consumer register

| Consumer | Verdict | One-line fix |
|---|---|---|
| Wiki builder `_source` (fact→claim) | **BREAKS** | facts must keep a non-NULL `chunk_id` into a real note-chunk, or the wiki goes empty |
| Wiki dirty bit (`wiki_built`) | **UNAFFECTED** | DB triggers on `facts`/`entity_mentions`/`entities` — producer-agnostic |
| Wiki grounding gate (`wiki.ground`) | **NEEDS ADAPTER** | "entailed by its cited chunk" is undefined for a fact the agent inferred conversationally |
| `wiki_citations` firewall trigger | **BREAKS (as a constraint)** | `chunk_id`/`note_id` NOT NULL + domain equality; an agent fact with no chunk cannot be cited at all |
| Derived-chunk minting | **BREAKS** | `_citation_chunk` dies with `_apply`; builder's fallback copy is documented as never-fires |
| `wiki_lint` (stale/contradiction/coverage) | **NEEDS ADAPTER** | keys on `status='superseded'` + `JOIN chunks ON c.id=f.chunk_id` |
| Correction-note loop (`file_correction`) | **BREAKS** | `plan_intent(correction=True)` is the elevation mechanism; it is in the deleted arbiter |
| Note search (`search/repo.py`) | **UNAFFECTED** | searches `chunks` + `wiki_*` only; no fact leg |
| Wiki search leg | **INHERITS** the builder's fate | — |
| Entity resolution embeddings | **NEEDS ADAPTER** | lazily embedded inside `_embedding_candidates`; no producer if resolution moves into the agent |
| Agent `read_entity` / `neighborhood` / `relate` / `full_graph` | **NEEDS ADAPTER** | all filter `status='active' AND assertion='asserted'`; semantics of those columns change |
| Agent currency overlay (`note_currency`, `analyte_currency`) | **NEEDS ADAPTER** | depends on a *note* being the fact's source-of-record |
| Reflexion citation verifier | **UNAFFECTED** | checks fact-id-in-scope, not provenance |
| Appointments + ICS feed | **BREAKS** | whole write path is note→extraction→`scheduledTime` fact→`project_appointments` |
| Lab results / EMR read-model | **BREAKS TWICE** | `project_emr` loses its caller **and** `ingest/emr/importer.py` targets the deleted arbiter |
| Place geofences / presence | **BREAKS (mostly self-healing)** | `save_place` writes prose for the extractor; the nightly `geofence_sweep` is the only backstop |
| Device⇄subject binding | **NEEDS ADAPTER** | `reconcile_device_bindings` loses its inline caller; `sweep_device_bindings` backstops |
| Lists | **UNAFFECTED** | agent-written today, no fact dependency |
| Family (`view_scope`) | **UNAFFECTED** | owner-curated subject rows, not fact-derived |
| Subjects / cross-subject firewall | **NEEDS ADAPTER** | `cross_subject_link` flagging lives in `arbiter.plan_intent` |
| `entity_hygiene`, `tag_consolidate`, `reembed_stale` | **UNAFFECTED / NEEDS ADAPTER** | pure SQL; `tag_consolidate` needs a tags producer |
| `consolidate_predicates` / `sync_predicates` | **NEEDS ADAPTER** | the two-tier registry's *writer* is the extractor |
| `reconcile_pending_integration` | **NEEDS ADAPTER** | keys on `note_analysis` absence |
| Purge (`analysis/purge.py`) | **NEEDS ADAPTER** | re-runs the projections; also the Wave-D wiki rebuild hook |
| `llm_usage` accounting | **NEEDS ADAPTER** | task keys `note.extract`/`integrate.note` disappear; a local model costs $0 but still needs a row |
| Run-log / Automations catalog | **NEEDS ADAPTER** | `integrate_note` ActionSpec + `integration` run kind |
| Review inbox (11 card kinds) | **PARTIALLY SUBSUMED** | most cards exist because the pipeline had no way to ask |
| Frontend AnalysisTab / EntityHistorySheet / ReviewScreen | **NEEDS ADAPTER** | render `confidence`, `extractor`, `source_snippet` spans, status chips |
| Grok eval harness (`tests/eval/`) | **BREAKS** | grades `IntegrationIntent` + `ArbiterPlan` objects that cease to exist |

---

## 2. Phase 6 — the wiki (the biggest consumer)

### 2.1 State of play (correcting the brief)

The brief calls Phase 6 "the declared next frontier". That is what `CLAUDE.md` says, but
**Waves A, B1, B2a, B2b and C are shipped**; only Wave D (grounding-gate tuning +
purge→rebuild wiring) is open — `docs/plans/PHASE6_WIKI_PLAN.md:3-8` and
`docs/ROADMAP.md:168-186` **[V]**. So the wiki is not a plan at risk, it is **shipped code
in production that reads the fact table every night at 03:30** (schedules re-enabled by
migration 0121, `PHASE6_WIKI_PLAN.md:5-7`). This raises the stakes: the redesign does not
"delay a plan", it **can silently empty a live surface**.

### 2.2 The five assumptions the wiki makes about a fact

**(a) Span-anchored citation is NOT what the wiki needs — a chunk FK is.** The wiki never
touches `entity_mentions.char_start`. What it needs is much harder:

```
backend/src/jbrain/wiki/builder.py:526-527
    " FROM app.facts f"
    " JOIN app.chunks c ON c.id = f.chunk_id"
```

An **INNER JOIN** on `f.chunk_id` **[V]**. A fact with `chunk_id IS NULL` is invisible to
`_source`, contributes nothing to notability (`builder.py:279-280`), and can never be
cited. And `app.wiki_citations.chunk_id` / `.note_id` are both **NOT NULL**
(`backend/migrations/versions/0046_wiki_graph_coupling.py:159-160`) with a Postgres
`SECURITY DEFINER` firewall trigger asserting `citation.domain = section.domain =
chunk.domain`, `citation.note_id = chunk.note_id`, and `= fact.domain` when fact-backed
(`0046:187-224`) **[V]**.

> **Verdict: BREAKS.** An agent that concludes "Priya is Maya's pediatrician" across three
> turns and writes it with `chunk_id=NULL` produces a fact that is real, queryable by the
> agent, invisible to the wiki, and **structurally uncitable in Postgres**. Not degraded —
> rejected by a trigger.
>
> **Fix (pick one, this is the single biggest design decision in the redesign):**
> 1. **Keep the chunk FK mandatory.** Every agent `write_fact` tool call must name the
>    chunk it read the claim from; the tool refuses a fact with no chunk. Cheapest for
>    downstream, hardest for the agent (a synthesized/multi-turn conclusion has no chunk).
> 2. **Mint a conversation chunk.** Turn 0 is the note; the agent's *conversation* becomes
>    a note too (or an appended chunk on the source note), so a synthesized fact cites the
>    turn that produced it. Preserves the FK, the firewall, and the References render —
>    and makes "the owner confirmed this in chat" a first-class citable source. **[A]:
>    this is the smallest change that keeps the wiki whole.**
> 3. **Make `wiki_citations.chunk_id` nullable + relax the trigger.** Reopens audit
>    blocker (c) the Phase-6 audit closed (`PHASE6_WIKI_PLAN.md:35-45`) and re-runs the
>    firewall proof. Costly.

**(b) Derived chunks (the ratcheting mechanism) die with `_apply`.** A health fact in a
`general` note cannot cite the general-domain chunk, so `_citation_chunk`
(`pipeline.py:1645-1688`) mints a same-domain `derived` copy at commit time, and
`builder._derived_chunk` (`builder.py:598-640`) is explicitly documented as a
never-normally-fires fallback: *"the pipeline itself REPOINTS a ratcheted fact's `chunk_id`
to its same-domain derived chunk at materialization … so in normal flow `_source` sees
chunk.domain == fact.domain and never calls this"* (`builder.py:610-615`) **[V]**.
`PHASE6_WIKI_PLAN.md:275-280` names chunk-only derived-chunk minting the **blocking DoD**
of Wave C — *"without it a ratcheted health/finance section has no citable chunk and
renders empty, so the entire firewalled-domain wiki depends on it"*.

> **Verdict: BREAKS.** **Fix:** the agent's fact-write tool must call the same
> get-or-create-derived-chunk routine whenever `fact.domain != chunk.domain`. Extract
> `_citation_chunk` out of `AnalysisPipeline` into a free function both writers share
> **before** deleting the pipeline.

**(c) `asserted` status discipline.** The builder publishes `f.status IN ('active',
'superseded')` and deliberately excludes `pending_review`/flagged rows including
`cross_subject_link` (`builder.py:529-532`) **[V]**. `_headword` additionally requires
`status='active' AND valid_to IS NULL AND assertion='asserted'` (`builder.py:216-218`).
Every graph read in `analysis/repo.py` (`relate:418`, `ego_graph:458,464`,
`full_graph:551`, `neighborhood:640,654`) requires `assertion='asserted'` **[V]**.

Under agent ingest `pending_review` acquires a *new* meaning: today it means "the
deterministic layer held this"; tomorrow it should mean "the agent asked and the owner has
not answered yet". Same column, different semantics, same consumers.

> **Verdict: NEEDS ADAPTER (cheap but must be deliberate).** **Fix:** keep the exact
> `status` and `assertion` vocabularies; define the agent's "asked, awaiting owner" state
> as `pending_review` so the wiki's existing exclusion holds unchanged. **Do not invent a
> new status value** — five separate SQL predicates would need updating and the builder's
> `_source` would publish an unrecognised status by default.
> Note the pre-existing gap **[V]**: `_source` does **not** filter `assertion`, so a
> `negated`/`hypothetical` fact is already citable. An agent writing many hedged facts
> will make that gap loud.

**(d) `confidence` is nearly vestigial already — but not dead.** Lever A of Ingest V2 has
already landed: `weight.py:88-95` records that `commit_status`/`assess` "were retired …
a fact commits by default and the weight no longer gates review" **[V]**. What survives:
the stored `confidence` feeds (i) the supersession low-confidence guard, (ii) the
correction path, (iii) the review-card process trace (`analysis/trace.py:1-16`), and
(iv) the PWA — `FactCitation` renders `fmtConfidence(fact.confidence)` and the extractor
name in the provenance line (`frontend/src/analysis/bits.tsx:112-125`,
`components/AnalysisTab.tsx:109`) **[V]**.

> **Verdict: NEEDS ADAPTER (small).** An agent's self-reported confidence is *exactly* the
> untrusted self-report `weight.py:1-9` was written to distrust. **Fix:** either store
> `NULL` and have the PWA hide the chip, or define a two-value convention
> (`1.0` = owner-confirmed in conversation, `NULL` = agent-asserted unconfirmed) and make
> the frontend render "confirmed by you" rather than a percentage.

**(e) `extractor` / `prompt_version` stamping.** Both are `NOT NULL` on `app.facts`
(`models/analysis.py:183-184`) and on `note_analysis` (`:202-203`) **[V]**. Grep shows they
are **write-mostly**: the only read paths are the note-analysis view (`notes/repo.py:296`,
`api/notes.py:357`) and the PWA provenance line **[V]**.

> **Verdict: UNAFFECTED (rename only).** **Fix:** stamp `extractor = "<local model id>"`
> and `prompt_version = "<agent tool contract version>"`. The `.prompt` digest CI guard
> (referenced at `wiki/lint.py:78-80`) should get a `.tool`-contract twin — the ROADMAP
> already lists that as carried-forward work (`ROADMAP.md:157-160`).

**(f) Supersession chains as revision history.** `superseded_by`
(`models/analysis.py:171-173`) is the property's revision history, and it has **three**
independent consumers **[V]**:
- `wiki_lint._verify_stale` (`wiki/lint.py:630-641`) — joins `f.status='superseded'` to a
  citation in a *current* revision to ask "does this article frame history as current?"
- The PWA per-predicate timeline rail (`frontend/src/screens/EntityHistorySheet.tsx:1-30`)
  — "Machine-retracted facts … are excluded here; they are audit-only, not value history."
- `analysis/repo.py:883-902` — the `history` grouping behind the entity page.

An agent that *edits* a fact in place instead of superseding it destroys all three
silently. An agent that supersedes over-eagerly floods the lint sweep with cards.

> **Verdict: NEEDS ADAPTER (contract, not code).** **Fix:** the agent's write tool must
> be **append-only by construction** — no `UPDATE facts SET value_json`. Offer exactly
> `assert_fact` / `supersede_fact` / `retract_fact`, and let the *tool* set
> `superseded_by`, mirroring `_upsert_fact`'s discipline. Preserve
> `purge.repair_chains` (`analysis/purge.py:138`) as the shared chain-repair primitive.

### 2.3 The nightly sweeps

- **`wiki_refresh` delta = the `wiki_built` dirty bit.** This is the **good news**. The
  bit is flipped by **Postgres triggers**, not by the pipeline:
  `wiki_dirty_entity_from_fact` on `app.facts`, `wiki_dirty_entity_from_mention` on
  `app.entity_mentions`, `wiki_entity_self_dirty` on `app.entities`
  (`0046_wiki_graph_coupling.py:58-149`) **[V]**. Any writer that touches those tables —
  agent tool included — dirties correctly, with the builder's mark-clean protected
  (`0046:36-37`, `builder._mark_built:827-830`).
  > **Verdict: UNAFFECTED.** The delta mechanism is producer-agnostic by design. Do not
  > let the agent write facts through a path that bypasses these tables.
- **`wiki_lint`'s `_buildable_entities` / `_coverage_gaps`** replicate `_source`'s
  `JOIN app.chunks ON c.id = f.chunk_id` *exactly, on purpose* (`wiki/lint.py:261-283`,
  `:284-327`) **[V]**. If agent-written facts commonly lack chunks, `_coverage_gaps`
  reports a permanent, un-actionable backlog of "notable entity with no article".
  > **Verdict: NEEDS ADAPTER.** Same fix as 2.2(a); if chunks stay mandatory this is free.
- **The grounding gate** verifies "each clause is entailed by its cited chunk text AND not
  contradicted by the subject's current fact set; **the entity graph wins on conflict**"
  (`wiki/rewriter.py:9-16`, `:157-190`) **[V]**. Under agent ingest the graph is *itself*
  LLM-authored, so the gate degrades from "prose is checked against extracted-from-note
  ground truth" to "one LLM's prose checked against another LLM's assertions". The
  conflict rule ("graph wins") stops being a safety property.
  > **Verdict: NEEDS ADAPTER (the real one).** **Fix:** split the fact table's trust:
  > mark facts **owner-confirmed** vs **agent-asserted**, and let the gate only treat
  > owner-confirmed facts as authoritative for the "graph wins" rule. Wave D's stated open
  > work is precisely *"grounding-gate tuning against real note corpora"*
  > (`PHASE6_WIKI_PLAN.md:288-292`) — that tuning must now be re-scoped.

### 2.4 The correction loop — the sharpest breakage

CLAUDE.md #7 and `PHASE6_WIKI_PLAN.md:200-213` make the correction note the **only** human
channel into the wiki. Its mechanism is a single arbiter branch:

```
backend/src/jbrain/analysis/pipeline.py:322   correction = note.provenance == "owner_correction"
backend/src/jbrain/analysis/pipeline.py:423   plan = plan_intent(intent, signals, correction=correction)
backend/src/jbrain/analysis/arbiter.py:137-144  fact_correction = correction and signals_i.surface_attested
                                                weight = 1.0 if fact_correction else effective_weight(...)
```

**[V]** — with the deliberate guard that an *inferred* fact inside a correction note is
**not** elevated (`arbiter.py:137-143`). Three surfaces mint `provenance="owner_correction"`
notes: `agent/wikiwritetools.py:48`, `api/wiki.py:260`, `api/analysis.py:220` **[V]**.

> **Verdict: BREAKS — and this is where the redesign is genuinely *better*.** The whole
> apparatus (mint a prose note → re-extract it → detect surface attestation → elevate
> weight → force-supersede → pin) exists **only because the owner had no direct write into
> the graph**. Ingest V2's Lever C already saw this and proposed a "direct correction"
> (`ENTITY_GRAPH_INGEST_V2_PLAN.md`, §11 ratified). A conversational agent with write tools
> collapses the round-trip: the owner says "no, that's wrong", the agent supersedes + pins.
> **Fix:** keep `provenance='owner_correction'` on the *conversation turn* record for
> audit; replace the extraction-based elevation with an explicit `correct_fact` tool that
> writes `pinned=true` + force-supersede. Keep the *guard*: only what the owner
> **literally stated** may be elevated — the agent's inference from the correction must not
> inherit the elevation (that is `arbiter.py:137-143`'s hard-won lesson, and it is
> *easier* to get wrong in a conversation than in a note).

### 2.5 Does this help, delay, or subsume Phase 6?

**All three, in that order, and the honest answer is "it does not subsume the wiki".**

- **Delays it.** Wave D is the only open wave. Both of its items —
  grounding-gate tuning and purge→rebuild wiring (`PHASE6_WIKI_PLAN.md:288-292`) — depend
  on a *stable fact shape*. Tuning a verifier against a corpus produced by a producer that
  is about to be deleted is wasted work. **[A]:** Wave D should be paused, not re-planned,
  until the fact-provenance contract (2.2a) is settled.
- **Helps it.** Three ways. (i) The correction loop simplifies dramatically (2.4).
  (ii) `PHASE6_WIKI_PLAN.md:0` "the graph-coupling line" was written *anticipating* exactly
  this rebuild — the plan is already partitioned into STABLE and GRAPH-COUPLED halves, and
  the cross-stream contract (`docs/archive/PHASE6_WIKI_GRAPH_CONTRACT.md`) is the pre-agreed
  hand-off protocol. Use it again rather than inventing one. (iii) The Talk board's
  "Editor" voice (`PHASE6_WIKI_PLAN.md:395-403`, Wave T2 shipped) is already an agent turn
  with wiki tools — the redesign makes ingest and editorial the *same* agent.
- **But it does not subsume it.** The claim "an agent that maintains a graph is halfway to
  one that maintains a wiki" conflates two things the codebase keeps deliberately apart:
  - The graph is **per-property current truth**, addressed by
    `(subject, entity, predicate, qualifier)` (`models/analysis.py:132-137`). The wiki is
    **narrative synthesis across an entity's whole fact set**, type-guided
    (`docs/reference/WIKI_TYPE_GUIDES.md`), cross-domain in shell and single-domain in
    section (`PHASE6_WIKI_PLAN.md:57-90`). Writing an edge and writing a Career section are
    not the same act.
  - The wiki is the thing that **cannot be trusted to the writer**: its whole value is the
    grounding gate + the Postgres citation firewall + `wiki_lint`'s corpus-wide
    contradiction sweep — three *adversarial* checks on LLM prose. An agent that writes
    both the facts and the articles has no independent check left. **[A]: the redesign
    makes the wiki's separation from the graph writer MORE important, not less.**
  - Concretely: the wiki is also the **read-optimized answer layer** for search
    (`search/service.py:162-167` puts wiki hits above notes) and for the agent itself
    (`agent/readtools.py:345-347`). A conversational graph-writer produces no articles,
    no `lead_summary`, no `wiki_index` embeddings, no search leg.

---

## 3. Search and retrieval

**"Fact statements are embedded and cited" is not true today — correcting the brief.**
`app.facts` has **no embedding column** (`models/analysis.py:131-188`) **[V]**. What is
embedded: note `chunks`, `wiki_index.summary_embedding`, `entities.summary_embedding`,
`canonical_predicates`, `external_source_chunks` **[V]**.

- **Note search leg** (`search/repo.py:15-51`): reads `app.chunks` JOIN `app.notes` only,
  and explicitly excludes `c.source_kind != 'derived'` (`repo.py:27`).
  > **UNAFFECTED.** Chunks come from ingest/embed, not from `_apply`. Notes remain turn 0,
  > so chunking and embedding are untouched.
- **Wiki search leg** (`search/repo.py:53-92`, fused at `search/service.py:151-175`):
  entirely dependent on `wiki_index` + `wiki_revisions`, i.e. on the builder.
  > **INHERITS §2's verdict.** If the builder starves, the answer layer silently
  > degrades to notes-only. No error, just worse answers.
- **Entity-page and per-domain derived chunks out of `_apply`.** Two distinct things:
  derived chunks (`pipeline.py:1645-1688`) — **BREAKS**, see 2.2(b); they carry **no
  embedding by design** (`pipeline.py:1678`) so they never affected ranking, only
  citability. There is no "entity page chunk" — entity pages are rendered from
  `analysis/repo.py:789-958`, not chunked **[V]**.
- **Entity resolution embeddings** are minted **lazily inside the resolver**:
  `_embedding_candidates` embeds `name; aliases; summary` for rows where
  `summary_embedding IS NULL` and writes it back (`analysis/entities.py:397-440`) **[V]**.
  `reembed_stale` only re-embeds rows that *already have* a summary
  (`analysis/reembed.py:58-72`).
  > **NEEDS ADAPTER.** If entity resolution moves into the agent's tool
  > (`find_or_create_entity`), that tool must keep calling the same layered resolver —
  > otherwise layer-2 similarity dies and duplicate entities proliferate, which then
  > floods `merge_proposal` cards and breaks wiki notability counts.

---

## 4. The Full Brain agent's own tools

The agent already reads the graph through six surfaces, all in `analysis/repo.py`
and surfaced in `agent/readtools.py` **[V]**:

| Tool | Repo method | Filter it depends on |
|---|---|---|
| `read_entity` | `entity_view:789` | `status='active'`, `valid_to IS NULL` (`repo.py:911`) |
| `find_entity` | `list_entities:285` | `fact_count` over `status IN ('active','pending_review')` (`:322`) |
| `relate` | `relate:368` | `f.status='active' AND f.assertion='asserted'` (`:418`) |
| `neighborhood` (n-hop) | `neighborhood:589` + `analysis/neighborhood.py:209` | same, both directions, + co-mentions via `entity_mentions` (`:675-680`) |
| graph screens | `ego_graph:439`, `full_graph:525` | same |
| currency overlay | `note_currency:1032`, `analyte_currency:1105` | `f.status IN ('superseded','retracted','pending_review')` |

**Does the shape of an agent-written graph change what these return?** Yes, in four ways
**[A, reasoned from the filters above]**:

1. **Volume and shape.** The n-hop walk is capped (`readtools.py:979` `MAX_DEPTH`,
   `total_cap`) and ranked by `_rank_candidates` (`neighborhood.py:161`). A conversational
   writer emits fewer, chunkier, more semantic edges than "CAPTURE EVERYTHING THE NOTE
   STATES" (`ENTITY_GRAPH_REFOCUS_PLAN.md:28-33`) — which is the *stated goal* of the
   refocus. Traversal gets better, not worse.
2. **`assertion` becomes load-bearing in a new way.** A conversational agent naturally
   records hedges ("she might be moving to Austin"). Five queries drop non-`asserted`
   facts; the wiki's `_source` does not. **Fix:** decide once whether hedges are facts at
   all; if yes, add the `assertion='asserted'` filter to `builder._source:531`.
3. **The currency overlay's premise weakens.** `format_currency`
   (`readtools.py:387-400`) tells the model *"the note text above is the original record,
   but these facts are no longer current"* — a clean story when facts are derived from
   notes. When a fact was asserted in conversation and never appears in any note, the
   overlay's "read_entity for the current value" pointer is still right, but "this note's
   value was replaced by a newer note" (`readtools.py:370-373`) becomes a lie.
   > **NEEDS ADAPTER:** widen the phrasing to name the *source kind* (note vs conversation).
4. **The tools should now be written as a matched read/write pair.** Today
   `readtools.py` is read-only and every write stages a Proposal
   (`agent/mergetools.py:1-11`, `agent/appointmenttools.py:148-153`,
   `agent/locationtools.py:815-822`). The redesign inverts that premise. The
   `save_place` docstring is the clearest statement of the *old* world **[V]**:

   ```
   backend/src/jbrain/agent/locationtools.py:520-527
     "a self-contained, prose statement that DRIVES the normal extractor to mint a Place
      entity with a `geofence` fact (notes are the sole source of truth, #7; there is no
      direct-fact path)"
   ```

   > **NEEDS ADAPTER, favourably.** Once a direct-fact path exists,
   > `save_place`/`manage_appointment`/`propose_merge` can stop composing prose for a
   > downstream extractor to re-parse. That prose round-trip is a documented source of
   > fragility (`appointmenttools.py:136-141` — the note wording must be phrased so
   > re-extraction resolves to the *same* entity). Deleting it is a real win.

**Reflexion / grounding verifier: UNAFFECTED [V].** `verify_citations`
(`agent/reflexion.py:292-303`) only checks that a cited fact id is in the session's
RLS scope; `_is_grounded` (`:306-327`) grounds on citation markers or token overlap
against surfaced source text. `EntityRef` already carries fact statements so a
graph-sourced answer grounds (`agent/contracts.py:117-120`, `readtools.py:695-700`).
Nothing there reads provenance.

---

## 5. Structured records — the silent-breakage section

### 5.1 Appointments + the ICS feed — **BREAKS**

Verified chain **[V]**:
`agent/appointmenttools.py:148-224` stages a Proposal → approval re-enters as an
agent note → extraction → a `scheduledTime` **state** fact on an `appointment`/`event`
entity → `project_appointments` (`pipeline.py:986`) → `app.appointments` →
`appointments/repo.py` → read tools + the ICS feed.

`_project_one` (`analysis/appointment_projection.py:177-240`) reads, in order:
the single `status='active'` `scheduledTime` fact ordered by `valid_from DESC`; its
`value_json` as `{"start": ISO, "end": ISO}`; `fact.temporal_precision` to decide
`all_day`; `fact.temporal_token_id → TemporalToken.rrule` for recurrence, falling back to
a separate `recurrence` predicate fact; then a facet load for organizer/attendee/mode/URL;
and it **deletes the row** when no live `scheduledTime` remains. The venue is split into
`app.appointment_locations` under its own domain because a `location`/`address` fact floors
into the location domain (`appointment_projection.py:16-22`).

Reschedule semantics are entirely supersession: *"a reschedule lands on the same
appointment entity … functional, so supersession (validity-newest-wins) already left
exactly one"* (`pipeline.py:982-983`, `appointment_projection.py:180-182`) **[V]**.

> **What must replace it.** Three things, none optional:
> 1. **Re-home the projections.** `project_appointments`/`project_emr`/
>    `project_place_geofences`/`reconcile_device_bindings` must be called by the agent's
>    fact-write tool, in the same transaction, over the set of touched entity ids — exactly
>    as `pipeline.py:978-992` does. Simplest correct fix: a `commit_facts(session,
>    touched_entity_ids)` helper that both the tool and `purge.py:110-120` call.
> 2. **Preserve the typed `value_json` shapes.** `{"start","end"}` for `scheduledTime`,
>    `{center:{latitude,longitude}, radiusMeters, polygon}` for `geofence`
>    (`geofence_projection.py:29-34`). An agent writing `value_json = {"when": "..."}`
>    produces a fact that looks fine on the entity page and **removes the appointment from
>    the calendar** — `_project_one` returns early at `starts_at is None`
>    (`appointment_projection.py:212-214`). This is the exact "sentence-as-value" class of
>    regression the Grok eval harness was built to catch (`tests/eval/README.md:1-8`).
> 3. **Keep functional supersession for `scheduledTime`.** Two active `scheduledTime`
>    facts is not an error the projector detects — it silently takes the newest by
>    `valid_from` and the other becomes invisible.

### 5.2 Lab results / EMR — **BREAKS TWICE**

- `project_emr` (`pipeline.py:987`) loses its inline caller like the others. Worse, it
  derives `report_status` from the *supersession chain shape* — "a lone active reading is
  `final`; an active head with a superseded predecessor at its qualifier is `corrected` …
  a `pending_review` reading is `preliminary`" (`analysis/emr_projection.py:1-15`) **[V]**.
  Every one of those three depends on the exact status/chain discipline of §2.2(f).
- **The EMR importer targets the deleted arbiter directly.**
  `ingest/emr/importer.py:1-17` **[V]**: *"the LLM Extractor and Integrator are bypassed:
  this builder emits the SAME `IntegrationIntent` … and the shipped deterministic core
  (`plan_intent` → `apply_intent`) validates, weighs, firewalls, supersedes, and commits
  it."* `ingest/emr/integrate.py:120-124` calls `plan_intent` then
  `pipeline.apply_intent`.
  > **This is the easiest thing in the repo to miss.** EMR import is a *deterministic,
  > non-LLM* producer that borrows the arbiter as a commit engine. Deleting
  > `plan_intent`/`apply_intent` breaks a shipped feature that has nothing to do with note
  > extraction. It also runs a Layer-2 location firewall guard on the way in
  > (`ingest/emr/firewall.py`, `importer.py:9-11`).
  > **Fix:** `apply_intent` (or its successor `commit_facts`) must survive the deletion as
  > a **shared commit engine** with two callers — the agent's tools and the EMR importer.
  > The thing being deleted should be the *LLM extraction + Integrator turn-loop*, **not
  > the deterministic commit core.** [A, but strongly supported by `importer.py:1-17`.]

### 5.3 Places / geofences / presence — **BREAKS, partly self-healing**

`save_place` composes prose for the extractor (`locationtools.py:520-539`) →
`geofence` fact on a Place entity → `project_place_geofences` (`pipeline.py:988`) →
`app.place_geofence` (PostGIS) → geofence transitions → workflow events → presence/trail.
**Fix:** the agent writes the `geofence` fact directly with the schema-exact `value_json`
and calls the projector. **Backstop that survives:** `geofence_sweep`
(`workflow/scheduler.py:140`) rebuilds the mirror from the graph nightly
(`locations/geofence.py:270-279`), so a missed inline projection self-heals within a day —
but only if the *fact* was written correctly.

### 5.4 Device⇄subject binding — **NEEDS ADAPTER**

`reconcile_device_bindings` runs inline on the full-owner fact-apply path
(`pipeline.py:989-992`); `sweep_device_bindings` backstops it in `geofence_sweep`
(`locations/geofence.py:275-282`) **[V]**. Same fix as 5.3.

### 5.5 Lists — **UNAFFECTED [V]**

`lists/service.py:1-3`: *"owner-managed structured records the agent maintains directly."*
`agent/listtools.py:51-113` writes through `ListsRepo` with no fact involvement.
`list_items.source_note_id` is a nullable trace column only (`models/lists.py:38-51`).
Lists are, in fact, **the precedent** for what the redesign proposes everywhere else.

### 5.6 Family / subjects — **UNAFFECTED, with one adapter [V]**

`family/repo.py:1-8` manages `view_scope` rows for the one family group under
`is_full_owner` RLS; nothing reads facts. Subjects are created by `devices/repo.py:67`
and by `get_or_create_me` (`analysis/entities.py:606-639`).
**The adapter:** the *cross-subject firewall* lives in the arbiter —
`arbiter.py:126-132` forces any fact whose mention could not be pinned to a single
same-subject identity into review as `cross_subject_link`, and the wiki refuses to publish
those (`builder.py:530`). That guard must be reimplemented in the agent's write tool, not
left to the model's judgment. **This is a safety property, not a quality one.**

---

## 6. Reconcilers, hygiene sweeps, self-heal

| Sweep | Code | Verdict |
|---|---|---|
| `entity_hygiene` | `analysis/hygiene.py:1-14` — hard-deletes provisional orphan entities via `purge.sweep_orphaned_entities:272`, pure SQL | **UNAFFECTED** — it explicitly exists to catch entities stranded by *retraction* rather than note deletion, which agent ingest will produce more of, not less |
| `tag_consolidate` | `analysis/tagconsolidate.py:1-12` — normalizes `notes.tags` | **NEEDS ADAPTER** — the tags producer is `NoteAnalysis.tags`, written at `pipeline.py:956`. The agent must keep writing them or the sweep sweeps nothing |
| `reembed_stale` | `analysis/reembed.py:58-72` — entity summaries + external chunks | **UNAFFECTED** |
| `consolidate_predicates` | `analysis/consolidation.py:1-21` — rewrites drift spellings onto registry canon | **NEEDS ADAPTER** — it is the retroactive twin of parse-time normalization the extractor does. If the agent picks predicates freely, this sweep becomes *more* load-bearing, and its conservative "leave the collision alone" branch (`:9-17`) will fire far more often |
| `sync_predicates` | `registry.py:210` | **NEEDS ADAPTER** — same registry |
| `reconcile_pending_notes` / `_unembedded_notes` | `scheduler.py:89,119` | **UNAFFECTED** — ingest/embed are upstream of the cut |
| `reconcile_pending_integration` | `scheduler.py:101` | **NEEDS ADAPTER** — "re-enqueue integration for indexed-but-unintegrated notes" keys on the absence of a `note_analysis` row. Its successor must key on "note with no agent conversation" |
| `geofence_sweep` | `scheduler.py:140` | **UNAFFECTED (and now more important)** — see 5.3 |
| `purge_deleted_artifacts` / `analysis/purge.py` | `purge.py:65-137` | **NEEDS ADAPTER** — re-runs the three projections at `:118-120` and repairs chains at `:138`. Also carries Wave D's missing wiki-rebuild hook (`PHASE6_WIKI_PLAN.md:288-292`) |
| `wiki_refresh` / `_rebuild` / `_reindex` / `_prune` | `wiki/actions.py` | see §2 |
| `wiki_lint` | `wiki/lint.py` | see §2.3 |

### 6.1 "Who runs them without an arbiter, and should they open conversations?"

**Verified:** none of the sweeps calls the arbiter. All eleven run under `SYSTEM_CTX` as
plain SQL or (for `wiki_lint` Wave B) a metered LLM call. The arbiter's involvement is
**only** at fact-commit time.

**The real question is the review inbox.** Eleven card kinds are filed today
(`ambiguous_mention`, `attribute_collision`, `confirm_entity`, `domain_promotion`,
`extraction_truncated`, `fact_conflict`, `inverse_proposal`, `low_confidence`,
`low_confidence_inference`, `merge_proposal`, plus `wiki_contradiction`/`wiki_stale_claim`
from lint) **[V, grep over `analysis/*.py` + `wiki/lint.py`]**. Ingest V2's whole thesis is
that most of them are noise from a disposition default tuned for a multi-author corpus
(`ENTITY_GRAPH_INGEST_V2_PLAN.md`, Thesis).

> **[A] The redesign should convert *ingest-time* cards into conversation turns and leave
> *sweep-time* cards as cards.** The distinction is whether the owner is present:
> - **Ingest-time, owner present → ask in the conversation.** `ambiguous_mention`
>   ("which Sarah?"), `confirm_entity`, `low_confidence_inference`, `attribute_collision`
>   at write time. These are exactly the questions the redesign says the agent should ask.
> - **Sweep-time, no owner → still a card.** `wiki_contradiction`, `wiki_stale_claim`,
>   `merge_proposal` from the nightly hygiene pass, `fact_conflict` discovered by a
>   reconciler. A 03:30 sweep has nobody to talk to; it must file, and the inbox must
>   survive. `wiki/lint.py:757` `_file_card` and the whole `review_items` resolution
>   machinery (`analysis/repo.py:1431-1560`) should be kept intact.
> - **Do not delete `review_items`.** It is the only asynchronous channel to the owner, and
>   the Ops/debug surfaces and the PWA badge count depend on it
>   (`ARCHITECTURE.md:184-190`).

---

## 7. Usage, observability, Ops

- **`llm_usage`.** `usage.py:1-9` prices from a config table at query time; the recorder is
  "the SINGLE chokepoint every LLM call passes through" (`usage.py:44-49`) **[V]**.
  Three task keys disappear: `note.extract`, `integrate.note`, `entity.disambiguate`
  (`llm/router.py:54-71`), plus `correction_note.extract` if 2.4's fix lands.
  > **NEEDS ADAPTER.** A local model has no dollar cost, so the Ops usage card will show a
  > sharp drop that is real. But the *token* accounting still matters for on-box capacity
  > planning, and `TASK_REASONING_BUCKET` (`router.py:130-152`) must gain an entry for the
  > new task (`ingest.turn`?) or the settings screen has no row to render. Note the
  > adapter-only rule (CLAUDE.md #1) applies to the local model too — it must go through
  > `router.complete`/the local gateway, not a bare HTTP call.
- **Run-log / Automations catalog.** `integrate_note` is one of the six seeded `ActionSpec`
  rows (`workflow/registry.py:177-186`) mirrored in migration 0035, and the seed-lockstep
  test "pins the shipped six" (`wiki/actions.py:3-5`) **[V]**. `analysis/persist.py:1-25`
  writes one `app.runs` row per `integrate_note` with `kind='integration'` and a
  deterministic step trace `extract → integrate → arbiter` (`persist.py:70`), plus
  `resolution_pin` rows keyed `(note_id, chunk_id, occurrence_index, decision_kind)`.
  > **NEEDS ADAPTER + a migration.** Replacing `integrate_note` means (i) a migration to
  > swap the `app.actions` seed row, (ii) updating the lockstep test, (iii) deciding what
  > the new run's step trace is. **Keep the `runs` row**: it is how the owner diagnoses
  > ingest from the PWA with no terminal (CLAUDE.md #10). An agent conversation that writes
  > the graph *must* still produce a run-log entry — and `models/agent.py:72-84` already
  > has a CHECK requiring `session_id`+`prompt_version` for `kind='agent'` runs, so an
  > ingest-conversation run needs its own kind or those fields.
- **`box_events`** is owner-only RLS telemetry read by the Ops load probe
  (`api/ops.py:478`) **[V]**; nothing in the analysis pipeline emits to it.
  > **UNAFFECTED.**
- **`flow_trace`** (`analysis/flow_trace.py:1-18`, env-flagged operator lighting over the
  seams `vision → extract → integrate → recover → plan → per-fact decision`) and
  **`trace.py`** (the persisted per-card WHY, rendered by a frontend timeline,
  `analysis/trace.py:1-16`) both describe the deleted stages verbatim.
  > **BREAKS (observability only).** Both must be re-authored against the agent's turn/tool
  > trace. `trace.py`'s payload shape is deliberately renderer-agnostic
  > (`{"stages":[{key,name,version,summary,rows}]}`), so the frontend timeline can be
  > reused if the new writer emits the same shape.
- **The Grok eval harness** (`backend/tests/eval/README.md:1-30`) grades the real chain
  "extract → integrate → `plan_intent`" at intent level and `apply_intent` in `--db` mode,
  asserting bare `value_json`, no junk entities, correct resolution/disposition/
  cross-subject/supersession **[V]**.
  > **BREAKS.** This is the repo's only quality gate on graph writes and it asserts on
  > objects that cease to exist. **It must be re-authored before the cutover, not after** —
  > it is the only thing standing between this redesign and the class of regression it
  > was built for. Its *assertions* (bare values, no duplicate entities, supersession
  > closure) all survive verbatim; only the harness plumbing changes.
- **Test blast radius [V]:** 73 of 534 files under `backend/tests/` reference
  `app.facts` / `Fact(` / `plan_intent` / `apply_intent` / `integrate_note`.

---

## 8. The three things most likely to be missed

1. **`ingest/emr/importer.py` borrows the arbiter as a commit engine** (§5.2). Deterministic
   EMR import has no LLM in it and still dies.
2. **The four typed projections have exactly two callers** (§0, §5.1). Nothing errors when
   they stop being called — the calendar just quietly stops updating.
3. **`wiki_citations.chunk_id` is `NOT NULL` behind a Postgres trigger** (§2.2a). This is
   not an app-level convention that can be relaxed in a follow-up PR; it is a firewall the
   Phase-6 audit specifically hardened, and an agent fact without a chunk is *rejected by
   the database*, not merely unpublished.

---

## Open questions for the owner

1. **Does an agent-asserted fact carry a chunk?** This single answer determines whether the
   wiki survives the cutover intact (§2.2a). Three options: mandatory chunk on every
   `write_fact`; mint a "conversation chunk" so the turn itself is citable; or make
   `wiki_citations.chunk_id` nullable and re-run the firewall proof. Recommendation:
   **option 2** — it is the smallest change and it makes "you told me in chat" a
   first-class, citable source.
2. **Does the deterministic *commit core* die, or only the LLM extraction?** `apply_intent`
   is a shared engine used by EMR import and by re-analysis, and it owns supersession,
   shape checks, the domain firewall, derived-chunk minting and the four projections.
   Recommendation: **keep it, delete the Extractor + Integrator turn-loop above it**, and
   let the agent's tools call it. This turns a rewrite into a re-plumb.
3. **Do we keep `confidence` at all?** If yes, what does an agent's number mean given
   `weight.py:1-9` explicitly distrusts model self-reports? Recommendation: replace the
   float with an owner-confirmed boolean and let the PWA render "confirmed by you".
4. **Should the fact table record *who asserted it* — note-derived vs agent-inferred vs
   owner-confirmed?** The wiki's grounding gate ("the entity graph wins on conflict") stops
   being a safety property when the graph is itself LLM-written (§2.3). A provenance
   enum is the cheapest way to keep the gate meaningful.
5. **Is Wave D paused?** Tuning the grounding gate against a corpus produced by a producer
   about to be deleted is wasted work. Recommendation: pause D, reuse
   `docs/archive/PHASE6_WIKI_GRAPH_CONTRACT.md` as the hand-off protocol for round two.
6. **Ingest-time questions in the chat vs sweep-time cards in the inbox** — is that the
   split you want (§6.1)? The nightly sweeps have nobody to talk to at 03:30 and must keep
   filing cards; the review inbox should shrink, not disappear.
7. **What replaces the Grok eval harness, and does it gate the cutover?** It is the only
   automated check on graph quality and it asserts against `IntegrationIntent`/`ArbiterPlan`.
8. **Does the local model reliably emit the typed `value_json` shapes** the projections
   require (`{"start","end"}`, `{center,radiusMeters}`)? A wrong shape does not error — it
   removes an appointment from your calendar. Worth a spike against the on-box model before
   committing, in the spirit of `ENTITY_GRAPH_INGEST_V2_PLAN.md` §15.
