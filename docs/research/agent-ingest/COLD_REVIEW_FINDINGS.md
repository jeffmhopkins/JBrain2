# Agent-Conversation Ingestion — Cold Review Findings

> **Status:** Research · **Last verified:** 2026-09-08 — six independent reviewers against
> the ratified `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` (red team, fact-check,
> downstream consumers, tool surface, sequencing, frontend/data-model). Findings are
> recorded here; the plan has not yet been revised against them.

Every finding below is code-grounded. Ordered by what must be decided or fixed first.

## A — Findings that invalidate a ratified decision

### A1. D10 does not retire one invariant; it swaps the intake principal from a zero-tool stranger to the owner

Today intake content is processed by a principal that structurally cannot act:
`INTAKE_TOOLS = frozenset()` (`agent/agents.py:455`), and `intake_context()`
(`db/session.py:103-119`) carries `principal_kind="intake_link"` with no subject and no
domain scopes — so `is_owner()` is false and every `USING(app.is_owner())` table denies
it. `intake/materialize.py:9-18` sets domain, subject, kind and provenance from the
owner's link config, so *"a poisoned transcript can influence only the note TEXT."*

D1 ("same agent, loop, memory and session as chat") plus constraint 2's `owner_scoped=True`
replaces that. Migration `0015:13-15` is explicit that `owner_scoped` *"only restricts
domain data, never owner identity"* — `is_owner()` stays **true**. The curator profile is
the `allow=None` wildcard, which `toolregistry.py:114-149` resolves to every registered
tool except the `web` class and `NEVER_DEFAULT` — including the `sensitive` class.

The payoff tool is `file_correction` (`agent/wikiwritetools.py:34-61`): no proposal, no
confirmation, mints a note with `provenance="owner_correction"`, which
`pipeline.py:322` turns into `correction=True` — full weight, force-supersede, pin,
bypassing the pinned, irrealis and low-confidence guards in `decide()`. Its own docstring
names the gate being removed: *"These run only inside the owner's agent session … which is
the gate on these privileged writes."* Also in the wildcard: `add_source_exclusion` with
`article_id=None` plus `wiki_rebuild {"target":"all"}` — a one-call wiki denial of service.

**D8 is not containment.** D8 unlocks the full surface when the owner replies; D3 makes
replying the correction mechanism. So the attacker's optimal move is to make the first pass
write something visibly wrong, and the unlock is triggered by the attack being noticed.

**Not an egress risk.** `_admits` refuses the `web` class under `allow=None`
(`toolregistry.py:143-145`), so there is no straightforward exfiltration channel out of a
curator note conversation. The exposure is graph corruption and privileged local writes —
unless a later wave grants `extra_tools`, which `toolregistry.py:136-142` admits *ahead* of
the web gate.

### A2. `owner_prefs` breaks invariants #3, #5 and #6, and its gate is the one kind the plan itself calls not-a-gate

`remember.tool` already implements #3: *"This NEVER writes on its own … calling this stages
the change for the owner to approve rather than saving it."* The plan models `prefs_write`
on `archivist_memory_write` instead — a bare unconfirmed full replace
(`models/archivist.py:41-50`). The archivist model is safe because of two properties
`owner_prefs` does not have: it is `permission: web`, so the curator wildcard excludes it,
and the archivist reads no knowledge base and no untrusted note text.

D15's "fires only when you ask for it" is a **prompt** constraint. Constraint 9 states the
correct standard — *"enforced by the registry, not the prompt … not an instruction the
model can be talked out of"* — and D15 is the one write in the design that fails it. There
is no code-visible difference between an owner turn and a note body; both arrive as message
content in one window, and `loop.py:1695` folds tool results back with no provenance tag.

Modelled on `archivist_memory`, the table has no `domain_code` (`0094:26-40`), so
`owner_scoped` narrowing does not touch it: an injection landing in a **general** note
installs a standing instruction steering **health and finance** notes. That is invariant #5.

### A3. `pending_review` is a load-bearing clinical status, not only a review hold

`supersession.decide()` returns `insert_status="pending_review"` at twelve sites. The
sharpest: `_lab_status_transition` (`supersession.py:410,484`) maps FHIR `preliminary` →
`pending_review`, and `emr_projection.py:117,132,141-150` reads it back as
`lab_result.status="preliminary", is_current=False`. `repo.py:1110-1138 analyte_currency`
filters on it and `readtools.py:320-332` surfaces it to the agent.

