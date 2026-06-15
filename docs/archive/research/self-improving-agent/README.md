# Self-improving agent — research & synthesis

The research behind **`docs/ASSISTANT.md`** (the binding design). Brief: distill
Hermes and other self-improving open-source assistants into a paradigm and feature
set for evolving JBrain2's Phase-4 agent — a smart, tool-using, self-improving
assistant with excellent long-term memory (the RAG DB) and immediate working
memory (MD-style local memory), **without the breadth-bloat** of those systems.

Produced by a parallel swarm: four researchers (A–D), a reviewer (E), and a red
team (F). No source, prompts, or schema were changed — these are design dossiers.
The synthesis lives in `docs/ASSISTANT.md`; this README is the decision surface.

## The dossiers

| | Dossier | Owns | Headline finding |
|---|---|---|---|
| A | [landscape survey](A-landscape-survey.md) | The field; "what is Hermes"; lean-vs-bloat | "Hermes" = **Nous Hermes Agent** (file memory + skill library); its bloat is **breadth, not depth** — steal the core, refuse the surface. 8 paradigms distilled |
| B | [memory architecture](B-memory-architecture.md) | The two-tier memory (RAG DB + MD) | Agent memory is **metacognitive, not factual**; pointers-not-copies; the bright line *"if it would belong in the wiki, it can't live in agent memory"* — enforced as a foreign-key impossibility |
| C | [self-improvement loops](C-self-improvement-loops.md) | The four loops + per-loop autonomy | Loops are **not equally safe to automate**; gate by blast-radius × reversibility; JBrain2 already owns the machinery (`.prompt` versioning, review inbox, workflow engine) |
| D | [tooling & runtime](D-tooling-and-architecture.md) | Lean agent loop + `.tool` sidecars | **Build, don't buy** — a ~300-line ReAct loop over the LLM adapter with native tool calling; no framework; tools mirror the `.prompt` convention |
| E | [JBrain2-fit review](E-jbrain-fit-review.md) | Compliance + inter-dossier conflicts | Sound, with two structural fixes: **phase-stage** the loops (agent is Phase 4, loops lean on 5–7) and **own the write-time domain classifier** (owner sessions carry all scopes) |
| F | [red team](F-redteam.md) | Adversarial review | Dossiers **defend each lane in isolation and trust the LLM at the seams**; 3 Critical leaks; **12 mandatory invariants (I-1..I-12)** |

## The synthesis decisions

Where the dossiers conflicted, `docs/ASSISTANT.md` resolves as follows:

1. **Phase-stage the loops** (E). Phase 4 ships only what Phase 1–3 supports: thin
   loop + Reflexion + Tier-A memory (RLS-tested). Skills/prompt-self-edit/Tier-B
   align to Phases 5–6.
2. **Behavioral memory is owner-confirmed-write only** (F overrides C's "auto" for
   the behavioral tier). The "owner direct interaction" trust boundary is wrong as
   stated — untrusted content is routinely quoted into owner turns.
3. **Episodic domain scope is fail-closed, RLS-enforced** (F overrides B's
   "split mixed conversation by LLM classifier"). Most-restrictive touched domain;
   never decomposed into a `general` row.
4. **Skill promotion:** read-only compositions auto-promote (eval-gated, including
   a **safety regression**, not success alone); mutating/cross-domain skills are
   owner-gated (reconciles A's "review every skill" with C's auto-promote, and
   removes the need for a dry-run engine).
5. **MD memory and skills are storage-backed rows, not filesystem paths** (E
   overrides A's `MEMORY.md`/`~/.hermes/skills/` framing) — non-negotiable #2.
6. **`provenance` lives on `notes`**, agent notes get normal (not elevated)
   extraction weight, distinct review-inbox item, rate-limited (E + F).
7. **No code execution in the agent**; `spawn_subagent` is context isolation only
   (E carrying A's constraint).
8. **The eval harness is a first-class, gating work item** for Loops 2 & 4 (E).
9. The **12 red-team invariants** are baked into `ASSISTANT.md` as the assistant's
   non-negotiables, extending CLAUDE.md's list.

## The through-line

> Every place dossiers A–D hand a firewall decision to an LLM — domain-classifying
> an episode, trusting "owner interaction," auto-promoting a "safe composition,"
> scoring a skill on success alone — is a place the firewall must instead be
> enforced by **RLS, by an owner confirmation, or by a fail-closed default**,
> because untrusted content reaches the model and the model is the thing under
> attack. The agent improves *how it works*; the notes→facts→wiki pipeline owns
> *what is true*.
