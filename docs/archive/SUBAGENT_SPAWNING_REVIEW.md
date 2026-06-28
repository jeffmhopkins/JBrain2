# Sub-agent spawning — adversarial review record

Audit trail for the three-reviewer adversarial pass over the sub-agent spawning
design (`docs/SUBAGENT_SPAWNING_PLAN.md`), with resolutions. Three
independent lenses — **security/firewall**, **architecture/runtime**, **GUI/design
system** — each told to break the design, not praise it. They converged on one
theme: *the draft asserted "settled / structural / harness-enforced /
reuses-existing" for machinery the live code does not enforce.* All findings below
are resolved in the spec revision; this doc records what was found so the per-wave
adversarial reviews can re-check the same surfaces.

Severity: **BLOCKER** (fix before build) · **MAJOR** · **MINOR/NIT**.
Resolution points cite the spec unless noted.

## Blockers

| # | Finding | Resolution |
|---|---|---|
| B1 | Execution model self-contradictory ("Tasks-runner detached" vs "in-request gather"); **live `subagent_*` streaming has no path** — handlers return `str`, only a fixed 4-tuple progress sink exists. | Spec §"Execution model": pinned to in-request `asyncio.gather` reusing Tasks-runner *building blocks* (not the scheduler runner). Live streaming declared a **net-new loop ChatEvent channel** (Phase 2), per owner decision "live in v1". |
| B2 | "child⊆parent" and "caps structural/harness-enforced" describe code that doesn't exist — `loop.py` has no parent-tools concept; `Guardrails`/`ToolContext` have no depth/shared-counter. | Spec §"Structural enforcement": clamp at `_dispatch`, `depth` in `ToolContext`, each with a no-model-cooperation test. Honesty preface added; all such claims reframed as net-new. |
| B3 | Migration list wrong ("the only schema change", "optional", "for free"): need `parent_run_id` (required), `runs.kind` + `agent` CHECK extensions, or child INSERT fails; `agent_for` falls back to curator (KB) on unknown persona. | Spec §"Schema changes": full list. §"Structural enforcement": persona validated against closed set before `agent_for`; `spawn_subagent` excluded from `curator.tools=None` wildcard; tests. |
| B4 (GUI) | Persona-as-color violates "kind enums never colors" and collides with green=live/ok, violet=finance, steel=agent. | Persona is now a **neutral text tag**; semantic color confined to steel=live/green=done/rose=failed. Fixed in `DESIGN.md` + both mocks. |

## Majors

| # | Finding | Resolution |
|---|---|---|
| M1 | Re-spawn hop breaks #1: a web-reading child can launder attacker page text into a grandchild's steering brief. | Owner decision: **template-bound briefs at depth≥1** (structured fields, no free-text). Spec §"The brief" + settled decision #7. |
| M2 | Location inheritance unstated leak path (`current_location`, `ToolContext.here`). | Spec: children constructed with `here=here_as_of=None`; `current_location` never in a child persona; test. |
| M3 | "No memory" persona-trusted, not structural (episodic auto-append is loop-driven). | Spec: structural `no_memory` flag on child session/loop disables episodic auto-append; test. |
| M4 | No owner gate on the fan; `_dispatch` never consults permission policy → 21 agents on one owner turn. | Owner decision: **direct, caps-bounded** (chatbot feel); this makes the structural caps (B2) load-bearing, stated explicitly (settled #8). |
| M5 | Tree budget can't "reuse" accounting — frozen per-loop int that re-sums conversation each step (over-counts); per-child-cap × shared-counter × admission-floor contradictory. | Spec §"Guardrails & tree budget": redefine unit as **incremental spend**; single shared counter + root reserve + admission floor; `per_child_cap` demoted to sanity ceiling; real 4-site loop change. |
| M6 | Reflexion runs per child (esp. buffer-retry N=2), uncosted in budget. | Spec: children run with **reflexion disabled**, `buffer_retry` forced off; the parent synthesis turn is the critique-worthy one. |
| M7 | Error / cancel / budget-exhausted states absent from mocks and spec; mocks happy-path only → gate provisional. | Mocks add failed-child, Stop/cancel, budget-exhausted/truncated (scenario switchers); spec + `DESIGN.md` require them. Gate re-reviewed and **owner-approved**. |
| M8 | `activeTurn`-set lift underspecified (busy/abort/runId singletons; two keyspaces; fan reconnect). | Spec §"Execution model": parent turn stays the single gated turn; in-chat reads parent-turn events, tree reads child rows; reconnect replays the parent run; `activeTurn`-set drives row glyphs only (not send-gating). |
| M9 | ASSISTANT.md hatch materially widened (tool set, summary-only, one-hatch) without reconciliation. | Spec §"Reconciliation": the 3 widened properties tabled with rationale; **owner-approved**. |
| M10 | Long-fan / depth-2 vertical bloat; children not excluded from top-level bucketing; archived-parent rails. | Spec §GUI + `DESIGN.md`: row cap + "show N more" + scroll region; rail collapses by default past a threshold; children filtered from top-level bucketing; archived rail collapsed. |

## Minors / nits

| # | Finding | Resolution |
|---|---|---|
| m1 | Depth off-by-one prose. | Stated as `spawn allowed iff parent.depth < 2` (settled #6). |
| m2 | jerv surface mischaracterized as "web only" (has image/transcribe/video/metrics/location). | Spec §Personas: corrected; test that child effective tools include no non-web jerv tool. |
| m3 | Mandated RLS test vacuous under jerv-only-root. | Spec: right-sized — `agent_sessions` RLS is owner-only; the meaningful test is the clamp, not table RLS. |
| m4 | Note-deletion cascade (#11) left open though vacuous under no-KB. | Spec: closed — children touch no notes, cascade vacuous. |
| m5 | Synthesis card not in the registry list (bespoke green frame). | Registered as `subagent_synthesis` tool-view (DESIGN.md, same-PR rule); neutral standard frame in mocks. |
| m6 | Mocks used inline hex/rgba + color-from-JS-map; no reduced-motion/aria. | Mocks: token-bound classes, neutral persona, `prefers-reduced-motion`, `aria-hidden` glyphs, group toggle as a real `button` with `aria-expanded`, tree roles. |

## What held up (keep as-is)

- SSRF guard re-applies per child automatically (it is an *internal-target* guard,
  not an exfil guard — the no-KB/no-location/no-memory sandbox does the
  egress-safety work).
- The *blocking* mechanics of fan-in A are sound (an awaited handler can `gather`).
- **jerv-as-root with an empty-scope tree is a genuinely strong firewall.**
- ProposalTree / OpsCard disclosure / TurnGlyph / LiveToolStatus reuse is sound.
