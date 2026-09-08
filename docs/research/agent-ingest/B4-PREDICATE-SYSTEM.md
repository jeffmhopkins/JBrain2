> **Status:** Research · **Last verified:** 2026-09-08

# B4 — What of the predicate/entity system earns its keep when an agent writes the graph

Scope: the proposed change deletes `note.extract → integrate → arbiter → apply`. A note
becomes turn 0 of an agent conversation; a tool-using **local** agent (`gpt-oss-120b`,
`qwen3.8-27b-abliterated` for vision, one large model resident at a time) decides meaning and
writes entities/facts through tools, committing when confident and asking the owner when not.
The owner keeps an entity/predicate system. The DB is disposable dev data.

This doc audits each surviving mechanism against that model, then proposes a clean-slate schema.

---

## 1. Recommendation first

**Keep the graph. Retire the vocabulary bureaucracy.** The address `entity.predicate[.qualifier]`,
the supersession chain, span-anchored mentions, aliases, `distinct_from`, pins and the domain
column are what make the graph an *arbiter of current truth* and a navigation spine — none of
them depend on who decides meaning. Everything built to make a *single-shot JSON emitter*
converge on a vocabulary — the 1,222-line YAML registry, `canonical_predicates` + its 384-d
embeddings, `predicate_aliases`, the shape-validator, the display-name projection, the
corroboration promoter — was compensating for a writer that could not see the graph, could not
be told "no", and could not retry. A tool-using agent can do all three.

| Mechanism | Today (verified) | Verdict under agent-writes-graph |
|---|---|---|
| Two-tier predicate model (declared = tier-1) | `schema/models.py:167`; consulted at `pipeline.py:793`, `pipeline.py:1878` | **Idea necessary, mechanism redundant.** Keep a ~30-spelling *contract set* in code; delete the 24 type YAMLs as its carrier |
| Embedding canonicalization, STRONG/WEAK bands, `new_predicate` cards | already retired (`predicates.py:6-12`); residue = `canonical_predicates`, `predicate_aliases`, `decide_predicates` picker (`pipeline.py:643-659`), legacy verbs (`repo.py:1616-1712`) | **Delete the residue.** Two tables, one HNSW index, ~450 lines serving one review-card picker |
| Typed value shapes / `value_json` validation | `schema/models.py:199-255`, `pipeline.py:1853-1908` | **Necessary, wrong place.** Move shape from a post-hoc YAML check to the *write tool's parameter schema*, returning a tool error the agent can retry — never a silent drop |
| Vocabulary invariant ("names never rejected; values may be") | `entity.md` §"The vocabulary invariant"; enforced by `_canonicalize_predicates` (`pipeline.py:783-815`) | **Keep verbatim.** It is the reason an agent can write a novel concept at all |
| Structural identity `(subject, entity, predicate, qualifier)` | `models/analysis.py:131-137`, `pipeline.py:1598-1624` | **Keep, and make it explicit** — the object is silently part of the key for non-functional edges today |
| Supersession chains as revision history | `supersession.py:526` (`decide`, 817 lines) | **Keep the chain; delete the decision engine.** Per-kind policy becomes tool doctrine + 4 hard invariants |
| Assertion status enum | `0006_analysis_schema.py:168-170`; floor at `supersession.py:204` | **Keep.** Six values, load-bearing for the wiki, trivially agent-set |
| Pinned facts | `models/analysis.py:170`; honored `supersession.py:9-11` | **Keep — it is the confidence split's hard boundary** |
| Chain repair | `purge.py:43,138` | **Keep** while notes can be deleted/re-analyzed |
| Entity resolution: exact alias + collision gate | `entities.py:565-583,865-884` | **Keep as the engine backstop** (determinism across re-runs) |
| Entity resolution: embedding layer, `entity.disambiguate`, `near_duplicate_entity` | `entities.py:381-382,397-480,482-520` | **Cut.** The agent has `find_entity`/`neighborhood` and the note in context |
| Declared-name alias sniffing | `entities.py:669-716` (`_NAMING_PREDICATES`, `_NAME_VALUE_KEYS`) | **Cut the sniffing; keep aliases.** The agent writes an alias explicitly |
| `distinct_from`, merge-as-tombstone | `entities.py:772-786,828-862` | **Keep.** Negative knowledge is the only thing that stops a re-proposal loop |
| `canonical_name` projection (`display_name`) | `canonical.py:88-160`; 20 of 24 types declare `display_name: [name]` | **Cut the projection.** The agent names the entity |
| `entities.summary` | **never written by any code path** (verified) | **Keep the column, make the agent fill it** — it is the entity's only vector handle |
| provisional→confirmed promotion | `canonical.py:168-266`, setting default OFF (`settings_store.py:413`) | **Cut the counter, keep the status.** Confirmation is a judgment; the agent is now the judge |
| Nightly sweeps | `hygiene.py:28`, `reembed.py:36`, `tagconsolidate.py:31`, `consolidation.py:132` | **Three survive unchanged** (pure SQL/embedding, scheduler-run). `consolidate_predicates` dies with `renamed_from`; its guarded rewrite is *reused* by the reconciler |
| Registry as a *gate/validator* | `schema/defs/**` (1,222 lines, 121 distinct predicates) | **Retire.** Replace with retrieval-of-existing-vocabulary at write time + a code-level contract set |
| Drift control | — | **Push vocabulary into context (not a lookup tool) + a weekly reconciler conversation** (§6) |

