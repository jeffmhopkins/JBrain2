# JBrain2 — Entity Graph Refocus Plan (spine, not encyclopedia)

> **Status (plan doc only — no code lands from this file).** Owner-ratified
> direction, grounded by six scoped codebase researchers (two-tier split,
> prompting, traversal, relatedTo, review-inbox impact, eval fallout). Waves per
> `docs/PROCESS.md`: one PR per wave, per-task + per-wave adversarial review,
> CI green before merge. **No GUI surface changes anywhere in this plan — the
> three-mock GUI gate never trips.** No new tables (one optional pure-index
> migration), no new dependencies (`dev-setup.sh` untouched). Open owner
> decisions collected in §8 with recommended defaults — but PROCESS.md names
> "plan open decisions" as owner-escalation items, so §8 is presented for
> **explicit owner ratification in one pass at plan sign-off, before the
> Wave 1 branch is cut**. Wave 1 hard-codes decisions 1, 2, 3, 6, and 9;
> they cannot be treated as pre-approved defaults, only as recommendations
> pending that ratification.

## Thesis

JBrain2's graph has drifted maximalist. The evidence:

1. **The extraction prompt optimizes for completeness.**
   `analysis/prompts/note_extract.prompt` (v28, ~28KB) opens with "CAPTURE
   EVERYTHING THE NOTE STATES", carries ~8 MUST-emit rule blocks, elaborate
   soft-fact (preference/goal/task) datum machinery, and a `min_facts: 12`
   floor on the per-note fact budget (`prompt.py:36-47`).
2. **The predicate vocabulary cannot converge.** Per
   `docs/PREDICATE_CANONICALIZATION.md` §5a, embedding calibration showed true
   drift spellings land at 0.57–0.72 cosine — overlapping novel predicates —
   so the STRONG auto-merge band (0.90, `analysis/predicates.py:38`)
   effectively never fires and **every** unknown predicate files a
   `new_predicate` review card (one open card per distinct raw spelling,
   `pipeline.py:820-831`). Review noise, forever.

The refocus: entities/places/objects are **navigation tools** and **arbiters of
current truth for a small set of root facts** — not an attempt to capture all
knowledge as facts. Notes stay the store of rich knowledge (fully searchable);
the graph is the spine for edge-following ("pull all notes within 3 connections
of this person"). Three moves, three waves: a two-tier predicate model, a
salience-first prompt rewrite, and an n-hop neighborhood agent tool.

## 0. What this plan does NOT do

- **No corpus re-run.** Re-extraction stays per-note on-demand
  (`POST /api/notes/{id}/analyze`); v28 facts persist on untouched notes,
  stamped and auditable. When a note IS edited/re-analyzed, the existing sweep
  (`pipeline.py:921-978`) quietly retracts facts the leaner extraction no
  longer emits — **this is intended behavior**, chain repair and pinned-fact
  survival included, and the owner should expect it.
- **Notes remain the sole sources of truth**; the wiki stays machine-written.
- **No controlled-ontology gate.** The vocabulary invariant from
  `docs/entity.md` holds verbatim: *"Storage accepts any predicate. Shape
  validation may reject a malformed `value_json`; predicate-name validation
  may never reject anything."* Tier-2 is *less* processing, never a rejection.
- **No demotion of long-tail facts to prose-only rows.** Tier-2 facts keep
  their fact rows and today's per-kind supersession defaults (harness
  scenarios like `hist_preference_retrospective_still_supersedes` stay valid).
- **No GUI work.** Review cards simply stop arriving; `NewPredicateCard`
  keeps rendering legacy/decided cards via the existing block registry.
- **`relatedTo`/`about` is deferred** (§6) — build only against observed
  traversal gaps.

## 1. The two-tier predicate model

**The tier flag is registry declaration — no new field.**
`SchemaRegistry.declares_predicate` (`schema/models.py:168`) is already
consulted at every decision seam: canonicalization skip (`pipeline.py:743`),
shape-check guard (`pipeline.py:1900`), `predicate_known` weight signal
(`arbiter.py:574`). Declared-in-registry IS tier-1. Demotion = deletion from
the type YAMLs. The dead per-type `allow_open_predicates` field (loaded at
`loader.py:285`, consumed nowhere) is removed rather than overloaded.

**Tier-1 criterion.** A predicate stays declared iff it earns ≥1 of:
functional supersession (truth arbiter) · firewall domain floor · display/alias
projection · reciprocity (symmetric/inverse pair) · ref-edge traversal · a
typed projection (ICS appointment, geofence, device binding).