So "nothing writes `pending_review` again" makes a preliminary lab reading land as
`active`/current — citable, and publishable by the wiki (`builder.py:529`). But if
`decide()` keeps writing it, as constraint 4 implies, the rows are held with **no card, no
queue and no resolver**: permanently inert, invisible to the wiki and to relationship
traversal (`entities.py:154,176,182`), with the owner seeing only a note that produced
nothing.

### A4. The review inbox is a shared bus with five non-ingest producers

D4 treats it as an ingest surface. It is also the only output for:

| Producer | Site | What is lost |
|---|---|---|
| EMR domain firewall (Layer 2) | `ingest/emr/firewall.py:47-48` | A location-lock predicate on a health EMR entity is held out and carded. Silently dropped without it — and this is a CLAUDE.md #3 firewall path, i.e. 100%-coverage |
| `wiki_lint` Wave B | `wiki/lint.py:536,671,773-799` | The corpus-wide contradiction/stale-claim sweep's only actionable output. Payloads carry **no `note_id`** — nothing for D4's redirect to target. Migration `0120` exists solely to admit these kinds |
| EMR import / intake / reconcile | `import_handler.py:283-300`, `intake_handler.py:172-192`, `integrate.py:55-86` | Every "no parser matched", "ZIP failed", "ambiguous draw" outcome |
| Entity promotion | `pipeline.py:1354-1382` | `confirm_entity` cards; `entity_id` only, no note, no thread |
| Predicate-canon executor | `agent/proposaltools.py:224-249` → `repo.py:1615-1710` | A registered **workflow-engine** Proposal leaf that enacts via `resolve_review` |

`X8-DOWNSTREAM-CONSUMERS.md:640` put this to the owner explicitly — *"the review inbox
should shrink, not disappear"* — and D4 answers without recording that it was considered.

Separately, `resolution.changed` is one of three workflow event types
(`workflow/events.py:51`), seeded as a trigger in migration `0040:56` bound to the
consolidate pipeline. Deleting the inbox orphans a seeded trigger and removes the only
driver of retroactive predicate consolidation.

The live kind list is **15**, not the 7 in `0006` (superseded through `0120`). Six redirect
cleanly to a note thread; three concern **two** notes (the conflicting fact comes from an
earlier note whose thread has no record of it); three have no note at all.

### A5. EMR Layer 2 has no floor backstop, and D9+D2+D4 removes all three of its legs

`ingest/emr/firewall.py:3-28` is explicit: this path has **NO domain-floor backstop**
(`domain_floor` only ratchets a *general* note), `address` and `geo` are **deliberately**
outside the floor dict, and the guard routes them to a card and **never commits**. D9 routes
EMR through the conversation, D2 retires holds, D4 deletes the card. Nothing keeps a
patient's home address out of `health`, where a health-scoped session reads it without
holding `location`. `EMR_IMPORT_PLAN.md` is absent from the docs-to-reconcile list.

## B — Constraints that are wrong as written

### B1. Constraint 2's mechanism is backwards: `fact.domain` is the primary escalation input

`pipeline.py:1945-1949`: `extracted_domain = fact.domain or note_domain`; the floor fires
**only** when the model already said `general`. Two consequences:

- *Escalation.* A model emitting `fact.domain="health"` from a general note ratchets for
  free. `commit_facts` must either honour a model-supplied domain — the model-facing
  escalation constraint 2 forbids — or drop it. The plan picks neither.
- *De-escalation, which is the leak.* `domain_floor` is a ~45-entry hand-curated allowlist
  whose docstring says *"unknown predicates fall back to the model"*
  (`extraction.py:155-186`). Under the ratified two-tier model a long-tail predicate
  commits raw (`pred_longtail_commits_raw.json`), and `dx`, `bpSystolic`, `glucoseLevel`,
  `therapist` are not in the list. A clinical fact declared `general` lands in `general`,
  wiki-publishable. The I5 net was the residual cover, and `arbiter.py:157-160` says so:
  *"the asymmetric-caution net that replaces the ceiling's incidental firewall coverage."*
