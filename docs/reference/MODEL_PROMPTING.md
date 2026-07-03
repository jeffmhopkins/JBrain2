# Model prompting reference — gpt-oss-120b & Qwen3-VL-30B

> **Status:** Living · **Last verified:** 2026-07-03

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
  exhaustively.
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

> **Config gap (as of this writing):** neither `llama_swap_config.py` nor the LLM
> adapter sends any sampling params, so every Qwen call runs at llama.cpp
> defaults (temp 0.8, top_p 0.95, top_k 40, presence_penalty 0). For deterministic
> jobs — OCR, classification, titling — that is the wrong end of the dial. See
> "Actionable" below.

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

## Actionable: the sampling gap

The single highest-leverage change is **per-task sampling**. Today all local
calls inherit llama.cpp defaults (temp 0.8, top_p 0.95, top_k 40, no presence
penalty) regardless of task. Recommended direction (not yet built):

- Plumb per-task sampling through the LLM adapter, sourced from the `.prompt`
  `config:` block (which already carries `max_tokens`), so each prompt can pin its
  own temperature / top_p / top_k / presence_penalty.
- **Qwen `vision` prompts:** `vision_ocr` → near-greedy (temp ≈ 0–0.2) with
  presence_penalty ≈ 1.5 to prevent loops; `vision_caption` / `video_frame` →
  Qwen's VL defaults (temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5).
- **gpt-oss `low` prompts** (session_title, entity_disambiguate, triage_classify,
  video_summary): these are deterministic classify/title jobs — they benefit from
  a low temperature too, but the primary lever there is the low reasoning effort
  they already run at, not Qwen's VL numbers. Tune temperature down if they show
  variance; don't apply the Qwen VL sampling table to them.
- Server-wide `--temp/--top-p` flags in `llama_swap_config.py` are the blunt
  alternative; they apply to the whole served model and can't distinguish OCR from
  captioning (or one text tier from another), so prefer the per-task route.