**Tier-2 semantics.** Any undeclared predicate: stored raw (the invariant),
searchable, traversable via its entity edges, **no embed round-trip, no
`new_predicate` card, no unknown-predicate weight penalty**. The cheap durable
`predicate_aliases` collapse (honoring past owner decisions) is kept.

**The tier-1 set** (~90–95 predicates survive; ~30–40 demote). Facets kept
whole: Named, Temporal, Recurrence, Located, Monetary, ExternalIdentified,
Lifecycle, Related, Prioritized; Contactable keeps email/telephone, demotes
`url`. Per type:

| Type | Keep (tier-1) | Demote (tier-2) |
|---|---|---|
| person | birthDate, deathDate, gender, spouse, parent, children, sibling, relative, friend, colleague, worksFor, owns, homeLocation, birthPlace, **treatedBy (promoted — see below)** | knowsLanguage, nationality, weight*, height*, goal, siblingCount |
| appointment | all (scheduledTime is the owner's "appointment time binding"; rest feed ICS) | — |
| place | all (geofence projection, containedInPlace traversal) | — |
| role | all (the reified edge — traversal-critical) | — |
| medication / lab_result | all (health firewall floors + typed parsers) | — |
| animal | species, birthDate, deathDate | breed, sex, color |
| organization | parentOrganization, location | foundingDate, numberOfEmployees |
| vehicle | manufacturer, model, modelDate (display composition), licensePlate | mileage, fuelType, bodyType, color* |
| device | deviceType, lastLocation, operatedBy | manufacturer, model |
| product | location (lock-marked presence-revealing — firewall/Located, ref-edge traversal) | brand, manufacturer, model, category, purchaseDate, warrantyExpiration* |
| financial_account | institution, accountType | currency |
| bill | payee, account, dueDate, invoiceNumber | billingPeriod, paidOn, minimumPaymentDue, confirmationNumber |
| insurance_policy | insurer, insures, subscriber, policyType | premium, renewalDate* |
| subscription | provider, paymentMethod, renewalDate, billingCycle | plan, serviceType |
| creative_work | author, publisher, about, workType, consumptionStatus | datePublished, url, rating |
| document | about, issuer, documentType, contentUrl | encodingFormat, inLanguage, dateIssued |
| goal/habit/project/task | contributesTo, parentGoal/parentProject, partOf, assignee, lead, blockedBy, client, targetDate, dueDate, completedDate, lastPerformed | description, cadence |
| trip | destination, origin, traveler, accommodation | transport, purpose, description |

\* borderline — owner call in §8, default shown here.

**`treatedBy` promotion.** Today prompt-MANDATED (v28 lines 168/178/186) but
registry-UNKNOWN — every treatedBy fact eats the unknown-predicate penalty and
filed a card, despite living in the reciprocity map
(`supersession.py:126-129`) and the owner's tier-1 list. It gets declared in
`person.yaml`: `value_shape: ref`, `range_type: person`, `kind: relationship`,
non-functional (a person accumulates providers), `renamed_from: [treated_by,
seenBy, caredForBy]`, and `treatedby` is added to the health domain-floor list
(`extraction.py:149`). The inverse `hasTreated` stays reciprocity-map-only.

**What tier-1 keeps, verified safe against the trim:** the firewall floor
(`_DOMAIN_BY_PREDICATE`, `extraction.py:149-171`) is a hardcoded list
independent of the registry — trimming YAMLs cannot weaken it; supersession
functionals, reciprocity maps, display projection (`canonical.py:88-145`),
alias seeding (`entities.py:665-683`), and value-shape enforcement
(`pipeline.py:1875-1929`, declares-guarded) all keep full treatment for the
surviving core. The residual hardcoded `FUNCTIONAL_PREDICATES` set
(`supersession.py:26-28`) stays as-is (deliberate role-edge spellings).

## 2. Waves overview

| Wave | Delivers | Depends on | Size |
|---|---|---|---|
| 1 | Two-tier registry trim + canonicalization retirement + open-card sweep + test/eval inversion + docs | §8 decisions 1, 2, 3, 6, 9 ratified at plan sign-off | M-L (test rework dominates) |
| 2 | note-extract v29 (salience) + integrate v11 + eval/golden re-tier — graded corpus, nightly extraction cases, **and the shipped integrate eval** — **same wave** + owner grok-eval gate | Wave 1 (the trimmed registry is what the prompt's tier-1 vocabulary is CI-checked against) | M-L |
| 3 | `neighborhood()` traversal + agent tool + `facts.object_entity_id` index | none (read-only surface; sequenced last, could swap earlier if Wave 2 stalls on the owner eval gate) | M |

Conventional Commits per wave:
`feat(schema): two-tier predicates — registry is tier-1, long-tail commits raw`,
`feat(analysis): salience-first extraction (note-extract-v29, integrate-v11)`,
`feat(agent): n-hop neighborhood traversal tool`.

## 3. Wave 1 — two-tier pipeline (no prompt change yet)

Behavior after this wave: an unknown predicate hits the durable-alias collapse;
on a miss it **commits raw — zero embeds, zero cards** (structured log
`predicate.longtail_kept`). Tier-1 keeps everything it has today. The extraction
prompt is still v28 (maximalist), so fact volume is unchanged — this wave only
removes the noise machinery; it is independently shippable.

**Kickoff precondition (PROCESS.md open-decision escalation).** This wave
hard-codes §8 decisions 1 (delete the card path), 2 (demote-by-deletion),
3 (borderline predicates), 6 (the open-card sweep), and 9 (unconditional
weight-penalty removal — which flips inferred long-tail preference facts from
review to commit). Per PROCESS.md these are owner-escalation items: they are
ratified in the one-pass §8 sign-off before this wave's branch is cut, and any
default the owner overrides is applied here before T1.1 starts.

**T1.1 — Registry trim + treatedBy promotion (S/M).**
- Files: `schema/defs/types/*.yaml` (22 files), `facets.yaml` per the §1 table;
  `schema/models.py` + `loader.py` (remove `allow_open_predicates`).
- Demotion is deletion: stored facts stay put (storage never gates); the
  demoted predicate's `renamed_from` attractor and `functional` flag lapse
  (stated as accepted, §9).
- Tests: `test_schema_registry.py`, loader tests, `registry_seed_rows` surface
  (`predicates.py:75-98`).
- Non-negotiables: none new; no migration.

**T1.2 — Retire tier-2 canonicalization + weight penalty (M).**
- `pipeline.py:727-789` `_canonicalize_predicates`: keep the alias collapse;
  delete the embed + band decision + card-filing branch and
  `_file_new_predicate_review` (`:801-848`) itself (dead paths deleted, not
  stranded — the 80% coverage gate demands it).
- `weight.py:34` delete `UNKNOWN_PREDICATE_PENALTY` and the `-0.1` in
  `ceiling()`; remove `predicate_known` from `ConfidenceSignals` + its
  computation (`arbiter.py:574`, frozen dataclass — touches `_CONSERVATIVE`
  and tests) unless the eval still reads it.
- Settings: keep `predicate_canonicalization` but repurposed — it now gates
  only the held-fact predicate-suggestion picker (`pipeline.py:596-612`,
  suggestion UX, not vocabulary policing). `value_shape_enforce` unchanged
  (tier-1-only by the `:1900` guard). No new flag.
- Keep: `canonical_predicates` table + embeddings + `sync_predicates` (they
  anchor the aliases FK and the picker), `_apply_resolution` new_predicate
  branches (`repo.py:1332-1444`) so legacy cards stay resolvable, and the
  `predicate_resolution_executor`.
- Tests: rewrite `test_predicate_canon_pg.py` (cold-match case becomes
  raw-commit/no-card), keep `test_predicate_resolve_pg.py` legacy-resolution
  coverage, `test_analysis_weight.py`, `test_dispatcher_pg.py` unchanged
  (resolution.changed keying is unaffected).
- Non-negotiables: no new table → no new RLS test; security paths untouched.

**T1.3 — Open-card retirement sweep + seed prune (S/M).**
- One-shot idempotent boot sweep (code, not an alembic data migration — the
  policy lives in registry code and testcontainers can test it): DELETE
  `review_items WHERE kind='new_predicate' AND status='open'`, logging each
  retired predicate. **Open-only, kind-scoped** — resolved/dismissed rows are
  human history and survive (precedent: `_sweep_stale_ambiguous`,
  `purge.delete_review_items(statuses=('open',))`). Deferred-lane cards are
  left parked (owner explicitly parked them — §8).
- Pre-flight count: `SELECT count(*) ... kind='new_predicate' AND
  status='open'` (= distinct unregistered spellings, exact by the dedup rule).
- `sync_predicates`: prune `origin='seed'` rows no longer in the trimmed
  registry, guarded NOT EXISTS against `predicate_aliases` FK references and
  minted rows, **and against seed rows referenced by any surviving
  `new_predicate` card's suggestion payload** (deferred-lane cards, plus any
  open card the sweep did not retire). T1.2 deliberately keeps the legacy
  resolution path alive, and resolving a surviving card via `map_to_existing`
  calls `record_predicate_alias` (`predicates.py:149-164`), whose INSERT
  carries an FK to `canonical_predicates` — if the resolution target is a
  demoted seed row the prune deleted, the resolution fails at apply time and
  the suggestion picker can no longer offer it. Acceptable alternative if the
  payload scan proves fiddly: drop the prune entirely — the rows are inert
  picker fodder and doctrine already treats canonicals as permanent.
- Tests: integration tests for the sweep (open retired, resolved/deferred
  survive, idempotent re-run) and the guarded prune — including the
  parked-card-referenced seed row surviving and its resolution still applying.
- Non-negotiables: sweep runs on an RLS-scoped SYSTEM_CTX session like the
  pipeline; no new table.

**T1.4 — Eval/test inversion + docs (M).** (Serializes lightly after T1.2's
behavior is fixed; everything else in the wave is parallel.)
- Add a **negative review-card assertion** (`absent_review_cards` /
  `max_review_cards: 0`) to the corpus `Expect` schema (`tests/eval/cases.py`)
  and `check_case_db` (`assertions.py:342-360`), unit-tested in
  `test_eval_assertions_db.py` — pure Python, CI-verified without a model.
- INVERT `tests/eval/corpus/predicates.json`: both `canon-novel-*` cases
  become "long-tail predicate commits raw, files NO card" (DB-hard).
- New harness scenario: unknown long-tail predicate → fact committed raw,
  `review_items: [{"kind": "new_predicate", "count": 0}]` (mirrors
  `rel_conjoined_past_employers.json:49`, which stays green).
- Audit the ~14 long-tail-predicate harness scenarios: tier-2 keeps card-free
  canonicalization but unchanged per-kind supersession, so they should stay
  green untouched — verify, and check all **three** strict-xfail scenarios
  (`adv_same_first_name_collapses`, `adv_two_birthdays_attribute_collision`,
  `own_transfer_subject_cannot_move`) for accidental xpass.
- Docs in the same PR: `docs/PREDICATE_CANONICALIZATION.md` status update (its
  §5a finding is this plan's justification); CLAUDE.md pointer to this plan +
  fix the stale "migrations through 0044" note (actual head: 0112).

## 4. Wave 2 — salience-first prompts + eval migration (same wave, same PR)

The prompt's tier-1 vocabulary digest is **hand-authored in the `.prompt` file
at rewrite time, then CI-checked against the Wave-1 trimmed registry** — the
registry stays the source of truth via an assertion, not via dynamic
rendering. A registry-driven render is explicitly rejected: `prompt.py`
renders a static `.prompt` file (only `max_facts` is templated), the rendered
output is sha256-pinned (`test_promptfile.py:122-133`), and `PROMPT_VERSION`
is stamped on every fact — templating the vocabulary would make every future
YAML edit silently change `SYSTEM_PROMPT`, forcing a version bump + repin per
registry change and muddying what `PROMPT_VERSION` identifies. Instead T2.1
adds a cheap CI test asserting every predicate the prompt's tier-1 digest
lists satisfies `registry.declares_predicate` (drift between the hand list and
the registry is red CI). Prompt edits and their digest-pin updates are
**assigned to one task each, never split** (a prose edit without the version +
sha256 repin in `test_promptfile.py:122-133` is red CI).

**T2.1 — Extraction prompt rewrite: note-extract-v29 (M).**
- `analysis/prompts/note_extract.prompt`: replace the completeness framing
  (v28 lines 171/175/199) with the salience contract — *emit a fact when it is
  (1) a navigation edge (kinship, worksFor/memberOf, treatedBy, owns,
  homeLocation, appointment + provider/time/place) or (2) a root fact the
  graph arbitrates current truth for (name.*, identifier, status, scheduled
  time, home address, medication/diagnosis, a measurement reading). Everything
  else stays in the note's prose — a skipped fact is still findable by search;
  a minted fact the owner must curate forever.*
- Keep verbatim/near-verbatim: CAPTURE-ONLY-WHAT-STATED, DATA-NOT-INSTRUCTIONS,
  pasted-reference, app-chrome, WHOSE-RECORD (incl. the treatedBy MUST),
  homeLocation-vs-location, kind taxonomy + KINSHIP-IS-AN-EDGE, name.* rule,
  assertion semantics, refs, temporal + closed intervals, AGE→birthDate,
  **per-fact domain (firewall — security path, survives verbatim)**,
  confidence, temporal_tokens. Mentions stay generous for person/org/place/
  event (they are the co-mention spine); "or thing" softens to
  salient/owned/recurring.
- Cut: the preference/goal/task datum machinery in rule :209; worked example
  226 (or invert it into a negative example); the ":178 not-a-minimalist"
  tail; merge examples 228+230. Add ONE negative example (rich journal
  paragraph → 3 mentions, 1–2 facts).
- Config: keep `max_facts: 40` (runaway bound); **lower `min_facts` 12 → 6**
  (it floors the CAP, not the output; deleting it lets a dense 40-word family
  roster clip tier-1 kinship edges). Reword the user-prompt budget line
  (`prompt.py:136-137`).
- domain_guidance: keep both blocks (firewall-relevant entity SHAPE); soften
  only "each LAB as its OWN mention" → labs the owner tracks/recurring.
- Do NOT mention relatedTo (a catch-all erodes the tier discipline).
- No engine change: cap enforcement, `extraction_truncated` card, map-reduce
  grouping, dedup, object binding all stay as the over-emission safety net.
- Tests, same task: bump version + repin sha256 (`test_promptfile.py:130-133`),
  version string (`test_analysis_extraction.py:515`), rewrite the
  CAPTURE-EVERYTHING prose needles (`:403-417`) to salience needles, trim
  `:419-451`, **keep `:454-463` (name.*) and `:499-505` (per-fact domain)
  essentially intact**, keep `:466-497` (temporal), update budget tests
  `:161-204` for MIN_FACTS=6. **New: the vocabulary-drift CI test** — parse
  the tier-1 predicate list out of the rendered prompt (a delimited digest
  block makes this trivial) and assert each entry satisfies
  `registry.declares_predicate`; pure Python, no model.

**T2.2 — Integrator prompt: integrate-v11 (S).**
- Rule 2 (CARRY FORWARD) **stays** — the salience filter lives in the
  extractor; rule 2 prevents silent inter-stage drops. Add: *"The extraction
  is deliberately SELECTIVE… an absent fact is a choice, not an omission: do
  not add facts to make the note look complete."*
- Narrow the inference license (":41 plus any you confidently infer" and the
  reflective-note hobby/habit/plan clause) to identity-relevant inferences
  only (gender rule stays). **This narrowing is load-bearing** — otherwise the
  integrator re-inflates what the extractor filtered.
- Add a digest pin for integrate_note.prompt mirroring the extract pin (today
  only a version-equality assertion exists — prose can drift unversioned).
- **Integrate-eval gold migration, same task:** the shipped integrate eval
  (`src/jbrain/evals/integrate_cases/` + `integrate_runner.py`) drives the
  REAL integrate prompt through the adapter and scores judgment + safety
  dimensions — a v11 prose rewrite can regress those scores or strand golds
  exactly like the extraction goldens (§9's same-wave rule applies). Audit and
  re-grade the `integrate_cases` golds against the narrowed inference license
  in this task: any gold that expects an inferred hobby/habit/plan-style fact
  the v11 license no longer permits is re-graded; the safety-dimension golds
  (never mint a name, never put a sentence in value_json) must survive
  untouched. Run the integrate eval before/after
  (`python -m evals.box.run_layer integrate`, the owner-run calibration
  track) and hand the comparison to T2.3's wave-review artifact.
- Tests: `test_analysis_integrate.py` version + new pin.

**T2.3 — Eval corpus re-tier + acceptance instrumentation (M-L).**
- Graded corpus (`tests/eval/corpus/`): keep every tier-1 hard case unchanged
  as the recall floor; add a **`Me.treatedBy` case** — treatedBy is today
  covered only in the nightly extraction eval (`20_relationships.json`,
  `91_health_clinical.json`) and appears nowhere in the graded corpus, so the
  new case has no advisory predecessor to calibrate against: land it
  advisory-first per the corpus pattern and harden only after the full ≥3-run
  Grok calibration pass, like the other new hard cases; flip
  `mixed-domain-journal` DB-hard (the §5 promise — a steered vocabulary makes
  its health floor deterministic); tighten `max_facts` on the journal/article
  cases (calibrate against ≥3 Grok runs before hardening — land
  advisory-first per the corpus pattern); add `absent_review_cards` sweeps on
  2–3 ordinary cases.
- Nightly extraction eval (`src/jbrain/evals/cases/`, shipped package): keep
  the ~90 mention-recall files 00–80 as the navigation-spine gate (salience
  cuts FACTS, not mentions); re-grade long-tail `value`/fact expectations in
  60/70/90; keep tier-1 + health values in 91/92. Add a corpus-total
  committed-fact count to `run.py`'s report (the "leaner" metric). Add the
  small `absent_edges`/`absent_predicates` scorer extension (pure Python,
  unit-tested in `test_eval_scoring.py`). Run `evals.audit` (CI-enforced).
- **Owner acceptance step (wave-review artifact, required):** before/after
  `scripts/grok-eval.sh --db` output + the fact-volume comparison, **plus the
  before/after integrate-eval run from T2.2** (judgment + safety scores). The
  real-model gates are opt-in and CI cannot judge prompt quality — this
  artifact is how the wave review sees both prompts.

## 5. Wave 3 — n-hop neighborhood traversal + agent tool

The substrate exists: `entity_mentions` carries a direct `note_id`,
`facts.object_entity_id` is the typed ref edge, and `SqlAnalysisRepo.ego_graph`
(`repo.py:423`) already does Python-iterated both-directions BFS inside one
RLS-scoped transaction — with `test_entity_neighbors_pg.py:171-186` proving
mid-traversal vanish (a GENERAL_ONLY session transitively loses the health
branch). We follow that proven pattern, NOT a recursive CTE (correct but
non-idiomatic; per-hop caps/paths are awkward; scale is personal-corpus small).

**T3.1 — Pure BFS layer (S/M).** New `analysis/neighborhood.py`
(graph_context's two-layer idiom; repo.py is 1678 lines — keep BFS out of it):
takes per-hop edge batches, returns entities `{id, name, kind, domain, hop,
path}` + notes `{note_id, hop, connects}`. First-visit-wins dedup with a parent
map (one connecting path per node: `Me —spouse→ Celine —co-mention(note X)→
Dr. Patel`). Caps: `per_hop_limit` (typed-ref candidates rank above co-mention;
co-mention ordered by count desc then recency), `total_cap` ~75, all arguments
with defaults so evals can tune without code churn. Unit tests: hub-note
explosion, path reconstruction — no DB.

**T3.2 — Retrieval layer + RLS isolation (M).** `SqlAnalysisRepo.neighborhood`
— one `scoped_session`, hops iterated in Python (ego_graph pattern), depth
clamped 1..3:
- Ref edges per hop: reuse ego_graph's out/in SQL (active, asserted,
  non-merged); dedup derived reciprocal shadows (`derived_from_fact_id`) on
  one arm like `entity_view`.
- Co-mention edges per hop: `entity_mentions m1 ⋈ m2 ON note_id`, **INNER
  JOIN** `entities` and `notes` (never LEFT — a LEFT JOIN would leak an
  out-of-scope entity's bare uuid; RLS must drop the whole edge). Hub damping:
  a note with more than `hub_cap` (default 8) distinct entities is not
  expanded through, but still appears in the notes result.
- Notes collection: `entity_mentions` for the final entity set, live notes
  only, stamped `min(hop)` + connecting names, hop-then-recency order, cap ~40.
- Module docstring states it is **RLS-reliant, owner/agent-session-only** —
  never call under SYSTEM_CTX (the `graph_context.py:174-180` hazard).
- Tests: extend the `test_entity_neighbors_pg.py` seed (co-mention chain,
  15-entity hub note, health branch): depth semantics, damping,
  merged-tombstone + deleted-note exclusion, and the **RLS isolation test**
  (GENERAL_ONLY loses the health branch through BOTH edge kinds at hops 2–3;
  UNSCOPED sees nothing) — CLAUDE.md rule 3 satisfied with no new table. The
  per-wave security review red-teams the LEFT-JOIN leak specifically.

**T3.3 — `neighborhood.tool` + handler (S).** New sidecar (`permission: read`,
params: anchor / hops 1-3 default 2 / kinds relationships|co-mentions|both /
limit): description partitions cleanly against `relate` (relate = one named
relationship; neighborhood = the whole vicinity — misrouting is the failure
mode). Handler in the readtools idiom, compact per-hop lines + connecting
notes, `ToolOutput(text, sources, entities)` chips; read-class with no
`domains` → default curator gets it via the existing wildcard. Tests:
`test_agent_readtools.py` pattern; registry pairing test auto-covers wiring;
`.tool` digest pin refresh; if agent system-prompt guidance changes, bump +
repin `test_agent_loop.py:122-129`.

**T3.4 — `facts.object_entity_id` index (XS, parallel).** Migration ≥0113:
pure index (inbound ref-edge expansion seq-scans facts today; a 3-hop frontier
multiplies it). No new table → no new RLS test. Reversible.

**Deferred from this wave:** any HTTP endpoint or GraphScreen change (GUI
surface → three-mock gate); a PWA neighborhood view is a later wave that
starts with the gate. Refactoring ego_graph/full_graph onto the new engine is
also deferred (don't touch the shipped graph-view contract).

## 6. `relatedTo`/`about` — deferred (decision, not a wave)

Researcher finding, decisive: intent validation requires every fact object to
be a mention_ref from THIS note (`intent.py:214-219`, fatal) — so relatedTo can
only connect entities already co-mentioned, which the traversal already links
at 2 hops. Its only marginal population is *inferred* associations, which the
weight model hard-routes to review (`INFERRED_CEILING 0.6 <` relationship
threshold `0.7`) + a `low_confidence_inference` card — manufacturing exactly
the noise this plan removes. The genuinely missed cross-note cases (episode
continuation, unnamed groups) are unreachable by relatedTo too; the recovery
path is identity resolution, not a new predicate.

**Defer→build trigger:** 5 concrete observed dead-ends ("X and Y are related
but >3 hops apart, the notes imply the link") from agent transcripts/owner
reports after Wave 3 ships. The build is then one wave-task (S-M): one YAML
declaration in the Related facet (declared → card-free automatically; naming
`associatedWith` to avoid the `relative`/schema:relatedTo embedding collision),
optional SYMMETRIC_PREDICATES entry, one OPTIONAL integrator rule with a
never-substitute guard, substitution-guard eval cases, and a deliberate
weight-model decision for inferred emission. Wave 3's traversal reads fact
edges bidirectionally, so it picks relatedTo up later with zero changes.

## 7. Eval acceptance criteria

1. **Tier-1 recall floor:** every currently-hard corpus case pinning a tier-1
   predicate stays green (intent + DB): nickname-goes-by-single,
   nickname-multi-declaration-prod-bug, value-fidelity-birthdate-bare,
   relationship-enumerated-children, supersession-employer-change,
   temporal-future-job-expected, temporal-appointment-next-friday-expected,
   future-state-expected-not-asserted, + the new treatedBy case (lands
   advisory-first per T2.3, hardened after its ≥3-run calibration).
2. **Firewall floors:** all domains.json DB-hard `committed_domains` cases
   green; `mixed-domain-journal` flipped DB-hard and green.
3. **Long-tail volume drops materially:** corpus-total committed-fact count
   (new run.py report) down ≥30–40% on journal/article-style cases; tightened
   per-case `max_facts` green across 3 repeated runs (Grok variance).
4. **Zero new_predicate cards:** the inverted predicates.json cases (DB-hard)
   + `absent_review_cards` sweeps + the deterministic harness scenario (CI).
5. **No navigation regression:** max_entities/forbidden_entities cases
   unchanged; nightly-eval mention files 00–80 keep their pass rate (mentions
   are the spine — salience cuts facts, not mentions).
6. **Integrate eval holds:** `src/jbrain/evals/integrate_cases/` judgment +
   safety scores on the re-graded golds match or beat the v10 baseline in the
   before/after run (T2.2); safety-dimension golds unchanged — the v11
   narrowing may never trade an entity fabrication or a prose value for
   judgment points.
7. **neighborhood() correctness:** fixture-graph integration test asserts
   exact entity/note sets, hop distances, edge-kind unions, damping, and RLS
   scoping — CI-gated, no model.
8. **Repeatability:** the DB-mode hard set green across ≥3 consecutive
   grok-eval runs before Wave 2 merges; before/after output — grok-eval
   extraction AND the integrate-eval run — is the wave-review artifact.

## 8. Open decisions for the owner (deduped; recommended default first)

> **Ratification gate (PROCESS.md).** PROCESS.md's escalation list names
> "plan open decisions" as owner-escalation items, so these are NOT
> pre-approved by this document: the whole section is presented to the owner
> for explicit ratification in one pass at plan sign-off, before the Wave 1
> branch is cut. Decisions **1, 2, 3, 6, and 9** are hard-coded by Wave 1 and
> block its kickoff; the rest can be ratified in the same pass or escalated
> at the wave that consumes them.

1. **Tier-1 drift cards.** Default: delete the card path entirely (Wave 1) —
   the card existed to grow a vocabulary this plan stops growing; the prompt
   digest steers to exact tier-1 spellings, and `renamed_from` + the
   consolidation sweep heal known drifts. Alternative: keep a narrow embed
   path that cards only WEAK-matches whose nearest canonical is declared
   (better drift recall for truth arbiters, keeps an embed per unknown).
2. **Demote-by-deletion vs a `tier:` field.** Default: deletion — declared-ness
   is the flag, no second axis. Cost: demoted predicates lose `renamed_from`
   attractors and shape checks (spellings may fragment — accepted as
   prose-grade).
3. **Borderline predicates.** Defaults per the §1 table: vehicle
   color/fuelType/bodyType demote (entity-resolution hints live in prose +
   mentions); insurance renewalDate demotes, subscription renewalDate keeps;
   person weight/height demote as *predicates* but measurements stay tier-1
   via the health types (next item); creative_work rating demotes; product
   keeps only `location` (presence-revealing, firewall-relevant) —
   brand/manufacturer/model/category/purchaseDate/warrantyExpiration demote
   consistent with the vehicle/device rows (warrantyExpiration is the
   borderline: functional, but no projection or floor consumes it).
4. **Health/finance measurements tier-1?** Default: KEEP (medication,
   lab_result, blood pressure, account balance) — they are the canonical
   current-truth series and the firewall eval's committed-fact anchor.
5. **Preferences/goals/tasks as facts?** Default: preference datum machinery
   is cut from the prompt (preferences → prose); goal/task/project/habit keep
   their structural tier-1 edges (partOf, dueDate, assignee…) so tracking
   still works, but `description`/`cadence` demote.
6. **Existing open new_predicate cards.** Default: one-shot open-only sweep
   (T1.3), deferred-lane cards left parked, resolution verbs kept alive for
   stragglers (with the T1.3 prune guard protecting their resolution
   targets). Alternative: leave all for manual triage.
7. **`min_facts`.** Default: lower to 6 (cap floor, protects dense short notes'
   kinship edges). Alternative: delete and accept `extraction_truncated` as
   the only signal.
8. **Predicate embeddings.** Default: keep `canonical_predicates` embeddings +
   sync + the held-fact suggestion picker (gated by the repurposed setting);
   don't strip the embed path wholesale. `--canon` eval mode narrows to
   alias-collapse/drift coverage.
9. **Weight-penalty removal scope.** Default: unconditional. Side effect:
   inferred long-tail *preference*-kind facts move 0.5 → 0.6 and flip from
   review to commit; if unwanted, keep the penalty for `inferred` facts only.
10. **treatedBy details.** Default: non-functional accumulating, added to the
    health floor, inverse via the reciprocity map only (no declared
    hasTreated).
11. **Traversal tuning.** Defaults: hub_cap 8 (hub notes appear but aren't
    expanded through); co-mention expansion allowed at all hops (tunable
    argument if it dilutes); notes collected from mentions only (facts
    provenance union deferred); new `neighborhood` sidecar (not a widened
    `relate`), default-curator visible.
12. **relatedTo trigger.** Default: 5 observed dead-ends post-Wave-3; name it
    `associatedWith` if built.
13. **Minted long-tail canonicals** (`origin='minted'`). Default: leave them
    (permanent by doctrine; they only feed the suggestion picker); prune is
    out of scope.

## 9. Risks (honest)

- **Phase 6 leans harder on retrieval quality.** Wiki claims source from
  facts; a spine-only graph means article richness depends on chunk retrieval,
  and the notability gate (`NOTABILITY_MIN_FACTS=3`) may de-page entities that
  were only notable via long-tail facts. Accepted under the thesis, but it is
  a visible behavior change — the Phase 6 plan should be re-read against this
  one before its graph-coupled waves start.
- **Silent tier-1 chain fork** (the cost of decision #1's default): a novel
  spelling near a truth arbiter (`employedBy`) now commits raw with no card;
  only `renamed_from` drifts heal. Mitigations: the v29 prompt digest lists
  exact tier-1 spellings; eval criterion 1 pins them; if forks appear, the
  narrow tier-1-adjacent card path (decision #1 alt) is a small follow-up.
- **Golden/eval migration is the bulk of the work** — five surfaces (harness,
  graded corpus, shipped nightly extraction cases, shipped integrate eval,
  CI prose/digest pins) must move in the same wave as each change or CI is
  red / nightly and calibration baselines pollute. Digest pins are same-PR
  obligations owned by single tasks, never split.
- **The real-model gates are opt-in.** A lean prompt can merge green while
  regressing tier-1 recall or integrator judgment; the before/after
  grok-eval + integrate-eval wave-review artifact (criterion 8) is the
  mitigation and is required, not optional.
- **Quiet retraction on touched notes** — by design (§0), but user-visible:
  editing an old note sheds its long-tail v28 facts with no inbox signal.
- **Demoted functional attributes** (vehicle fuelType, animal breed) shift
  from silent supersession to occasional attribute-collision review cards;
  the leaner prompt extracting fewer such facts is the main damper.
- **Hub-note fan-out** is bounded by heuristics (hub_cap, per-hop, total)
  that can sever legitimate small-gathering links or flood context; they ship
  as tunable arguments and the eval fixtures exercise the explosion case.
- **Coverage gate on deleted paths:** removing card filing deletes covered
  lines + tests together; verify pipeline.py module coverage locally before
  the Wave-1 PR.
- **Docs drift:** PREDICATE_CANONICALIZATION.md must be re-statused in Wave 1
  (its §5a finding justifies this plan); CLAUDE.md's migration head is stale
  (0112, not 0044) and gets fixed in the same PR.
