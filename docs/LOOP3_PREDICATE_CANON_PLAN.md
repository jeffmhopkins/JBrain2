# Loop 3a — predicate-canon self-improvement (owner-gated MVP)

Binding multi-wave plan (docs/PROCESS.md). Builds the **deferred Phase-5 self-improvement
loop** of `docs/PREDICATE_CANONICALIZATION.md` §6.5: *"Agent reviews minted predicates,
proposes merges/renames into the registry via correction notes; consolidation sweep heals
stored drift."* First half of ROADMAP **Loop 3**; the Tier-B durable-knowledge half is the
next plan.

Posture (owner's call, mirrors Loop 2): **owner-gated MVP** — the agent only *stages*
proposals; owner approval is the sole gate; nothing auto-applies; the eval/groundedness
regression gate (ASSISTANT.md #6) is deferred (it needs the replay-eval seam already
deferred in Loop 2).

## What already ships (the substrate)

- Embedding canonicalization is live and default-ON: `AnalysisPipeline._canonicalize_predicates`
  (`pipeline.py:687`) calls `decide_predicates` (`predicates.py`) → STRONG (≥0.90) rewrites the
  fact's predicate in place; WEAK/cold file an idempotent **`new_predicate` review card** carrying
  the raw predicate + ranked embedding-neighbor suggestions.
- The owner can already resolve a card: `AnalysisRepo.resolve_review` (`repo.py:1318`) — actions
  `map_to_existing` (heal stored facts raw→canonical via `rewrite_predicate`, emit
  `predicate_remapped` → consolidate sweep), `accept_as_new`/`suggest_better` (mint a
  `canonical_predicates` row, `origin='minted'`; `sync_predicates` backfills the embedding),
  `reject` (dismiss). `/analysis/review` API + the Review screen drive it.
- `canonical_predicates` (migration 0031): global reference data, global SELECT + owner/system
  INSERT, HNSW index, seeded from the registry YAML + extended by mints.
- Reusable Loop-2 machinery: `SelfImprovementGate` (kill-switch + budget), Proposals
  (`ProposalRepo.stage/decide/enact`, `LeafExecutor`, `build_leaf_executor`), the engine-action +
  nightly-seed-migration pattern (`skilldistill.py`/`skillsweep.py`, migrations 0054/0055),
  `_owner_principal_id`.

## The gap this loop closes

1. **Durable aliases.** Even an *owner's* `map_to_existing` only heals *stored* facts — there is
   **no runtime alias store** the canonicalize path consults, so the next run re-emits the drift
   spelling and re-files the card (the `repo.py:1338-1343` TODO defers this to "a Phase-5 correction
   note"). Without this, an agent proposing the same resolution would loop forever.
2. **The agent as reviewer.** Today only the owner resolves cards (UI/API). The loop lets the agent
   batch open cards and *propose* resolutions — owner-gated (staged proposals, no auto-resolve).

## Wave 1 — durable predicate aliases (close the re-drift loop; no agent yet)

A confirmed raw→canonical mapping must collapse the drift spelling **at canonicalize time** on
later runs, not just heal stored facts once.

- **`predicate_aliases` table** (migration): `raw_norm` (PK, the normalized raw spelling),
  `canonical_name` (FK→`canonical_predicates`), `origin` (`review`), `created_at`. Global reference
  data, self-extending → the `canonical_predicates` RLS pattern (global SELECT; INSERT/DELETE
  owner/system only) + **an RLS isolation test** (non-neg #3).
- **Consult aliases first** in the canonicalize path: before `decide_predicates` embeds, an unknown
  predicate whose `raw_norm` has an alias is treated as a STRONG hit → rewrite in place, no embed,
  no card. (Same normalization `normalize_predicate` uses, so spelling variants collapse.)
- **Write an alias on resolution**: `resolve_review`'s `map_to_existing` (and `suggest_better` when
  it renames) records the durable alias, so the **existing owner path** becomes durable. Idempotent
  (`ON CONFLICT DO NOTHING`).
- Tests: alias hit short-circuits to STRONG (a unit/integration on the canonicalize path); a
  resolved `map_to_existing` writes the alias and a re-run does NOT re-file the card; RLS isolation;
  alias never points at a non-canonical (FK + guard). Per-task + the RLS gate.

*Independently valuable and safe — it only makes a change the owner already approved durable. No
agent, no LLM, no new egress.*

## Wave 2 — agent-proposed predicate review (the self-improvement loop)

A nightly, budget-gated `predicate_review` engine action that turns the agent into a *proposing*
reviewer, owner-gated.

- **`predicate_review` action**: gate (`SelfImprovementGate`) → fetch a batch of open
  `new_predicate` cards → for each, derive a **suggested resolution from the card's existing
  embedding-neighbor suggestions** (embedding-only; the LLM shortlist of §3.1a/§7 is **deferred** —
  no hot-path or batch `complete` call in the MVP): a strong top-neighbor → propose
  `map_to_existing`; otherwise propose `accept_as_new` (mint under the raw name). Stage **one owner
  proposal** whose leaves each carry `{card_id, action, canonical_name?}` in `preview` (so the owner
  sees exactly what each leaf will do).
- **Executor**: on owner approval, the leaf calls the **shipped** `resolve_review(ctx, card_id,
  action, payload)` — reusing all the committed resolution logic (rewrite, mint, the durable alias
  from Wave 1, the consolidate event). New leaf op `predicate_resolve`; routed in
  `build_leaf_executor` like `skill_promote`.
- **Owner-gated** (no auto-resolve), single-domain per card (a card already carries its
  `note_domain`; the proposal's domain = the card's), attributed to the live owner principal
  (`_owner_principal_id`), staged under SYSTEM_CTX.
- **Seed** the nightly `predicate_review` schedule **DISABLED** (migration; mirrors 0054/0055).
- Tests: a card → a staged proposal (never auto-resolved); enact runs `resolve_review` and the card
  closes + alias lands; kill-switch/budget refusal; idempotency (no duplicate proposal for an
  already-proposed card); RLS. Per-wave adversarial review (RLS/firewall + the data/instruction
  boundary — a card's raw predicate + statement is untrusted model output, so the proposal preview
  is data, never instruction).

## Cross-cutting non-negotiables

All DB on RLS-scoped sessions; the domain firewall holds in Postgres + **an RLS isolation test per
new table/query path** (`predicate_aliases`; the alias-consult read; the card-batch read); the
agent is a **source, not an editor** of citable knowledge — it never writes the fact graph or the
registry, only *stages a proposal* that, on owner approval, runs the **already-shipped**
resolution; **owner review is the trust gate** for every change; the card's raw predicate/statement
is **untrusted model output** treated as data in the proposal preview, never instruction; LLM (if
any) via the **router adapter only**; tests-with-code (80% / security-100% on the executor + RLS);
Conventional Commits + one PR per wave + CI green; no new deps; `dev-setup.sh` current.

## Red-team (findings resolved before build)

- **F1 — alias key normalization (Wave 1).** The stored `raw_norm` and the canonicalize-time lookup
  MUST use the *same* normalization the registry uses, or aliases silently miss. → one shared
  `normalize_predicate` key for both the write and the consult.
- **F2 — consult seam (Wave 1).** Put the alias check inside `decide_predicates` (it already has the
  session + the predicate batch): an aliased predicate returns `band="strong"` with no embed and no
  card; everything else falls through to the existing cosine path.
- **F3 — accept_as_new needs no alias (Wave 1).** A minted predicate self-matches STRONG on its own
  `canonical_predicates` embedding once `sync_predicates` backfills it, so only `map_to_existing` /
  renaming `suggest_better` write an alias. The mint→backfill window (a minted predicate re-cards
  until the nightly embed lands) is **pre-existing**, not a Loop-3a regression — left as-is.
- **F4 — proposal idempotency (Wave 2).** A nightly re-run must not re-propose a card that already
  has an open predicate-canon proposal. → before staging, collect the card_ids referenced by open
  proposals of this kind and skip them.
- **F5 — proposal kind (Wave 2).** The `app.proposals` kind CHECK is a closed set; Wave 2 includes a
  migration adding the new kind (mirrors how `skill-promotion`/`appointment` were added).
- **F6 — executor reuse (Wave 2).** The leaf executor calls `analysis.resolve_review(...)` via the
  **existing** `analysis` dependency `build_leaf_executor` already injects (it carries
  `merge_entities` today) — no new executor wiring param.
- **F7 — untrusted preview (Wave 2).** A card's raw predicate + statement is untrusted model output;
  the executor acts only on structured fields (`card_id`, `action`, `canonical_name`), never on the
  raw text as instruction; the preview renders it as data (an adversarial-injection assertion).

## Deferred (explicitly out of this MVP)

- **Registry-YAML promotion / git write path.** Durable aliases + minted predicates live **in-DB**
  (`predicate_aliases` / `canonical_predicates.origin='minted'`), consulted at canonicalize time.
  Materializing them back into the shipped `schema/defs/**.yaml` contract (a git PR) is a distinct
  later concern — the agent gets **no schema-file/git write path** in this MVP (master rule).
- **LLM-proposed canonical-name shortlist** (§3.1a/§7) — embedding neighbors only for now.
- **Auto-resolution** (no owner) — gated on the deferred replay-eval + a confidence calibration.
- **The Tier-B durable-knowledge half of Loop 3** — its own plan (next).
