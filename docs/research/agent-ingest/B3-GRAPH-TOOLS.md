# B3 — The Agent's Graph Tool Surface

> **Status:** Research · **Last verified:** 2026-09-08

Scope: design the **write** tool surface a tool-using agent uses to build the
entity/predicate graph from a note, replacing the deterministic
`extract → Integrator → arbiter (plan_intent) → apply (apply_intent/_apply)`
pipeline. Written against the shipped code on branch
`claude/agent-predicate-db-redesign-xog54v`. Owner decisions are taken as binding:
auto first pass on capture, confidence-split write authority, no deterministic
arbiter, DB disposable, local-only inference (`gpt-oss-120b` text /
`qwen3.8-27b-abliterated` vision, one large model resident), questions to a silent
queue, entity-predicate model retained.

Everything cited `path:line` is **verified** by reading the code at that line.
Claims about how a local 120b behaves are marked **verified (measured)** where
`docs/reference/MODEL_PROMPTING.md` records a live measurement, and **assumed**
otherwise.

---

## 0. Recommendation first

**Five model-facing write tools, one of them batched, no UUIDs in the model's
context, and a fixed server-side list of structurally-dangerous cases that force a
question no matter what the model claims.**

| Tool | Shape | Why it exists |
|---|---|---|
| `resolve_entity` | batched (≤12 names) | turns surfaces into run-scoped handles; the only minting path |
| `assert_fact` | batched (≤8 edges) | the one fact-writing verb; supersession is a *consequence*, not a parameter |
| `retract_fact` | single | the correction channel ("no, that's wrong") — the only thing `decide()` cannot express |
| `merge_entities` | single | fold a duplicate; direction is server-chosen |
| `ask_owner` | single | explicit question when the agent itself is stuck |

**Deliberately NOT tools:**

- **`supersede_fact`** — `supersession.decide()` (`backend/src/jbrain/analysis/supersession.py:526`)
  already computes supersession correctly from `(candidate, existing, predicate)`
  with per-kind policy, bi-temporal ordering, pinned/irrealis/low-confidence
  guards, interval close, and re-open detection. A model-facing `supersede_fact`
  hands a 120b the one decision the codebase has spent the most effort getting
  right. `assert_fact` calls `decide()` and *reports* what happened in one short
  line.
- **`link_note_to_entity`** — mention creation folds into `resolve_entity`
  (the agent names the surface it saw; the server locates the span with the
  existing `_locate`, `backend/src/jbrain/analysis/pipeline.py:195`). A separate
  tool would be a second, weaker path to the same row.