- *Unverifiable writes.* Where the floor does fire, the narrowed conversation cannot SELECT
  the row it just wrote. The chip renders a write nobody in that session can verify.

### B2. Constraint 2 also breaks entity resolution — narrowing does **not** match the existing ratchet

`_exact_matches` (`entities.py:563-580`) has **no domain predicate**. It is layer 1 of
`resolve_entity` and today sees every domain under `SYSTEM_CTX`. Under `(dom,'general')`: a
name matching only in a third domain misses and mints a duplicate; `AmbiguousEntity` and
`same_name_entity_ids` under-count, so an ambiguity that files a card today auto-links;
`record_alias`'s collision guard and `alias_owner` lose cross-domain signal; graph context
shrinks. Only layers 2–3 carry the ratchet. Also: `integrate_note` **is** stamped on the
main path — `SYSTEM_CTX` comes from `pipeline.py:311`, where the handler opens its own
session, not from the stamp.

### B3. Constraints 1 and 2 are mutually unsatisfiable, and constraint 1's premise is already solved

`app.chunks` carries `WITH CHECK (app.has_domain_scope(domain_code))` (`0003:99-102`), so a
`(general,'general')` session **cannot insert a health chunk**; the plan's escalation
carve-out covers fact writes only. And the requirement is unnecessary: `_citation_chunk`
(`pipeline.py:1645-1687`) already get-or-creates a `source_kind='derived'` same-domain copy
when a fact ratchets — that is what satisfies the `0046` trigger today. Worse, chunking a
clarification block *only* in the fact's domain would make it **unsearchable**: derived
chunks carry no embedding and `search/repo.py:27` excludes them.

Correct rule: chunk normally in the note's captured domain; let `_citation_chunk` mint the
derived copy. The plan must still name the derived-chunk INSERT as needing the same
escalation the floored fact write gets.

### B4. The end-of-turn sweep is a data-loss primitive, and cannot be per-turn

Two independent failures:

- *Per-turn is wrong.* Turn 2 of a conversation has a `touched` set of one fact, so the
  sweep retracts everything turn 1 committed. `touched` must accumulate across the whole
  conversation, which makes it **durable per-turn state** — a schema decision landing in W1.
- *Partial turns destroy data.* `loop.py:131-133` sets `max_steps=20` and
  `max_consecutive_tool_errors=3`, and `loop.py:905-911` returns those as ordinary stop
  reasons. A turn that ends partway has asserted a *prefix*; the sweep then retracts
  everything it did not reach. On a re-run of an already-analysed note that is silent
  destruction of correct data — and Risk 2 accepts exactly the unreliable tool-calling that
  triggers it.

Related: `_rebuild_mentions` (`pipeline.py:1278`) is `DELETE … WHERE note_id` then
re-insert. Called per tool call it wipes what the previous call wrote; it needs an
incremental upsert keyed on (note, chunk, span).

### B5. The `merge_entity_pair` remedy does not work under constraint 2

The `UPDATE`s (`entities.py:836-859`) are already implicitly scoped — RLS filters them. The
problem is that `RETURNING` returns only rows the session can **see**, so a narrowed session
cannot count what it left behind. "Fail loudly on rows left behind" requires reading outside
scope — the escalation the same sentence forbids. And the tombstone `UPDATE` runs *first*,
so it can affect zero rows while the repoints partially succeed.

### B6. "Moves to `SECURITY DEFINER`" is this repo's idiom for *bypassing* RLS

`0045:203` — *"SECURITY DEFINER so the section lookup **bypasses RLS**"*; `0046:183-188`
likewise. A `SECURITY DEFINER` delete over `facts`/`entities` is an RLS-bypassing
destructive primitive, safe only if it re-asserts the domain predicate internally and is
unreachable from any model-facing tool. The plan states neither. Also, the DELETE grant
covers **seven** tables — `0006:260-261` already granted `entity_aliases` and
`entity_mentions`.

## C — Sequencing

- **W2 cannot precede W3.** 116 `.tool` files contain no graph-write verb, so with D8 a W2
  first pass runs with an empty tool surface. W2 would display the agent reading a note and
  writing nothing — and D13's retreat is then *worse* than today: a visible thread that
  demonstrably does not explain the graph, while the old pipeline writes out of band.
