# Note-Conversation Persona — Proposed Tool Surface

> **Status:** Research · **Last verified:** 2026-09-09 — an independent design pass for
> `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` W3. **The two write tools are now BUILT
> to this document** (`backend/src/jbrain/agent/graphwritetools.py`, W3/T2a): batched,
> `quote` required and checked, no `domain`/`inferred`/`supersedes` field, no `enum`, a
> per-element savepoint, failures as text, and the result shapes below implemented as the
> ACI. Three corrections this document owes, found once there was code against the real
> schema — the design is unchanged, the details were wrong:
>
> 1. **`assert_fact` has six fields, not four.** `statement` and `when` are required
>    alongside subject/predicate/object/quote. The `statement` is what the wiki and the
>    review cards print (a synthesized one reads as a row of fields), and without `when`
>    no dated fact is expressible at all — the appointments projection and every interval
>    depend on it. Both are REQUIRED with an explicit empty-string escape for `when`,
>    since R3 says an optional field is never filled. This is two more fields than the
>    probe measured; the degradation path (drop `when`, synthesize the statement) is a
>    sidecar plus handler change, as gap 4 anticipated.
> 2. **"`domain_floor` runs first regardless" was not true for the spelling this persona
>    writes.** The floor's table is keyed on the camelCase the note.extract prompt
>    teaches; `assert_fact`'s natural spelling is snake_case, and `blood_pressure` missed
>    the table entirely. The lookup is now separator- and case-insensitive with a
>    dotted-base fallback. Without that fix, D18's "the agent chooses the domain for novel
>    predicates" would silently have extended to *registered* ones.
> 3. **`resolve_entity` is not free of the resolver's own vocabulary.** A handle is this
>    module's addressing and must never reach `_resolve_entities`, which resolves — and
>    failing that MINTS — on `mention.name`: an `e1` ref creates an entity called "e1".
>    Refs are surfaces; handles are the model's shorthand for them.

The complete proposed tool list for the persona described in the plan: the agent that
reads a note, writes the entity/fact graph through tools, and shows the owner what it
did. Grounded in the shipped `.tool` sidecar format, the existing write path, and
`INTENT_SCHEMA` parity.

## The three rules that shaped the list

**R1 — Every model-facing verb is additive.** The persona can add to the graph and
replace a value with a newer one. Retraction is not a verb; it is the engine's settle
sweep. "That's simply false" is already expressible as `assertion: "negated"`
(`extraction.py:24`). This is stricter than `B3-GRAPH-TOOLS.md`, which proposed a
`retract_fact` — that was written before D4 removed the review inbox, and a retracted
head with no inbox is invisible on a box the owner cannot shell into.

**R2 — Dangerous authority is withheld by not binding the handler, never by prompt**
(plan constraint 9). The unattended/on-reply split is two `frozenset`s in `agents.py`
shaped like `JERV_TOOLS` / `ARCHIVIST_TOOLS`, not one set with flags.

**R3 — Required fields are the only reliable fields.** In-repo evidence: across 85
consecutive `scratch_write` v2 calls, gpt-oss-120b filled the required `filename` every
time and the optional `content` never once. llama.cpp compiles `required` into the tool
grammar. Anything load-bearing is `required` even when the call reads awkwardly.

## The list

| Tool | Set | permission | side_effecting | Batch | Purpose |
| --- | --- | --- | --- | --- | --- |
| `resolve_entity` | unattended | mutate | yes | ≤12 | surfaces → run handles; the only minting path; writes the mention spine |
| `assert_fact` | unattended | mutate | yes | ≤8 | the one fact-writing verb |
| `ask_owner` | unattended | mutate | yes | — | record an open question on this thread and stop |
| `find_entity` | unattended | read | no | — | *inherited unchanged* |
| `read_entity` | unattended | read | no | — | *inherited unchanged* |
| `prefs_read` | unattended | read | no | — | standing instructions (**weakest tool — see Cuts**) |
| `correct_fact` | on-reply | sensitive | yes | — | the owner disagrees: force-supersede + pin |
| `merge_entities` | on-reply | sensitive | yes | — | fold a duplicate; direction server-chosen |
| `prefs_write` | on-reply | sensitive | yes | — | delta-edit a standing rule, on owner request only |
| `search`, `read_note`, `relate` | on-reply | read | no | — | *inherited unchanged* |
| `current_time` | both | read | no | — | *inherited unchanged* |
| `note_mentions` | conditional | mutate | yes | ≤16 | only if the deterministic layers prove insufficient |

Six tools unattended, three of which write. `ARCHIVIST_TOOLS` is 12 and works on this
model; `JERV_TOOLS` is ~45 and is where reliability visibly degrades.

## Schema decisions worth defending

