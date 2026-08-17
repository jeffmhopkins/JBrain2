# Tool Catalog — a scalable tool surface for a growing tool count

> **Status:** In progress (W1 shipped) · **Last verified:** 2026-08-17 ·
> **Waves:** W0a◻️ W0b◻️ W1✅ W2◻️ W3◻️
> **W1 (umbrellas) is shipped** — the four action/source families collapsed with no capability
> change and no measurable tool-selection regression on the live gpt-oss-120b (validated first via
> the `/api/debug/tool-probe` harness, then per-family): `grokipedia` (5→1),
> `public_records` (4→1 fan-out over identity/court/license/federal_register), `research_report`
> reads (3→1, show/remove kept separate), `external_video` reads (3→1, show/remove/check_channel
> kept separate). jerv's tool surface: **48 → 37**. The read-umbrella + separate-destructive split
> preserves the sub-agent parent⊆child clamp (a library/report child holds only the read umbrella).
> Remaining: **W0b** (trim the fattest descriptions — sequenced by `JERV_CONTEXT_BUDGET_PLAN` §4),
> **W0a** (the family/summary metadata decision in §5) and
> the **gated W2/W3** (the catalog machinery) — behind a pre-built selection-accuracy eval and
> resolving the mode-(a) / native-tool-calling contradiction in §7. See §10.
> (W0a = metadata, W0b = trim — split in §6; W1 = umbrella-consolidate source/action families; W2 = the catalog:
> always-on menu + hot core + `tool_guide`-before-call + auto-load-on-miss; W3 = tune the hot
> core, migrate remaining families, measure.)

> Reconciled with the root `CLAUDE.md` non-negotiables: this changes only how a turn's tool
> schemas are *assembled and disclosed* — it does not touch the LLM adapter, storage, or RLS.
> Tool **permissions/allowlists are unchanged and still authoritative**: reading a guide or
> arming a schema can never grant a persona a tool it isn't allowed (the arm step is gated by
> the same scope/allowlist as a direct call today). Tests land with the code; the menu is
> re-injected as DATA, never as instruction.

## 1. Why

jerv's tool surface is growing and will keep growing (each new capability — the records
adapters, media tools, civic-data sources — adds tools). Measured today (verified against the
sidecars in review):