- **The rebuild sweep belongs in W1.** It is ~80% composition of shipped parts
  (`purge_note_artifacts` + `backfill_pending_integration`, already resumable and
  Ops-fireable) — task-sized. In W4 it breaks three of its four stated jobs: an acceptance
  instrument arriving with the cutover has validated nothing, a rollback lever shipping with
  what it rolls back is not a lever, and D15 puts `owner_prefs`' re-run affordance in W3
  depending on W4 — circular.
- **Three unflagged hazards in that sweep.** `purge_note_artifacts` deletes facts with **no
  `pinned` filter** (`purge.py:96`) — correct for a privacy delete, catastrophic for a
  rebuild, and a surviving pinned fact then re-collides with its re-derived twin, filing a
  collision card per pinned fact. It deletes review items with no status filter, destroying
  resolved human history. And `wiki_citations.fact_id` is `ON DELETE SET NULL` while
  `wiki_articles.entity_ref` is a soft ref with no FK, so a rebuild silently degrades every
  published revision to chunk-only claims and orphans articles to dead ids. A graph rebuild
  must chain into a wiki rebuild.
- **W5 contradicts constraint 4.** `decide()` returns `pending_review` paired with a
  `review_kind` at eight sites for deterministic conflicts. The `review_items` table and the
  `pending_review` status must **survive** W5; only the screen and the arbiter-derived kinds
  can go.
- **W5's "never split deletion from replacement" contradicts D13**, which mandates keeping
  the old chain alive across three waves. Rewrite it as the per-PR rule it means. And
  decompose: the `SECURITY DEFINER` + grant revoke is a security-path change needing its own
  RLS isolation test, and cannot ride a 3,000-line deletion.
- **Five task-sized items sit off the critical path** and are currently pinned behind waves
  that do not gate them: the rebuild sweep, the `merge_entity_pair` fix, the settle-clause
  fix, `owner_prefs` storage, and the harness runner re-point.

## D — The harness and eval claims are wrong in both directions

- **Smaller than stated.** Only **1 of 75** scenarios authors an intent
  (`rel_conjoined_past_employers.json`); the runner synthesizes the other 74 via
  `_compile_intent` (`runner.py:86-158`). The input-half job is one function, not 75 files.
- **Larger than stated.** "The `expect` half is untouched" is false: **51 of 75** assert on
  `review_items`; **9** assert a card exists and will fail; **47** assert `count: 0` and
  become **vacuously true** — they stop testing anything and never go red, which is worse
  than breaking. **8** assert `pending_review` fact statuses. The checker's `reviews`
  snapshot leg (`scenario.py:122,165,183-199`) and the eval assertions
  (`tests/eval/assertions.py:161,307-381`) need re-pointing too.
- **The one adversarial scenario becomes a tautology.**
  `adv_prompt_injection_body_inert.json` says in its own description that the harness *is*
  the model and the scripted extraction *"deliberately does NOT comply."* Re-authored as
  scripted tool calls it scripts a benign sequence and asserts a benign graph — proving
  nothing about the risk D10 introduces. W3 needs a **new** scenario running a real model
  with the real write registry against a hostile body.
- **The eval corpora are unlisted.** `evals/integrate_runner.py` drives the real
  `integrate.note` prompt and scores `IntegrationIntent` against golds; under a tool loop
  both prompts and `INTENT_SCHEMA` / `intent_parse.py` are superseded, invalidating
  `integrate_cases/`, `cases/` (325) and `disambiguate_cases/`.

## E — D6's clarification blocks are a destroy-and-rebuild, not an append

The append is cheap to *store* — into `notes.body`, where offsets stay valid in a way a body
edit never achieves. Everything downstream is not:

1. `ingest/pipeline.py:109` deletes **every** chunk of the note and rebuilds with fresh ids.
2. `chunker.py:19-21` — a short block merges into the preceding paragraph and re-windows
   every section chunk, so there is no stable chunk to hang a citation on.
3. `facts.chunk_id` is `ON DELETE SET NULL` (`0006:187`) → every fact of the note goes NULL.
4. The `refresh_id` path (`pipeline.py:2048-2058`) updates statement/confidence but **never
   re-links `chunk_id`** — its comment claiming "citations survive" is already false today.
