# Deep-research scratchpad — a run-scoped findings ledger

> **Status:** In progress · **Last verified:** 2026-08-09 · **Waves:** P1✅ P1.5✅ P2◻️
> (P1 = the in-memory ledger + the scope-model refactor of `deep_research._run`, shipped
> with unit coverage and a byte-identical no-regression seam. **P1.5 = the behavioral change**:
> the synthesizer is fed a per-finding claim→source binding so it cites the source a finding
> actually drew on instead of re-guessing by title — the anti-mislabelling fix (§8).
> **Not yet on-box validated** — the citation path is delicate (see the v9–v16 history), so the
> effect on real citation quality wants a run against the actual box/model before it's relied on.
> P2 = the scope-model unlocks — feed the ANALYSIS entry into the critique, per-researcher
> partitioning for a comparison run — deferred until a comparison/partitioned mode needs them.)

Reconciled with the root `CLAUDE.md` non-negotiables: no new datastore (the ledger is an
in-memory object that lives and dies with one in-request run — no migration, no RLS table,
no isolation test, because there is no cross-turn actor to firewall against); every model
call still routes through the LLM adapter; the feeding-waves security envelope
(`compose_feed_block`) is preserved verbatim as the layer that neutralizes attacker-influenced
fetched text — the ledger only decides *which* entries a stage sees, never how they are
rendered.

---

## 1. Why

The owner observed that `deep_research` "just passes it and relies on the agent to use context
to find what it needs — this may be contributing to hallucinations," and asked whether a
plan-like scratchpad (as the jerv Planning Tool has) would help. Four independent researchers
(advocate / skeptic / architect / prior-art) evaluated it against the real code and the
`deep_research` v9–v16 hallucination history. The findings, condensed:

- **The framing was half-inverted.** `compose_feed_block` (`agent/briefs.py`) already hands
  every downstream stage a *structured, per-researcher, labelled* block (`## {angle} ({persona})`
  per child) — not a formless blob. A jerv-style prose scratchpad would largely re-implement
  what exists.
- **jerv's durability does NOT transfer.** jerv's step-results scratchpad is a `results jsonb`
  DB column *because a plan spans many owner turns and each continuation step is a fresh,
  context-less turn* — the DB is its only channel between steps. A `deep_research` run is the
  opposite: the whole pipeline runs in **one coroutine, one process, one owner turn**, holding
  every stage's output in local variables. A DB-backed scratchpad would add a migration + a
  mandatory RLS isolation test to solve a problem that does not exist here. The codebase already
  reserves durable checkpointing for the *background deepest lane* (`on_round`, explicitly
  `None` on the in-request path) — the deliberate signal.
- **A scratchpad will not fix number-invention.** The documented v12/v14 failures (invented
  sample sizes, a fabricated "doubling", a mislabelled year) are the local `gpt-oss-120b`
  fabricating precision — a *generation-discipline* problem addressed by prompt rules +
  mechanical backstops, a separate track from this one.

What *is* worth doing, and what P1 ships: make the intermediate findings a **first-class,
run-scoped ledger with an explicit visibility model**, so the owner's actual ask — "scoped so
researchers don't all cross-talk, but the whole set is fed to the critique/revise" — is an
inspectable, testable rule rather than an implicit property of which list each call happened to
be handed. For today's default flat fan this is ~80% a behaviour-preserving refactor; its
payoff is the explicit scope model, a single source collection, per-entry source identity as
the substrate for a comparison mode, and the removal of a class of "which list do I pass here"
footguns.

## 2. The ledger (`agent/research_scratchpad.py`)

An in-memory object threaded through one `_run`:

```
ScratchEntry: author, persona, stage, scope, text, sources[], result?   # ≈ _ChildResult + (stage, scope)
Scratchpad:   add_child / add_children / add_text ;  read(scopes) ;  children(scopes?)
```