- **`assert_fact.quote` is required**, even though inferred facts commit under D2.
  Optional is the `scratch_write` v2 failure re-run: the model would omit it universally
  and every fact would land inferred and weight-capped. Required plus "quote the passage
  the inference rests on" buys the grammar's reliability while `compute_signals`
  (`arbiter.py:617-670`) still decides server-side whether the quote actually attests.
  The model cannot claim attestation; it can only offer text.
- **No `domain` field, ever** (the firewall red-team rule, `arbiter.py:163-165`). A
  `sensitive` field is a strictly-upward dial into `ratchet_domain`; `domain_floor` runs
  first regardless.
- **No `inferred`, no `supersedes`, no `correction`** on `assert_fact` — recomputed,
  owned by `decide()`, and withheld to the on-reply tool respectively.
- **No enum anywhere.** Enumerated values live in descriptions and are validated in
  handlers (`STRIX_HALO_SETUP.md:592-601`).
- **`correct_fact` addresses by identity key** `(entity, predicate, qualifier)`, not by
  fact id — `read_entity`'s `_edge_line` prints no fact id (`readtools.py:596-604`), so
  id-addressing would force a `read_entity` v5 and a second addressing vocabulary. On a
  multi-row key the handler mints `f1`/`f2` handles and the model retries.
- **A batch never rolls back its successful elements.** Per-element savepoint; element 4
  failing must not undo 1–3, or whole-note atomicity returns through the side door.

## Result shapes are the ACI

Every handler reports failures as text rather than raising — `loop.py:1677-1693` turns an
exception into a generic "hit an internal error" the model learns nothing from. Results
name what the server did unasked, because that is the model's only window into `decide()`:

```
1 ok   Kaiya.treatedBy → Dr. Patel (already recorded; refreshed)
3 ok   Me.homeLocation → 412 Oak St — replaced 118 Pine Ave, kept as history
5 held Me.bodyWeight 178 lb clashes with 182 lb at the same time — recorded but not
       live. ask_owner which is right.
6 err  facts[5].object "e9": no such handle. resolve_entity first.
assert_fact: 4 calls left this note
```

`held` is not a confidence gate — those are gone. It is only what `decide()` itself
returns as unresolvable: `fact_conflict` / `attribute_collision`, or a pinned head.

## Verbs deliberately NOT tools

`supersede_fact` (inside `assert_fact`), any delete (the settle sweep), `finish`/`settle`
(engine hook — gpt-oss does not honour protocol obligations stated in prose), the D3 chip
(a render of a persisted call), the D6 clarification block (engine, on every owner turn),
and a predicate-lookup tool (the tier-1 spellings ride `assert_fact`'s description as a
CI-checked digest; a lookup is a round trip on a one-slot box to answer what the
description already answers).

## Gaps this surface exposes

Each is something the current pipeline does that no proposed tool covers:

1. **Nothing writes note title and tags.** `_apply` writes them into `note_analysis`
   (`pipeline.py:955-975`), and that row's existence is the PWA's "analyzed" watermark
   (`models/notes.py:82-87`). Recommended: one cheap constrained-JSON call at settle (the
   `_disambiguate` pattern) — no tools in that call, so `json_schema` + `strict` + the
   re-ask all still work. A `note_summary` tool is the alternative and is worse.
2. **Nothing writes `distinct_from`.** `are_distinct` (`entities.py:772`) is what stops
   the same disambiguation question re-asking on every re-analysis. The engine's
   answer-handler must write it when the owner answers a merge question negatively.
3. **`fhir_status` is not expressible.** It is EMR-only, set by the importer. Under D9
   the EMR import goes *through* the conversation, so the importer must reach W1's
   `commit_facts` directly rather than via the model — otherwise lab-status transitions
   (`_lab_status_transition`, `supersession.py:537`) silently stop working.
4. **`resolved_end` and `precision` are dropped as model fields.** Precision derives from
   the ISO shape; an end date normally arrives as `_interval_close` from a later note. A
   bounded interval stated in one sentence ("we lived there 2019 to 2023") now needs the
   closing note. If it bites, add `when_end` as a seventh flat scalar, never a nested
   object.
5. **Structured value shapes** (`postal_address`, `geo`) are reachable only through
   `statement` + shape recovery. A deliberate narrowing: the model is never asked to nest.

## Registry enforcement — three things that will bite

- **Every new write tool must be added to `NEVER_DEFAULT`** (`toolregistry.py:34-41`).
  Only `web` and `NEVER_DEFAULT` are excluded from the `allow=None` wildcard, so a
  `mutate`-classed graph-write tool would otherwise be handed to the **curator on every
  ordinary chat turn**.
- **`permission` is documentation, not a gate.** `outcome_for` / `DEFAULT_OWNER_POLICY`
  (`agent/session.py:113`) is defined and unit-tested but never called by the loop;
  staging happens inside individual handlers. The allowlist and `NEVER_DEFAULT` are the
  real mechanisms.