- **a `done`/`finish` tool** — the end-of-run settle (retraction sweep, canonical
  reprojection, projections) is an **engine hook** on turn end, never a call the
  model must remember. gpt-oss does not reliably follow protocol obligations
  stated only in prose (**verified (measured)**,
  `docs/reference/MODEL_PROMPTING.md:229-238`: a prompt-stated tool budget "is not
  self-enforcing… enforce it in the loop/handler and let the prompt merely
  describe it").

Two structural recommendations that are as load-bearing as the tool list:

1. **Run-scoped short handles (`e1`, `f4`), never UUIDs.** A 36-char UUID in a
   tool argument is the single most likely thing a local model gets wrong, and the
   house pattern already avoids it: the Integrator addresses entities by opaque
   `mention_ref` (`analysis/intent.py:52`) and `read_entity` accepts the magic id
   `"me"` (`agent/tools/read_entity.tool:20-23`). Handles also give idempotency
   keys and a cheap audit trail for free.
2. **Run the graph tools under a note-domain-scoped session, not `SYSTEM_CTX`.**
   `app.facts` already carries `WITH CHECK (app.has_domain_scope(domain_code))`
   (`backend/migrations/versions/0006_analysis_schema.py:242-243`), so Postgres can
   be the firewall backstop for agent writes. Today ingest runs `SYSTEM_CTX`
   (`pipeline.py:311`) and the firewall is re-implemented in application code
   (`pipeline.py:377-380`: "`build_graph_context` applies the domain firewall
   itself (RLS does not scope SYSTEM_CTX)"). Moving the write path onto a scoped
   session is a genuine hardening the deterministic pipeline does not have — and
   it matters more when a model, not a validated intent object, is choosing the
   rows. (One carve-out, §4.3.)

---

## 1. Inventory: today's write path

### 1.1 The stages

```
integrate_note                pipeline.py:305   note → extraction → Integrator → plan → apply
  _extract_note               pipeline.py:230   per-source, budgeted note.extract calls
  build_graph_context         graph_context.py:309  candidate entities + their facts, rendered as text
  Integrator.integrate        integrate.py      → IntegrationIntent
  recover_dropped_fields      arbiter.py:408    restore object refs the integrator dropped
  derive_kinship_gender       arbiter.py:350    deterministic gender from kinship edges
  canonicalize_intent         pipeline.py:778   durable predicate-alias collapse (tier-1 only)
  dedup_intent_facts          arbiter.py:499    collapse the degenerate object-less twin
  compute_signals             arbiter.py:617    surface_attested / is_supersede — server-computed
  plan_intent                 arbiter.py:95     commit / review / reject partition
  apply_intent                pipeline.py:480   → _apply, one transaction
    _resolve_from_intent      pipeline.py:560   honor / refuse the agent's coreference
    _apply                    pipeline.py:825   the deterministic write
```

`_apply` in order (`pipeline.py:825-993`):

| Step | Line | What it guarantees |
|---|---|---|
| `_resolve_entities` | `:845` | agent override first, deterministic `resolve_entity` fallback |
| `_rebuild_mentions` | `:1275` | delete+insert this note's span-anchored mentions; returns `anchor_for` |
| `_upsert_tokens` | `:1511` | temporal tokens; every datetime traces to words |
| per-fact `_upsert_fact` / `_insert_held_fact` | `:1909` / `:1690` | the fact write |
| `_register_declared_aliases` | `:1384` | self-naming facts become exact aliases |
| retraction sweep | `:904-917` | facts this note no longer asserts → `retracted` (pinned + derived excluded) |
| shadow sweep | `:923-936` | derived reciprocals follow their source |
| `purge.repair_chains` | `:943` (`purge.py:138`) | survivors re-attach past doomed links or are restored |
| `purge.delete_review_items(..., statuses=("open",))` | `:949` | unservable open cards retired; resolved history survives |
| `_sweep_stale_ambiguous` | `:995` | obsolete ambiguous-mention cards retired |
| `_reproject_entities` | `:1318` (`canonical.py:88`) | `canonical_name` = live projection of `name.*` facts |
| `_promote_corroborated` | `:1331` (`canonical.py:233`) | provisional → confirmed at ≥3 distinct same-domain notes |
| `NoteAnalysis` upsert | `:955` | watermark: title/tags/extractor/prompt_version |
| projections | `:986-992` | appointments, EMR, place geofences, device bindings |

Inside `_upsert_fact` (`:1909-2262`):

- **assertion normalization** `:1922` — future → `expected`, undated "used to" → closed.
- **unlinked-entity skip** `:1924-1938` — a fact whose subject or object did not
  resolve is dropped, never guessed.
- **domain floor then ratchet** `:1941-1947` — `domain_floor(predicate)`
  (`extraction.py:189`, table at `:158-186`) raises a `general` fact into
  health/finance/location; `ratchet_domain` (`extraction.py:195`) permits UP only,
  files `domain_promotion` otherwise (`:2245-2262`).
- **temporal token binding** `:1949-1966`.
- **value-shape check** `:1853-1907` — tier-1 predicates only
  (`registry.declares_predicate`), recovers a scalar from prose, coerces enums,
  drops a violating `value_json` when `value_shape_enforce` is on. Never rejects
  the fact.
- **candidate retrieval + `decide()`** `:1993-2011` — `_existing_facts` scopes to
  `(entity, predicate, qualifier, subject, object, domain)`.
- **three write shapes**: interval close in place `:2019-2043`; refresh in place
  (citations survive, plus the held→active promotion and the derived→primary
  adoption) `:2045-2130`; fresh insert `:2132-2262`.
- **citation firewall** `_citation_chunk` `:1645` — a ratcheted fact cites a
  get-or-create *derived* chunk in its own domain, so no citation crosses the
  firewall.
- **side effects** `_apply_decision_side_effects` `:2274` — SCD-2 close of
  superseded rows, holds, review-card filing.
- **reciprocity** `_materialize_inverse` `:2362` — only for an ACTIVE and OPEN
  edge; cross-subject object → `inverse_proposal` card, writes nothing `:2390-2411`.
- **shadow chain propagation** `:2544`.

### 1.2 The invariant ledger

Every invariant the current write path enforces, and where it lives under the tool
design. "Tool" = the tool implementation (server side of the call), "PG" =
Postgres constraint/policy, "Engine" = the turn-loop hook.

| # | Invariant | Today | Under tools |
|---|---|---|---|
| I1 | Every fact has a note | `facts.note_id NOT NULL` (`0006:186`) | **PG** (unchanged) |
| I2 | A fact may only be written into a domain the writer holds | *not enforced* — ingest is `SYSTEM_CTX` | **PG** — `WITH CHECK (app.has_domain_scope(domain_code))` (`0006:242-243`) once the run is scoped (§0.2) |
| I3 | Sensitive predicates floor into their domain | `domain_floor` `extraction.py:189` | **Tool** — server-computed from the predicate, never a tool parameter |
| I4 | Domain ratchets UP only; a down-move is a review card | `ratchet_domain` `extraction.py:195` | **Tool** — the `sensitive` arg is a one-way dial (§3.3) |
| I5 | A fact's citation never crosses the firewall | `_citation_chunk` `pipeline.py:1645` | **Tool** (unchanged code, called from `assert_fact`) |
| I6 | kind/assertion/status/precision are closed enums | CHECK constraints `0006:161-181` | **PG** (unchanged) — a bad enum is a tool error the model retries |
| I7 | Identity key `(subject, entity, predicate, qualifier)` is the graph address | `facts_identity_idx` `0006:197`, `_existing_facts` `pipeline.py:1588` | **Tool** |
| I8 | Re-assertion of an identical value refreshes in place; citations survive | `decide()` refresh branch `supersession.py:539-560` | **Tool** — this *is* `assert_fact` idempotency (§3.2) |
| I9 | `event`/`measurement` never auto-supersede; same-instant clash → review | `supersession.py:585-595` | **Tool** → forced ask (§4.2) |
| I10 | `attribute` never auto-supersedes (two birthdays is a bug) | `supersession.py:596-618` | **Tool** → forced ask |
| I11 | `state`/functional-`relationship` supersede silently on strictly-newer validity, history retained | Lever B `supersession.py:783-807` | **Tool** |
| I12 | Non-functional relationships accumulate; opposite-polarity same-object is a contradiction | `supersession.py:620-651` | **Tool** → forced ask |
| I13 | Supersession compares **validity** time, never capture time | `_validity` / `key()` `supersession.py:371,665` | **Tool** |
| I14 | A pinned (human-decided) fact is re-flagged, never flipped | `supersession.py:711-716, 760-768` | **Tool** → forced ask |
| I15 | A low-self-confidence read never overwrites a confident prior | `supersession.py:769-782` | **Tool** — and `confidence` is now a tool arg, so this is the one place the model's number bites |
| I16 | An irrealis candidate never displaces an asserted head | `supersession.py:749-757` | **Tool** |
| I17 | Retrospective facts land as closed history, never the current head | `supersession.py:672-690, 810-817` | **Tool** |
| I18 | Reciprocal edges exist only for ACTIVE + OPEN relationships | `pipeline.py:2224-2233` | **Tool** |
| I19 | A reciprocal onto a *different* security subject is proposed, never written | `pipeline.py:2390-2411` | **Tool** → forced ask |
| I20 | Cross-subject attribution is always staged | `arbiter.py:132-135` | **Tool** → forced ask |
| I21 | 2+ live entities share a surface ⇒ engine decides, not the model | `_resolve_from_intent` `pipeline.py:585-597`, `same_name_entity_ids` `entities.py:584` | **Tool** → forced ask (§4.2). *This one is currently a guard against the agent's own guess; it survives verbatim.* |
| I22 | A declared name colliding with a different live entity files a merge, never widens an alias | `register_declared_alias` `entities.py:720`, `alias_owner` `:761` | **Tool** → forced ask |
| I23 | Near-duplicate declared names propose a merge, never auto-link | `near_duplicate_entity` `entities.py:482`, `_propose_near_duplicate` `pipeline.py:1429` | **Tool** → forced ask |
| I24 | A rejected merge (`distinct_from`) is never re-proposed | `are_distinct` `entities.py:772` | **Tool** — `merge_entities` refuses |
| I25 | The merge survivor is the more-anchored identity; the owner is never merged away | `plan_merge` `entities.py:799` | **Tool** — direction is server-chosen, not an argument |
| I26 | Merge = tombstone + repoint (reversible) | `merge_entity_pair` `entities.py:828` | **Tool** (same function) |
| I27 | Subject-bearing entities never auto-link on embedding similarity | `resolve_entity` `entities.py:925-933` | **Tool** |
| I28 | `canonical_name` is a projection of current `name.*` facts | `reproject_canonical_name` `canonical.py:88` | **Engine** (settle) |
| I29 | provisional → confirmed on ≥3 distinct same-domain notes; contested identity → card | `promote_if_corroborated` `canonical.py:233` | **Engine** (settle) |
| I30 | Facts this note no longer asserts are retracted quietly; pinned + derived excluded | sweep `pipeline.py:904-936` | **Engine** (settle) — **and this is the riskiest transplant** (§5.4) |
| I31 | Retraction repairs supersession chains | `purge.repair_chains` `purge.py:138` | **Tool** + **Engine** |
| I32 | Open cards on a retracted fact are retired; resolved history survives | `purge.delete_review_items(statuses=("open",))` `pipeline.py:949` | **Tool** + **Engine** |
| I33 | Every mention is span-anchored; merges are reversible | `_rebuild_mentions` `pipeline.py:1275` | **Tool** (`resolve_entity`) |
| I34 | The agent never supplies offsets it could fabricate | `AttestedSpan` docstring `intent.py:37-44`; `_locate` `pipeline.py:195` | **Tool** — `assert_fact` takes a `quote`, the server re-derives the span (§5.1) |
| I35 | Attestation is recomputed from the note, not taken from the model | `compute_signals` `arbiter.py:617-670` | **Tool** — the heart of §4 |
| I36 | Inferred facts cap at 0.6 / 0.4-when-superseding; a model number may only LOWER | `weight.py:28-31, 65-86` | **Tool** |
| I37 | An inferred fact on a floored-sensitive predicate is held (I5 net) | `arbiter.py:159-171` | **Tool** → forced ask |
| I38 | Value shape is validated for tier-1 predicates; a bad `value_json` drops, the fact survives | `_shape_check` `pipeline.py:1853` | **Tool** |
| I39 | Storage accepts **any** predicate; predicate-name validation never rejects | `docs/reference/entity.md:131-133`, `ENTITY_GRAPH_REFOCUS_PLAN.md:69-88` | **Tool** — no predicate allowlist in the schema |
| I40 | Temporal values are stored absolute with precision + original phrase | `_upsert_tokens` `pipeline.py:1511`, `_token_for_fact` `:1554` | **Tool** |
| I41 | Projections stay in sync with touched entities | `pipeline.py:986-992` | **Engine** (settle) |

**Lost, deliberately:**

| Lost | Was | Why acceptable / what replaces it |
|---|---|---|
| Whole-note atomicity (N5: a fatal violation commits nothing) | `plan_intent` reject branch `arbiter.py:114-122`, `apply_intent` `pipeline.py:501-509` | Writes now land per call. The *fatal* structural checks (`unknown_entity_ref`, `bad_kind`, `bad_assertion`, `bad_confidence` — `intent.py:212-235`) become per-call argument validation returning a tool error the model self-corrects from (`loop.py:147-149`). Owner decision "DB disposable" absorbs the residue. |
| `dedup_intent_facts` (the degenerate object-less twin) | `arbiter.py:499` | Largely dissolves: the object is a **required handle**, not a prose inference, so the twin cannot lose its object. Residual identical restatements collapse via I8. |
| `recover_dropped_fields` | `arbiter.py:408` | Same reason — nothing to recover when the ref is an argument. |
| `derive_kinship_gender` | `arbiter.py:350` | Drop the *derivation*; keep `_gender_grounded` (`arbiter.py:312`) as an attestation input. The agent can now simply ask. |
| Per-source fact budget, map-reduce grouping, `extraction_truncated` | `prompt.fact_cap`, `ANALYSIS.md:591-630` | There is no fact budget to clip when the agent decides what to write. Replaced by a per-run write budget (§6). **Open question** for very long notes vs. a 128k local context. |
| `note.extract` structured-output contract | `extraction.py` | Replaced by tool calls. Note that the salience contract (`ENTITY_GRAPH_REFOCUS_PLAN.md §4`) moves from the extraction prompt into the agent's system prompt + the `assert_fact` description. |

---

## 2. What the agent already has to read with

The write tools must compose with these, unchanged where possible.

| Tool | Sidecar | Handler | Returns |
|---|---|---|---|
| `search` | `agent/tools/search.tool` (v2) | `readtools.py:719` | RRF hybrid over notes/chunks, with a currency overlay flagging superseded facts |
| `read_note` | `read_note.tool` (v2) | `readtools.py:800` | full note text + domain + date |
| `find_entity` | `find_entity.tool` (v2) | `readtools.py:942` | name/alias → id, kind, domain as chips |
| `read_entity` | `read_entity.tool` (v4) | `readtools.py:921`, formatter `readtools.py:637` | kind, aliases, **current facts as edges**, inbound edges, ≤5 source notes |
| `relate` | `relate.tool` (v2) | `readtools.py:954` | follow one named relationship from an anchor |
| `neighborhood` | `neighborhood.tool` (v1) | `readtools.py:864` | n-hop (1-3) BFS over ref edges + co-mentions, with hop + path + connecting notes |

Sidecar mechanics (**verified**):

- `.tool` = YAML frontmatter (`ToolSpec`, `agent/contracts.py:69-90`) + prose body;
  loaded and validated at startup by `agent/toolfile.py:63`, bound to a handler by
  `agent/toolregistry.py`.
- `ToolFile.digest` (`toolfile.py:53-61`) is sha256 over description + spec
  (+ `examples` when present); pinned per version in
  `backend/tests/unit/test_agent_readtools.py:977` (`test_sidecars_pinned_to_their_versions`).
  Editing prose or params without a `version` bump is red CI.
- Every new `.tool` also owes a `STEP_LABELS` entry and an inline-arg policy in
  `frontend/src/agent/toolSummary.ts`, gated by
  `backend/tests/unit/test_tool_step_polish.py` (`ASSISTANT.md:254-263`).
- Handlers return `str` or `ToolOutput` (`agent/loop.py:355-392`) which can carry
  `sources`, `entities`, `view`, `proposal`.
- Permission classes and the session policy: `contracts.py:16`,
  `DEFAULT_OWNER_POLICY` `contracts.py:56` (`mutate`/`sensitive` → staged).
- Engine ceilings: `Guardrails` (`loop.py:127-133`), `ToolCallBudget`
  (`loop.py:224-247`) — the per-tool hard cap that exists precisely because
  prompt-stated budgets don't hold on gpt-oss.

**One read-side change is required.** `_edge_line` (`readtools.py:598-607`) prints
`predicate: statement → name (id=…)` but **no fact id**, so the model has nothing
to pass to `retract_fact`. The id is already in the view row
(`_FACT_SELECT`, `analysis/repo.py:140`), so this is a formatter change:
print the fact's run-scoped handle on each edge. `read_entity` v4 → v5 + digest
repin. No new read tool.

---

## 3. The tool set

### 3.0 Handles

The dispatcher keeps a per-run handle table `{handle → (kind, uuid)}`.
`e<N>` = entity, `f<N>` = fact, `q<N>` = queued question. Handles are minted by
the turn-0 context block and by every tool result; the model never sees or types a
UUID. `"me"` is always a valid entity handle (`read_entity.tool:20-23` precedent).
Unknown handle → tool error naming the handle and the tools that mint one.

Turn 0 pre-seeds handles for the entities `build_graph_context`
(`graph_context.py:309`) already retrieves for this note's surfaces — so for the
common note the agent asserts against known entities with **zero** resolve calls.

### 3.1 `resolve_entity`

```yaml
name: resolve_entity
version: 1
permission: mutate
params:
  type: object
  properties:
    mentions:
      type: array
      maxItems: 12
      items:
        type: object
        properties:
          surface:  {type: string}   # verbatim text from the note
          name:     {type: string}   # the name to use if a new entity is minted
          kind:     {type: string}   # schema.org type name: Person, Organization, Place, …
        required: [surface]
  required: [mentions]
```

Server behaviour, per element:

1. First person / `"Me"` → `get_or_create_me` (`entities.py:606`).
2. `resolve_entity` (`entities.py:865`) unchanged: exact alias → relationship hop
   → embedding (strong single non-subject only, `:381`, `:925-933`) → mint
   provisional (`create_provisional` `entities.py:642`).
3. `AmbiguousEntity` (2+ exact matches) → **does not resolve**; queues an
   `agent_question` and returns the handle `q<N>` instead of `e<N>` (I21).
4. Minting is refused when the proposed name collides with a live entity
   (`alias_owner` `entities.py:761`) or strongly near-duplicates one
   (`near_duplicate_entity` `entities.py:482`): returns the **existing** handle and
   queues a merge question (I22, I23).
5. Writes the span-anchored mention via `_locate` (`pipeline.py:195`) + the
   existing `EntityMention` insert shape (`pipeline.py:1296-1315`). A surface that
   does not locate still resolves, but records no mention (and any fact resting on
   it will not be surface-attested).

Idempotency: keyed on `(run, normalized surface)`. Re-calling returns the same
handle and does not duplicate the mention (delete+insert per run, as `_rebuild_mentions`
does per note).

Result (one line per element, ≤90 chars):

```
e1 Me [Person]
e2 Dr. Patel [Person] new
e3 Kaiya [Animal] existing · 2 notes
q1 "Chris" matches 2 people — asked; no handle
```

### 3.2 `assert_fact`

```yaml
name: assert_fact
version: 1
permission: mutate
params:
  type: object
  properties:
    facts:
      type: array
      maxItems: 8
      items:
        type: object
        properties:
          entity:    {type: string}                 # handle, e.g. "e1"
          predicate: {type: string}                 # schema.org-guided, free text
          qualifier: {type: string}                 # default ""
          kind:      {enum: [event, measurement, state, attribute, preference, relationship]}
          value:     {type: string}                 # scalar value; omit for a ref edge
          unit:      {type: string}                 # optional, pairs with value
          object:    {type: string}                 # handle — for a relationship edge
          statement: {type: string}                 # one clean sentence
          assertion: {enum: [asserted, negated, hypothetical, reported, question, expected]}
          quote:     {type: string}                 # verbatim note text this rests on
          when:      {type: string}                 # ISO date/datetime/interval, resolved
          when_phrase: {type: string}               # the words that produced `when`
          sensitive: {enum: [health, finance, location]}   # one-way dial only
          confidence: {type: number, minimum: 0, maximum: 1}
        required: [entity, predicate, kind, statement, assertion]
  required: [facts]
```

Notes on the schema shape:

- **Flat, scalar fields.** `value`+`unit` instead of a nested `value_json` object:
  gpt-oss fills flat args reliably (**verified**, the `public_records` collapse —
  `ASSISTANT.md:449-452` — "a tool-selection probe confirmed gpt-oss-120b fills
  `name`/`sources` reliably"), and nested objects-in-arrays are the shape that
  breaks. The server builds `value_json` as `{"value": …}` or `{"value": …, "unit": …}`
  and then runs `_shape_check` (`pipeline.py:1853`) which coerces enums and
  recovers a scalar from prose. Structured shapes the registry declares
  (`postal_address`, `geo`) are reachable through `statement` + shape recovery, not
  by asking the model to nest.
- **No `domain`.** The firewall red-team already rejected a model per-fact domain
  as a trust source (`arbiter.py:163-165`). `sensitive` is a strictly-upward dial
  fed into `ratchet_domain`; the server always applies `domain_floor` first.
- **No `inferred` flag.** It is recomputed (§4.1) — the model claiming
  "not inferred" is exactly the claim `compute_signals` exists to distrust
  (`arbiter.py:625-628`).
- **No `supersedes`.** `decide()` owns it.
- `predicate` is free text (I39). The tier-1 preferred spellings ride the tool's
  prose body as a delimited digest, CI-checked against `registry.declares_predicate`
  the way the extraction prompt's digest already is
  (`backend/tests/unit/test_promptfile.py:163-191`).

Server pipeline per element (reusing existing functions verbatim):

```
normalize_future_assertion / normalize_past_assertion   pipeline.py:1922
resolve handles → ResolvedEntity                        (handle table)
domain_floor → ratchet_domain                           extraction.py:189,195
locate quote → span; recompute attestation              pipeline.py:195, arbiter.py:617
temporal token                                          pipeline.py:1554
_shape_check                                            pipeline.py:1853
Candidate + _existing_facts + decide()                  pipeline.py:1993, supersession.py:526
FORCED-ASK GATE (§4.2)                                  new
write: close / refresh / insert                         pipeline.py:2019, 2045, 2132
_citation_chunk                                         pipeline.py:1645
_apply_decision_side_effects                            pipeline.py:2274
_materialize_inverse (active+open only)                 pipeline.py:2362
_propagate_supersession_to_shadows                      pipeline.py:2544
_register_declared_aliases (naming predicates)          pipeline.py:1384
```

**Idempotency.** Guaranteed by I8: an identical value at the same identity key
takes `decide()`'s refresh branch (`supersession.py:539-560`) — updates rendering
and provenance in place, no new row, no chain link. So a retried batch (a timeout,
a model repeating itself) is safe by construction. The tool additionally
short-circuits an exact `(entity, predicate, qualifier, value, object)` repeat
*within the same run* and returns the original handle.

**Partial failure.** Per element, independently, each in its own savepoint. Three
outcomes: `ok`, `asked` (written `pending_review` + question queued), `error`
(nothing written). A batch never rolls back its successful elements — the run
carries a `touched` set, so the settle sweep sees exactly what landed.

**Result** — one line per element, no echo of the input, no UUIDs:

```
1 ok f7 Me.homeLocation → 412 Oak St (superseded 118 Pine, kept as history)
2 ok f8 Kaiya.treatedBy → Dr. Patel
3 asked q2 Me.birthDate: already 1986-03-19 — two birthdays, queued
4 error facts[3].object "e9": unknown handle (resolve_entity first)
```

Rationale for terseness: the local box holds one large model with a ~32k primed
prefix, and an extra call/large result evicts it — measured at ~100 s cold
prefill vs 0.99 s warm (**verified (measured)**, `ASSISTANT.md:368-372`).
`CROSS_TURN_TOOL_RESULTS_PLAN.md:170-173` names self-capping as a hard
requirement; `MODEL_PROMPTING.md:240-241` records that verbose framing inflates
output.

### 3.3 `retract_fact`

```yaml
name: retract_fact
version: 1
permission: mutate
params:
  type: object
  properties:
    fact:   {type: string}   # handle from read_entity or a prior assert_fact
    reason: {type: string}   # one line, recorded, not a graph fact
  required: [fact, reason]
```

Server: set `status='retracted'`, run `purge.repair_chains` (`purge.py:138`) and
`purge.delete_review_items(..., statuses=("open",))` (`pipeline.py:949`), and
retract derived shadows (`pipeline.py:923-936`). **Refuses** a `pinned` fact
(I14) and a fact sourced from a different note than the one being processed unless
the owner is in the loop — an ingest run correcting another note's fact is
exactly the cross-note blast radius the review inbox owns. Idempotent (already
retracted → `ok`, no-op).

This tool exists because it is the **only** graph effect `decide()` cannot
express: `decide()` always inserts or refreshes; it never says "that was wrong."
Today that verb lives only in the review inbox
(`analysis/repo.py` resolution handlers).

### 3.4 `merge_entities`

```yaml
name: merge_entities
version: 1
permission: mutate
params:
  type: object
  properties:
    a:      {type: string}   # entity handle
    b:      {type: string}   # entity handle
    reason: {type: string}
  required: [a, b, reason]
```

Server: `are_distinct` (`entities.py:772`) → refuse (I24); `plan_merge`
(`entities.py:799`) picks the survivor (I25); `merge_entity_pair`
(`entities.py:828`) folds and repoints, returning the repointed row ids so an
un-merge is a replay (I26). **Forced ask** when either side is subject-linked
(`subject_id IS NOT NULL`) and they differ, or when either is `confirmed` — the
blast radius there is a cross-subject identity collapse, and no model confidence
buys it.

Returns `ok e2 ← e5 (folded 3 mentions, 4 facts)` or `asked q3 …`.

### 3.5 `ask_owner`

```yaml
name: ask_owner
version: 1
permission: mutate
params:
  type: object
  properties:
    question: {type: string}                       # one sentence, self-contained
    options:  {type: array, items: {type: string}, maxItems: 5}
    about:    {type: array, items: {type: string}} # handles the question concerns
  required: [question]
```

Writes one `review_items` row (`models/analysis.py:208`) of a new kind
`agent_question`, `domain_code` = the note's domain (so it rides the existing
firewall and RLS with no new table, and the existing inbox UI/API keep working).
Payload carries the question, options, referenced ids, note id, run id, and the
conversation turn. **Silent queue** per the owner decision: no push, no
interruption of the capture flow; the owner answers in the inbox or by replying in
the conversation.

Dedup: keyed on `(note_id, about-ids, normalized question)` so a re-run of the
same note does not stack duplicates — the same discipline
`_file_ambiguous_review` (`pipeline.py:1239`) and the merge-card dedup already use.

The owner's answer is fed back as a new turn in the same conversation (the
existing enact→agent outcome loop shape, `ASSISTANT.md:1143-1149`), so the agent
finishes the write itself rather than a second machine executor doing it.

**`ask_owner` is not the only way a question gets asked.** When the server's
forced-ask gate fires it queues the question *itself* and reports `asked` in the
tool result. Requiring the model to notice a refusal and then make a second
`ask_owner` call would be a protocol obligation, and protocol obligations stated
in prose do not hold on this model (`MODEL_PROMPTING.md:229-238`).

---

## 4. Confidence-split mechanics at the tool boundary

**Recommendation: confidence is both a parameter and a computed signal, related
asymmetrically — the model's number can only *lower* a server-computed ceiling —
and a fixed structural list overrides both.** This is not new arithmetic; it is
the shipped weight model (`analysis/weight.py`) moved to the tool boundary.

### 4.1 Three signals, three owners

| Signal | Who sets it | Code |
|---|---|---|
| `surface_attested` | **server**, recomputed from the note text | `compute_signals` `arbiter.py:617-670` |
| `is_supersede` | **server**, from `decide()`'s outcome | `supersession.py:526` |
| `self_confidence` | **model** (`confidence` arg) | `weight.py:74-86` |

`effective_weight` (`weight.py:74`) then reads, verbatim:

- surface-attested → full ceiling (1.0). The note is the authority; the model's
  self-report is noisy run-to-run and does not drag a stated fact down.
- inferred → `min(self_confidence, ceiling)` where `ceiling` is `0.6`, or `0.4`
  when the write would overwrite existing history (`weight.py:28-31`). The
  docstring states the rule the design needs: *"it may only LOWER it, never
  inflate (the anti-inflation rule that keeps a confident guess from buying a
  commit)"*.

So `confidence: 0.99` on a fact the note never states is worth **0.4** if it would
overwrite. The stored `facts.confidence` is that capped number, and it is what
I15's low-confidence supersession guard reads (`supersession.py:769-782`) — which
is the one place the model's number changes an outcome, and it can only make the
write *safer*.

**Why `surface_attested` cannot be a parameter.** `arbiter.py:625-628` states the
reason as built: *"Both must hold: an agent could claim a span it didn't read;
requiring the surface to be present in the chunks is the deterministic check."*
The recomputation is cheap and pure — normalized token containment over the note's
chunks (`_norm`/`_token_present` `arbiter.py:232,239`), plus the object-name,
stored-value, date-phrase, gender-term and time backstops (`arbiter.py:193, 253,
312, 580, 596`). All of it runs inside `assert_fact` with the note text the run
already holds.

### 4.2 The forced-ask gate (dangerous cases, model-claim-blind)

Evaluated **after** `decide()` and **before** the write. If any condition holds,
the fact is written `pending_review` (inert, exactly `_insert_held_fact`'s shape,
`pipeline.py:1690`) and a question is queued. The model's `confidence` is not read
by this gate at all.

| Condition | Source | Question asked |
|---|---|---|
| Surface resolves to 2+ live entities | `same_name_entity_ids` `entities.py:584` (guard at `pipeline.py:585-597`) | "Which Chris?" with the candidates as options |
| Cross-subject attribution | `arbiter.py:132-135` | "Record this against <subject>?" |
| Reciprocal would land on a different subject | `pipeline.py:2390-2411` | inverse proposal |
| New entity name collides with a live entity | `alias_owner` `entities.py:761` | "Same person as <existing>?" |
| New entity strongly near-duplicates a live one | `near_duplicate_entity` `entities.py:482` | merge question |
| `decide()` returns `review_kind='attribute_collision'` | `supersession.py:596-618` | "Two values for <predicate> — which?" |
| `decide()` returns `review_kind='fact_conflict'` (same-instant clash, polarity contradiction, irrealis-vs-asserted) | `supersession.py:585, 621, 749` | conflict question |
| Current head is `pinned` | `supersession.py:711, 760` | "You decided this before — change it?" |
| Inferred **and** predicate has a domain floor (I5 net) | `arbiter.py:159-171` | "The note doesn't say this — record it as health?" |
| Inferred **and** would supersede a live head | `INFERRED_OVERWRITE_CEILING` `weight.py:31` | "Replace <old> with a value the note doesn't state?" |
| Value fails a declared tier-1 shape with enforcement on | `_shape_check` `pipeline.py:1853` | *(no question — the value drops, the fact survives on its statement; I38)* |
| Merge where either side is subject-linked or confirmed | `plan_merge` `entities.py:799` | merge question |

Answers to the mission's three named cases:

- **Brand-new entity that collides by name.** The server does not mint. It returns
  the existing handle and queues a merge question. Claimed confidence is not
  consulted. This preserves `ANALYSIS.md:266-276` exactly: the collision is the
  *high-confidence same-person signal*, and the response is a merge proposal, never
  widening one name across two entities.
- **Cross-domain fact.** The model cannot down-ratchet: `ratchet_domain`
  (`extraction.py:195-207`) returns the note's domain and `needs_promotion=True`,
  which files a `domain_promotion` card (`pipeline.py:2245-2262`). The `sensitive`
  arg can only move UP.
- **Firewall-domain value.** Two layers. Application: `domain_floor`
  (`extraction.py:189`) forces the domain from the predicate regardless of what the
  model said. Postgres: with the run on a note-domain-scoped session, the
  `WITH CHECK (app.has_domain_scope(domain_code))` policy
  (`0006_analysis_schema.py:242-243`) makes a write into an unheld domain
  physically fail. That is the recommendation in §0.2 and it is the difference
  between "the code is careful" and "the database refuses."

### 4.3 The one carve-out

`_exact_matches` (`entities.py:565`) is **deliberately domain-blind**, because
declared-name collision detection needs cross-domain visibility
(`ANALYSIS.md:311-313`: *"`_exact_matches` being domain-blind is load-bearing, not
a leak"*). Under a scoped session it would go blind and I22 would silently stop
firing. Recommendation: keep that one lookup on a narrow `SYSTEM_CTX` helper that
returns **only** `(id, kind, subject_id)` — never a name, statement, or domain —
so the collision check keeps working without becoming a read channel. Flag it as a
security-review item for whichever wave lands this.

---

## 5. Provenance

### 5.1 Citations survive because the span is re-derived, never supplied

`assert_fact` takes a `quote` — verbatim note text. The server runs the existing
`_locate` (`pipeline.py:195`) to get `(chunk_id, char_start, char_end)`, then
`_citation_chunk` (`pipeline.py:1645`) to keep the citation inside the fact's own
domain. This is the shipped rule stated for the Integrator
(`intent.py:37-44`): *"The agent names a chunk and the surface text; the arbiter
RE-DERIVES the offsets from the chunk itself — the agent never supplies offsets it
could fabricate."* The tool boundary inherits it unchanged; only the producer
differs.

Three grades, all already implemented:

1. **Quote locates exactly** → span-anchored citation, `surface_attested=True`,
   full ceiling.
2. **Quote does not locate but the value/object/date/gender-term/time is present in
   the note** → the deterministic backstops fire (`arbiter.py:193, 253, 312, 580,
   596`), still `surface_attested`. These exist precisely because a model's quote
   drifts run-to-run.
3. **Nothing grounds** → `inferred`: the fact still commits (Lever A —
   `ANALYSIS.md:129-138`, a fact commits by default), at a capped weight, and the
   forced-ask gate catches the dangerous subset. Its `chunk_id` falls back to the
   entity's mention anchor and then to the note's first chunk
   (`pipeline.py:2135-2136`), so `facts.note_id` (NOT NULL, `0006:186`) always
   holds and purge-on-note-delete still reaches it.

**Nothing changes about deletion.** Purge is keyed on `note_id`
(`facts.note_id ... ON DELETE CASCADE`, `0006:186`), so agent-written facts die
with their note exactly like extractor-written ones. Notes-as-sole-source-of-truth
is untouched by moving the producer.

### 5.2 Turn provenance

Reuse the two provenance columns that already exist rather than migrating `facts`:

- `facts.extractor` ← `agent:<provider>:<model>` (today `f"{provider}:{model}"`,
  `pipeline.py:424`).
- `facts.prompt_version` ← the ingest-agent system-prompt version plus the write
  tools' digest generation, e.g. `ingest-v1/tools-3` — so a tool-surface change is
  as auditable as a prompt version bump is today, and the same `.tool` digest pin
  (`toolfile.py:53`, `test_agent_readtools.py:977`) enforces it.

For the conversation link, add **one** table — `app.graph_writes`:
`(id, run_id, turn_seq, tool, arg_digest, target_kind, target_id, outcome,
question_id, domain_code, created_at)`. It is the audit log the deterministic
pipeline gets from `flow_trace` (`analysis/flow_trace.py`) + the run-log persist
(`pipeline.py:452-461`), and it is what makes "which turn wrote this, and what did
the model actually say" answerable. New table ⇒ **RLS isolation test**
(CLAUDE.md #3). `run_id` ties to the Phase-5 workflow `runs` row the agent loop
already writes (`ASSISTANT.md:156-157`).

### 5.3 What a citation *means* now

Worth stating in `ANALYSIS.md` when this lands: a citation used to assert "an
extractor found this value at this span." It now asserts "the agent wrote this
fact, and this span is where the note supports it" — with the *support* check
still deterministic (§5.1) but the *decision* the model's. The wiki's
grounding contract (`wiki.ground`, high effort) is unaffected: it verifies claims
against retrieved chunks, not against who minted the fact.

### 5.4 The riskiest transplant: the retraction sweep

`_apply`'s sweep (`pipeline.py:904-936`) retracts every non-pinned, non-derived
fact of this note that the run did not touch. Under the deterministic pipeline
that is safe: the run either produced a complete extraction or was rejected
wholesale. Under an agent it is **not**: a run that stops early — the model asked
a question, hit `max_steps` (`loop.py:810`), errored out, or the box OOM'd — would
sweep away facts it simply hadn't got to yet.

Recommendation: run the sweep at settle **only when all of**:

1. the run ended `end_turn` (not `max_steps` / `too_many_errors` / `budget`,
   `loop.py:412`);
2. the run made ≥1 successful `assert_fact` element;
3. no question is queued for this note from this run;
4. this is a **re-analysis** (the note already has facts) — a first pass has
   nothing to sweep.

Otherwise: leave the untouched facts alone and record the skip. A stale fact is
recoverable; a swept one is a silent data loss on a box the owner cannot shell
into (CLAUDE.md #10). This is the single most important non-obvious rule in the
whole transplant.

---

## 6. Batch vs one-fact-per-call

**Recommendation: batch `assert_fact` (≤8) and `resolve_entity` (≤12); keep
`retract_fact`, `merge_entities`, `ask_owner` single.**

**Token cost.** The box holds one large model at a time
(`MODEL_PROMPTING.md`, `ANALYSIS.md:543-549`). Every tool round trip re-runs the
turn through the loop and re-prefills whatever the KV cache did not keep; the
measured penalty for losing the primed prefix on this box is ~100 s vs 0.99 s
(**verified (measured)**, `ASSISTANT.md:368-372`). Ten facts as ten calls is ten
round trips through a memory-bound machine for a job the owner expects to happen
quietly in the background. It also multiplies against `max_steps`
(`loop.py:131`, effort-scaled at `:216`): one-fact-per-call makes a fact-rich note
hit the step cap and get force-cut mid-note.

**Error recovery.** The failure mode that matters is *partial*, and a batch handles
it better than a sequence: per-element savepoints mean element 4 failing does not
undo elements 1-3, and the result names element 4 by index with an actionable
message so the next call fixes only that one. A sequence of single calls has the
same recovery but pays the round-trip tax for every one. The one thing a batch
must never do is roll back on any element — that would resurrect the whole-note
atomicity the design deliberately dropped (§1.2).

**Reliability.** Two opposing pressures, resolved by keeping the *elements* flat:

- gpt-oss fills flat scalar args reliably (**verified**, `ASSISTANT.md:449-452`).
- Arrays of deeply-nested objects are where structured-output reliability degrades
  (**assumed** — no on-box measurement in this repo).

So: an array of flat 12-field objects, no nesting, no arrays inside elements.
`value`/`unit` as two scalars rather than a `value_json` object is the concrete
application of this.

**Engine ceilings, not prose ceilings.** Give the ingest run a `ToolCallBudget`
(`loop.py:224-247`) on `assert_fact` — say 6 calls (≤48 facts). The salience
contract (`ENTITY_GRAPH_REFOCUS_PLAN.md §4`) says the graph is a spine, not an
encyclopedia; a prompt that says so will not hold on this model
(`MODEL_PROMPTING.md:229-238`), so the ceiling belongs in the handler, with the
remaining count appended to every result — exactly the pattern the deep-research
scout needed.

---

## 7. Worked transcripts

Formatting: `→` a tool call, `←` its result. Handles as the model sees them.

### 7.1 Simple note

> **Note (general, 2026-09-08):** "Took Kaiya to Dr. Patel Tuesday for her
> six-month checkup. She's due for her rabies booster in March."

**Turn 0 context (server-composed, no tool call):** the note text, plus the
pre-seeded handle block from `build_graph_context` (`graph_context.py:309`,
rendered by `render_graph_context` `:155`) with handles substituted for UUIDs:

```
Owner: e1 "Me" [Person]
Known nearby:
  e2 Kaiya [Animal] · species: dog · owner → e1
  e3 Dr. Patel [Person] · treatedBy edges from e2 (2 notes)
```

```
→ assert_fact {facts: [
    {entity: "e2", predicate: "treatedBy", kind: "relationship", object: "e3",
     statement: "Kaiya is treated by Dr. Patel.", assertion: "asserted",
     quote: "Took Kaiya to Dr. Patel", confidence: 0.95},
    {entity: "e2", predicate: "checkup", kind: "event",
     statement: "Kaiya had a six-month checkup with Dr. Patel.", assertion: "asserted",
     quote: "for her six-month checkup", when: "2026-09-01", when_phrase: "Tuesday",
     confidence: 0.9},
    {entity: "e2", predicate: "immunization", qualifier: "rabies", kind: "event",
     statement: "Kaiya is due for a rabies booster in March.", assertion: "expected",
     quote: "due for her rabies booster in March", when: "2027-03",
     when_phrase: "in March", confidence: 0.85}]}

← 1 ok f1 Kaiya.treatedBy → Dr. Patel (already active, refreshed)
  2 ok f2 Kaiya.checkup 2026-09-01
  3 ok f3 Kaiya.immunization.rabies 2027-03 [expected] · health
  budget: 5 assert_fact calls left
```

Notes on what the server did without being asked: element 1 hit `decide()`'s
refresh branch (I8) — same edge already live, so rendering + provenance updated in
place and no duplicate row; element 3's predicate is on the health floor
(`extraction.py:158-175`, `immunization`), so it ratcheted from `general` to
`health` (I3) and got a derived health citation chunk (I5, `_citation_chunk`);
being `expected` it neither supersedes nor mints a reciprocal.
`Tuesday` resolved against the note's capture anchor
(`local_anchor` `pipeline.py:161`) and became a temporal token with its phrase
retained (I40). Turn ends; the settle hook reprojects `canonical_name` for e2/e3
and refreshes the appointment/EMR projections. Total: **one** tool call.

### 7.2 A note that conflicts with an existing fact

> **Note (general, 2026-09-08):** "We finally closed on the house — 412 Oak
> Street as of the 1st. Also I weigh 178 now."

Turn 0 seeds `e1 Me` with `homeLocation → 118 Pine Ave (state, valid_from 2019-04)`
and `bodyWeight → 182 lb (measurement, 2026-08-14)`.

```
→ resolve_entity {mentions: [{surface: "412 Oak Street", name: "412 Oak Street", kind: "Place"}]}
← e4 412 Oak Street [Place] new

→ assert_fact {facts: [
    {entity: "e1", predicate: "homeLocation", kind: "state", object: "e4",
     statement: "Jeff's home is 412 Oak Street.", assertion: "asserted",
     quote: "412 Oak Street as of the 1st", when: "2026-09-01",
     when_phrase: "as of the 1st", confidence: 0.95},
    {entity: "e1", predicate: "bodyWeight", kind: "measurement", value: "178", unit: "lb",
     statement: "Jeff weighs 178 lb.", assertion: "asserted",
     quote: "I weigh 178 now", when: "2026-09-08", confidence: 0.9}]}

← 1 ok f4 Me.homeLocation → 412 Oak Street
    superseded: 118 Pine Ave, closed 2019-04→2026-09-01, kept as history
  2 ok f5 Me.bodyWeight 178 lb 2026-09-08 · health (accumulated, 182 lb 2026-08-14 kept)
```

Both conflicts resolved **silently and correctly without the model deciding
anything**:

- `homeLocation` is `state` with strictly-newer validity ⇒ Lever B silent
  supersession, history retained, no card (`supersession.py:783-807`). The old row
  gets an SCD-2 `valid_to` (`pipeline.py:2290-2295`).
- `bodyWeight` is `measurement` ⇒ time series, accumulate; no supersession is even
  possible (`supersession.py:585-595`), and it floors to `health`
  (`extraction.py:171-175` — `bodyweight` is deliberately on the floor list where
  bare `weight` is not).

Now the same note, one word different — *"I weigh 178, same as last month"*, where
last month's row says 182 at the same instant. `decide()` returns
`review_kind='fact_conflict'` with `conflicting_id` set, the forced-ask gate fires,
and:

```
← 2 asked q1 Me.bodyWeight: 178 lb clashes with 182 lb at 2026-08-14 — queued
```

The fact is written `pending_review` (inert, linked to its card,
`pipeline.py:1690`) and the run continues. The model does not get a chance to
"be confident" past this.

### 7.3 An ambiguous person reference

> **Note (general, 2026-09-08):** "Lunch with Chris — he's leaving Acme in
> October."

Two live `Chris` entities exist (Chris Alvarez, a colleague; Chris Boone, a
neighbour).

```
→ resolve_entity {mentions: [
    {surface: "Chris", name: "Chris", kind: "Person"},
    {surface: "Acme", name: "Acme", kind: "Organization"}]}

← q1 "Chris" matches 2 people — asked; no handle
  e5 Acme [Organization] existing
```

The server refused to resolve (I21) and queued the question with the candidates as
options. Nothing about the model's certainty entered into it: this is the exact
guard `_resolve_from_intent` (`pipeline.py:585-597`) already applies to the
Integrator's own `existing` pick, for the reason `ANALYSIS.md:296-301` gives — a
per-run bare-name guess flips run-to-run, and a silent flip is the one outcome no
layer may produce.

The agent's options now:

```
→ neighborhood {anchor: "e5", hops: 1}
← Acme [Organization] — 1 hop:
  Chris Alvarez [Person] worksFor → Acme (3 notes)
  … 4 more
```

That is corroborating evidence, but it is **not** a licence to resolve: the
handle was never minted, so `assert_fact` has nothing to point at. The right move
is to add the evidence to the question rather than route around it:

```
→ ask_owner {question: "Is the Chris who is leaving Acme in October Chris Alvarez (works at Acme) or Chris Boone?",
             options: ["Chris Alvarez", "Chris Boone", "someone else"],
             about: ["e5"]}
← merged into q1 (same question already queued)

→ assert_fact {facts: [
    {entity: "e5", predicate: "name", kind: "attribute", value: "Acme",
     statement: "Acme is an organization.", assertion: "asserted", quote: "Acme"}]}
← 1 skipped: nothing new (Acme.name already active)
```

The agent finishes its turn with the note's one unambiguous edge unwritten-because-
unneeded, one question queued, and no wrong link. When the owner answers
"Chris Alvarez" in the inbox, the answer returns as a turn in the same
conversation, `resolve_entity` is re-run with the resolution pinned, and the
`worksFor`-ending edge lands then — as a closed interval, because
`normalize_past_assertion`/`_interval_close` (`pipeline.py:1922`,
`supersession.py:377`) handle "leaving in October" as an end date on the existing
employment edge rather than a new one.

---

## 8. What the tool surface costs to build

Reused essentially verbatim: `supersession.decide` and everything under it,
`_shape_check`, `_citation_chunk`, `_upsert_tokens`/`_token_for_fact`,
`_apply_decision_side_effects`, `_materialize_inverse`,
`_propagate_supersession_to_shadows`, `_register_declared_aliases`,
`entities.resolve_entity`/`create_provisional`/`plan_merge`/`merge_entity_pair`/
`near_duplicate_entity`, `canonical.reproject_canonical_name`/`promote_if_corroborated`,
`purge.repair_chains`/`delete_review_items`, `extraction.domain_floor`/`ratchet_domain`,
`arbiter.compute_signals` and its backstops, `weight.ceiling`/`effective_weight`,
all four projections.

Genuinely new: the handle table, the forced-ask gate as one named function, the
`agent_question` review kind, `app.graph_writes` + its RLS test, the settle hook
with its four gating conditions, and five `.tool` sidecars with their digest pins,
`STEP_LABELS` entries and inline-arg policies.

Deleted: `analysis/intent.py`, `analysis/intent_parse.py`, `analysis/arbiter.py`'s
planning half (`plan_intent`, `plan_to_extraction`, `dedup_intent_facts`,
`recover_dropped_fields`, `derive_kinship_gender`), `analysis/integrate.py`,
`analysis/integrate_prompt.py`, `apply_intent`, and the `note.extract` prompt and
parser. The coverage gate means the tests go in the same PR
(`ENTITY_GRAPH_REFOCUS_PLAN.md:565-567` records this exact hazard).

---

## Open questions for the owner

1. **Does the agent read the note, or is it handed to it?** Turn 0 carrying the
   note text plus a pre-seeded handle block is the cheapest and most reliable
   (zero tool calls for the common note). The alternative — the agent calling
   `read_note` first — costs a round trip on a one-slot box but generalizes to
   long/multi-attachment notes. Recommended default: **hand it over**, with a
   `read_note` fallback for notes above a size threshold.
2. **Long notes.** The deterministic pipeline split a long note into per-source,
   token-bounded groups with their own fact budgets
   (`ANALYSIS.md:591-630`). An agent has a 128k context and no fact budget. Is
   "one conversation per note, however long" acceptable, or should a long note
   still be walked source-by-source? This is the one place the transplant loses a
   measured protection (the "car loan for the Kia" regression) with nothing
   equivalent proposed.
3. **The retraction sweep gating (§5.4).** Confirm the four conditions — in
   particular: on a re-analysis where the agent asked a question and stopped, do
   you want the note's old facts left standing (recommended) or swept?
4. **`retract_fact` blast radius.** May an ingest run retract a fact sourced from a
   *different* note when the current note contradicts it, or must that always route
   through a question? Recommended: always a question.
5. **`merge_entities` autonomy.** With "DB disposable" and confidence-split
   authority, should the agent merge two provisional non-subject entities directly
   (recommended) — or does every merge stay a question, as today
   (`ANALYSIS.md:186-189`)?
6. **Where do questions surface?** Recommended: the existing `review_items` inbox
   with a new `agent_question` kind (no new table, existing RLS, existing UI
   plumbing). Alternative: a dedicated queue with its own screen. The former is
   the "lean litmus test" answer (`ASSISTANT.md:128-130`).
7. **Scoped session vs `SYSTEM_CTX` (§0.2, §4.3).** Moving agent writes onto a
   note-domain-scoped session makes Postgres the firewall, but needs the narrow
   `SYSTEM_CTX` carve-out for domain-blind name-collision detection. Accept the
   carve-out, or keep `SYSTEM_CTX` for the whole run and rely on application code
   as today?
8. **Predicate steering.** Should the tier-1 preferred spellings live in the
   `assert_fact` description (CI-checked against the registry, mirroring
   `test_promptfile.py:163-191`), or in the ingest agent's system prompt? They
   cannot live in both without the contradiction gpt-oss handles badly
   (`MODEL_PROMPTING.md:213-216`).
9. **Vision.** OCR/caption stay pre-tool products (attachment extracts feeding
   chunks) so the text agent never swaps the resident model mid-run — or does the
   ingest agent get `analyze_image` and pay the residency swap? Recommended:
   pre-tool, as today.
10. **Effort.** `integrate.note` is the deliberate **High**-effort task today
    (`MODEL_PROMPTING.md:270`). But High buys runaway pre-tool reasoning and a
    larger step cap, which is exactly wrong for a tool-driven persona
    (`:222-228`). Recommended: run ingest at **medium** with engine budgets, and
    treat the drop as something to measure rather than assume.
