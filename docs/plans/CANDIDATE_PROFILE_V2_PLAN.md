# JBrain2 — Candidate Profile v2 (Records Pre-gather) Plan

> **Status:** In progress · **Last verified:** 2026-08-14 · **Waves:** C1✅ C2◻️

A leaner, more reliable variant of the `candidate_profile` report preset, offered **side-by-side**
with the shipped preset for a risk-free A/B (the `daily_news_v2` pattern — see
`DAILY_NEWS_V2_PLAN.md`). Two preset policies (`records_subject`, `lean_tail`) route `_run` through
new engine behaviour; the shipped `candidate_profile` sets neither and is byte-unchanged, so the two
can be run on the same candidate and compared in the box before either is promoted.

Peer to `REPORT_PRESET_PLAN.md` (the preset engine this extends) and `DEEP_PRODUCE_PLAN.md` (the
`Directive` that carries the new policies). It borrows the daily-news insight — *gathering is
deterministic work that should spend zero model tokens; reserve the model for writing* — and applies
it to the one gather step that was both load-bearing and, in v1, silently broken.

## The problem with v1 for this shape

Two things, found by comparing `candidate_profile` against the redone daily-news engine:

1. **The alias-harvest the methodology hinges on never runs.** `candidate_profile`'s objective and
   its "Controversies & legal record" angle command `public_records(name, sources=[…])` as the
   FIRST step — resolve the candidate's identity/aliases on Wikidata, then search CourtListener,
   NPPES, and the Federal Register under *every* prior/maiden name, because a license or case is
   very often filed under a name that is not the ballot name. But the `research` gather persona's
   tool ceiling (`RESEARCH_TOOLS`) does **not** include `public_records` (only its companion
   `portal_search` was ever added), and the parent⊆child clamp only narrows — so the gather
   sub-agents cannot call it and silently fall back to plain `web_search`. The single most
   important step in the preset was hoped for, not guaranteed. (The global allowlist gap is real for
   all research; v2 sidesteps it for this preset by running the lookups in engine code, and leaves
   the standalone allowlist fix as a separate call — see "Not doing".)

2. **The write tail always runs three full report writes.** A `report`-shaped preset runs
   draft → (mechanical backstop re-synth) → critique → revise unconditionally; the revise fires on
   *any* non-empty critique, even one with nothing material to fix — the same ~5-minute regression
   the daily-news redesign cut for briefs.

## The v2 design

**Two opt-in policies on the preset engine; the shipped preset opts into neither.**

1. **Deterministic public-records pre-gather (`records_subject`).** Before the gather fan, the
   engine itself — zero model tokens — resolves the subject on Wikidata (canonical name + every
   alias/maiden/former name + occupations), then concurrently searches CourtListener, NPPES, and the
   Federal Register under the ballot name AND every harvested alias, bounded (≤5 names, ≤6 hits per
   source per name). The result is wrapped as ONE synthetic gather finding — the alias set,
   labelled by which name each record was found under, with each URL a `read=True` citable
   `WebSource` — and prepended to the gather list, so it takes the low `[^n]` slots and flows into
   the analyst cross-check and the writer exactly like any finding. Best-effort: any absent client
   or dry lookup yields nothing and the fan gather stands alone (the run is unaffected). This
   guarantees the alias-harvest + primary-record sweep in code, and the v2 angle prose is rewritten
   to *build on* the injected finding instead of commanding an unreachable tool.

2. **Lean write tail (`lean_tail`).** The grounding critique still runs (it is the accountability-
   critical citation-faithfulness check), but it may return the sentinel `NO REVISION NEEDED` for a
   genuinely clean draft, and the engine then skips the second full-report revise write. Any concrete
   problem still returns a critique and drives the revise exactly as before — this spares only the
   redundant pass, never a real one. (The ~200-line objective is deliberately **not** trimmed from
   the later synth passes: it sits in a cacheable prompt prefix, so re-sending it is nearly free,
   and dropping it risked losing the nuanced status/label rules on the revise.)

## Wiring

- `research_presets.py`: two new preset fields — `records_subject` (a `{{var}}` template, rendered
  per run; its slots join the required variables) and `lean_tail` (a scalar bool). `records_subject`
  requires `sources: web` at load.
- `Directive` carries both; `_run_preset` fills them from the rendered preset.
- `_run`: after the gather branch, if `records_subject` is set and web-sourced, call
  `_prefetch_public_records` and prepend its finding (beside the existing EMR-seed / feed-pre-pull
  injection seams). In the critique/revise tail, pass `allow_skip=lean_tail` to `_critique` (which
  then offers the sentinel) and skip the revise when the sentinel comes back.
- `DeepResearchService` gains the four keyless public-records clients (`wikidata`, `courtlistener`,
  `nppes`, `federal_register`) — the SAME instances the `public_records` tool uses — threaded from
  `build_registry`/`main.py` alongside `feeds`/`searxng`/`fetcher`.
- New `candidate_profile_v2.preset`: a faithful twin of `candidate_profile` (same variables,
  sections, and angle substance) that sets `records_subject: "{{candidate}}"` and `lean_tail: true`,
  with the alias-harvest prose rewritten to build on the pre-gathered finding.
- The `deep_research` tool description lists `candidate_profile_v2` as an EXPLICIT-opt-in
  experimental twin: jerv uses it only when the owner asks for the "v2"/"experimental" profile and
  passes the name verbatim; an ordinary profile request still routes to `candidate_profile`. This
  correction landed after a first live A/B attempt: with v2 UN-advertised, jerv silently normalized
  an explicit "candidate_profile_v2" request back to the known `candidate_profile` (the twin never
  ran). Unlike `daily_news_v2` — pointed at by a scheduled task's preset name, bypassing jerv's
  tool-choice — a candidate profile is invoked conversationally, so the twin has to be namable.

## Waves

- **C1 ✅** — the two policies + `_prefetch_public_records` + the `candidate_profile_v2` twin,
  landed on-branch with unit coverage (preset load/render/validation; the pre-gather injects an
  alias-cross-referenced citable finding; v1 runs no pre-gather even with clients wired; `lean_tail`
  skips the revise on a clean critique). The shipped `candidate_profile` is byte-unchanged.
- **C2 ◻️** — live on-box A/B against `candidate_profile` on real candidates: measure record
  coverage (aliases surfaced, primary-source citations), faithfulness, and wall-clock; tune the name-
  variant cap / per-source limits. If v2 wins, **promote** it (fold the policies into
  `candidate_profile`, retire the twin) and decide the standalone `RESEARCH_TOOLS` allowlist fix.

## Not doing (yet)

- **The global `public_records`/`grokipedia` allowlist fix.** Adding them to `RESEARCH_TOOLS` would
  fix the tool gap for *all* research, but it also changes v1's live behaviour — muddying the A/B.
  v2 delivers the records for this preset deterministically instead; the allowlist fix is a separate,
  small change to greenlight after the A/B (or alongside, the owner's call).
- **Deterministic FEC pre-gather.** There is no FEC client (FEC is reached via `web_search` on
  `fec.gov`); the gather agents still cover funding. Adding an OpenFEC client is a follow-up.
