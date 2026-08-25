# Context compaction — one cross-session capability (compact tool + auto-compaction)

> **Status:** Proposed · **Last verified:** 2026-08-25

Give the agent loop a way to keep going when a single turn's context fills up,
instead of ending the turn. Two surfaces, one mechanism: an **on-demand
`compact` tool** the agent calls mid-turn, and **automatic compaction** that
fires when a turn approaches the model's context window. Built once, inside the
shared loop, so **every** caller inherits it — jerv (interactive), spawned
sub-agents, jmolt's autonomous night, owner Tasks, plan continuations, intake.

Motivated by two live needs that are the same need: jmolt is meant to spend an
unsupervised hour reading and posting, but its night is a single agent turn
whose context only grows — it will hit the window long before the clock; and
jerv's long fan/ReAct turns (a scout doing 20+ fetches, a child carrying a 60k
transcript) hit the same wall. Today an overflow just **kills the turn**
(`stop_reason=context_overflow`), losing all the work. Composed from four
research passes (loop seam, existing machinery, accurate accounting, strategy +
firewall safety); their findings and file references are folded in below.

## 0. Cold-review verdict (2026-08-25) — design reversed; do NOT build as first written

Four cold, adversarial reviewers (mechanism, trigger/accounting, safety/fence,
methods) independently concluded the **summarize-in-place** design in §2–§11 below
should NOT be built as written. Sections §1–§9 remain useful research context, but
where they conflict with this section, **this section wins.** The reversal:

- **Summarization is the wrong default method here.** A fabrication-prone local
  120B (documented, prompt-resistant — `../plans/DEEP_RESEARCH_SCRATCHPAD_PLAN.md`)
  in every turn's hot path re-imports the exact failure this repo already engineers
  around. The method becomes: **Tier 0 offload (verbatim, reuse `tool_artifacts`) +
  Tier 1 deterministic, LLM-free structured eviction** (drop stale reasoning /
  non-artifact tool bodies, keep a typed one-line record per round + the citation
  side-channel + IDs/URLs verbatim + the last K rounds). Once you list what's
  load-bearing, there is almost nothing left for a summarizer to add — so the
  **LLM summarizer is demoted to a gated, off-by-default emergency backstop**,
  entered only when deterministic eviction + narrowing the verbatim window still
  can't clear headroom, and shipped only **after an on-box A/B** (the deep_research
  P1.5 precedent).
- **Safety: the fence approach in §8 does not hold.** The summary would be minted
  as a `UserMessage` — jmolt's *trusted* channel — and the `untrusted_external_data`
  tag §7/§8 reuse appears **zero times** in `jerv.prompt`/`jmolt.prompt`, so it
  no-ops in exactly the sessions that read hostile text. Summarizing strips the
  prose `_FENCE`; "keep it fenced" is a prompt-hope on a model whose threat model
  declares fences non-binding. It reopens jmolt's M2/A3 memory-injection vector
  **intra-night**. The "most-restrictive domain" stamp is uncomputable from today's
  per-turn machinery, and the offload store stamps `general` (most-permissive), the
  opposite of fail-closed. These are why the summarizer is gated, not merely tuned.
- **Mechanism: no repeated-compaction story, no termination guarantee.** "Compact
  once" (§5) contradicts a continuously growing turn; the plan defines no 2nd..Nth
  compaction, no progress floor, and the summarizer reads the very near-overflow
  context it's shrinking (it can overflow during compaction). The verbatim window
  (§11.5) is an unbounded floor with no escape.