---

## 2. The two-tier predicate model

**How it works today (verified).** The tier flag is *registry declaration*, no dedicated field:
`SchemaRegistry.declares_predicate` (`schema/models.py:167-171`) does a normalized membership test
against `known_predicates` (`schema/models.py:121-124`), built by the loader from every type's
`effective_predicates`. It is consulted at exactly two live seams:

- `pipeline.py:793` — an undeclared predicate skips canonicalization entirely and commits raw
  (`predicate.longtail_kept`, `pipeline.py:806-814`);
- `pipeline.py:1878` — an undeclared predicate skips shape validation.

The third consumer named by the refocus plan — the unknown-predicate weight penalty — **is gone**:
`weight.ceiling` (`weight.py:65-71`) states explicitly that tier-2 carries no penalty.

**What it was solving.** The prompt bet on LLM convergence for predicate spellings; against a real
model the vocabulary was non-deterministic (`PREDICATE_CANONICALIZATION.md` §1), so *every*
unknown spelling filed a review card. Two-tier stopped the card flood by declaring that only
declared predicates get machinery.

**Verdict: the idea is necessary, the mechanism is redundant.** The tier split is correct and
survives — a handful of spellings are read by *code* and must be exact; everything else is prose
with a key. But the registry is no longer the authority for any of them, and has not been for a
while. The contract set is already spread across five hardcoded copies:

| Contract | Where it actually lives |
|---|---|
| Firewall domain floor (~45 spellings) | `extraction.py:158-186` `_DOMAIN_BY_PREDICATE` — hardcoded, registry-independent |
| Functional (supersede-on-change) | `supersession.py:26-28` allowlist **unioned** with the registry flag (`supersession.py:31-36`); 45 YAML predicates carry `functional: true` |
| Reciprocity / inverse edges | `supersession.py:91-145` `INVERSE_PAIRS` + `SYMMETRIC_PREDICATES` — hardcoded |
| Typed projections | `appointment_projection.py:43-46,271-282`, `geofence_projection.py:23`, `emr_projection.py:116,258,272,298` — hardcoded string literals (`scheduledTime`, `recurrence`, `status`, `geofence`, `value`, `hasObservation`, `attender`, `period`) |
| The prompt's tier-1 digest (~110 spellings) | `analysis/prompts/note_extract.prompt:202-226`, **hand-authored**, CI-checked against the YAML |
| Graph-context surfacing order | `graph_context.py:38-52` `_IDENTITY_PREDICATES` — hardcoded |

So the YAML is the *sixth* copy, and the only one nothing reads at a decision point except the two
seams above. Meanwhile 20 of 24 types declare `display_name: [name]` — the projection engine is a
no-op for all but person/place/vehicle/role/organization.

**Recommendation.** Delete `schema/defs/**` and the loader. Keep one ~40-line
`jbrain.graph.contract` module holding: the projection spellings, the functional set, the
reciprocity map, the firewall floor, the identity-surfacing order. That module *is* tier-1, it sits
next to its consumers, and it is small enough to read in one screen. Tier-2 is everything else,
unchanged: stored raw, searchable, traversable, never rejected.

---

## 3. Embedding-assisted canonicalization and the card flood

**Already dead, verified.** `analysis/predicates.py:6-12` records the retirement: the Phase-4
calibration (`PREDICATE_CANONICALIZATION.md` §5a — true drift at 0.57–0.72 cosine, overlapping
genuine novels, nearest neighbours sometimes plain wrong) showed no threshold separates drift from
novelty. `_PRED_STRONG` was left at 0.90 so auto-merge effectively never fires, `_PRED_WEAK` at
0.55 so everything files a card — and then the card path itself was deleted, with a one-shot boot
sweep retiring the open backlog (`predicates.py:208-272`).