- A producer/review CHILD is recorded with its backing `_ChildResult` (so `ok`, `truncated`,
  the roster, and its own reached sources ride along). The synthesized DRAFT is a text-only
  entry (`result=None`), so it never enters the roster or the source registry.
- **Insertion order IS run order** (gather → analyst → refill rounds → draft → critic), so
  `children()` reproduces the exact roster and `_collect_sources` over it keeps the first-seen
  `[^n]` numbering the previous `[*gather, *analyst, *refill]` concatenation produced.

## 3. The visibility model (the owner's "scoped, no crosstalk" rule, made first-class)

| Stage | Reads scopes | Writes |
|---|---|---|
| gather child | ∅ — spawned in isolation, never handed a sibling's entry (no crosstalk) | `RESEARCH` |
| analyze | `{RESEARCH}` | `ANALYSIS` |
| reflect | `{RESEARCH}` (+ the analyst summary, threaded as a string) | — (gaps) |
| refill child | ∅ — isolated | `RESEARCH` |
| synthesize | `{RESEARCH}` (+ analyst) | `DRAFT` |
| critique | `{DRAFT}` + the RESEARCH source registry | `CRITIQUE` |
| revise | `{RESEARCH}` + analyst + `{DRAFT}` + `{CRITIQUE}` | `DRAFT` (replace) |

Researcher **isolation is the ABSENCE of a read** — a gather/refill child is spawned with no
ledger access at all, so a RESEARCH entry is never handed sideways to a sibling. The analyst,
synthesizer, critique, and revise stages read the whole research set. This was already true of
the flat fan; P1 makes it queryable and covered by a test instead of implicit.

## 4. Data flow

```
     RUN-SCOPED Scratchpad (in-memory; dies with _run)
  gather fan  ─► add N  scope=RESEARCH        (siblings read ∅)
  analyze     ─► read{RESEARCH}             ─► add scope=ANALYSIS
  reflect     ─► read{RESEARCH}(+analyst)   ─► gaps
  refill fan  ─► add M  scope=RESEARCH        (siblings read ∅)
  synthesize  ─► read{RESEARCH}(+analyst)   ─► DRAFT (text entry)
  critique    ─► read{DRAFT}+RESEARCH srcs  ─► add scope=CRITIQUE
  revise      ─► read{RESEARCH,DRAFT,CRITIQUE}(+analyst) ─► replace DRAFT
        every findings read() → compose_feed_block (envelope unchanged)
```

The ledger is a **structuring layer over** `compose_feed_block`, never a replacement: the
entries a stage reads are still serialized through that envelope, which neutralizes boundary
sentinels in fetched text and size-caps each summary. Source collection now happens **once**
(`_collect_sources(scratch.children({RESEARCH, ANALYSIS}))`) instead of being recomputed over
ad-hoc concatenations at three points.

## 5. What P1 changed (reuse-first)

- **New** `agent/research_scratchpad.py` — the two dataclasses + `read`/`children`. No DB, no
  tool, no persona/allowlist change, no prompt change, no migration.
- **`agent/deep_research.py`** — `_run` instantiates one `Scratchpad`, records each stage into
  it, and reads the RESEARCH/ANALYSIS scopes where it previously threaded `gather`/`refill`/
  `results` lists. `_findings_block` is now entry-based; the sequential-staging feed inside
  `_gather_staged` keeps its child-based helper (`_stage_feed`) because staging feeds *within*
  the gather round, before entries carry a scope. `_analyze`/`_reflect`/`_synthesize` take
  ledger entries.
- **No behaviour change on the default path.** The composed feed a stage reads is byte-identical
  to feeding the raw children (guarded by a unit test); the citation registry, roster, and
  report view are unchanged (the shipped `test_deep_research.py` suite — 113 tests — passes
  untouched).

## 6. Tests (`tests/unit/test_research_scratchpad.py`)

