# Adversarial review — sub-agent feeding waves (v1 plan)

Three independent adversarial reviewers (reviewer ≠ author, per `docs/PROCESS.md`)
read the v1 `SUBAGENT_FEEDING_WAVES_PLAN.md` (4 waves, nested feeding, up-front mint,
per-wave budget) with distinct lenses. Verdict: **reject v1 as written.** The plan
was re-scoped to a minimal single-hop feed (v2) that resolves every blocking finding.
Findings are kept here as the record and as the re-check list for each build wave's
gate.

## Convergence map (strongest signal = independently hit)

| # | Finding | Sec | Arch | GUI | v2 resolution |
|---|---|:--:|:--:|:--:|---|
| 1 | `<untrusted_external_data>` envelope exists in **no prompt** — asserted, not enforced | ● | | | Real pinned prompt clause + round-trip test |
| 2 | Verbatim interpolation → delimiter break-out (`</…>` in a summary) | ● | | | Delimiter neutralization before interpolation |
| 3 | Serial wall-clock (1200s/child) **exceeds the 3600s turn cap**; no tree-wide clock | ● | ● | ● | `TREE_WALL_CLOCK_S` deadline, skip-loud; `MAX_WAVES=2` |
| 4 | Mint-up-front orphans skipped sessions/run-logs; `admit` has no release → cap double-count | | ● | | Per-wave mint/admit |
| 5 | Skip predicate unpinned; an ERROR summary (attacker text) can be fed forward | ● | ● | | `ok`-based skip; failed summaries never fed |
| 6 | Bounded DAG engine — crosses the lean litmus; cheap guard alone fixes the bug | | ● | ● | De-scoped to single hop; guard is the primary fix |
| 7 | Behavioural fix is a soft prompt nudge + brittle regex guard | | | ● | Guard structural + measurable acceptance bar |
| 8 | No live surface for 4 nested waves + feed edges on a 352px phone | | | ● | `MAX_WAVES=2`, no nesting, scoped mocks, text feed affordance |
| 9 | Budget re-check coarser than serial exec; final (deliverable) wave starved | ● | ● | ● | Per-child re-admission + reserved final-wave floor |
| 10 | Skip vocab blur (cascade vs budget vs deadline vs truncated) | ● | | ● | Explicit `skip_reason` enum, distinct copy |
| 11 | Depth-0 fed-task validation contradicts current `_resolve_brief` (needs a `str`) | ● | | | Explicit `fed ⇒ template-bound` branch + test |
| 12 | "Byte-identical `tasks:[…]`" in tension with the v4 digest bump | | ● | | Literal early-return + characterization test |
| 13 | Fed block can blow the consumer context window (no truncation) | | ● | | Per-feed-block token cap + `[truncated]` marker |
| 14 | `wave`/`fed_from` only on ephemeral events — not queryable after the fact | | | ● | Persist on the run-log |
| 15 | F1 ships the scheduler but `wave` field is F2 → merged-F1 renders as hung children | | ● | | `wave` telemetry folded into F1 |
| 16 | D4 nesting multiplies the envelope hole and the UX blast radius across depths | ● | ● | ● | D4 reverted — no nesting |

## Reviewer verdicts (verbatim gist)

- **Security / red-team:** *"NOT safe to build as written."* The central safety
  property (feeding neutralized by a "declared non-executable" envelope) is asserted
  but enforced nowhere; fed text is interpolated verbatim with no delimiter escaping.
  Same "asserted-as-structural, actually model-mediated" trap the prior spawn review
  flagged. Before build: make the envelope real, escape the tokens, pin the `ok`-based
  skip predicate, add a tree-wide wall-clock cap; keep nesting gated until the
  envelope is genuinely enforced.
- **Architecture / correctness:** *"Reject as specified."* Up-front-mint / per-wave-skip
  orphans session and total-agent-cap state (two criticals); local-serial wall-clock
  math structurally exceeds the parent turn cap; the shape is a bounded DAG engine
  duplicating the Phase-5 workflow engine while the actual foot-gun is closed by the
  cheap sibling-reference guard alone. Ship F1's validation guard; defer the scheduler.
- **GUI / product / operability:** The security reasoning was fixable but the plan
  "under-delivers on the two things that determine whether it feels good" — it fixes
  the empty-run with a soft nudge + brittle guard rather than a structural forcing
  function, and hand-waves the hardest part (a legible live surface for serial nested
  waves with feed edges and four failure states on a cramped phone) onto a mock gate
  it never scopes.

## Owner decision

Re-scope to **minimal single-hop feed** (middle path): `MAX_WAVES=2`, no nesting, one
**enforced + escaped** feed hop, per-child admission, a tree-wide wall-clock cap. See
`docs/SUBAGENT_FEEDING_WAVES_PLAN.md` v2.