**What still exists, and what it costs:**

- `app.canonical_predicates` (migration `0031_canonical_predicates.py:31-46`) — one row per
  registry predicate, `vector(384)` + HNSW index + its own RLS policy;
- `app.predicate_aliases` (`0056_predicate_aliases.py:30-45`) — the durable owner-confirmed
  raw→canonical map, FK'd to the above;
- `decide_predicates`/`nearest_predicates` (`predicates.py:153-195,275-295`), whose **only**
  consumer is the held-fact predicate-suggestion picker (`pipeline.py:643-659`), gated by the
  repurposed `predicate_canonicalization` setting (`settings_store.py:37`);
- the legacy resolution verbs `map_to_existing` / `accept_as_new` / `suggest_better`
  (`repo.py:1616-1712`) plus the `new_predicate` review kind (`0120_wiki_lint_review_kinds.py:23-30`).

**Verdict: actively harmful to carry forward.** The picker exists so the owner can retype a
predicate on a card the agent will no longer file. In the conversation model the correction channel
is a sentence — "that should be `treatedBy`" — and the agent re-writes through the same tool it
wrote with, which is strictly better UX and needs no embedding index. Keeping the tables means
keeping an RLS surface, a seed job (`sync_predicates`, `workflow/registry.py:210`), an FK web, and
a resolution path for a card kind that no longer exists.

**Recommendation.** Drop both tables and the picker. **Keep exactly one artifact from this
subsystem:** `consolidation.rewrite_predicate` (`consolidation.py:97-129`) — the guarded in-place
rename (same row, same id, citations intact; refuses when a live twin holds the canonical address;
never touches pinned or retracted rows). That is the safe primitive the reconciler in §6 needs.

---

## 4. Typed value shapes and the vocabulary invariant

**How it works today (verified).** `_shape_check` (`pipeline.py:1853-1908`) runs after the entity is
resolved (it needs the entity kind), and does three things:

1. **recovers** a scalar the model left null, from the statement (`recover_scalar_value`);
2. **coerces** an enum written as prose — `{"value": "Female (inferred from 'wife')"}` → `"female"`
   (`schema/models.py:199-223`, conservative: exactly one whole-word member match);
3. **validates** against the declared `value_shape` (`schema/models.py:225-255`) and, when
   `value_shape_enforce` is on (`settings_store.py:50`), **drops `value_json`** on violation — the
   fact survives on its statement.

All three are repairs of *extraction-shaped* failures: a one-shot JSON emitter that forgets the
datum, buries an enum in a rationale, or puts a scalar where an edge belongs. The invariant
(`entity.md`) is the guard rail: *storage accepts any predicate; shape validation may reject a
malformed `value_json`; predicate-name validation may never reject anything.*

**Verdict: necessary, wrong place.** A tool call has a typed signature. The three repairs collapse
into the tool contract:

- a `ref`-shaped predicate is a *different tool parameter* (`object` vs `value`), so "minted a value
  as if it were the target" becomes structurally unrepresentable rather than caught downstream;
- an enum member is a tool-level `enum`, and a violation returns a **tool error** naming the
  members — the agent sees it and retries, which is what a silent drop plus a `log.warning` can
  never do;
- "always emit a concise datum" (`note_extract.prompt:236`) becomes a required parameter.

**Recommendation.** Keep `value_json` and keep shape checking, but express it as the write tool's
parameter schema plus a ~50-line validator that *returns an error to the agent* instead of dropping.
Keep the invariant verbatim, and state its tool-era form: **a tool may reject a malformed value; it
may never reject a predicate name.** Shape members for the handful of closed enums (gender, fact
kind, assertion, appointment status) live in `jbrain.graph.contract`, not in 24 YAMLs.

---

## 5. Identity, chains, resolution, projections, sweeps

### 5.1 Structural identity — keep, and make it honest
`(subject_id, entity_id, predicate, qualifier)` is the graph address (`models/analysis.py:131-137`,
DDL `0006_analysis_schema.py:151-160`). **But the real key includes the object**: `_existing_facts`
(`pipeline.py:1598-1624`) adds `object_entity_id` to the candidate query for non-functional
predicates and *excludes* it for functional ones, so `me.owns→Civic` and `me.owns→kayak` are
different facts while a new employer supersedes the old. That branch is correct and invisible;
under an agent writer it should be stated in the schema (a partial unique index on the live head,
§7) so a double-write is a DB conflict the tool can report, not a silent fork.

