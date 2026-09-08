# B1 — Pipeline teardown map (extract → Integrator → arbiter → apply)

> **Status:** Research · **Last verified:** 2026-09-08

The exhaustive inventory of what the proposed conversational-ingest cutover
deletes, rewrites, keeps, or must decouple. Every claim below is cited
`path:line`. **Verified** = I read the code/migration/test at that line.
**Assumed** = inferred from a docstring or a naming convention without reading
the full body; each such claim is marked inline.

Scope note: the owner's decision 3 gutted *the pipeline* (extract → Integrator →
arbiter → apply). It did **not** gut the predicate registry, typed shapes, the
review inbox, or the facts/entities schema. This map therefore separates
"machinery that produces graph writes" (dies) from "machinery that stores, reads,
projects, repairs, and reviews graph writes" (lives, but loses its only caller).
Recommendations beyond the ratified decisions are flagged **[RECOMMENDATION]**.

---

## 0. Executive shape of the blast radius

| Area | DELETE | REWRITE | KEEP-BUT-DECOUPLE | KEEP-AS-IS |
|---|---:|---:|---:|---:|
| `backend/src/jbrain/analysis/**` (13,321 L total) | 6,580 | 1,964 | 1,105 | 3,672 |
| Prompt assets (`.prompt`) | 462 L / ~43 KB | — | — | — |
| `backend/src/jbrain/evals/**` (7,690 L) | ~7,247 | — | — | 443 (wiki-lint runner + cases + scores) |
| `backend/evals/**` (CLI, 407 L) | ~250 | ~157 | — | — |
| `backend/tests/harness/**` (5,976 L) | 5,976 | — | — | — |
| `backend/tests/eval/**` (2,957 L) | 2,957 | — | — | — |
| Unit tests | ~5,259 | ~1,250 | — | — |
| Integration tests | ~5,834 | ~700 | — | — |
| Frontend | 0 | ~350 (review card kinds) | ~1,900 | ~2,600 |
| Migrations | 0 (forward-only; DB is disposable) | — | — | — |

**Net ≈ 38–40k lines removed or rewritten**, of which ~8.5k is production backend
code and the remaining ~30k is test/eval/scenario corpus. The corpus is the
larger number by far and is the real cost: 75 harness scenarios, 12 extract eval
case files, and a graded real-Grok corpus all encode the *deterministic
arbiter's* semantics and cannot be carried forward unchanged.

---

## 1. The chain, end to end (verified)

Entry is a single job kind. There is exactly one production caller.

```
POST /api/notes                       api/notes.py (create)
  → ingest_note job                   ingest/pipeline.py:78  IngestPipeline.ingest_note
      chunk + FTS + embed staging     ingest/pipeline.py:383 _build_chunks
      OCR/transcribe gate             ingest/pipeline.py:252/327
      attachments_expected gate       ingest/pipeline.py:186  attachments_settled
      emit note.ingested              ingest/pipeline.py:210-224 wf_events.emit_event
  → workflow dispatcher               workflow/dispatcher.py:291,339,364
      trigger 0000…0e0002             migrations/versions/0040_seed_event_triggers.py:50-51
      pipeline event_integrate_note   → action integrate_note (registry.py:176)
  → integrate_note job                worker.py:746 → analysis/pipeline.py:305
        1. load note + paragraph chunks         pipeline.py:311-352
        2. per-source grouping                  pipeline.py:361-366 (prompt.py:80)
        3. note.extract call(s) + merge         pipeline.py:230 _extract_note
        4. build graph_context                  pipeline.py:378-390 (graph_context.py:309)
        5. Integrator (integrate.note call)     pipeline.py:392-398 (integrate.py:38)
        6. recover_dropped_fields               arbiter.py:408
        7. derive_kinship_gender                arbiter.py:350
        8. canonicalize_intent (predicates)     pipeline.py:778
        9. dedup_intent_facts                   arbiter.py:499
       10. compute_signals                      arbiter.py:617
       11. plan_intent  (THE ARBITER)           arbiter.py:95
       12. apply_intent (THE WRITER)            pipeline.py:480 → _apply pipeline.py:825
       13. Note.integration_state='integrated'  pipeline.py:434-438
       14. run-log + resolution pins (gated)    pipeline.py:450-465 (persist.py:172)
```

Re-run path: `POST /api/notes/{id}/analyze` → same `integrate_note` job
(`api/notes.py:366-393`, verified). Self-heal path:
`queue.backfill_pending_integration` (`queue.py:583-645`, verified) and its
scheduled twin `reconcile_pending_integration` (`workflow/scheduler.py:100`).
OCR-failure escape hatch: `ingest/ocr.py:101-131 enqueue_analysis_fallback`
enqueues `integrate_note` directly, bypassing the event.

---

## 2. Module inventory — `backend/src/jbrain/analysis/**`

Line counts are whole-file `wc -l` (verified). Classification is mine.

### 2.1 DELETE — the judgment/disposition/write machinery (6,580 L)

