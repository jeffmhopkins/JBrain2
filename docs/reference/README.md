# Reference — how the system is

> **Status:** Living · **Last verified:** 2026-09-23

Binding design references: the architecture, the standards, and the models that
describe how JBrain2 *is* built. These are `Living` docs (per
`../DOC_LIFECYCLE.md`) — corrected continuously, never "shipped."

| Doc | What it covers |
|---|---|
| `PROMPT_CACHE.md` | How the interactive agent's ~30k-token prefix is kept ready across restarts and evictions — the three layers (slot, WarmKeeper, disk store), the fingerprint that decides when a saved file is reusable, which models are eligible and why, the numbers measured on the box (a cold load processes **11** prompt tokens of 30,546; restore 94 ms warm against a 118 s prefill), and the no-terminal routes for reading and operating it. Written because the design lived only in a module docstring, and the feature had already shipped inert twice behind a store that looked identical whether it was working or dead. |
| `ARCHITECTURE.md` | System shape: containers, the one-database design, the knowledge pipeline, security model, operations. |
| `SERVICES.md` | Concrete inventory of everything the box runs: every Docker container (core + opt-in), the on-box GPU model services, the PWA + JBrain360 Android app, and the functions baked in (agent, pipeline, workflow engine, wiki). |
| `DEVELOPMENT.md` | Binding standards: the architectural constitution, comments, testing, git, releases, `dev-setup.sh`. |
| `PROCESS.md` | Binding multi-wave execution process: parallel tasks, per-task/per-wave adversarial review, one PR per wave, the GUI mock gate, CI gates. |
| `DESIGN.md` | Binding GUI design system: theming, components, navigation, the agent tool-view contract. |
| `ANALYSIS.md` | The note→fact→entity pipeline (extract → Integrator → arbiter), supersession, the review inbox. |
| `entity.md` | The entity & soft-schema model: predicates, facets, names, relationships, resolution. |
| `ENTITY_GRAPH_REFOCUS_PLAN.md` | The two-tier predicate model + entity-graph refocus (shipped; kept as the canonical description). |
| `PREDICATE_CANONICALIZATION.md` | The original predicate-registry design (Superseded by `ENTITY_GRAPH_REFOCUS_PLAN.md`; kept — still cited). |
| `ASSISTANT.md` | The tool-calling agent design: runtime, two-tier memory, security non-negotiables. |
| `MODEL_ACCESS_INVENTORY.md` | Evidence-only inventory of every place that loads, unloads, admits, warms, evicts, measures or demands a model, across all five consumers of box memory. Every row carries a `file:line` and a verbatim quote. The evidence base for `../plans/LOCAL_MODEL_ACCESS_PLAN.md`. |
| `MODEL_PROMPTING.md` | Prompting reference for the two local models (gpt-oss-120b, Qwen3-VL) and the sampling gap. |
| `WIKI_TYPE_GUIDES.md` | Phase-6 editorial config — per-entity-type article guides the wiki builder loads. |
| `PANEL_COMMANDS.md` | What the room-endpoint pet answers to — every phrase, and which ones are CONFIRMED working by a child saying them out loud. Written after six of eight reported "broken commands" turned out to be animation names that were never in the vocabulary; records the two that genuinely fail (`dance`, `burp`), the three hypotheses the evidence killed, and why the instrumentation has to be widened before the fix. |
| `LOCATION_ASSISTANT_TOOLS.md` | Reference catalog of candidate location tools (✅ spine shipped; 🟡/⛔ parked ideas). |