### 5.2 Supersession — keep the chain, delete the engine
`supersession.decide` is 817 lines encoding real judgment: events never auto-supersede, measurements
accumulate, attributes hold for review (two birthdays is a bug, not news), states close SCD-2
intervals, schedule bindings order by `reported_at` not validity (`supersession.py:172-175`),
irrealis never displaces an asserted head (`:190`), a low-confidence candidate parks instead of
overwriting (`:183`). Under "no deterministic arbiter", that knowledge becomes **tool doctrine**
(the tool description states the per-kind policy) plus a small set of **hard invariants the tool
enforces regardless of what the agent says**:

1. never modify a `pinned` fact (only re-flag it);
2. never delete a fact — supersede, retract, or close an interval;
3. a supersede must name the fact id it supersedes (the agent read it, so it has the id);
4. ordering uses validity time, never capture time.

Keep the chain (`superseded_by`, `valid_from/valid_to`, `status`) exactly as is: it is what makes
`me.weight` a time series and `me.address` an interval history, and it is what the wiki and the
agent's "what holds now" read. This is the largest judgment call in the doc — the loss is the
cheap safety nets (the low-confidence guard, the attribute-collision hold). Recommend keeping those
two as invariants 5 and 6 rather than as an engine.

### 5.3 Assertion status, pins, chain repair — keep unchanged
Six-value enum (`0006_analysis_schema.py:168-170`), the current-truth floor
(`CURRENT_ASSERTIONS`, `supersession.py:204`), `pinned` as the human override, and chain repair on
note deletion / re-analysis (`purge.py:43,138`) are all cheap, writer-agnostic, and load-bearing for
privacy ("delete = gone") and for the wiki ("rule out diabetes" is not a diabetes fact).

### 5.4 Entity resolution — keep the backstop, cut the guessing layers
Layered today (`entities.py:865-936`): first-person → `Me`; exact alias (one match auto-links, 2+ →
`AmbiguousEntity` card); a relationship hop for "my dentist"; then an embedding layer
(`_EMBED_STRONG=0.90`/`_EMBED_WEAK=0.78`, `entities.py:381-382`) and `NeedsDisambiguation` → a cheap
LLM call; else a provisional entity (`entities.py:642-664`). The agent already proposes resolutions
and the deterministic resolver is only the fallback (`pipeline.py:560-609`).

- **Keep** `_exact_matches` + the same-name collision gate (`entities.py:565-583`). ANALYSIS.md's
  strongest argument stands: re-analysis rebuilds mentions wholesale, so an LLM-resolved bare name
  would flip run to run — "the one outcome no layer may produce". The gate must therefore bind the
  agent too, exactly as `_resolve_from_intent` already withholds a same-name `existing` override.
- **Cut** the embedding candidate layer and `entity.disambiguate`: an agent with `find_entity`,
  `read_entity` and `neighborhood` (`agent/tools/*.tool`) does that retrieval with the note in
  context, which is more signal than a name-vector ever had.
- **Cut** `near_duplicate_entity` (`entities.py:482-520`) — a second embedding band for the same job.
- **Cut** the declared-name sniffing (`_NAMING_PREDICATES`/`_CANONICAL_NAMING`/`_NAME_VALUE_KEYS`,
  `entities.py:669-716`): a hand-maintained list of ten spellings and five value keys, kept "in
  lockstep" with the registry by comment. The agent calls `add_alias(entity, "Celine Kitina Hopkins")`.
- **Keep** `register_declared_alias`'s collision refusal (`entities.py:731-742`), `alias_owner`,
  `are_distinct` (`entities.py:772-786`), `plan_merge`'s more-anchored-side-wins
  (`entities.py:788-826`), and `merge_entity_pair`'s tombstone+repoint (`entities.py:828-862`).
  `distinct_from` matters *more* under an agent: it is the durable "we already decided these are
  different people" that stops the agent re-proposing a merge every time the name recurs.
- **Cut** `promote_if_corroborated` + `corroboration_count` + `confirm_entity` card
  (`canonical.py:168-266`, `pipeline.py:1331-1383`) — a 3-distinct-note counter, default OFF. Keep
  the `provisional|confirmed|merged` status (it gates purge survival and merge precedence) and let
  the agent set `confirmed` when it means it.