5. `wiki/builder.py` INNER JOINs chunks, so those facts vanish from their articles. The
   article *does* rebuild via the mention cascade — without them. **Fail-silent.**
6. `wiki_citations.chunk_id` is `ON DELETE CASCADE`, so published revisions lose their
   citations between re-ingest and the next refresh.
7. `embed_note` re-embeds the whole note; the mention cascade re-dirties every mentioned
   entity, queueing an LLM article rebuild each.

This is a real bug today on manual edits; **D6 makes it fire on every answered question**, on
exactly the notes with the most citations, while the owner sits in the thread waiting. The
fix (re-anchor `chunk_id` on the refresh path; make the builder's join loud) belongs in W1.

Also: `DESIGN.md:697` is settled binding text — *"saving PATCHes the body and re-triggers
ingestion"* — so "frozen" is a convention with nothing enforcing it unless the editor is
retired. And no mock shows a note body with clarification blocks, so DESIGN.md rule 2 (no
reuse exemption) is not satisfied.

## F — Smaller corrections

- **The abliterated checkpoint is not the live vision route.** `router.py:58-59,181` default
  `vision.ocr`, `vision.caption` and `agent.vision` to `xai:grok-4.3`, and the catalog labels
  the model *"a RED-TEAM PROBE … not as a worker model"* (`local_catalog.py:814,868-870`).
  Accurate framing: *selectable* for the vision route, and inverting the boundary if
  selected. The sharper risk the plan misses is that the same entry is selectable for
  **`agent.turn`**.
- **The no-`enum` rule is narrower than stated.** `STRIX_HALO_SETUP.md:592-601` scopes it to
  gpt-oss's harmony path and the enum × full-optional-field interaction; *"Cloud models and
  Qwen are unaffected."* Sound as a `.tool`-authoring rule, wrong as a blanket claim.
- **`read_note` returns note bodies unframed** (`readtools.py:799-811`), which is safe today
  only because bodies are owner-authored. `intake/turn.py:29-38`'s `_RECIPIENT_FRAME` is the
  correct pattern and is what D10 discards.
- **The hygiene sweep does not hard-delete never-promoted entities.** Its criterion is
  orphanhood (`purge.py:240-260`) plus an age guard, and its schedule ships disabled;
  `_promote_corroborated` is gated off by default (`ENTITY_PROMOTION_DEFAULT = False`). The
  projections argument for W1 stands; this justification does not.
- **Three projections plus a device binding**, not "four typed projections" — and
  `device_binding` has a second caller (`locations/geofence.py:275-282`), so it is partly
  self-healing. W1's list also omits `_reproject_entities` → `reproject_canonical_name`,
  `_register_declared_aliases`, `repair_chains`, `_sweep_stale_ambiguous`,
  `_sync_truncation_review`, `_upsert_tokens`, `_materialize_inverse` and
  `_propagate_supersession_to_shadows`.
- **Chunks *are* hard-deleted on note delete**, so `chunk_id` cascades do fire; only the
  note-row FKs do not.
- **Every new table needs an RLS isolation test** (CLAUDE.md #3). The word "RLS" does not
  appear in the plan, and W2/W3 add four tables.
- **`file_correction` is the wiki's correction lever.** `PHASE6_WIKI_PLAN.md:255-261,332-334`
  names `plan_intent(correction=True)` as the shipped exit criterion for the wiki correction
  loop, and `wiki/lint.py:790` offers "File correction note" as a card action. D11 retires
  the path without porting it; `PHASE6_WIKI_PLAN.md` is not on the reconcile list.
- **`save_place` and `manage_appointment` compose engineered prose** to drive the extractor
  into a specific predicate shape (`locationtools.py:520-540`,
  `appointmenttools.py:149-224`). Read conversationally there is no guarantee the shape
  lands, and a wrong shape does not error — `_geometry()` returns `None` and the fence
  silently disappears. Both also create the recursion D1 does not address: a tool call
  produces a note, which opens another conversation.
- **Docs unlisted:** `PHASE6_WIKI_PLAN.md`, `EMR_IMPORT_PLAN.md`, `SERVICES.md:218`,
  `docs/mocks/silent-queue/`, and migrations `0040` (seeded trigger), `0024`, `0118`, `0120`.
