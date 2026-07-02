# Model prompting reference — gpt-oss-120b & Qwen3-VL-30B

Behavioural notes and prompting guidance for the two local models this box runs,
so that every `.prompt` we write is shaped to the model that will actually
execute it. This is a **reference**, not a spec: it records what the model
vendors and the community have published about how these models behave, and how
that maps onto JBrain's prompt set. When a prompt is edited, check it against the
"Do / Don't" list for its tier.

Grounding: the model behind each tier is defined in
`backend/src/jbrain/llm/local_catalog.py`; the served `llama-server` command is
built in `backend/src/jbrain/llm/llama_swap_config.py`. A prompt's `strength:`
frontmatter selects the tier.

## Which model runs which prompt

| `strength:` | Served by | Prompts |
|---|---|---|
| `high` | **gpt-oss-120b** — "High-stakes reasoning", **Med** effort (live default) | jerv, research, review, summarize, archivist, teacher, curator (`system`), intake, intake_materialize, correction_mine, wiki_editor, note_extract, integrate_note |
| `low` | **gpt-oss-120b** — "Lightweight", **Low** effort | session_title, entity_disambiguate, triage_classify, video_summary |
| `vision` | **Qwen3-VL-30B-A3B** — "Vision", no reasoning level | vision_ocr, vision_caption, video_frame |

The routing is owner-configurable (Settings → LLM Settings): three tiers, each a
provider + reasoning-effort pick. Live defaults are High-stakes → gpt-oss @ Med,
Lightweight → gpt-oss @ Low, Vision → Qwen3-VL. Per-task overrides exist within
each tier.

**gpt-oss-120b serves both text tiers** — `high` and `low` are the *same model*
at different reasoning effort, so the gpt-oss guidance below governs 17 of our 20
prompts. Qwen3-VL serves **only the three `vision` prompts**. (The catalog lists
Qwen with a `"low"` tier as a capable cheap text *fallback*, but live routing
sends `low` text to gpt-oss at low effort — so ignore the Qwen text column for our
prompt set.)

> `high`/`low` route to the local reasoning model on a self-hosted box. If a
> deployment routes them to a cloud model instead, the gpt-oss notes stop
> applying to those prompts — but the domain-firewall design keeps
> health/finance/location analysis local, so treat gpt-oss as the default target
> for text prompts.

---

## gpt-oss-120b (the `high` reasoning tier)

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
  the prompt. It trades latency for depth. The `high` tier runs at **Med** effort
  by default here — deliberately, because full `high` effort is *slow* and tends
  to over-think before acting; only bump a task to High when it genuinely needs
  it. Our `low`-strength prompts
  (session_title, entity_disambiguate, triage_classify, video_summary) run this
  same model at **low effort** — right for their short, deterministic
  classify/title jobs: minimal chain-of-thought, fast. Write those prompts so the
  answer needs almost no reasoning (a clear rule and output shape), because at low
  effort there is little to spend.

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