### 5.5 Denormalized projections
- **`canonical_name`**: keep the column, delete the projection. `reproject_canonical_name`
  (`canonical.py:88-160`) recomputes the display name from `name.*` facts through a per-type
  precedence, with an animal special case (`canonical.py:59-71`) and a refusal when another entity
  owns the name (`canonical.py:150-155`). All of that exists because the writer could not be asked
  "what is this thing called?". The agent can. Keep the *refusal* (don't claim a contested name) as
  a tool check.
- **`entities.summary`**: **verified dead.** No code path writes it — grep over `backend/src` and
  `backend/migrations` finds only reads (`entities.py:221,252,289,414,431,473`) and the embedding
  column. It is nonetheless the text the resolution vectors are built from
  (`entities.py:431`: `canonical_name; aliases; summary`). Have the agent write one line at entity
  creation and this dead column becomes the entity's search handle.

### 5.6 Nightly hygiene — who runs them if the arbiter is gone
**The arbiter never ran them; the workflow scheduler does** (`worker.py:842-849`,
`workflow/scheduler.py`), and it is untouched by this change.

| Sweep | Verdict |
|---|---|
| `entity_hygiene` (`hygiene.py:28-49` → `purge.sweep_orphaned_entities:272`) | **Keep unchanged.** Pure SQL, deletes provisional orphans stranded by retraction; ships disabled, Ops-fireable. *More* needed under an agent that can retract |
| merge proposals | **Not a sweep** — filed inline by the pipeline (`pipeline.py:1429-1510`). Becomes an agent judgment in-conversation, plus the §6 reconciler for cross-note cases |
| `reembed_stale` (`reembed.py:36`) | **Keep unchanged.** Embedding-model migration, writer-agnostic |
| `tag_consolidate` (`tagconsolidate.py:31`) | **Keep unchanged** |
| `consolidate_predicates` (`consolidation.py:132-163`) | **Dies with `renamed_from`** — it plans renames from the registry (`consolidation.py:86-94`). Its guarded `rewrite_predicate` survives as the reconciler's write primitive |

---

## 6. Drift control for a local 120b with no canonicalization pass

Three candidate mechanisms:

**A. Retrieval of existing vocabulary at write time.** Show the agent what the graph already uses —
for this entity, and for its kind — before it writes. Substrate exists: `graph_context.py` already
renders each candidate entity's active facts as `predicate → value` lines, identity predicates
first, capped (`graph_context.py:38-58`).

**B. A per-write canonicalization pass.** This is the thing that failed (§3) and it costs an embed
round-trip on the hot path. Do not rebuild it.

**C. A cheap post-hoc reconciler conversation.** Periodic, over aggregate usage, writing through the
guarded rename.

**Recommendation: A + C, weighted toward A, and never B.**

**A, done as a push not a pull.** `MODEL_PROMPTING.md:218-221` is explicit that gpt-oss "prefers
its own knowledge over tools" and needs a concrete trigger to call one; `:229-236` is explicit that
prompt-stated budgets need an engine backstop. So do **not** ship a `list_predicates` tool and hope
the agent consults it. Instead:

1. the write tool's *description* carries the ~30-spelling contract set (small, stable, and the only
   spellings a code path reads);
2. the **conversation's opening context** — the same block that already carries the graph context —
   carries `predicates already used on <kind>` with usage counts, from the vocabulary view (§7);
3. every successful write **echoes back** the entity's current predicate list in the tool result, so
   the second fact in a note is written against the first;
4. a near-miss write (the agent coins `treated_by` where `treatedBy` has 40 uses on this kind)
   returns a **soft advisory in the tool result** — "committed as `treated_by`; `treatedBy` (40 uses)
   is the established spelling; call `rename_predicate` if you meant it" — never a rejection.

This is the invariant restated as ergonomics: the name is never refused, but the agent is never
ignorant either. It also fixes the drift the *prompt digest* causes today — a hand-maintained list
in a `.prompt` file that must be CI-checked against a YAML (`ENTITY_GRAPH_REFOCUS_PLAN.md` §4)
becomes a query over the graph, which cannot drift from the graph by construction.

**C, weekly, one conversation.** Over `app.predicate_vocabulary`: "these spellings have ≤2 uses;
these have ≥5 on the same kind; propose merges." This succeeds where the embedding bands failed
because it sees exactly what §7 of `PREDICATE_CANONICALIZATION.md` named as the missing lever — the
entity kind, usage counts, and example statements — and because it makes one decision per candidate
*pair* with the owner reachable, rather than a per-fact decision on the ingest path. It writes
through `rewrite_predicate` (already guarded: never a pinned or retracted row, never onto an
occupied live address) and files a question card when unsure. Ride the existing engine: an
`ActionSpec` with `cost_class="expensive"` and `precondition="model_already_loaded"`
(`workflow/preconditions.py:46-60`) so a nightly sweep never evicts a model the owner is using —
which matters precisely because only one large model is resident at a time.