- **~50 tools on jerv** (`JERV_TOOLS` resolves to **48**) **≈ ~23–26k tokens** of schema —
  the description body + params JSON of those 48 sidecars sums to ~93k chars (÷~3.5–4). jerv
  owns the fat ones: `analyze_stream` ~6.5k, `deep_produce` ~6k, `web_fetch` ~5.7k,
  `deep_research` ~5.5k, `spawn_subagent` ~5.1k chars (whole-file, params included). Against
  jerv's ~131k window (gpt-oss-120b, 128K) that's **~18–20%**, sent on every turn and every
  ReAct step. (The whole repo has ~102 `.tool` sidecars across all personas; 48 is jerv's slice.)
- Two costs grow with the count: **window occupancy** (crowding out working context on long
  deep-research/plan turns) and — the one that bites first — **tool-selection accuracy** (a
  model picks the wrong or misses a tool as the list lengthens and descriptions blur).

**The figures above are the pre-W1 baseline, kept as the record of why this plan exists.**
Current count (2026-08-17): W1's umbrellas took jerv 48 → 37, and growth has since put it at
**44 tools ≈ 111.0k chars of description+params (~27.7k tokens; ~28.7k serialized with the
examples `as_llm_tool` appends)** — the canvas trio being the most recent addition. That is the
plan's own thesis on display: a one-off consolidation buys headroom, not a ceiling. Re-measure
before W0b rather than quoting either number.

**On caching (corrected after review):** there is **no Anthropic prompt caching wired up in this
repo** — zero `cache_control` breakpoints anywhere in `backend/` (the adapter sends
`tools`/`system`/`messages` with no cache markers). And jerv's *default* model is the local
gpt-oss-120b, where Anthropic-style billed caching doesn't apply at all — the only reuse is
llama.cpp **KV-prefix reuse**, a *latency* mechanism, not a dollar one. So the earlier "caching
already blunts the dollar cost" framing was wrong. This cuts two ways for the plan:

- The `tools` block sits near the front of the prompt (before the conversation), so **arming a
  tool mid-turn invalidates KV-prefix reuse from the tools block onward** — the whole
  conversation after it must be re-prefilled that step. Mode (a)'s dynamic per-turn tools array
  therefore has a real latency cost on exactly the model it most wants to protect.
- Regardless, the durable targets remain **window occupancy** and **selection accuracy**,
  neither of which any caching helps.

The goal: make the tool count effectively **unbounded with a ~flat baseline intake**, while
*never* reducing the model's awareness of what tools exist.

## 2. The core principle

> **Separate DISCOVERY from INVOCATION-SCHEMA.** Always show a complete, compact *menu* (so the
> model always knows a tool exists), and load a tool's verbose *use-guide + parameters* only
> when it's about to use it.

The deferred payload is a **use-guide**, not a bare JSON schema — a guide can teach *when to
use* the tool, the common param combos, and the gotchas (e.g. "after `deep_research`, still
call `write_plan_result`; running the tool doesn't tick the box"). For a local model
(gpt-oss-120b) that guidance improves call accuracy more than a dry schema does.

## 3. Design

**Menu (always in context).** Every tool as one line: `name` — ≤~12-word summary, grouped by
`family`. All of jerv's tools ≈ **~600–1000 tokens** (vs ~23k today), and the whole surface
stays discoverable.

**Hot core (always fully loaded).** The ~6–10 highest-frequency tools keep their full
guide+schema inline so everyday turns need **no** guide hop. Initial set (tune in W3):
`web_search`, `web_fetch`, `read_plan`, `write_plan`, `write_plan_result`, `spawn_subagent`,
`deep_research`. These cover the common path; everything else is menu-only until armed.

**`tool_guide(name)` — read before first use.** Returns the tool's full use-guide (what it
does, when to reach for it, parameters with an example, pitfalls) **and arms its schema into
the callable set for the rest of the turn**. This double duty is the crux: a one-line menu
entry is enough to *know* a tool exists but not to emit a valid structured *call* — the guide
step is what makes the real call possible. **Cost to name honestly:** this is a full extra
ReAct round-trip (generate → dispatch guide → observe → generate the real call) per distinct
non-hot tool per turn, paid by the slow local model. The hot core exists to keep that off the
common path; the long tail pays it.

**Auto-load-on-miss (the guardrail) — OPEN ISSUE, see §7.** The intent: if the model emits a
call to a tool that isn't armed yet, the runtime arms it and continues rather than erroring. As
written this **cannot fire under mode (a)** with native, grammar-constrained tool-calling —
the model can only emit a `tool_use` for a tool in the turn's `tools` array, so an unarmed tool
has no token to emit. Resolving this is a precondition for W2 (§7, §10).

**Families / namespaces.** Each `.tool` declares a `family` (`records`, `media`, `research`,
`civic`, `weather`, `planning`, …). Families organize the menu and enable umbrellas.

**Umbrella tools.** Where tools are one shape over many sources/actions, collapse to one
dispatch tool with a `source`/`action` param — the biggest single count reducer, and it keeps
each capability fully documented in one guide:
- `records` — CourtListener / NPPES (`provider_license`) / Federal Register / Wikidata
  (`resolve_identity`) — 4 tools fan under one `public_records(name, sources=[…])`.
- `grokipedia_*` (5) → `grokipedia(action, …)`.
- `*_research_report` (5) → `research_report(action, …)`.
- `*_external_video` (5) → `external_video(action, …)`.
  Rough effect: **19 tools → 4 umbrellas**. Tradeoff to watch: a discriminated-union
  `action`/`source` param is harder for a weak local model to fill than N flat tools (§7).

## 4. Two implementation modes — ship (b), migrate to (a)

- **(b) Defer only the prose (first).** Keep every tool's short summary **+ parameter schema**
  always callable; move just the verbose usage prose into `tool_guide`. Zero call-mechanics
  risk, no hop to *call*, and **no per-turn loop surgery** (the tools array is still assembled
  once). Captures most of the token win immediately (for jerv the prose is the fat part) and
  de-risks the mechanics while the catalog beds in.
- **(a) Defer the schema too (later).** Menu-only tools aren't in the callable set until their
  guide arms them (dynamic per-turn tools array). Maximum intake savings; the mode the count
  scales under. **But** it requires (1) recomputing/re-sending the turn's `tools` + `allowed`
  set *mid-ReAct-loop* — real change to the loop's step machinery, not just registry metadata —
  and (2) a resolution to the auto-load-on-miss/native-tool-calling contradiction (§7). Do not
  start (a) until both are settled.

## 5. Fit to the codebase (verified in review)

- `.tool` sidecars need `family:` and `summary:` — **not a free field-add.** Frontmatter is
  validated by `ToolSpec` with `model_config = ConfigDict(extra="forbid")`
  (`agent/contracts.py:66`), and `ToolSpec` folds into `ToolFile.digest` (`agent/toolfile.py`)
  which the CI version-guard pins. Two routes, each with a cost: (a) declare them as `ToolSpec`
  fields → every sidecar's digest changes and demands a version bump; (b) pop them in the parser
  like `self_editable` → keeps them out of the digest, but `summary`/`family` **are**
  model-facing (they render into the menu the model reads), so popping them removes model-facing
  prose from the very guard designed to force deliberate version bumps. Pick (a) and accept the
  one-time bump wave, or (b) and extend the digest to cover the menu fields. Decide in W0a.
  `JERV_CONTEXT_BUDGET_PLAN` W3 (config-derived lists injected at `schemas_for` time) inherits
  this same question from the other side — injected prose ESCAPES the digest rather than
  breaking it — and blocks on the decision taken here.
- `ToolRegistry` (`agent/toolregistry.py:80`) exists and strictly pairs sidecars with handlers;
  it's the right home for the family/summary metadata, the hot-core set, the menu render, and a
  `guide(name)` accessor — all net-new.
- The per-turn assembly seam is **`ToolRegistry.schemas_for(scopes, allow, extra, hidden)`**
  (`agent/toolregistry.py:144`), called by `AgentLoop` each turn (`agent/loop.py`, ~564/808/1185).
  It emits the callable schemas today; it's the correct extension point to emit menu + hot-core +
  armed schemas and to track the turn's armed set. (Earlier drafts called this an "AgentLoop
  method" — it's a registry method the loop invokes; the seam is real either way.)
- A new `tool_guide` tool: permission mirrors the requested tool's class; only lists/arms tools
  the persona is already allowed (the per-turn `allowed` set is already threaded into
  `ToolContext`).
- Personas opt in behind a flag (`catalog_mode` on the frozen `AgentProfile`, mirroring existing
  optional flags like `budget_multiplier`/`extra_tools`): jerv first; `curator` (a `tools=None`
  wildcard) can stay full-load or opt in later.

## 6. Waves

- **W0a — metadata.** Add `family` + `summary` to every `.tool` — the substrate the catalog
  waves read. Blocked on the §5 decision (`ToolSpec` is `extra="forbid"` and both fields fold
  into `ToolFile.digest`, so this is a real decision, not a free add).
- **W0b — trim.** Trim the fattest descriptions to essentials. **Not "no-change":** the trims
  touch model-facing content, so digests change and CI requires a `version:` bump plus a
  pin-hash update in `tests/unit/test_agent_readtools.py` for every sidecar touched — broad
  but mechanical.

  *W0 was one wave (metadata + trim) because the digest/version-bump cost is shared. It is
  split because `JERV_CONTEXT_BUDGET_PLAN` sequences the trim as a near-term wave while the
  metadata half stays blocked on §5 — and shipping half of an undivided W0 would leave this
  doc's header reading `W0◻️` with the work merged (`DOC_LIFECYCLE` transition 3).*

  Two corrections from that plan's adversarial review, both binding on W0b:

  - **The fattest five are a moving target — re-measure at the start of the wave.** As of
    2026-08-17 they are `web_fetch` 7,206 · `canvas` 6,723 · `deep_research` 6,703 ·
    `spawn_subagent` 6,438 · `deep_produce` 5,896 (desc+params chars) = **29.7% of jerv's
    111.0k**. `canvas` landed in second place days after this scope was written and displaced
    `analyze_stream`; sixteen sidecars now exceed 2.5k. Trim by measurement, not by the list.
  - **Target ~15–20%, not 20–30%, on the five fattest.** A paragraph-level read found most of
    the bulk is load-bearing: `web_fetch`'s anti-URL-fabrication rule and its "a search FORM is
    not evidence of absence" paragraph (both written after production failures), its
    YouTube-captions paragraph (which already *is* the cross-tool disambiguation), and its
    `extract` workflow (a mechanical backstop against the number-invention class);
    `analyze_stream`'s `mode`/`captions` prose, which carries the schema constraint an `enum`
    cannot (§ the GBNF segfault); `deep_research`'s ~2.6k preset catalog, which the model
    cannot call a preset without. `deep_produce`'s ~70% overlap with `deep_research` is the one
    large genuine reclaim — and capturing it means a W1-style umbrella merge, not a trim.
  - **`/api/debug/tool-probe` is not an acceptance gate.** It is a single ad-hoc converse — one
    `user_text`, caller-supplied tool list, proposed calls returned, no handler run, no case
    set, no baseline, no threshold. It was built to bisect gateway segfaults, and it found one.
    W0b changes *semantic* content, whose failure modes are argument-level and downstream
    (a guessed URL, `sources: web` for a library-only question). Either pre-build a fixture set
    with a numeric baseline — which is also finding #2 below, arriving early — or state plainly
    that W0b's acceptance is human review of the diff.
- **W1 — umbrellas.** Consolidate the source/action families (`records`, `grokipedia`,
  `research_report`, `external_video`), 19 → 4. Big count drop, no new machinery, per-source
  clarity preserved inside one guide. Keep each umbrella's param a typed discriminated union.
- **W2 — the catalog.** Build the menu + hot core + `tool_guide` + the turn-assembly change,
  mode **(b)** only, jerv behind `catalog_mode`. **Precondition:** the §7 mode-(a) contradiction
  is documented as out-of-scope for (b) (which doesn't hit it) and a *pre-built* selection-accuracy
  eval exists with a baseline number and a pass threshold (§10). Measure baseline intake tokens
  **and** selection accuracy before/after.
- **W3 — tune + migrate.** Settle the hot-core set from real usage (see telemetry note in §9),
  and *only if* the eval clears it, migrate to mode **(a)** — including the mid-loop tools-array
  recompute and a resolved auto-load-on-miss — bring remaining families under the catalog,
  re-measure.

## 7. Risks & mitigations

- **Mode (a) vs native tool-calling (the one to solve first).** Under native,
  grammar-constrained tool-calling (`ASSISTANT.md`'s hard commitment; the local model decodes
  against the tools grammar), the model *cannot* emit a `tool_use` for a tool absent from the
  turn's `tools` array — so "auto-load-on-miss" can't trigger through the sanctioned channel,
  and catching a prose mention would reintroduce the action-parsing DSL the design forbids.
  **Mitigation:** mode (b) sidesteps it entirely (schema always present). For mode (a), settle
  one of: a two-phase turn (menu-only "intent" pass → arm → real call), or keep the full
  `tools` array but defer only prose (i.e. (a) may simply not be worth it over (b)). Resolve on
  paper before any (a) work.
- **Catalog can hurt the primary cost (accuracy) in the long tail.** Replacing full schemas with
  ≤12-word lines *reduces* disambiguating info exactly where growth lands. Two adjacent tail
  summaries can collide (e.g. "search U.S. court records by name" vs "search federal
  rules/notices by name"). **Mitigation:** summaries must be written to disambiguate, and W2
  gates on measured accuracy, not vibes.
- **Guide hop is a real per-tool round-trip tax** on the slow model, and guides+schemas
  accumulate in the transcript for the rest of a turn that touches several tail tools —
  partially refilling the window. **Mitigation:** hot core covers the common path; keep guides
  tight.
- **KV-prefix cache churn** → menu + hot core are stable; but arming mid-turn (mode a)
  invalidates KV reuse from the tools block onward (§1). Another reason (a) waits.
- **Umbrella dispatch tools becoming grab-bags / harder to call** → keep each umbrella to ONE
  coherent family with a typed `source`/`action`; a guide per umbrella documents each variant.
  Watch that the polymorphic param doesn't cost more accuracy than the count it saves.

## 8. Success criteria

- Baseline per-turn tool intake stays ~flat (menu + hot core) as the tool count grows past
  today's ~50 toward 100+.
- Tool-selection accuracy on the eval set is **maintained or improved** vs. full-load — a
  numeric threshold on a fixed prompt set, not a vibe.
- No capability is undiscoverable from the menu.

## 9. Out of scope / open questions

- The exact hot-core membership (W3 decides from usage).
- Whether the guide is generated from the sidecar body or authored separately (lean: sidecar
  body *is* the guide; `summary` is the menu line).
- **Telemetry for the hot core:** raw data exists — every run writes a step log with tool calls
  (`runs` rows). What's missing is aggregation/ranking over it and a refresh cadence, so the hot
  core is a hand-maintained list until that's built. Scope the aggregation in W3.
- The full name-search *retrieval* registry (ranked search over tool guides) is deliberately
  **not** in this plan — it's a 100s-of-tools solution; the menu + hot core + `tool_guide`
  suffices well past our expected surface.

## 10. Review findings & recommendation (2026-08-08)

Two independent reviewers (a design/architecture lens and a codebase-fit lens) went over the
draft. Their verified facts are folded into §1/§3/§5/§6/§7 above. The load-bearing conclusions:

**Verified accurate:** the ~48-tool / ~23–26k-token footprint and the fat-tool list; that
`ToolRegistry`, `AgentLoop`, `schemas_for`, the `tools=None` curator, and `AgentProfile` flags
all exist and are the right seams; every umbrella family count (5/5/5/4).

**Corrected in this revision:** no prompt caching in the repo (the "dollars are solved" premise
was wrong); ~15% → ~18–20% of window; `schemas_for` is a registry method (not AgentLoop's);
`ToolSpec` `extra="forbid"` + digest guard makes `family`/`summary` a real decision, not a free
add; W0 is broad (digest/version bumps), not "no-change." (W0 is now split W0a/W0b — §6.)

**The two real design risks (both gate the catalog, not the cheap waves):**
1. **Mode (a) + auto-load-on-miss contradicts native tool-calling** (§7). Must be resolved on
   paper before any (a) work.
2. **W2's accuracy gate isn't a gate** if its eval harness is built *inside* W2. The eval must
   be pre-built with a baseline and threshold *before* the wave it gates.

**Recommendation — split the plan:**
- **Do now:** W0a/W0b (metadata + trim) and W1 (umbrellas). Low risk, no new machinery, capture the
  bulk of the token win at today's scale. Ship as their own PR(s).
- **Gate:** W2/W3 (the catalog machinery) behind (1) resolving the mode-(a) contradiction and
  (2) a pre-built selection-accuracy eval with a numeric baseline/threshold — and only if a
  measured occupancy/accuracy problem survives W0+W1.