| File | L | Key symbols (`file:line`) | Role | Why it dies |
|---|---:|---|---|---|
| `pipeline.py` | 2,583 | `AnalysisPipeline:277`, `integrate_note:305`, `apply_intent:480`, `_apply:825`, `_upsert_fact:1909`, `_insert_held_fact:1690`, `_resolve_entities:1068`, `_disambiguate:1152`, `_rebuild_mentions:1275`, `_upsert_tokens:1511`, `_citation_chunk:1645`, `_apply_decision_side_effects:2274`, `_materialize_inverse:2362`, `_propagate_supersession_to_shadows:2544` | The whole job handler and the deterministic writer | It *is* the pipeline |
| `extraction.py` | 1,065 | `parse_extraction:797`, `dedup_facts:617`, `merge_extractions:1005`, `link_relationship_objects:663`, `finalize_temporal:416`, `normalize_future_assertion:210`, `ratchet_domain:195`, `domain_floor:189` | Parse/validate/repair the `note.extract` JSON | No `note.extract` call survives |
| `arbiter.py` | 768 | `plan_intent:95`, `compute_signals:617`, `dedup_intent_facts:499`, `recover_dropped_fields:408`, `derive_kinship_gender:350`, `plan_to_extraction:714`, `_value_attested:253`, `_object_named:193` | The deterministic disposition brain | Explicitly gutted (decision 2) |
| `intent.py` | 284 | `IntegrationIntent:124`, `IntentFact:71`, `EntityResolution:47`, `validate_intent:158`, `has_fatal:283` | The agent↔arbiter seam contract | The seam disappears; the agent writes through tools |
| `intent_parse.py` | 293 | `parse_intent:265`, `INTENT_SCHEMA` | Parse the `integrate.note` structured output | Same |
| `graph_context.py` | 362 | `build_graph_context:309`, `render_graph_context:155`, `rank_and_bound:117`, `_owner_neighbor_ids:206` | Pre-baked context block injected into the Integrator prompt | Replaced by agent read tools (the plan's own "bounded read-tool traversal loop" — `integrate.py:1-20` says this first cut is context injection *instead of* traversal) |
| `integrate.py` | 64 | `Integrator:34`, `Integrator.integrate:38` | One constrained `complete` call producing an intent | Replaced by a tool-loop agent |
| `integrate_prompt.py` | 53 | `build_integrate_prompt:23`, `INTEGRATE_PROMPT_VERSION` | Loader facade for `integrate_note.prompt` | Same |
| `prompt.py` | 167 | `fact_cap:44`, `group_texts:60`, `group_texts_by_source:80`, `prompt_block:122`, `build_user_prompt:150`, `PROMPT_VERSION` | Loader facade + fact-budget/grouping for `note.extract` | **Partly load-bearing**: `prompt_block` and the per-source grouping encode the "an attachment must never crowd out the body" rule (ANALYSIS.md "Per-source extraction"). That *policy* must survive even though this module doesn't |
| `weight.py` | 95 | `ConfidenceSignals:51`, `ceiling:65`, `effective_weight:74` | Deterministic confidence ceiling; "the model's self-confidence may only lower it" | The confidence-split write authority (decision 2) needs a replacement rule — see §9 |
| `trace.py` | 143 | `build_trace:61` | Projects a held fact's 3 pipeline stages into the review card's `trace` payload | Its three stages (extraction/integration/arbiter) no longer exist |
| `flow_trace.py` | 266 | `extract:119`, `intent:156`, `plan:200`, `commit:234`, `vision:81` | Operator flow tracing, one INFO event per seam | Seams gone. **Note**: `ingest/ocr.py:40` imports `flow_trace` for the `vision` seam (verified) — that one call site survives and needs a home |
| `persist.py` | 286 | `IntegrationRunLog:163`, `persist:172`, `build_pins:74`, `build_run_steps:60` | Writes the `app.runs` integration run + resolution pins | Both the run kind and the pins are intent-shaped |
| `pins.py` | 151 | `build_pin:106`, `pin_holds:136`, `occurrence_index_at:51` | Span-keyed memoization of the Integrator's stochastic decisions ("the silent flip" guard) | **The problem it solves does not go away** — a conversational agent re-running a note is *more* stochastic, not less. See §9 and the open questions |

Subtotal **6,580 L**.

### 2.2 REWRITE — policy that survives but not in this shape (1,964 L)

| File | L | Symbols | What must survive |
|---|---:|---|---|
| `entities.py` | 936 | `resolve_entity:865`, `_exact_matches:565`, `same_name_entity_ids:584`, `_relationship_hop:322`, `_embedding_candidates:397`, `get_or_create_me:606`, `create_provisional:642`, `register_declared_alias:720`, `plan_merge:799`, `merge_entity_pair:828`, `are_distinct:772`, `near_duplicate_entity:482`, `build_disambiguation_prompt:538` | Layers 1/2b/2 are pure graph lookups an agent tool should *call*, not re-implement in prose. Layer 3 (`entity.disambiguate`, a second LLM call) is redundant once the ingest agent is itself the disambiguator. `get_or_create_me` and the "Me" hard-link are load-bearing everywhere |
| `supersession.py` | 817 | `decide:526`, `FactView:208`, `Candidate:232`, `Decision:265`, `is_functional:31`, `inverse_predicate:146`, `_lab_status_transition:410`, `values_equal:348`, `_interval_close:377` | Per-kind conflict policy. **`_lab_status_transition` is EMR-only and must survive verbatim** (`ingest/emr/integrate.py` depends on it, verified via `tests/unit/test_supersession_lab_status.py`). The rest becomes either a write-tool guard or agent-visible policy |
| `display.py` | 211 | `inference_display:183`, `collision_display:99`, `merge_display:142`, `ambiguous_display:171`, `truncation_display:156`, `confirm_entity_display:199`, `promotion_display:128`, `mark_snippet:22` | Card display fields the frontend renders verbatim. Six of the seven builders serve card kinds only the arbiter files (§4.3), so they die with their kinds; `mark_snippet` + `value_label` + `object_or_value` are generic |

Subtotal **1,964 L**.

### 2.3 KEEP-BUT-DECOUPLE — survives, but loses its only caller (1,105 L)

These are called **from inside `_apply`** (`pipeline.py:986-992`, verified) and
have no other production trigger. The new agent write path must re-hook them, or
they become dead code that silently stops maintaining its read-models.

| File | L | Entry | Hook site today |
|---|---:|---|---|
| `appointment_projection.py` | 461 | `project_appointments:156` | `pipeline.py:986` |
| `emr_projection.py` | 404 | `project_emr:44` | `pipeline.py:987` |
| `geofence_projection.py` | 106 | `project_place_geofences:49` | `pipeline.py:988` (also `locations/geofence.py:276`, so it has a second caller — verified) |
| `device_binding.py` | 134 | `reconcile_device_bindings:34` | `pipeline.py:992` (sweep twin at `locations/geofence.py:275`) |

Also in this class but counted under KEEP because they have other callers:
`canonical.py:88 reproject_canonical_name` and `canonical.py:233
promote_if_corroborated` are called at `pipeline.py:954-955` only
(`wiki/builder.py:32` imports the *other* two functions from the module —
verified), so **canonical name projection and provisional→confirmed promotion
lose their trigger too**.

### 2.4 KEEP-AS-IS (3,672 L)

| File | L | Why it survives |
|---|---:|---|
| `repo.py` | 1,951 | The read + review-resolution API. `note_analysis_view:201`, `list_entities:285`, `entity_view:789`, `ego_graph:439`, `full_graph:525`, `neighborhood:589`, `relate:368`, `list_review:1152`, `resolve_review:1216`, `resolve_review_batch:1278`, `reopen_review:1366`, `_apply_resolution:1431`, `_reverse_effects:1827`, `merge_entities:959`, `note_currency:1032`, `analyte_currency:1105`, `predicate_suggestions:1182`. Consumed by `api/analysis.py`, `api/agent.py:61`, `api/proposals.py:19`, `agent/connectortools.py:32`, `agent/proposaltools.py:25`, `agent/mergetools.py:32` (all verified) |
| `purge.py` | 336 | Note-deletion purge; `notes/repo.py:11` and `workflow/scheduler.py:502` are the callers (verified). `_apply` also calls `purge.repair_chains` / `purge.delete_review_items` (`pipeline.py:943-949`) for the re-run retraction sweep — that *usage* dies, the module doesn't |
| `predicates.py` | 295 | Two-tier registry index. `embed.py:263` (seed rows) and `repo.py:1182` (suggestion picker) are non-analysis callers; `worker.py:26` calls `retire_open_new_predicate_cards:208` at boot |
| `consolidation.py` | 125 | The `consolidate_predicates` nightly action (`worker.py:813`) |
| `neighborhood.py` | 310 | Pure BFS behind `agent/readtools.py:80` (the `neighborhood` tool) |
| `relationships.py` | 110 | `predicate_candidates:98` behind `agent/readtools.py:86` |
| `hygiene.py` / `reembed.py` / `tagconsolidate.py` | 49 / 157 / 71 | Three nightly engine actions registered at `main.py:65-68` and `worker.py:24-28` |
| `canonical.py` | 266 | `name_fact_value:36` + `project_display_name:73` used by `wiki/builder.py:32` |
| `__init__.py` | 2 | — |

---

## 3. Prompt / tool assets and their digest pins

### 3.1 Assets owned by this chain — DELETE

| Asset | Size | Frontmatter |
|---|---|---|
| `backend/src/jbrain/analysis/prompts/note_extract.prompt` | 259 L / 30,688 B | `name: note.extract`, `version: note-extract-v31`, `strength: high`, `max_tokens: 16384`, `max_facts: 40`, `min_facts: 6` (verified, lines 1-12) |
| `backend/src/jbrain/analysis/prompts/integrate_note.prompt` | 168 L / 11,207 B | `name: integrate.note`, `version: integrate-v14`, `strength: high`, `max_tokens: 16384` (verified) |
| `backend/src/jbrain/analysis/prompts/entity_disambiguate.prompt` | 35 L / 1,370 B | `name: entity.disambiguate`, `version: entity-disambiguate-v1`, `strength: low`, `max_tokens: 4096` (verified) |

Total **462 L / ~43 KB** of prompt prose. The `note_extract` prompt at 30 KB is
the single largest prompt asset in the repo.

### 3.2 Digest-pin tests — DELETE / REWRITE

| Test | `file:line` | Pin | Fate |
|---|---|---|---|
| `test_prompt_content_is_pinned_to_its_version` | `backend/tests/unit/test_promptfile.py:144-161` | `("note-extract-v31", "be803b17…c006c")` over `SYSTEM_PROMPT + \x00 + json(EXTRACTION_SCHEMA)` | DELETE |
| `test_tier1_vocabulary_digest_matches_the_registry` | `test_promptfile.py:163-191` | Asserts every predicate in the prompt's `BEGIN-TIER1-VOCABULARY` block is registry-declared and canonical (≥40 entries) | **REWRITE — this one is load-bearing.** It is the only mechanical link between the schema registry and what the model is told to emit. A conversational agent still needs the tier-1 vocabulary in its system prompt, and still needs this drift check |
| `test_note_extract_file_round_trips_to_the_imported_constants` | `test_promptfile.py:100-104` | file ↔ `prompt.py` constants | DELETE |
| `test_entity_disambiguate_file_round_trips…` | `test_promptfile.py:107-123` | file ↔ `entities.py` constants | DELETE |
| integrate-prompt digest | `backend/tests/unit/test_analysis_integrate.py:113-125` | `(INTEGRATE_PROMPT_VERSION, sha256(INTEGRATE_SYSTEM + \x00 + json(INTENT_SCHEMA)))` | DELETE |
| `test_vision_files_round_trip_and_run_on_the_vision_tier` | `test_promptfile.py:126-141` | `vision_ocr` / `vision_caption` | **KEEP-AS-IS** — the vision chain survives (decision 5 keeps OCR/media) |

The generic `.prompt` loader (`backend/src/jbrain/llm/promptfile.py`) and the
rest of `test_promptfile.py` (frontmatter/validation tests, lines 15-98) are
KEEP-AS-IS.

### 3.3 `.tool` assets

**No `.tool` file is owned by this chain** (verified: `find … -name '*.tool'`
returns 139 files, all under `agent/tools/`). The chain never used the tool
registry — that is precisely what changes. The graph-adjacent tools that already
exist and would be the seed of the new write surface:

- Read: `agent/tools/read_entity.tool`, `find_entity.tool`, `relate.tool`,
  `neighborhood.tool`, `read_note.tool`, `read_wiki.tool`, `search.tool`
  (handlers at `agent/readtools.py:994-997`, `816`, verified).
- Write/propose (all currently go through the **Proposal** staging table, not
  the graph): `propose_merge.tool` (`agent/mergetools.py:93`),
  `propose_correction.tool` (`agent/proposaltools.py:85`),
  `file_correction.tool` + `request_rebuild.tool` (`agent/wikiwritetools.py:98-99`),
  `save_place.tool` (`agent/locationtools.py:886`),
  `manage_appointment.tool` (`agent/appointmenttools.py:224`).

**[RECOMMENDATION]** The new ingest agent's write tools should be a *new* family
(`assert_fact`, `link_mention`, `supersede`, `ask_owner`), not overloads of these
— the existing six all stage Proposals for owner approval, which is the opposite
of the confidence-split "commit when confident" authority.

---

## 4. Database

### 4.1 Tables owned by the graph (created by migration 0006, verified)

`backend/migrations/versions/0006_analysis_schema.py`:

| Table | Line | Owner after cutover | Notes |
|---|---:|---|---|
| `app.entities` | :38 | **KEEP** | `status IN ('provisional','confirmed','merged')` CHECK at :53. Later columns: `wiki_built` (0046), `image_sha` (0052) |
| `app.entity_aliases` | :71 | **KEEP** | Written only by `entities.py:720 register_declared_alias` and `create_provisional` — both in the REWRITE set |
| `app.entity_mentions` | :89 | **KEEP** | `link_method IN ('exact_alias','embedding','llm','human')` CHECK at :100. **`'llm'` is the disambiguate layer's value; `pipeline.py:139 _DB_LINK_METHODS` is the app-side mirror.** A conversational agent's link is arguably a new method — see open questions |
| `app.entity_distinctions` | :112 | **KEEP** | `distinct_from` negative knowledge; `entity_a < entity_b` CHECK at :121 |
| `app.temporal_tokens` | :129 | **KEEP** | `kind IN ('point','range','recurrence')` :134; `temporal_precision` CHECK :138. Written only by `pipeline.py:1511 _upsert_tokens` |
| `app.facts` | :153 | **KEEP** | `kind` CHECK :162, `assertion` CHECK :168, `status` CHECK :180, identity index :197 |
| `app.review_items` | :206 | **KEEP** | `kind` CHECK :208 (evolved, §4.3); `status` CHECK :215 (+`deferred` in 0024) |
| `app.note_analysis` | 0007 | **REWRITE** | `title`/`tags`/`extractor`/`prompt_version`/`analyzed_at`. `prompt_version` is meaningless once there is no versioned single prompt; `extractor` becomes the agent+model |
| `app.canonical_predicates` | 0031 | **KEEP** | Two-tier registry index; `descriptor` + `embedding` |
| `app.resolution_pin` | 0036 | **DELETE (or rewrite)** | Only writer is `persist.py:217 _upsert_pins`. Keyed `(note_id, chunk_id, occurrence_index, decision_kind)` |
| `app.runs` / `app.run_steps` | 0036/0037 | **KEEP-BUT-DECOUPLE** | `kind='integration'` rows are written by `persist.py:172`; the run log is shared with the agent (`agent/runlog.py`) and Ops. A conversation-shaped ingest run should ride the *same* table |

RLS: all of 0006's tables carry `has_domain_scope(domain_code)` FORCE RLS
(`0006:237-243`, verified). **No table is dropped**, so no new RLS isolation test
is needed for a deletion; a new table (e.g. a conversation/question queue) needs
one per CLAUDE.md #3.

### 4.2 Columns owned by this chain specifically

- `notes.integration_state` (`models/notes.py:43-46`) — `pending_integration` →
  `integrated` / `stale`; written at `pipeline.py:434-438` and flipped to
  `'stale'` on re-ingest at `ingest/pipeline.py:117-127`. **KEEP-BUT-DECOUPLE**:
  it is the durability guarantee the reconciler keys on (`queue.py:583`).
- `notes.attachments_expected` (migration 0154/0156, `models/notes.py:50-54`) —
  the capture-race gate. **KEEP-AS-IS**: it gates *when ingestion runs*, which is
  still needed (the agent must not read a note before its image lands).
- `notes.wiki_built` / `entities.wiki_built` (0045/0046) — **KEEP-AS-IS**, see §7.1.
- `facts.prompt_version`, `facts.extractor` — **REWRITE** (same reasoning as
  `note_analysis`).
- `facts.derived_from_fact_id` (0013) — the inverse/shadow edge link, written by
  `pipeline.py:2362 _materialize_inverse`. **KEEP-BUT-DECOUPLE**: reciprocity
  materialization is graph policy the new writer still owes.

### 4.3 Review-item kinds — who files what (verified by grep)

Current allowlist, `migrations/versions/0120_wiki_lint_review_kinds.py:24-31`:

| Kind | Filed at | Fate |
|---|---|---|
| `fact_conflict` | `supersession.py:600,655,718,758` | REWRITE (policy survives, filer moves) |
| `attribute_collision` | `supersession.py:614,622` | REWRITE |
| `merge_proposal` | `pipeline.py:1497` (+ agent `propose_merge`) | KEEP (agent already files these) |
| `ambiguous_mention` | `pipeline.py:1239 _file_ambiguous_review`; flagged at `arbiter.py:130`, `intent.py:183` | **DELETE the filer** — decision 7 says an agent question goes to a silent queue instead |
| `domain_promotion` | `pipeline.py:2245` | KEEP-BUT-DECOUPLE (domain ratchet is a firewall rule, not arbiter policy) |
| `low_confidence` | `supersession.py:471`; also `ingest/emr/firewall.py:47`, `reconcile.py:30`, `intake_handler.py:32` | **KEEP — EMR/intake file it independently** |
| `low_confidence_inference` | `pipeline.py:718` (`_file_inference_reviews:610`) | DELETE (this is the arbiter's I5 sensitive-inference net) |
| `new_predicate` | retired-only at `predicates.py:239-251`; **no live filer** | Already dead; DELETE the kind |
| `confirm_entity` | `pipeline.py:1373` | KEEP-BUT-DECOUPLE (corroboration promotion) |
| `extraction_truncated` | `pipeline.py:1065` (`_sync_truncation_review:1019`) | DELETE (there is no fact budget to truncate) |
| `inverse_proposal` | `pipeline.py:2397` | KEEP-BUT-DECOUPLE |
| `split_proposal` | **never filed** (verified: no writer in `src/`) | Dead enum value — DELETE |
| `shape_mismatch` | **never filed** — `_shape_check` only logs (`pipeline.py:1905`, verified) | Dead enum value — DELETE |
| `wiki_contradiction` / `wiki_stale_claim` | `wiki/lint.py:536,671` | **KEEP-AS-IS** |

Since the DB is disposable, the CHECK can simply be re-authored in one forward
migration rather than a chain of add/drop.

### 4.4 Migrations touching this chain (verified by grep)

Owned outright: `0006`, `0007`, `0009`, `0013`, `0023`, `0024`, `0030`, `0031`,
`0032`, `0033`, `0034`, `0035`, `0038`, `0040`, `0056`, `0114`, `0118`, `0120`,
`0163`. Shared with survivors: `0026`/`0028` (appointments), `0046`/`0047`/`0050`/
`0051`/`0052`/`0053` (wiki), `0062`/`0073` (geofence), `0116`/`0117` (EMR
projections), `0122` (EMR triggers), `0066` (hygiene sweeps).

**Recommendation given decision 4 (disposable DB):** do *not* rewrite history.
Add one forward migration that (a) re-authors the `review_items` kind CHECK, (b)
drops `app.resolution_pin` if the pin mechanism is not carried forward, (c)
adjusts `note_analysis`/`facts` provenance columns, and (d) re-seeds
`app.actions` + the event triggers. A destructive reset then makes the historical
chain irrelevant.

---

## 5. Jobs, queue, workflow engine

### 5.1 Actions

`backend/src/jbrain/workflow/registry.py:153-217` declares the shipped six
(verified). Relevant rows:

| Action | Line | Handler binding | Fate |
|---|---:|---|---|
| `ingest_note` | :154-164 | `worker.py:741` | **KEEP-AS-IS** (chunk/FTS/embed still runs first) |
| `embed_note` | :165-175 | `worker.py:742` | KEEP-AS-IS |
| `integrate_note` | :176-186 (`cost_class="expensive"`, `dedup_key_expr="note_id"`, `category="note"`, description "Extract facts, resolve entities, and write the graph.") | `worker.py:746 analyzer.integrate_note` | **DELETE or REWRITE in place.** Keeping the *name* is the cheapest cutover: every trigger, reconciler, dedup guard, and Ops surface keys on the string `integrate_note` |
| `ocr_attachment` | :187-197 | `worker.py:748` | KEEP-AS-IS |
| `consolidate_predicates` | :198-208 | `worker.py:813` | KEEP-AS-IS |
| `sync_predicates` | :209-219 | `worker.py:815` | KEEP-AS-IS |

`app.actions` is the reference projection of these six
(`migrations/versions/0035_actions.py:75` seeds the `integrate_note` row,
verified) and `tests/integration/test_actions_rls.py` asserts an exact set match
— so renaming the action requires touching the seed *and* that test.

### 5.2 Triggers and events

- `note.ingested` → `event_integrate_note` pipeline → `integrate_note` action;
  trigger id `00000000-0000-0000-0000-0000000e0002`
  (`0040_seed_event_triggers.py:50-51`, verified). **KEEP the wiring, swap the
  handler** is the low-risk move.
- The same `note.ingested` event drives the two EMR stages
  (`0122_seed_emr_import_triggers.py:38-53`, verified) via `payload_equals`
  filters on `destination`/`has_zip_attachment`/`has_pdf_attachment`. **These must
  keep firing** — see §7.3.
- `resolution.changed` → `consolidate_predicates` (`0040:52-57`), emitted from
  `analysis/repo.py:70 _emit_resolution_event`. **KEEP-AS-IS.**

### 5.3 Dispatcher guards

`workflow/dispatcher.py:291 _NOTE_DEDUP_KINDS = {"ingest_note","integrate_note"}`
and `:339-370` (skip on a queued integrate twin; a *running* one does not
suppress). **KEEP-BUT-DECOUPLE** — a resumable conversation has a different
once-only semantics than a one-shot job (see open questions).

### 5.4 Queue helpers

| Symbol | `file:line` | Fate |
|---|---|---|
| `queue.has_active_analysis` | `queue.py:308-330` (literal `kind = 'integrate_note'`) | KEEP-BUT-DECOUPLE |
| `queue.backfill_pending_integration` | `queue.py:583-645` (INSERT…SELECT of `integrate_note` jobs, excludes active jobs/outstanding OCR, honours the settle window) | KEEP-BUT-DECOUPLE |
| `queue.has_active_ocr_for_note` / `_transcribe` | `queue.py:331-367` | KEEP-AS-IS |
| `RECONCILE_PENDING_INTEGRATION_ACTION` | `workflow/scheduler.py:100-110` | KEEP-BUT-DECOUPLE |
| `ingest.enqueue_analysis_fallback` | `ingest/ocr.py:101-131` | KEEP-BUT-DECOUPLE |

### 5.5 Settings / config toggles owned by the chain

| Key | `file:line` | Fate |
|---|---|---|
| `integration_persist` | `settings_store.py:439`, `:809` | DELETE (gates `persist.py`) |
| `value_shape_enforce` | `settings_store.py:50`, `:531` | KEEP (typed shapes survive) |
| `predicate_canonicalization` | `settings_store.py:525` | KEEP |
| `analysis_trace` | `config.py:340` | DELETE (or repoint at the new agent's trace) |
| `image_analysis_mode` | `settings_store.py:515` | KEEP-AS-IS |

### 5.6 LLM task profiles

`llm/router.py:54-57,71` declares `note.extract`, `entity.disambiguate`,
`fact.adjudicate`, `correction_note.extract`, `integrate.note`; strength tiers at
`:132-146` (verified). Surfaced on the LLM settings screen via
`api/llm_settings.py:71-76` (verified). Fate:

- `note.extract`, `integrate.note`, `entity.disambiguate` → **DELETE** the task
  names, add one for the ingest agent.
- `fact.adjudicate` and `correction_note.extract` are declared in the router but
  **have no call site** (verified by grep across `src/`) — dead already.
- `llm_usage` accounting (`usage.py`, `LlmRouter._record`) is task-keyed and
  append-only; historical rows for deleted tasks stay valid. **KEEP-AS-IS** — but
  the AI-usage card's per-task breakdown will show retired task names forever,
  which is correct (it is a ledger).

---

## 6. API and frontend

### 6.1 Backend routes

| Route | `file:line` | Fate |
|---|---|---|
| `POST /api/notes/{id}/analyze` | `api/notes.py:366-393` (202 + job id; 409 while queued/running or while ingest/OCR will run one) | **REWRITE** — "re-analyze" becomes "start/resume the conversation" |
| `POST /api/attachments/{id}/analyze` | `api/notes.py:396+` | KEEP-AS-IS (vision re-run) |
| `GET /api/notes/{id}/analysis` | `api/analysis.py:51-57` | KEEP-AS-IS (reads `note_analysis` + facts) |
| `GET /api/entities`, `/{id}`, `/{id}/neighbors`, `/{id}/image` (GET/PUT) | `api/analysis.py:59-127` | KEEP-AS-IS |
| `GET /api/graph` | `api/analysis.py:128-136` | KEEP-AS-IS |
| `GET /api/review` | `api/analysis.py:137-148` | KEEP-AS-IS |
| `GET /api/review/{id}/predicate-suggestions` | `api/analysis.py:149-172` | KEEP-AS-IS |
| `POST /api/review/{id}/resolve`, `/resolve-batch`, `/reopen` | `api/analysis.py:173-193,252-278` | KEEP-AS-IS |
| `POST /api/review/{id}/correction` | `api/analysis.py:194-251` (mints an `owner_correction` note) | **KEEP-BUT-DECOUPLE** — the correction note's privileged force-supersede is implemented at `pipeline.py:322` (`correction = note.provenance == "owner_correction"`) and consumed by `plan_intent(…, correction=…)` at `arbiter.py:95`. That privilege must be re-expressed in the agent |

### 6.2 Frontend callers (verified by grep on `frontend/src/api/client.ts`)

| Call | `client.ts:line` | Screen | Fate |
|---|---:|---|---|
| `noteAnalysis` | :3078 | `components/AnalysisTab.tsx` (615 L) | KEEP-AS-IS |
| `analyzeNote` | :3084 | `AnalysisTab` provenance footer re-run | REWRITE (verb changes) |
| `analyzeAttachment` | :2491 | `components/ImageExtracts.tsx` | KEEP-AS-IS |
| `entities` / `entity` | :3094,:3099 | `screens/EntityScreen.tsx` (368 L) | KEEP-AS-IS |
| `entityNeighbors` / `graph` | :3205,:3213 | `screens/GraphScreen.tsx` (969 L) | KEEP-AS-IS |
| `review` / `resolveReview` / `resolveBatch` / `reviewCorrection` / `reopenReview` / `predicateSuggestions` | :3332,:3344,:3354,:3364,:3373,:3382 | `screens/ReviewScreen.tsx` (575 L) + `review/**` (1,323 L) | KEEP-AS-IS unless card kinds change |

Card-kind coupling: `frontend/src/review/blocks/registry.ts:41-63` declares a
block sequence per `ReviewItem["kind"]` (verified). Removing
`ambiguous_mention`, `low_confidence_inference`, `extraction_truncated`,
`new_predicate`, `split_proposal` deletes five rows there plus their blocks
(`ClaimInference.tsx` 312 L, `Trace.tsx` 145 L, `NewPredicateCard.tsx` 109 L,
`ClaimNotice.tsx` 13 L) — **~350 L REWRITE**, and `Trace.tsx` renders exactly the
three stages `analysis/trace.py:61` produces.

Per decision 6 and `docs/reference/PROCESS.md`, **any new surface (the silent
question queue, the conversation view) trips the three-mock GUI gate.** The
existing screens above do not, as long as their payload shapes hold.

---

## 7. Hidden couplings (the part that bites)

### 7.1 The wiki — coupled through Postgres triggers, not code ✅

`migrations/versions/0046_wiki_graph_coupling.py:24-37` (verified) installs three
SECURITY-DEFINER triggers that flip `entities.wiki_built=false` on **any**
INSERT/UPDATE/DELETE on `facts`, INSERT/DELETE on `entity_mentions`, and identity
UPDATEs on `entities`. The builder (`wiki/builder.py:303-311`) scans for
`NOT wiki_built`.

**This is the single best piece of news in the teardown**: any new writer — an
agent tool included — automatically drives the wiki, with no code coupling to
`analysis/`. Classification: **KEEP-AS-IS, no work.**
`wiki/builder.py:32` imports `analysis.canonical.name_fact_value` and
`project_display_name` (KEEP), and `wiki/builder.py:216,526` read `app.facts`
directly. `wiki/lint.py` has 25 raw references to graph tables.

### 7.2 EMR import — **hard dependency on `plan_intent` + `apply_intent`** ⚠️

`backend/src/jbrain/ingest/emr/integrate.py:25-27` imports `plan_intent`,
`AnalysisPipeline`, `_ChunkRef`, and `ConfidenceSignals` (verified); its module
docstring says it commits "through the SHIPPED deterministic core — `plan_intent`
… then `AnalysisPipeline.apply_intent`". `ingest/emr/import_handler.py:37` and
`:92` take the shared `AnalysisPipeline` as a constructor argument, and
`worker.py:812` wires `"emr_parse": EmrImportPipeline(maker, blobs, analyzer).parse`.
`ingest/emr/importer.py:24` imports the `IntegrationIntent` dataclasses.

**This is the biggest conflict with the ratified decisions.** EMR import is a
*deterministic parse* of the owner's own note attachments (health `Records`
notes, `0122` triggers). Decision 5 says attachments are in the conversational
scope; decision 3 says the arbiter and apply die. But EMR deliberately *avoids*
an LLM and needs a deterministic writer. Options:

- **(a)** Keep `plan_intent` + `apply_intent` alive solely as the EMR write path
  (~1,900 L of the "deleted" code survives as private EMR machinery).
- **(b) [RECOMMENDATION]** Extract a thin, deterministic `commit_facts(session,
  facts) -> ids` writer out of `_apply` (the supersession/citation/domain/
  projection mechanics, `pipeline.py:1690-2300`) and have **both** the EMR
  importer and the new agent's write tools call it. That makes the agent's
  "write" a tool over the same primitive the EMR parser uses, and is the only way
  the domain floor, the per-domain citation chunk (`_citation_chunk:1645`), and
  the inverse materialization survive with one implementation.
- **(c)** Rewrite EMR to emit through the agent — rejected: it would put an LLM
  in a path that is deliberately deterministic and health-domain.

### 7.3 Intake, agent-authored, and correction notes all ride `integrate_note` ⚠️

Decision 5 scopes conversational ingestion to owner notes. But *every* note in
the system reaches the graph through the same job:

- `intake/materialize.py` turns a stranger's confirmed submission into **one
  note** which "re-enters the normal note pipeline" (docstring, verified) →
  `ingest_note` → `note.ingested` → `integrate_note`.
- `agent/proposaltools.py:180-192` and `:209-219` create notes and enqueue
  `ingest_note` (the agent-note and intake-note leaf executors).
- `agent/wikiwritetools.py:42-48` mints `provenance="owner_correction"` notes;
  `api/wiki.py:230-260` and `api/analysis.py:194-220` do the same.
- `agent/locationtools.py:815-886` (`save_place`) stages a Proposal whose leaf is
  a note (verified via `tests/integration/test_save_place_pg.py:267`, which
  drives `AnalysisPipeline.integrate_note` end to end).

So "connectors/intake keep today's behaviour" is not achievable by leaving those
paths alone — **they have no behaviour of their own; they borrow this one.**
Either the new agent handles them too (and then intake's untrusted-content rule
must be re-established inside the agent loop), or `integrate_note` must survive
in parallel for non-owner notes.

### 7.4 The correction-note privilege

`pipeline.py:322` reads `note.provenance == "owner_correction"` and threads it as
`plan_intent(correction=True)` (`arbiter.py:95`), which force-supersedes and pins
the current head. Three surfaces mint those notes (§7.3). The privilege has no
other implementation. **KEEP-BUT-DECOUPLE — must be re-expressed.**

### 7.5 Search — **no coupling** ✅

`search/repo.py` and `search/service.py` reference `entity_kind` only as a field
on *wiki article* hits (`search/repo.py:101`, `service.py:50-56`, verified).
Note/chunk search is FTS + embeddings, indexed at ingest. **Deleting the analysis
chain does not affect search ranking or recall.** (This contradicts a plausible
worry; it is verified.)

### 7.6 Appointments / lists / labs read-models

`appointment_projection.py`, `emr_projection.py` are re-derived *only* inside
`_apply` (`pipeline.py:986-987`). The `appointments` table is a denormalized
view; `agent/tools/read_appointment(s).tool`, `manage_appointment.tool`, the ICS
feed, and `api/appointments.py` all read it. **If the projection hook is not
re-established, appointments silently stop updating** while the API keeps
serving stale rows. Same for lab results/encounters
(`api/…`, `agent/labtools.py`, `agent/charttools.py:*` which reads `app.facts`
directly — 4 references, verified).

### 7.7 Geofences and device bindings

`geofence_projection.project_place_geofences` and
`device_binding.reconcile_device_bindings` have a second caller in
`locations/geofence.py:275-276` (the `geofence_sweep` action), so they degrade to
"eventually correct on the next sweep" rather than breaking. **KEEP-BUT-DECOUPLE,
low severity.**

### 7.8 Entity promotion and canonical names

`canonical.reproject_canonical_name` (:88) and `promote_if_corroborated` (:233)
run only at `pipeline.py:954-955`. Without a new hook: entity display names
freeze at first-mention surface form (the "Sammy" bug the module's docstring
names), and provisional entities never get confirmed — which in turn means
`purge.sweep_orphaned_entities` (:272, the nightly `entity_hygiene` action) will
happily hard-delete them. **This is a data-loss interaction, not a cosmetic one.**

### 7.9 Purge and note deletion

`notes/repo.py:11 → purge.purge_note_artifacts:65` deletes facts, mentions,
tokens, review items, and provisional orphans on note delete, and repairs
supersession chains. It is independent of the chain and **KEEP-AS-IS** — but its
correctness assumes the supersession-chain invariants `_upsert_fact` maintains
(`superseded_by`, `valid_to`, `derived_from_fact_id`). A new writer that
maintains them differently silently breaks `repair_chains:138`.

### 7.10 `box_events` and usage accounting

`box_events.py` is host/model telemetry (`MODEL_LOAD`, `PREFILL`) written via
`box_events.record/span`; **no analysis module writes it** (verified by grep).
`llm_usage` is written centrally by `LlmRouter._record` for every call regardless
of caller. **Both KEEP-AS-IS, no work.**

### 7.11 Reconcilers / self-heal

Three reconcilers key on `integrate_note` by string:
`queue.py:583-645`, `workflow/scheduler.py:100`, `ingest/ocr.py:128`. Plus the
boot backfill referenced at `worker.py:521`. All **KEEP-BUT-DECOUPLE**; the
cheapest path is to keep the job kind name.

### 7.12 The schema registry

`jbrain/schema/**` (967 L + 24 type YAMLs, 962 L) is loaded eagerly at
`worker.py:739 get_registry()` and consumed by `arbiter.py:364,424,630`,
`extraction.py:851`, `intent_parse.py:181,230`, `pipeline.py:637,789,1875`,
`supersession.py:36`, `canonical.py:138`, `predicates.py:76`, `repo.py:1673,1706`
and **`wiki/builder.py:227`** (verified). Six of those nine analysis call sites
die; the registry itself is **KEEP-AS-IS** and remains the tier-1 vocabulary the
new agent must be taught (§3.2).

---

## 8. Tests, evals, harnesses

### 8.1 DELETE outright

| Path | L | What it tests |
|---|---:|---|
| `backend/tests/harness/**` | 5,976 | The scripted-perfect-model scenario harness: `runner.py:37` imports `AnalysisPipeline, _extract_note, local_anchor`; `runner.py:326` drives `integrate_note` with a scripted extraction + intent. **75 scenario JSONs, 5,258 L** encode arbiter semantics (`adv_*`, `hist_*`, `own_*`, `plan_*`, `rel_*`, `rerun_*`, `health_*`, `i5_*`, `pred_*`) |
| `backend/tests/eval/**` | 2,957 | The real-Grok graded corpus: `runner.py` runs "extract → integrate (graph-aware) → plan_intent"; `assertions.py` checks an `IntegrationIntent` + `ArbiterPlan` against a case's `expect`; 5 corpus JSONs (1,588 L) |
| `backend/src/jbrain/evals/runner.py` + `cases/**` | 310 + 6,384 | 12 `note.extract` case files, scored offline and by the nightly `eval_run` |
| `backend/src/jbrain/evals/integrate_runner.py` + `integrate_cases/**` | 255 + 107 | `integrate.note` calibration |
| `backend/src/jbrain/evals/disambiguate_runner.py` + `disambiguate_cases/**` | 128 + 55 | `entity.disambiguate` calibration |
| `backend/evals/run.py`, `audit.py`, `box/run_layer.py` | ~407 | The `scripts/prompt-eval.sh` / `scripts/grok-eval.sh` CLIs and the on-box calibration runner (layers `extract`/`integrate`/`disambiguate`) |
| Unit: `test_analysis_arbiter.py` (1,419), `test_analysis_extraction.py` (1,684), `test_analysis_intent.py` (199), `test_analysis_intent_parse.py` (206), `test_analysis_integrate.py` (167), `test_analysis_persist.py` (168), `test_analysis_pins.py` (202), `test_analysis_prompt.py` (60), `test_analysis_weight.py` (76), `test_analysis_display.py` (184), `test_trace.py` (160), `test_flow_trace.py` (367), `test_graph_context_render.py` (202), `test_extract.py` (165) | 5,259 | — |
| Integration: `test_extraction_pg.py` (2,150), `test_apply_intent_pg.py` (1,092), `test_entity_resolution_pg.py` (960), `test_analysis_gating_pg.py` (472), `test_reanalysis_pg.py` (279), `test_integrate_persist_pg.py` (276), `test_integrate_note_pg.py` (255), `test_graph_context_pg.py` (223), `test_cutover_pg.py` (127), `test_harness_scenarios.py` (84) | 5,918 | — |
| Eval-scoring: `test_eval_scoring.py` (301), `test_eval_assertions.py` (234), `test_eval_assertions_db.py` (496), `test_integrate_eval.py` (241), `test_disambiguate_eval.py` (129), `test_eval_db_runner_pg.py` (295) | 1,696 | The gate logic for the above |

### 8.2 REWRITE

- `tests/unit/test_supersession.py` (893) + `test_entities.py` (165) — the policy
  survives, the call shape changes.
- `tests/unit/test_promptfile.py` (191) — keep the loader tests + the tier-1
  vocabulary drift check (§3.2), drop the three round-trips and the pinned digest.
- `tests/integration/test_canonical_projection_pg.py` (391),
  `test_current_three_valued_pg.py` (331), `test_note_purge_pg.py` (491),
  `test_review_reopen_pg.py` (544) — these drive the graph *through*
  `integrate_note` today; they need a new driver but their assertions survive.
- `tests/unit/test_workflow_registry.py`, `tests/integration/test_actions_rls.py`
  — assert the exact six-action set; touched by any rename.
- `tests/unit/test_no_evals_boot.py`, `test_main_registry.py`,
  `test_ci_budgets.py` — registry/boot invariants.

### 8.3 KEEP-AS-IS

`test_supersession_lab_status.py` (228, EMR lifecycle), `test_purge.py` (48),
`test_predicates*.py`, `test_neighborhood_bfs.py` (303),
`test_analysis_relationships.py` (44), `test_analysis_canonical.py` (68),
`test_analysis_consolidation.py` (28), `test_appointment_projection*.py`,
`test_geofence_projection*.py`, `test_hygiene_sweeps_pg.py` (380),
`test_analysis_rls.py` (348 — the RLS isolation tests for the graph tables,
which do not change because the tables do not change),
`test_wiki_*`, `test_search*`.

---

## 9. What the new design must re-own (the load-bearing list)

Every item here is behaviour that exists *only* in the deleted code and has no
other home. This is the checklist a conversational-ingest design has to answer.

1. **Determinism across re-runs.** `pins.py` exists because a stochastic
   re-decision is "the one outcome no layer may produce" (`pins.py:1-12`,
   ANALYSIS.md "Same-name coexistence"). A resumable conversation makes this
   worse: turn 0 re-run on an edited note may re-decide identity.
2. **Write authority ceiling.** `weight.py:65 ceiling` — the model's
   self-confidence may only *lower* a deterministic ceiling. Decision 2 replaces
   this with a confidence split; the split threshold is now the model's own
   number, which is exactly the untrusted input `weight.py` was built to distrust.
3. **The domain firewall on writes.** `extraction.py:189 domain_floor` +
   `:195 ratchet_domain` (health/finance/location ratchet up, never down) and
   `pipeline.py:1645 _citation_chunk` (a citation never crosses a domain). These
   are CLAUDE.md #3 obligations, not conveniences.
4. **Span anchoring.** `pipeline.py:195 _locate` + `_rebuild_mentions:1275` —
   every entity link is a `(chunk, char_start, char_end)` row, which is what makes
   merges reversible and citations clickable. A tool-writing agent must produce
   spans, not just names.
5. **Per-kind supersession.** `supersession.decide:526`, including
   `_interval_close:377` (SCD-2), `_lab_status_transition:410`, and
   `values_equal:348` unit conversion.
6. **Reciprocity / inverse edges.** `pipeline.py:2362 _materialize_inverse`,
   `:2544 _propagate_supersession_to_shadows`, `:2568 _update_shadows_in_place`.
7. **Structural-identity upsert + retraction sweep.** `_upsert_fact:1909` — same
   `(entity, predicate, qualifier)` key updates in place so citations survive;
   `retracted_by_reextraction` for a key that vanished.
8. **Per-source input partitioning.** `prompt.py:80 group_texts_by_source` — the
   sole-source-of-truth guarantee that an attachment can't crowd out body facts.
9. **The projection hooks** (§2.3) and **canonical-name reprojection /
   corroboration promotion** (§7.8).
10. **The correction-note privilege** (§7.4).
11. **The owner "Me" anchor.** `entities.py:606 get_or_create_me` — the
    hard-linked subject row every first-person resolution keys on.

---

## 10. Recommended cutover shape **[RECOMMENDATION — not owner-ratified]**

1. **Keep the job kind name `integrate_note`.** Every trigger, seed row,
   reconciler, dedup guard, and Ops surface keys on that literal. Swapping the
   handler at `worker.py:746` is a one-line change; renaming it is a ~15-file
   change across migrations, tests and the actions RLS assertion.
2. **Extract `commit_facts` before deleting `_apply`.** §7.2(b). Without it, EMR
   import, the domain firewall, span anchoring, supersession, and the projection
   hooks all have to be rebuilt from scratch inside tools.
3. **Keep the review inbox, retire five card kinds.** `ambiguous_mention`,
   `low_confidence_inference`, `extraction_truncated`, `new_predicate`,
   `split_proposal` (plus dead `shape_mismatch`). The agent's *questions* go to
   the new silent queue (decision 7); the inbox keeps genuine conflicts, merges,
   domain promotions and the wiki-lint kinds.
4. **Keep the pins mechanism, re-key it.** Rename `decision_kind` values and key
   on the agent's committed decisions rather than the intent's. It is 151 L and
   it is the only defence against the silent flip.
5. **Port the tier-1 vocabulary digest test** to whatever system prompt the
   ingest agent gets. It is the only mechanical registry↔prompt link in the repo.
6. **Do the destructive reset (decision 4) as one forward migration**, not a
   history rewrite.

---

## Open questions for the owner

1. **EMR import (§7.2) is the sharpest conflict.** It is an owner-note-attachment
   path (in conversational scope per decision 5) that deliberately writes the
   graph *without* an LLM, through `plan_intent` + `apply_intent`. Do we (a) keep
   the deterministic core alive as private EMR machinery, (b) extract a shared
   `commit_facts` primitive both EMR and the agent's tools call, or (c) put EMR
   parsing behind the agent (an LLM in the health-records path)?
2. **Intake, agent-authored, correction and `save_place` notes (§7.3) have no
   ingestion behaviour of their own** — they all borrow `integrate_note`. Does
   the ingest agent handle them too (and if so, how does intake's
   untrusted-stranger-content rule survive a tool-using agent?), or does the old
   pipeline stay alive in parallel for non-owner-authored notes?
3. **Determinism on re-run.** A note can be re-analyzed on demand, on edit, and
   by the reconciler. Today `resolution_pin` memoizes decisions so a re-run cannot
   silently re-decide. In a resumable conversation, is turn 0 re-run at all — or
   does re-analysis resume the *existing* conversation with the new note text?
4. **What is "confident" (decision 2)?** The deleted `weight.py` exists on the
   premise that a model's self-reported confidence is untrusted content that may
   only lower a deterministic ceiling. If the confidence split is the model's own
   number, do we accept that reversal, or do we keep a deterministic ceiling
   (surface-attested vs inferred; sensitive-predicate floor) as a hard gate the
   agent cannot talk its way past?
5. **Where do agent questions live?** Decision 7 says a silent queue. Is that a
   new table (needs an RLS isolation test, CLAUDE.md #3) or a new `review_items`
   kind (reuses the inbox plumbing but inherits its badge/notification
   behaviour)? This determines whether the GUI gate trips for one surface or two.
6. **Does the graph keep span-anchored mentions?** `entity_mentions` is what makes
   merges reversible and citations clickable, and its `link_method` CHECK
   (`0006:100`) has no value for "an agent decided in conversation". Do we add a
   method value, or does the agent emit spans through a tool that reuses the
   existing four?
7. **What happens to the 75 harness scenarios and the graded corpus (§8.1)?**
   They encode arbiter semantics against a scripted perfect model. Do we port
   their *intent* into conversation-level evals (expensive, ~8k L of new
   fixtures), or accept a period with no regression net over supersession,
   temporal, and firewall behaviour?
8. **`note_analysis.prompt_version` / `facts.prompt_version` / `facts.extractor`.**
   With no single versioned prompt, what identifies "which brain wrote this fact"
   for a future re-run migration — agent version, system-prompt digest, model id,
   or all three?
9. **Local-only inference (decision 8) and the one-model-resident constraint.**
   Today one note costs 1 `note.extract` call per source + 1 `integrate.note` +
   ≤1 disambiguate. A tool-loop agent is many turns. On `gpt-oss-120b` with a
   vision model that cannot be co-resident, an image note requires a model swap
   mid-conversation. Is the conversation allowed to block on a swap, or must
   vision stay a pre-pass (as `ocr_attachment` is today)?
10. **Do the four read-model projections (§2.3) get re-hooked synchronously in
    the agent's write tool, or become nightly reconcilers?** Synchronous keeps
    appointments/labs live; nightly is simpler but means the calendar lags the
    conversation.