- **Trigger: arithmetic OK, safety net broken.** The 80% trigger is sound for
  steady in-turn growth (the exact prior-step numerator dominates), but
  compact-and-retry on `LlmContextOverflowError` can **livelock** (overflow charges
  no cost/step budget) and **silences the operator's only "raise the on-box window"
  signal** (non-negotiable #10); overflow is **local-only and pre-200-only**, so the
  default cloud route and mid-stream overflow reach no backstop at all. Cheap real
  fixes: denominator = the live `/slots` `n_ctx` (not the saved window); pending-term
  = exact char count × a conservative ratio; a hard retry cap that falls back to the
  terminal `context_overflow` signal.
- **Rollout: "everything at once" is a risk concentration.** Stage it: deterministic
  eviction + offload first (no model on the hot path), the gated summarizer later
  behind an on-box A/B, `/chat` cross-turn persistence as its own change.
- **jmolt is split out.** For the one caller that reads attacker text unsupervised,
  in-place summarization is the worst fit; its memory is already externalized to a
  file scratchpad, so a **mechanical scratchpad-flush + fresh-context sitting** is
  strictly safer and needs no summarizer. That work moves to its own near-term plan,
  **`JMOLT_SITTINGS_PLAN.md`**; this document remains the deferred, general capability.

**Status of this doc:** parked as a researched-and-reviewed proposal. Not scheduled.
The near-term jmolt need is met by `JMOLT_SITTINGS_PLAN.md`; the general capability
here waits until the deterministic-eviction design (Tier 0/1) is specified to the
depth the cold review demands.

## 1. The core fact that shapes everything

There is **no compaction, summarization, or history truncation anywhere in the
backend today.** `context_window` / `context_tokens` exist but drive only the
PWA's live usage meter — nothing consumes them as a trigger (see
`../plans/CROSS_TURN_TOOL_RESULTS_PLAN.md` §5). So this is genuinely new
behaviour, and the meter already gives us the auto-trigger signal for free.

The context that actually overflows is the **in-turn `messages` list** inside
`AgentLoop`, not the cross-turn conversation. Within a turn, `messages` grows by
an `AssistantMessage` (tool calls) + a `ToolResultMessage` (observations) every
ReAct step; the fat is in the tool results (a `web_fetch`/YouTube body can be
tens of thousands of chars). Cross-turn, tool results already evaporate
(`ChatMessageIn` carries only `role`+`content`), so the durable-memory story is
already handled elsewhere (the artifact reference-line injection + `read_artifact`,
and the scratchpads). **This capability operates on the in-turn `messages`
list** — the volatile side. It is the complement to the durable-side memory
stores, not a fifth one.

## 2. Shape: two-tier eviction, one routine

When a turn needs to shed tokens (by tool call or by threshold), run one routine
on the in-turn `messages`:

1. **Offload first (lossless, preferred).** A tool result that is backed — or
   backable — by the cross-turn tool-result artifact store (`web_fetch`,
   YouTube, `ocr`, `gmail`, …; `agent/tool_artifacts.py`,
   `../plans/CROSS_TURN_TOOL_RESULTS_PLAN.md`) is **evicted from `messages` and
   replaced by its compact DATA-framed reference line** (the same line
   `api/agent.py` already injects). The exact bytes stay re-readable via
   `read_artifact` with no network — no summarization, no loss. This is the
   "tool-result eviction + retrieval-on-demand" pattern, and it minimises how
   much the weak local model has to summarize.
2. **Summarize only the remainder.** Content with no artifact backing (inline
   reasoning narrative, small search snippets, non-artifact tool text) in the
   compacted region is folded into **one** synthetic `UserMessage` — a
   Claude-Code-style "conversation-so-far" block — placed **after** the stable
   system/history prefix, with the last **K** ReAct exchanges kept **verbatim**.