Real fakes, no DB, per `CLAUDE.md` #5. The visibility model (`read(scopes)` partitions by
scope; researcher entries never leak into an unnamed scope), roster/source reproduction
(`children()` == the run roster in order; the DRAFT text entry is excluded), the entry
`ok`/`truncated` mirroring, the **byte-identical no-regression seam** (an entry-based feed
equals feeding the raw children, and a failed/empty child is dropped identically), the
**injection guard** (a RESEARCH entry that tries to close the data envelope is still
neutralized after a `read`), and two end-to-end wiring assertions over the real service:
gather researchers are spawned with no sibling feed (isolation), while the writer and the
analyst are fed the full research set. The existing `test_deep_research.py` suite is the
whole-pipeline no-regression proof.

## 7. Deferred (P2) and the separate track

- **P2 — scope-model unlocks.** Feed the ANALYSIS entry into the critique (one scope change);
  per-researcher partitioning for a comparison run (tag each candidate's researcher so findings
  can't bleed). Both are one-line refinements on the P1 substrate — built only when a
  comparison/partitioned mode needs them.
- **Separate track — number-invention.** The biggest remaining hallucination class
  (fabricated numbers/attribution by the local model) is orthogonal to the ledger; it stays on
  the prompt-discipline + mechanical-backstop lineage that shipped the v12/v14/v16 fixes (e.g.
  a "cited number appears verbatim in a cited source" backstop, reusing the one-retry re-synth
  pattern). Not in scope here.

## 8. Phase 1.5 — the claim→source binding (landed)

P1 made each ledger entry carry its own reached pages; P1.5 uses them. The synthesizer's feed
now **prefixes each finding with the exact `SOURCES` numbers that finding's own research pages
map to** — a line like `Sources this finding drew on: [^3], [^7]` heading the finding
(`_cited_findings_block`). This is the anti-mislabelling fix the four-researcher review
identified: the writer was told the children's own `[^n]` are private numbering and to renumber
"by choosing the source whose title best backs that claim" (the synth prompt) — i.e. it
re-derived every citation by title-matching against the whole registry, the documented root of
the v12/v14 mislabelled-citation failures. With the binding, a claim from a finding is cited
against the source THAT finding actually reached.

- **A binding, not a renumber.** It does NOT rewrite the children's inline `[^n]` (whose order
  is "the order the sources appeared" — a per-child order the existing design deliberately does
  not trust). It uses only each entry's `web_sources`, resolved through `_canonical_url` (the
  same dedup key the registry uses) to the FINAL curated registry index — correct regardless of
  the child's citation ordering, and robust to curation (a page pruned from the registry is left
  unbound). The note is PREFIXED so it survives the feed envelope's per-summary size cap (which
  truncates the tail).
- **Prompt.** `deep_research_synthesize.prompt` adds one clause pointing the writer at the
  per-finding binding and making title-matching the FALLBACK (a finding with no binding line,
  or a claim that spans findings). Every existing must-cite / all-noise-escape / on-topic rule
  is unchanged. Only the synthesize path changed — the analyst/reflect feed (`_findings_block`)
  and the critique are byte-stable. **Version note:** the prompt's `version` field is held at
  **v11** on this branch so the existing `test_deep_research.py` version pin passes without
  re-pushing that large file; bumping it to **v12** and updating the pin (+ a phrase assertion)
  is a trivial follow-up when this integrates.
- **Caveat — do not skip.** This changes the delicate citation path and is **NOT on-box
  validated**. The unit tests prove the binding is computed and fed correctly; whether the local
  model (gpt-oss-120b) actually cites better with it is an empirical question the v9–v16 lineage
  says must be checked on the real box before the improvement is relied on. Reverting is a
  one-liner (`_cited_findings_block` → `_findings_block` in `_synthesize`; drop the prompt
  clause).

Tests (`test_research_scratchpad.py`): the index map + per-finding markers (dedup via
`_canonical_url`, registry order, a curated-out page binds nothing); `_cited_findings_block`
prefixes each finding with its bound numbers and falls back cleanly with no registry / an unbound
page; end-to-end the writer's message carries the binding.