- **`maxItems` may not be honoured on the tool path.** It is validated on-box for the
  `json_schema` path (`intent_parse.py:48-58`), but llama.cpp builds its own grammar for
  tool calling and the two do not compose. Handlers must clamp and report.

Budgets are engine-side via `ToolCallBudget` (`loop.py:224-247`) with the remaining count
appended to every result — a prompt-stated cap will not hold, which is the deep-research
scout's in-repo lesson.

## Never bind, either set

- **`propose_correction`, `file_correction`, `save_place`, `propose_merge`, `remember`,
  `make_intake_link`** — all stage Proposals into the inbox D4 deletes. Worse,
  `propose_correction` and `file_correction` **write a note that re-enters ingestion**: a
  note body containing "record that…" would launder itself into an owner-attributed
  source note and a second conversation.
- **All outward-facing tools** (`web_*`, `news_*`, connectors, `gmail_*`) — D8 unattended,
  and they must stay out of the **on-reply** set too: the owner replying does not sanitize
  the note body, which is still in context. Untrusted content + private data + egress is
  the complete trifecta.
- **`analyze_image` / `analyze_video` / `analyze_stream`** — the abliterated-checkpoint
  caveat (`local_catalog.py:869-878`) in a persona that now holds write tools. Attachment
  OCR/caption stays a pre-tool product feeding chunks.
- **`neighborhood`** — retrieval sprawl, and its schema is the exact GBNF-segfault shape:
  an `enum` on an object with **zero required and four optional** properties
  (`agent/tools/neighborhood.tool`). The grammar is built over the whole tool union
  offered that turn, so admitting it puts that shape into this persona's union. Needs a v2
  with the enum in the description before it could be offered even on-reply.
- **`memory_edit` / `memory_read`** — `owner_prefs` is this persona's standing-instruction
  surface; two memory surfaces with overlapping meaning is the contradiction gpt-oss
  handles worst.

## Cuts, in the order they should be made

1. **`prefs_read`** — D15 already injects the document into the prompt, and `prefs_write`'s
   delta ops remove the read-then-merge need. Have `prefs_write` echo the numbered list in
   every result and refusal instead. Unattended set drops to five.
2. **`note_mentions`** — ship two deterministic layers first (`resolve_entity` writes a
   mention for every surface it locates; a settle-time alias scan over the handle table
   and turn-0 seeds catches pre-seeded entities the model never resolved). Add the tool
   only if measurement shows *referential* surfaces ("the dog", "she") degrading the spine.
3. **`assert_fact.confidence`** — only ever lowers a ceiling.
4. **`assert_fact.sensitive`** — `domain_floor` covers the known predicates already.
5. **The batch shape itself — MEASURED 2026-09-09, and the risk did not survive.**
   It was the largest unmeasured reliability risk here: gpt-oss fills flat scalar args
   reliably, and arrays-of-objects had never been tried on this box. Probed through
   `/tool-probe` against the live `agent.turn` route (gpt-oss-120b, reasoning low), 20
   samples per shape on one note:

   | shape | well-formed | items per turn |
   | --- | --- | --- |
   | `assert_fact` batched array-of-objects, 4 string fields | **20/20** | 7.6 |
   | `assert_fact` flat scalar, one fact per call | 20/20 | **1.0** |
   | `resolve_entity` batched array-of-objects, 2 fields | **20/20** | 8.9 |
   | `resolve_entity` batched array of plain strings | 20/20 | 8.4 |

   Well-formed here means every item carried all its required non-blank string fields
   *and* every `quote` was a verbatim substring of the note — 80/80 across all four
   shapes, no truncation, no invented field, no non-verbatim quote. **Ship batched.**
   The flat fallback is not a safety net worth holding open: it is equally well-formed
   and yields exactly one fact per turn, so the same note costs seven or eight round
   trips instead of one, on a serial GPU, while the owner waits (D14 risk 4).

   What this does **not** measure, and W3 still owes: a single-turn probe sees only the
   model's *first* call, and with the full six-tool set attached that first call is
   always `resolve_entity` — so `assert_fact`'s behaviour in the real loop, after
   resolution results come back, is unobserved. `/debug/replay` now takes inline
   `raw_tools` for exactly this, and is the instrument to use once W3's tools exist.
   Longer and messier notes, and the ≤8/≤12 ceilings under a note that overflows them,
   are also unprobed. Still: design the handlers so degradation stays a sidecar change.
6. **`ask_owner` calibration** is the behaviour to instrument in W3. Over-asking ends every
   note `awaiting_owner` and the sweep never runs; under-asking commits wrong links. The
   lever is description text, not schema.

**Not cuttable under any pressure:** `assert_fact.quote` staying required, and `inferred` /
`cross_subject` / `domain` staying out of the schema. Those four are what keep the model
supplying meaning and the engine supplying mechanics.