**Cost.** A per-kind vocabulary block is tens of lines against a 131,072-token context
(`MODEL_ACCESS_INVENTORY.md:291`); negligible. The reconciler is one long-context call per week.

**Residual risk to name: the firewall floor is spelling-keyed.** `_DOMAIN_BY_PREDICATE`
(`extraction.py:158-186`) floors `bloodglucose`/`treatedby`/`diagnosis` to health *by spelling*. A
free-vocabulary agent that writes `bg` or `sees_doctor` misses the floor. Keep the list as a
backstop, and add a spelling-independent net at the tool: a fact whose entity or object is of a
health/finance kind (medication, lab_result, medical_condition, financial_account) may not be
written `general`. Domain still ratchets up, never down.

---

## 7. The leanest schema that keeps the spine

Clean slate (DB is disposable). Unchanged tables are listed for completeness, not re-stated in full.

```sql
-- UNCHANGED from today: app.entities, app.entity_aliases, app.entity_mentions,
-- app.entity_distinctions, app.temporal_tokens  (0006_analysis_schema.py)
--   entities: same columns; what changes is the WRITER — the agent sets
--   canonical_name and summary directly; no projection engine, no promoter.

-- ============================ facts =====================================
CREATE TABLE app.facts (
    id                  uuid PRIMARY KEY,
    -- the graph address
    subject_id          uuid REFERENCES app.subjects(id),
    entity_id           uuid NOT NULL REFERENCES app.entities(id),
    predicate           text NOT NULL,              -- free text, never rejected
    qualifier           text NOT NULL DEFAULT '',
    object_entity_id    uuid REFERENCES app.entities(id),   -- part of the address (see below)

    kind                text NOT NULL CHECK (kind IN
                          ('event','measurement','state','attribute','preference','relationship')),
    statement           text NOT NULL,              -- embedded, cited, shown
    value_json          jsonb,                      -- the concise datum; shape checked at the TOOL
    assertion           text NOT NULL CHECK (assertion IN
                          ('asserted','negated','hypothetical','reported','question','expected')),

    valid_from          timestamptz,
    valid_to            timestamptz,
    reported_at         timestamptz NOT NULL,
    temporal_precision  text NOT NULL DEFAULT 'unknown'
                          CHECK (temporal_precision IN
                            ('instant','day','month','year','era','unknown')),
    temporal_token_id   uuid REFERENCES app.temporal_tokens(id),

    status              text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','superseded','pending_review','retracted')),
    pinned              boolean NOT NULL DEFAULT false,
    superseded_by       uuid REFERENCES app.facts(id),
    derived_from_fact_id uuid REFERENCES app.facts(id) ON DELETE CASCADE,

    note_id             uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
    chunk_id            uuid REFERENCES app.chunks(id) ON DELETE SET NULL,
    -- NEW: replaces extractor + prompt_version. Who wrote this edge, and in which
    -- conversation — the audit trail the arbiter trace used to carry.
    author              text NOT NULL CHECK (author IN ('agent','owner','projection')),
    run_id              uuid REFERENCES app.ingest_runs(id) ON DELETE SET NULL,
    confidence          real,
    domain_code         text NOT NULL REFERENCES app.domains(code),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX facts_identity_idx ON app.facts (entity_id, predicate, qualifier);
CREATE INDEX facts_object_idx   ON app.facts (object_entity_id) WHERE object_entity_id IS NOT NULL;
CREATE INDEX facts_note_idx     ON app.facts (note_id);
CREATE INDEX facts_pending_idx  ON app.facts (created_at) WHERE status = 'pending_review';

-- NEW, and the one genuinely new invariant: one LIVE head per graph address, in the
-- database rather than in Python. A double write is a conflict the tool reports to the
-- agent ("there is already a current value here — supersede it or pick another key"),
-- not a silent second head. app.address_object() returns NULL for the small functional
-- contract set (one current employer across ALL objects) and the object otherwise —
-- it is IMMUTABLE over a hardcoded array, i.e. jbrain.graph.contract in SQL form.
CREATE UNIQUE INDEX facts_live_head_idx ON app.facts (
    entity_id, predicate, qualifier, domain_code,
    coalesce(subject_id,        '00000000-0000-0000-0000-000000000000'::uuid),
    coalesce(app.address_object(predicate, object_entity_id),
                                '00000000-0000-0000-0000-000000000000'::uuid)
) WHERE status = 'active' AND valid_to IS NULL AND derived_from_fact_id IS NULL;

-- ======================= the ingest conversation ========================
-- Replaces app.note_analysis + the arbiter trace payload: one row per note-agent
-- conversation, so every fact is traceable to the turn that wrote it and a run is
-- replayable/auditable without log archaeology.
CREATE TABLE app.ingest_runs (
    id            uuid PRIMARY KEY,
    note_id       uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
    model         text NOT NULL,               -- e.g. local:gpt-oss-120b
    prompt_version text NOT NULL,
    status        text NOT NULL CHECK (status IN ('running','done','failed','asked')),
    transcript    jsonb NOT NULL DEFAULT '[]', -- turns + tool calls + tool results
    tool_calls    int  NOT NULL DEFAULT 0,     -- the engine-side budget backstop
    title         text,
    tags          text[] NOT NULL DEFAULT '{}',
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    domain_code   text NOT NULL REFERENCES app.domains(code)
);
CREATE INDEX ingest_runs_note_idx ON app.ingest_runs (note_id, started_at DESC);

-- ================== vocabulary, derived not declared =====================
-- The registry's replacement. Cannot drift from the graph: it IS the graph.
-- security_invoker so the firewall applies — a general-domain ingest session
-- cannot even see that health predicates exist.
CREATE VIEW app.predicate_vocabulary WITH (security_invoker = true) AS
SELECT e.kind,
       f.predicate,
       count(*)                                                   AS uses,
       max(f.reported_at)                                         AS last_used,
       (array_agg(f.statement ORDER BY f.reported_at DESC))[1]    AS example
FROM app.facts f
JOIN app.entities e ON e.id = f.entity_id
WHERE f.status IN ('active','pending_review')
  AND f.derived_from_fact_id IS NULL
GROUP BY 1, 2;

-- ============================ review inbox ==============================
-- Same table; the ingest-side kind list collapses to two. (Wiki kinds unchanged.)
--   'question'  — the agent is not confident and asks the owner (payload carries
--                 the proposed write + the alternatives it weighed)
--   'conflict'  — a tool refused a write (live head, contested name, same-name
--                 collision, pinned fact) and the agent could not resolve it
```

