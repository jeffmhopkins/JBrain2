# Model prompting reference — gpt-oss-120b & Qwen3-VL-30B

> **Status:** Living · **Last verified:** 2026-08-14

> **Applied (2026-08-14):** added `Qwen/Qwen3.8-27B` to the local catalog (Q8 + Q4 twin,
> `qwen3.8-27b` / `qwen3.8-27b-q4`) — the newer-generation successor to the qwen3.6-27b
> dense-27B vision hybrid. Same Qwen hybrid sampling split as 3.6 (non-thinking 0.7/0.80/pp 1.5,
> thinking 1.0/0.95), carried on the catalog entry; see the per-model table below.

> **Applied (2026-08-14):** the sampling gap below is CLOSED. Per-model recommended sampling
> now rides every call: each local model carries its vendor-recommended `sampling` (and, for a
> hybrid whose card splits thinking vs non-thinking, `sampling_thinking`) on its catalog entry
> (`jbrain.llm.local_catalog`), cloud models have a small table (`jbrain.llm.model_sampling`),
> and the router applies the resolved model's bundle to EVERY call — so a model runs at its
> card's values instead of llama.cpp's engine defaults (temp 0.8 / top_p 0.95 / top_k 40 /
> min_p 0.1) with no per-call code. A `.prompt` `config: sampling:` block overrides per task,
> merging on top (`vision_ocr` now pins near-greedy temp 0.1 + presence_penalty 1.5). Provider
> quirks are enforced at the client boundary, not in config: the xAI client sends only
> temperature/top_p (Grok 4.x reasoning models reject penalties and don't take top_k/min_p),
> and the Anthropic client sends temperature *or* top_p, never both (a 4xx since Opus 4.1). See
> "The sampling plumbing" below for the full per-model table.

> **Applied (2026-08-10):** a live daily-news run exposed the deep-research scout over-searching
> (34-36 `web_search` calls/angle), and a parallel audit checked the whole deep-research prompt
> set against this guide. Fixes that landed, all keyed to the gpt-oss behaviours below: the
> `research_scout` prompt (v5) traded a soft "stop early" plea for a countable ceiling and marks
> the angle's named sources a MENU not a checklist (contradiction-sensitivity + "exhaustive
> inflates"). A **second** live run (post-redeploy) showed the v5 prompt ceiling did **not**
> hold — the scouts still ran 12-27 `web_search` calls each, straight to the effort-scaled step
> cap (~42 tool calls, ~11 min for the worst) — confirming a prompt-stated tool budget is not
> self-enforcing on gpt-oss (it does not reliably count its own tool calls). So the ceiling moved
> into the **engine**: `ToolCallBudget` (loop.py) hard-caps `web_search` at `SCOUT_SEARCH_BUDGET`
> (8), the handler refuses calls past it and appends the remaining count to every result, and the
> prompt (v6) states the same 8 and notes it is enforced. A **third** run then showed capping
> search alone just moved the runaway to `web_fetch` — one scout looped to 23 reads (~10 min via
> the reader fallback) while its 4 peers finished in 2-3 min — so `web_fetch` got the same engine
> cap at `SCOUT_FETCH_BUDGET` (10) and the prompt (v7) states both. The reader (`research_fetch`)
> stays uncapped — it must open every URL it is handed. The lesson generalizes — see "Prompt
> budgets need an engine backstop" under *Behaviours to design around*. The daily_news angle
> briefs were slimmed to remove the "at least three / don't
> conclude empty / pivot" checklist; the synthesizer (`dr-synth-v13`) was made length-NEUTRAL so
> the per-run target line wins (it no longer hard-codes "8-10 pages / comprehensive", which fought
> the `brief`/spoken target), then (`dr-synth-v14`) given a countable coverage rule after a live
> run showed the writer compress ~13k of delivered reader findings into a 3k brief that declared
> whole sections empty (dropping an AI-model update, a Senate primary, and two completed launches
> the readers HAD fetched): brevity is now earned by keeping each delivered item tight, never by
> dropping items, and "no X appeared in the sources" is banned whenever a finding delivered an X
> (the delivered-content twin of the absence rule — "obeys countable output-shape rules"), then
> (`dr-synth-v15`) given citation-hygiene + no-recycling rules after a frontier review of a later
> run caught duplicate footnote definitions, an orphan `## Sources` entry, and an "Around the World"
> section padded by restating a domestic story (one number per source, list mirrors the body, each
> story in one section); freshness was fixed at the reader (`agent-research-fetch-v2` date-stamps
> each finding and flags stale pages) with a matching v15 rule against printing a stale figure as
> today's. The v15 citation-hygiene rules were then RETIRED into code (`dr-synth-v16`): a live run
> showed gpt-oss still botching the bookkeeping (it dropped the `## Sources` block outright), so
> `_finalize_sources` now rebuilds that block deterministically as a no-renumber projection of the
> in-body `[^n]` markers onto the source registry (duplicates/orphans/missing/out-of-range become
> impossible by construction), the one-shot `_backstop_critique` grew a dangling-citation gate and
> a scored keep-the-better-attempt guard (`reflexion.strictly_improves`), and the prompt stopped
> asking the model to author/reconcile the list — the exact "prompt = intent, engine = ceiling"
> move, applied to the citation apparatus. The `_critique` grounding gate was collapsed to one bounded,
> prioritized pass and its double-`FIRST` removed; and `research` (v16) / `review` (v8) had their
> stacked URL/verify restatements collapsed and a "highest-risk first" priority stated. The clean
> in-repo templates the audit pointed to: the planner, the reflect judge, and the `_analyze` brief.

Behavioural notes and prompting guidance for the two local models this box runs,
so that every `.prompt` we write is shaped to the model that will actually
execute it. This is a **reference**, not a spec: it records what the model
vendors and the community have published about how these models behave, and how
that maps onto JBrain's prompt set. When a prompt is edited, check it against the
"Do / Don't" list for its tier.

Grounding: the model behind a tier is defined in
`backend/src/jbrain/llm/local_catalog.py`; the served `llama-server` command is
built in `backend/src/jbrain/llm/llama_swap_config.py`; task→provider defaults are
in `backend/src/jbrain/llm/router.py` and the owner's live per-task overrides
(incl. reasoning effort) in `backend/src/jbrain/settings_store.py`.

## Two tiering concepts — don't conflate them

- **Per-task routing (authoritative, owner-configurable).** Every LLM call runs
  under a named *task* (`agent.turn`, `integrate.note`, `vision.ocr`, …). Each task
  is routed to a provider **and** a reasoning effort. The effort has a **codified
  default** — the task's reasoning bucket (`TASK_REASONING_BUCKET` in `router.py`) —
  so a fresh box is right without any hand-tuning; a stored per-task effort
  (`settings_store.py`, edited in Settings → LLM Settings) is a deliberate override.
- **Prompt `strength:` frontmatter (a default hint).** A prompt names a capability
  tier (`high`/`low`/`vision`) so it never hard-codes a model. It seeds a default,
  but the per-task config wins — e.g. `video_summary.prompt` declares
  `strength: low`, yet its task `video.summarize` sits in the Medium reasoning
  bucket for a richer summary.

The Settings screen groups *tasks* into **reasoning-level buckets** (defined in the
frontend `LLMSettingsScreen.tsx`, mirroring the backend map), so each bucket's
default effort is correct for every task in it — right by default, and an override
reads as a deviation (the card shows "mixed"):

| Bucket · default effort | Model | Tasks |
|---|---|---|
| **High reasoning** · high | gpt-oss-120b | `integrate.note`, `fact.adjudicate`, `wiki.ground` |
| **Medium reasoning** · medium | gpt-oss-120b | `agent.turn`, `note.extract`, `correction_note.extract`, `video.summarize`, `wiki.rewrite`, `intake.materialize` |
| **Low reasoning** · low | gpt-oss-120b | `entity.disambiguate`, `session.title`, `triage.classify` |
| **Vision** · none | Qwen3-VL-30B-A3B | `vision.ocr`, `vision.caption`, `agent.vision` |

The high/low buckets put their default effort on the wire; **Medium sends no
explicit effort** (the model's own default is medium) — which also preserves the
sub-agent spawner's contract that a child with no chosen effort reaches the model
with `reasoning_effort=None`.

**gpt-oss-120b serves all three text buckets** — one model at three efforts, so the
gpt-oss guidance below governs every text task. Qwen3-VL serves **only the three
Vision tasks**. The catalog also offers the **Qwen3.5 hybrids (0.8B tiny / 4B small)**
as reasoning-capable `low`-tier alternatives an operator can route the one-shots to;
their thinking is a chat-template toggle, so setting a task's effort to **`none`
actually turns thinking off** (a snappy Instruct one-shot), while any other level runs
the full trace. See the note under the Low bucket below.

Prompt → task, for reference: `agent.turn` runs the interactive personas (jerv,
curator/`system`, archivist, teacher) and the spawned sub-agents (research, review,
summarize); the rest map name-for-name (`note.extract`→note_extract,
`integrate.note`→integrate_note, `correction_note.extract`→correction_mine,
`wiki.rewrite`/`wiki.ground`→wiki_editor, `video.summarize`→video_summary,
`intake.materialize`→intake_materialize, the Low-bucket tasks→their same-named
prompts, and the Vision tasks→vision_ocr/vision_caption/video_frame).

> These route to the local models on a self-hosted box. If a deployment routes a
> task to a cloud model instead, the gpt-oss/Qwen notes stop applying to it — but
> the domain-firewall design keeps health/finance/location analysis local, so
> treat the local models as the default target.

---

## gpt-oss-120b (the High / Medium / Low text buckets)

An OpenAI open-weight reasoning MoE served here at MXFP4. It uses the **Harmony**
response format and emits a hidden chain-of-thought before its answer.

### How it reads our prompts
- **Harmony role hierarchy is System > Developer > User.** The model trusts a
  real System message most. JBrain's `.prompt` text is injected as the
  **Developer** instruction (the System slot carries the harness/date/effort
  preamble), and the turn's user content is User. Practical consequence: our
  prompt is authoritative for *task* rules, but it cannot override a genuine
  System instruction — keep prompt rules about the task, not about the harness.
- **Reasoning effort (low / medium / high) is set in the System message**, not
  the prompt. It trades latency for depth. Most High-stakes tasks run at **Med** by
  default — deliberately, because full **High** is *slow* and tends to over-think
  before acting; reserve High for the tasks that earn it (see "When to spend High
  effort"). The Lightweight one-shots (`entity.disambiguate`, `session.title`,
  `triage.classify`) run at **Low** — right for their short, deterministic
  classify/title jobs: minimal chain-of-thought, fast. Write those prompts so the
  answer needs almost no reasoning (a clear rule and output shape), because at low
  effort there is little to spend.

> **Local hybrid one-shots (Qwen3.5 tiny/small).** These models don't have gpt-oss's
> graded effort — thinking is a chat-template on/off toggle. `none` runs a true
> non-thinking Instruct pass (fast, a handful of tokens); **any** other level runs the
> *full* trace, which on the 0.8B ran ~2.3k tokens just to title one chat. Two
> consequences: (1) a one-shot's `max_tokens` must cover a full trace plus the answer,
> not just the answer — `session.title` and `entity.disambiguate` budget for this
> (4096); (2) if you route a one-shot to a Qwen hybrid and want it snappy, set its
> effort to `none` in Settings rather than `low`. The adapter maps `none`→
> `enable_thinking=false` and everything else→thinking on; the trace itself lands on the
> `reasoning_content` channel (deepseek format), so it never leaks into the answer.

### Behaviours to design around
- **Conflicting instructions degrade it badly.** gpt-oss is unusually sensitive
  to contradictions within a prompt (e.g. "be exhaustive" next to "be concise").
  It burns reasoning trying to reconcile them. Every prompt should be internally
  consistent; when two goals compete, state the priority explicitly.
- **It prefers its own knowledge over tools.** It will answer from parametric
  memory rather than search unless the prompt gives an explicit, concrete trigger
  ("when the answer depends on current events, recent facts, or specific sources,
  search *before* answering"). Name the trigger; don't just list the tool.
- **High effort → runaway pre-tool reasoning.** At high effort it can reason for a
  long time before making its first tool call. Prompts for tool-driven personas
  should push it to act early ("think briefly, then search") rather than plan
  exhaustively. (Corollary: **do not** raise a tool-driven persona to high effort to
  make it "follow the budget better" — that is reasoning-bound advice; higher effort
  buys *more* exhaustive planning and a *larger* step cap, i.e. more tool calls. The
  scout runs at `low` for exactly this reason.)
- **Prompt budgets need an engine backstop.** A "call this tool AT MOST N times"
  ceiling stated only in the prompt is **not** self-enforcing: gpt-oss does not
  reliably count its own tool calls and will run to the step cap regardless (the
  research scout kept over-searching under a v5 prompt that plainly said "AT MOST 6").
  When a tool-call count actually matters, enforce it in the loop/handler and let the
  prompt merely describe it — `ToolCallBudget` (loop.py) caps the scout's `web_search`
  (8) and `web_fetch` (10) and annotates each result with the remaining count. Prompt =
  intent; engine = ceiling. Expect whack-a-mole: capping one tool moved the scout's
  runaway from searches to fetches, so both needed a ceiling — cap the *behaviour*
  (total reach), not just the first symptom.
- **"Be exhaustive / comprehensive" inflates verbosity.** These phrases produce
  padded output. Ask for *tight*, *focused*, *lead-with-the-answer* instead.
- **Never instruct or reference the hidden chain-of-thought.** Do not tell it to
  "show your reasoning," "think step by step in your answer," or format the CoT —
  supervising the reasoning channel destabilises it. Ask only for the final
  answer's shape.
- **Strip prior-turn reasoning in multi-turn.** Only the final answers from prior
  turns should be replayed, never the reasoning traces (the loop handles this;
  noted here so prompts don't try to reference "your earlier reasoning").

### Do / Don't for a `high` prompt
- **Do** keep every instruction mutually consistent; state priorities when goals
  compete.
- **Do** give explicit, concrete tool triggers rather than a bare tool list.
- **Do** ask for tight, lead-with-the-answer output; cap scope.
- **Don't** use "exhaustive/comprehensive"; don't reference or shape the hidden
  reasoning; don't stack redundant restatements of the same rule (it reads as
  conflict).

### When to spend High effort

High effort is *slow* and over-reasons before acting, so it only pays off for a
task that is **all three of**: async (latency-tolerant), reasoning-bound (not
tool-bound), and correctness-critical. That is exactly why those tasks live in the
**High** bucket by default and everything else in Medium/Low — the bucket a task
sits in *is* this decision. The rationale, task by task:

| Task | Bucket | Why |
|---|---|---|
| `integrate.note` (Integrator) | **High** | Graph coreference/relationship/supersession calls that *write* the knowledge graph; runs in async ingestion, so latency is free. The best place to spend it. |
| `fact.adjudicate` (arbiter) | **High** | Hard conflict/supersession judgment the deterministic core then validates; async. |
| `wiki.ground` (Phase 6) | **High** | Strict "graph wins on conflict" grounding verification; correctness-critical, batch. |
| `wiki.rewrite` (Phase 6) | Medium | Generative drafting, not judgment; override to High only if grounding rejects too much. |
| `agent.turn` (chat) | Medium | Interactive (owner on phone) *and* tool-driven → High would buy runaway-before-tools + slow UX. Deep research depth is already tunable per *sub-agent* at spawn time. |
| `note.extract`, `correction_note.extract`, `video.summarize`, `intake.materialize` | Medium | Structured/bounded work; Medium is the right cost. |
| One-shots (`entity.disambiguate`, `session.title`, `triage.classify`) | Low | Deterministic; Low is correct. |

The test in one line: *async + reasoning-bound + correctness-critical → High;
interactive or tool-driven → never High.* A per-task override exists for the rare
exception, but with the buckets set up this way you should rarely need one.

Sources: [OpenAI gpt-oss model card (HF)](https://huggingface.co/openai/gpt-oss-120b) ·
[IBM watsonx — gpt-oss model behaviour & instruction guidelines](https://www.ibm.com/docs/en/watsonx/watson-orchestrate/base?topic=models-gpt-oss-model-behavior-instruction-guidelines) ·
[Harmony response format](https://cookbook.openai.com/articles/openai-harmony) ·
[Cameron R. Wolfe — reasoning-model prompting](https://cameronrwolfe.substack.com/).

---

## Qwen3-VL-30B-A3B (the `vision` tier)

An Alibaba multimodal MoE. Strong OCR (32 languages, robust to low light / blur /
tilt) and document-structure parsing, but **not hallucination-free** — on
hallucination benchmarks it trails purpose-built OCR engines, so verbatim tasks
need explicit "don't guess" guardrails. Plain system prompt; **no** hidden-CoT
hierarchy to manage.

### Recommended sampling (Qwen's published values)

Qwen serves only our image prompts, so the VL column is the one that matters
(the pure-text column is Qwen's own recommendation, kept for reference only —
we don't route text to Qwen).

| Param | Vision / VL tasks | (Qwen text, ref only) | llama.cpp default (what we serve today) |
|---|---|---|---|
| temperature | 0.7 | 1.0 | 0.8 |
| top_p | 0.8 | 1.0 | 0.95 |
| top_k | 20 | 40 | 40 |
| presence_penalty | **1.5** | 2.0 | 0.0 (off) |
| repetition_penalty | 1.0 | 1.0 | 1.0 |

The **presence_penalty ≈ 1.5** is the headline knob: Qwen calls it out to
suppress the repetition / endless-loop failure mode VL models fall into on dense
images and long OCR runs.

> **Now wired:** the VL column is what we serve — `qwen3-vl-30b`'s catalog `sampling`
> pins temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5, and the `vision_ocr`
> prompt overrides temperature down to 0.1 (near-greedy transcription) while keeping
> presence_penalty 1.5. The "llama.cpp default" column is what we USED to serve before
> the sampling plumbing (see the header note and "The sampling plumbing" below).

### Image / OCR behaviour
- Visual-token budget per image ≈ 256–1280 tokens (32× spatial compression);
  control it with `min_pixels`/`max_pixels` or `resized_height`/`resized_width`
  (multiples of 32). High-res inputs auto-downsample but spike preprocessing RAM;
  capping inputs around 1920×1080 keeps peak VRAM predictable.
- Keep the vision projector (`mmproj`) at **f16** — fine text degrades first under
  quantization. We already do this; the Q8_0 text weights are fine for OCR.
- For structured extraction, Qwen responds well to "extract into this schema"
  / JSON-shaped instructions; for verbatim capture, be explicit about *not*
  translating or normalizing the source.

### Do / Don't for a `vision` prompt
- **Do** keep verbatim-OCR guardrails explicit: mark illegible regions, never
  guess, emit nothing when there is no text.
- **Do** prefer near-greedy sampling for OCR once per-task sampling is plumbed
  (low temperature, presence_penalty to kill loops).
- **Do** state the output format concretely (plain text vs. one-sentence-per-fact
  vs. schema).
- **Don't** rely on the model to self-limit repetition without presence_penalty.
- **Don't** ask it to translate/normalize during a verbatim transcription.

Sources: [Qwen3-VL repo](https://github.com/QwenLM/Qwen3-VL) ·
[Unsloth — Qwen3-VL run guide](https://unsloth.ai/docs/models/qwen3-vl-how-to-run-and-fine-tune) ·
[Qwen3-VL-8B-Instruct model card](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct) ·
[Alibaba qwen-vl-ocr docs](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-ocr) ·
[Qwen3-VL Technical Report](https://arxiv.org/pdf/2511.21631).

---

## The sampling plumbing

Per-model recommended sampling rides every call, with a per-task override on top.
The chain: `jbrain.llm.model_sampling.default_sampling(provider, model, effort)`
returns the resolved model's recommended `Sampling` (local models carry it on their
catalog entry; cloud models are a small table). The router merges the prompt's
per-task override (`.prompt` `config: sampling:`) over that default and passes the
result to the provider client, which renders only the knobs its API accepts. So a
model runs at its card's values with NO per-call code, and a prompt pins only the
knobs it wants to deviate.

**Two layers, on purpose.** The recommended-value layer (`model_sampling` / the
catalog) is the vendor's numbers verbatim. The provider-quirk layer (each client's
`_apply_sampling`) drops what an API can't take. That split is what lets ONE
`vision_ocr` override (near-greedy + presence_penalty 1.5) do the right thing on both
the local VL model (both knobs land) and cloud Grok (only the temperature lands — its
reasoning model rejects penalties). It also means a hybrid reasoner picks its
thinking-mode vs non-thinking-mode row automatically from the resolved reasoning
effort (`sampling_thinking`).

### Per-model recommended values (what we now serve)

Local models (llama.cpp). top_k `0` and min_p `0` mean *disabled* — for a card that
prescribes only temp/top_p we sample purely on those rather than inherit llama.cpp's
top_k 40 / min_p 0.1. Hybrid rows show non-thinking → thinking.

| Model (served) | temp | top_p | top_k | min_p | presence_penalty | source |
|---|---|---|---|---|---|---|
| qwen3-vl-30b-a3b (+q4) | 0.7 | 0.8 | 20 | 0 | 1.5 | Qwen VL card |
| qwen3.6-27b (+q4) | 0.7→1.0 | 0.8→0.95 | 20 | 0 | 1.5→– | Qwen3.6 card |
| qwen3.8-27b (+q4) | 0.7→1.0 | 0.8→0.95 | 20 | 0 | 1.5→– | Qwen3.8 card |
| qwen3-coder-next (+q8) | 1.0 | 0.95 | 40 | 0 | – | Qwen Coder card |
| qwen3-30b-a3b | 0.7 | 0.8 | 20 | 0 | – | Qwen 30B-A3B (Instruct-2507) |
| qwen3.5-0.8b | 1.0→1.0 | 1.0→0.95 | 20 | 0 | 2.0→1.5 | Qwen3.5 card (loop-prone) |
| qwen3.5-4b | 0.7→1.0 | 0.8→0.95 | 20 | 0 | 1.5 | Qwen3.5 card |
| gpt-oss-120b | 1.0 | 1.0 | 0 | 0 | – | OpenAI (min_p 0 is critical) |
| glm-4.5-air | 0.6 | 0.95 | 0 | 0 | – | Z.ai GLM-4.5 API default |
| nemotron-3-super-120b | 1.0 | 0.95 | 0 | 0 | – | NVIDIA card (unified) |
| nemotron-3.5-lightning-30b | 1.0 | 0.95 | 0 | 0 | – | NVIDIA card (unified) |
| llama-4-scout-int4 | 0.6 | 0.9 | 0 | 0.01 | – | Meta config + Unsloth min_p |
| llama-3.3-70b | 0.6 | 0.9 | 0 | 0 | – | Meta generation_config |

Cloud: `xai:grok-4.3` → temp 0.7 / top_p 0.95 (documented default; penalties dropped —
Grok 4.x are reasoning models). `anthropic:claude-sonnet-4-6` → nothing set, so
Anthropic's own default (temp 1.0) stands and we never trip the temp-and-top_p-both
error; a per-task override may still set a lower temperature.

### Per-task overrides

A prompt deviates by adding a `config: sampling:` block; it merges over the model
default. Today the one override that matters is `vision_ocr` (near-greedy temp 0.1 +
presence_penalty 1.5 — verbatim transcription must not drift or loop). `vision_caption`
and the deterministic `low` classify/title jobs run at their model's defaults; the
primary lever for the latter is the low reasoning effort they already carry, not the
Qwen VL numbers — tune temperature down per-prompt if one shows variance.

Server-wide `--temp/--top-p` flags in `llama_swap_config.py` remain the blunt
alternative we deliberately did NOT take: they apply to a whole served model and can't
tell OCR from captioning on the same model, which is exactly what the per-task route
buys. Operator-tunable sampling (a Settings surface, like the per-task model/effort
overrides) is a possible follow-up — today the values live in code (catalog + prompts),
the same source-of-truth as the rest of the sampling doctrine.