Both the `compact` tool and auto-compaction share this routine. The summarizer
call reuses the existing `_forced_final` primitive (a tool-free "synthesize from
what you've gathered" turn already in the loop), run through the LLM adapter at
low/`none` effort.

## 3. The seam — where it hooks so all sessions inherit it

One owner: **`AgentLoop`**, operating on its per-step `messages` accumulator. The
logic lives in the two near-duplicate step loops — `AgentLoop.run_stream`
(interactive jerv, jmolt, Tasks, continuations, intake) and `AgentLoop.run`
(spawned sub-agents, wiki editor) — plus the reflexion `_produce_buffered`
clone, which must be kept in sync or `buffer_retry` turns silently miss
compaction. `LoopTurnExecutor.run_turn` is **not** a lower seam — it just
delegates to `run_stream`; and `/chat` and intake call `run_stream` directly, so
only `AgentLoop` itself is universal.

The splice is clean because `messages` is a flat, ordered, in-memory list the
loop rebuilds each turn from the caller's `conversation`, then grows by adjacent
`AssistantMessage`/`ToolResultMessage` **pairs**. Compaction keeps the seed
(system-adjacent prefix + current user turn) and the last K rounds, and rewrites
the middle. **Hard constraint:** cut only on whole ReAct rounds — never orphan a
`ToolResult` from its `ToolCall`, or the next provider call is malformed.

Resolve the window **inside** the loop (`self._router.context_window(self._task,
spec_override=self._model_override)`) rather than trusting the caller-passed
`context_window`, so the `run`/spawn path — which passes none today — also
inherits auto-compaction.

**Two known bypasses (name them, don't pretend they're covered):**
- **`deep_research`'s own synthesis/analysis/critique** calls `router.converse_stream`
  directly, not via `AgentLoop`; it already self-manages with a bespoke
  `_fit_findings_to_window`. Its research *children* run through
  `spawn → AgentLoop.run`, so they inherit compaction; only its top-level
  single-shot calls stay self-managed. In scope as a follow-up, not wave 1.
- **`/chat` cross-turn history** is client-supplied and replayed each turn.
  In-turn compaction shrinks only within-turn growth (the actual overflow mode);
  making a compacted turn's savings persist into the *next* turn would need a
  client/persistence contract. Backend-driven callers (jmolt, Tasks, spawn)
  build their conversation server-side and have no such split — so jmolt, the
  motivating case, is fully covered.

## 4. Accurate triggering (the owner's hard requirement)

Exact context fill is **only knowable after each model call** (`turn.usage.input_tokens`,
provider-reported). There is deliberately **no pre-call tokenizer** (calling
`/tokenize` was rejected — it costs a second full prompt send). So the trigger is
built from what is honestly available:

- **Numerator, exact:** the previous step's `turn.usage.input_tokens` — the loop
  already floors its live meter on this and persists it across turns.
- **Numerator, pending additions:** estimate the just-appended tool results via
  the existing per-model calibrated char/ratio estimator (`llm/prefill.py`),
  which converges within a turn or two.
- **Headroom:** reserve the per-step output budget (`TURN_MAX_TOKENS`) **plus a
  reasoning-trace margin** on local reasoning models — gpt-oss bills its hidden
  thinking trace against the same budget, so a prompt-only check under-counts on
  jmolt's route.
- **Denominator:** `router.context_window()` — exact for catalogued models
  (gpt-oss-120b = 131,072). **Two accuracy holes to guard:** an unlisted cloud
  model silently falls back to 128k, and a stale local `-c` override can
  over-report the served window.
- **Threshold, not the edge:** because the pending-additions term is an estimate
  (±10–20% until calibrated), fire at a **conservative fraction** (~75–80%),
  leaving room for the summarizer's own call. Claude Code uses ~83.5%; a weaker
  model + synthesis headroom argues lower.
- **Overflow as a backstop, never the primary trigger:** `LlmContextOverflowError`
  already exists and is caught — today it kills the turn. Re-wire it to
  **compact-and-retry** as a last resort. It must not be primary: it is
  local-only, and reaching it means the turn already failed.

## 5. What MUST survive compaction

Grounding here is **not carried in prose** — it rides a structured side-channel
that summarizing message *text* would silently destroy. Compaction must
**preserve, never regenerate**:

- The **citation side-channel** — `surfaced_sources` / `surfaced_entities` /
  `web_sources` (and a child's `AgentResult.web_sources`, the only way its
  citations cross the boundary). These are append-only accumulators that already
  outlive individual messages; leave them untouched and let the summary
  reference sources by their stable index. A summary that re-derives citations
  from its own prose is the classic provenance-collapse failure — and on gpt-oss
  it would invent citation numbers.
- The **KV-cache prefix** — system + owner-self + stable history must stay
  byte-stable (the layout `../plans/TOOL_CATALOG_PLAN.md` and the cache-stable
  code protect). Rewriting mid-history busts prefix reuse from the edit point on.
  So compact the **oldest stable prefix once** (accept a one-time re-prefill),
  not mutate mid-history every turn.
- The current user request; any pending plan and its recorded step results;
  artifact reference lines; and exact numbers / IDs / URLs.

## 6. The on-demand `compact` tool

Signature: `compact(keep?: str, focus?: str)` — both optional prose. `keep`
mirrors Claude Code's `/compact only keep …`; `focus` states what the agent is
about to do so the summary is task-relevant. **No numeric/offset args** — the
local model must not pick byte ranges.

Mechanics: the tool needs to reach the loop's `messages`, which `ToolContext`
does not expose today. Add a per-turn compaction callback to `ToolContext`
(mirroring how `emit_progress`/`emit_event` are injected), or return a
`ToolOutput.compact` sentinel the loop acts on **after** appending the round (so
the tool's own round is included). Return value to the model is a short
observation — "compacted N earlier steps, freed ~X tokens, sources preserved,
continue" — not the summary itself.

## 7. Reuse map (don't build a fifth of anything)

- **REUSE — the artifact substrate** (`agent/tool_artifacts.py`, `read_artifact`)
  as the offload target. Needs a new `kind` / synthetic `source_ref` for
  dialogue spans, and — unlike today's artifact injection, which only *adds* a
  reference — compaction must also *remove* the evicted body from `messages`.
- **REUSE — the security envelope** (`agent/briefs.py`: the
  `untrusted_external_data` sentinel + `neutralize_boundary()`) for any
  re-injected summary text.
- **ALIGN — `../plans/TOOL_CATALOG_PLAN.md` and
  `JERV_CONTEXT_BUDGET_PLAN.md`.** They reduce fixed per-turn overhead (tool
  schemas, prompt); compaction reduces accumulated conversation. Complementary
  knobs on the same window — they must share one token-accounting story, and
  compaction inherits their KV-cache-stability and injected-prose-governance
  constraints.
- **PRECEDENT — forced-final synthesis** (`_forced_final`) is today's crude
  *terminal* compaction; this generalises it to compact-and-continue. **Sequence
  them:** auto-compaction is the earlier, higher-ceiling intervention;
  forced-final stays the last-resort backstop, so a turn never compacts and then
  immediately force-finals on the same pressure. A supervised (owner-watching)
  turn — which already lifts caps hugely — is exactly where compaction, not
  termination, is wanted.
- **DISTINCT — the durable memory stores** (agent episodic/ACE memory, jmolt
  scratchpad, deep_research's in-memory ledger, the proposed `agent_scratchpad`).
  Compaction is the volatile-side complement; it writes into the artifact
  substrate, not a new store. Its one borrowed discipline: **structured deltas
  beat prose rewrites.**

## 8. Firewall / fence safety

Non-negotiable #8 already anticipates this feature by name: *"Every
agent-internal job (reflection, compaction) runs at the domain scope and
principal of the content/session that triggered it — never an escalation to
owner scope."* The summarizer call runs on the **triggering session's own
RLS-scoped `SessionContext`** (`ToolContext.session`), so Postgres RLS makes
cross-firewall reads impossible during the call — the summary can only contain
what that session could already see.

Checklist for the eventual build:
- [ ] Summarizer runs on the triggering session's RLS scope, never owner-escalated (#8).
- [ ] Summary stamped with the **most-restrictive domain** touched by the content it
      replaces (fail-closed — the asymmetric rule episodic memory already uses).
- [ ] The compaction summary is **not persisted** cross-session by the compaction step;
      if ever persisted, it needs its own RLS isolation test (#3).
- [ ] Summarizer runs **tool-free** (no actuator for an injected instruction), input
      wrapped in the `untrusted_external_data` boundary, output `neutralize_boundary()`-filtered.
- [ ] Any compacted `_FENCE`-marked content forces the **whole summary to stay fenced /
      non-authoritative** — mixed provenance downgrades to untrusted (jmolt: the summary of
      a poisoned Moltbook thread is still attacker text; attribute "an agent claimed X",
      never assert).
- [ ] The summary is injected as **DATA in the volatile suffix**, never as sanctioned
      instruction — it cannot mint plan approvals, preferences, or corrections (the
      data→instruction laundering path the scratchpad plan warns about).
- [ ] Citation side-channel preserved untouched (§5).
- [ ] All LLM calls via the adapter (#1); all persistence via the storage abstraction (#2).
- [ ] Auto-compaction **inhibited during jmolt's wind-down write window** and during any
      un-persisted scratchpad write — losing an un-persisted note there is jmolt's one
      irreversible failure.

## 9. Tuning for the weak local summarizer (the "be smart" core)

The local gpt-oss-120b's documented failure mode is **fabricated precision** —
inventing a sample size, a "doubling", a wrong year (evidence in
`../plans/DEEP_RESEARCH_SCRATCHPAD_PLAN.md`, survived three prompt versions). So:

1. **Offload beats summarize** — every artifact-backed body evicted-with-reference
   is one thing the model never has to (mis)summarize.
2. **Extractive, not abstractive** — the summarizer copies IDs, numbers, URLs,
   names, and pending TODOs **verbatim** into a fixed skeleton (Decisions / Open
   task state / Key facts verbatim / Sources-by-index). Never let it restate a
   number it could copy.
3. **Never summarize a summary in the same turn** — recursive re-summarization
   compounds drift; prefer widening the verbatim window or offloading more.
4. **Brain-dump before compact** — force the model to list load-bearing facts
   first, so the summarizer works from an explicit list, not implicit memory.
5. **A cheap post-compaction probe** — keep Reflexion's existing pure,
   no-model-call grounding verifier running after a compacted turn; if the
   answer's claims stop grounding against the (preserved) corpus, compaction
   dropped something.

## 10. Wave sketch (per the ratified decisions in §11)

> **Superseded by §0.** The cold review reversed the single-wave scope; the staged
> rollout in §0 replaces this sketch. Kept for the record.

The owner chose to build the whole capability in one wave rather than stage it.

- **W1 — the full capability, all sessions.** Auto-compaction (§2–§5) in
  `run` / `run_stream` / `_produce_buffered`, window resolved in-loop; the
  on-demand `compact(keep?, focus?)` tool (§6) with its `ToolContext` plumbing;
  and `/chat` cross-turn persistence — surface a compacted turn back to the PWA so
  its savings survive into the next turn (the one caller with a client-owned
  history, per §3). Two-tier eviction, offload-first with **conditional**
  summarization (§2, §11.4). `LlmContextOverflowError` re-wired to
  compact-and-retry. Full firewall/fence checklist (§8) and citation-side-channel
  preservation (§5). jmolt additionally gets its **live time-countdown** injected
  each step — compaction bounds its context, the clock makes it *use* the hour
  (§11.2). Real-Postgres + faked-LLM tests, including an RLS/fence test that a
  compacted summary of health/attacker content stays scoped and fenced.
- **W2 — the two known bypasses + on-box tuning.** `deep_research`'s own top-level
  synthesis/analysis/critique calls (which bypass `AgentLoop` and self-manage via
  `_fit_findings_to_window`) get their own compaction pass or a refactor onto the
  shared loop; and the threshold, verbatim window, extractive prompt, and probe are
  tuned against real long jerv turns and a real jmolt hour.

## 11. Decisions — ratified 2026-08-25 (then reversed by the §0 cold review)

> Decisions 1, 2, and 4 were **reversed** by the cold-review verdict (§0):
> everything-at-once → staged; jmolt in-place compaction → mechanical sittings
> (`JMOLT_SITTINGS_PLAN.md`); offload-first-then-summarize → deterministic eviction
> with the summarizer demoted to a gated backstop. Decisions 3 (threshold) and 5
> (verbatim window) stand but now apply to the deterministic path. Kept for the record.

1. **Scope — everything at once.** Auto-compaction + the on-demand `compact` tool
   + `/chat` cross-turn persistence ship together across all sessions in W1,
   rather than staging backend-only first.
2. **jmolt's night — compaction + a clock.** The bounded-sittings redesign is
   **dropped**: one night stays one turn, compaction keeps its context bounded, and
   a live "woke at X, ~N min left" countdown injected each step makes jmolt pace
   itself across the hour. Compaction solves *context*; the countdown solves
   *pacing* — both in scope, sittings not needed.
3. **Trigger threshold — ~80% of the window, tunable.** A config knob defaulting
   to 80%, calibrated on the box against real turns.
4. **Eviction — offload first, then summarize (conditionally).** Always evict
   artifact-backed tool bodies to their `read_artifact` reference line (lossless);
   run the local summarizer on the remainder **only if** offloading alone did not
   clear the threshold — so a typical big-fetch overflow summarizes nothing, and the
   weak model is a last resort under the §9 guards. (Owner was uncertain; recorded
   as the recommended offload-first bias — revisit toward offload-only if on-box
   summaries prove unreliable.)
5. **Verbatim window — the last 3–4 ReAct rounds** stay untouched, favouring
   reasoning-continuity over per-compaction reclaim; tunable alongside the
   threshold.

## 12. Non-negotiable reconciliation (when promoted)

LLM calls (the summarizer) via the adapter (#1); persistence (offload) via the
storage abstraction (#2); the summarizer on an RLS-scoped session with a fresh
fence + isolation test for any persisted summary (#3); tests land with the code,
security/firewall paths at 100% (#5); Conventional Commits + PR + green CI (#6);
docs travel with the code — promote this out of `../proposed/`, give it a
`../ROADMAP.md` slot, and resolve the open compaction question recorded in
`../reference/ASSISTANT.md` (#9).