### Diff against today's tables

| Table | Change |
|---|---|
| `app.entities` | **No schema change.** Writer changes; `summary` goes from never-written to agent-written |
| `app.entity_aliases`, `app.entity_mentions`, `app.entity_distinctions`, `app.temporal_tokens` | **Unchanged** |
| `app.facts` | `extractor`,`prompt_version` → `author`,`run_id`; **new** `facts_live_head_idx` unique partial index; `facts_object_idx` (today only added by `0114_facts_object_entity_idx.py`) folded in |
| `app.note_analysis` | **Dropped** — folded into `ingest_runs` (title/tags/watermark) |
| `app.canonical_predicates` | **Dropped** (with its HNSW index and RLS policy) |
| `app.predicate_aliases` | **Dropped** (FK'd to the above; its owner decisions are re-expressed as one-off renames) |
| `app.review_items` | Kind CHECK trimmed: ingest kinds `fact_conflict, attribute_collision, ambiguous_mention, low_confidence, low_confidence_inference, new_predicate, shape_mismatch, confirm_entity, extraction_truncated, inverse_proposal, split_proposal` → `question, conflict`; `merge_proposal`, `domain_promotion` and the wiki kinds stay |
| `app.ingest_runs` | **New** |
| `app.predicate_vocabulary` | **New view** |
| `backend/src/jbrain/schema/**` (1,222 lines YAML + loader + models) | **Deleted**, replaced by ~40 lines of `jbrain.graph.contract` |

RLS: every new object carries the existing pattern — `ingest_runs` gets `has_domain_scope` plus the
mandatory isolation test (CLAUDE.md #3); the view needs `security_invoker=true` **and** a test
proving a `GENERAL_ONLY` session sees no health predicate rows (this is a new leak surface: a
predicate *name* like `hemoglobinA1c` is itself a disclosure).

---

## 8. Does a registry still make sense when the writer is an agent?

**The case for keeping it.** It is data, not code; it is the only home for `renamed_from`, enum
members and value shapes; it is loader-validated at boot so a malformation fails loudly
(`entity.md` §"The schema registry"); it gives the prompt a stable vocabulary to steer toward; and
a schema.org-anchored spelling is a *better* attractor than whatever the graph happens to contain
on day one (the cold-start problem: an empty graph has no vocabulary to retrieve).

**The case against.** (1) It is the sixth copy of the vocabulary (§2) and the only one no decision
reads. (2) Its two live consumers are a skip-check and a shape-check, both of which move into the
tool. (3) `display_name` is `[name]` in 20 of 24 types. (4) `renamed_from` (35 declarations) only
pays off through a nightly sweep that exists to heal a drift the agent can be shown out of. (5) A
YAML validated at boot cannot tell the agent *what the graph actually contains* — which is the
question that matters at write time, and the one the registry structurally cannot answer.

**Recommendation: retire the registry as an authority; keep a contract module and a derived
vocabulary.** Concretely: (a) the ~30 spellings a code path reads move into
`jbrain.graph.contract`, in the same file as their consumers; (b) the steering vocabulary comes from
`app.predicate_vocabulary` at write time; (c) the cold-start gap is closed by seeding the contract
module's spellings into the tool description (they are schema.org-anchored, so the model already
knows them) — not by a 121-predicate YAML. The registry's genuine insight survives: *canonical where
it matters, open everywhere, gated nowhere.* What changes is that "where it matters" shrinks from
121 predicates to the ~30 a projection, a firewall floor, a supersession rule or a display actually
reads, and "canonical" is enforced by showing, not by validating.

---

## 9. Verified vs assumed

**Verified by reading code/docs in this repo** (every `path:line` above): the two-tier flag and its
two live seams; the retirement of the embed bands and the card path; the surviving picker, tables
and legacy verbs; `_shape_check`'s three behaviours; the identity key's hidden object component;
the per-kind supersession policy and its hardcoded allowlists; the resolution layers and their
bands; that `entities.summary` is never written; that `display_name` is `[name]` in 20 of 24 types;
that the firewall floor, reciprocity map and projection spellings are hardcoded outside the
registry; that the tier-1 prompt digest is hand-authored; that the nightly sweeps are scheduler-run,
not arbiter-run; the `gpt-oss-120b` behaviours cited from `MODEL_PROMPTING.md`.

**Assumed / not verified.** (a) Real drift rates and predicate cardinality in the owner's live
graph — no DB access from here; the §6 reconciler thresholds (≤2 uses vs ≥5) are placeholders to be
set from one `SELECT` against `predicate_vocabulary`. (b) That `gpt-oss-120b` emits tool calls
reliably enough for a multi-write conversation per note — `MODEL_PROMPTING.md` documents step-cap
overruns and budget non-compliance, not tool-schema failures, so this needs a spike. (c) That
`security_invoker` views behave under this codebase's RLS helpers (`app.has_domain_scope`) — needs a
testcontainer check before it is relied on. (d) Whether Postgres will accept
`app.address_object()` as `IMMUTABLE` in a unique index in practice (it can, over a hardcoded
array, but changing the functional set then requires an index rebuild — a real operational cost on
a box the owner runs with no terminal).

---

## Open questions for the owner

1. **Supersession judgment.** Are you comfortable that "an event never auto-supersedes" and "two
   birthdays hold for review" become *tool doctrine the agent can be talked out of*, backed only by
   4–6 hard invariants (never touch a pin, never delete, name the superseded id, validity-time
   ordering)? Or should the per-kind conflict policy stay deterministic in the tool — which is a
   small arbiter by another name?
2. **One live head as a DB constraint.** Do you want the unique partial index (§7)? It converts a
   class of agent mistakes into visible tool errors, at the cost of a hardcoded functional set
   baked into an index expression that needs a migration to change.
3. **Cold start vs derived vocabulary.** Seed the tool description with ~30 schema.org spellings and
   let everything else come from the graph — or keep a slimmed YAML (say 40 predicates) as a
   permanent spelling reference?
4. **The reconciler's authority.** May the weekly reconciler *apply* a rename it is confident about
   (guarded, reversible, citations intact), or must every merge become a card you approve?
5. **`entities.summary`.** Should the agent write a one-line summary per entity (making entity
   vector search work for the first time), or is the entity's fact list enough and the column
   should go?
6. **Confirmed entities.** With the corroboration counter gone, who sets `confirmed` — the agent
   when it is sure, or only you? It controls whether an entity survives the deletion of its last
   source note.
7. **Firewall vs free vocabulary.** Accept that the spelling-keyed health/finance floor weakens
   under an agent-chosen vocabulary, mitigated by the kind-based net in §6? Or require that any fact
   on a health/finance-kind entity be written through a *separate, typed* tool?
8. **Predicate names as disclosures.** The vocabulary view makes predicate spellings queryable. Is
   `security_invoker` scoping sufficient, or should the vocabulary block handed to a general-domain
   conversation be an explicit allowlist?
9. **Owner-decided aliases already recorded.** `predicate_aliases` holds past decisions of yours. On
   a clean slate: replay them as one-off renames, or accept the loss?
