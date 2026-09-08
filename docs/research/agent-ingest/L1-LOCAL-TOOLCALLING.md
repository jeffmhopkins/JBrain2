# L1 — The local tool-calling reality check

> **Status:** Research · **Last verified:** 2026-09-08

> **What this is.** A stress test of one premise in the proposed agent-ingest redesign — *a note
> becomes turn 0 of a tool-using conversation that writes the entity/predicate graph* — against
> the binding constraint that **inference is local-only** (`gpt-oss-120b`, ~69 GB resident,
> 131,072 ctx; `qwen3.8-27b-abliterated` for vision; one Strix Halo box, 128 GB unified).
>
> **Every claim is labelled.** `[REPO]` = measured or read in this tree, with `path:line`.
> `[WEB]` = published elsewhere, with a URL. `[DERIVED]` = arithmetic over `[REPO]` numbers,
> shown so it can be checked. Line numbers rot; re-grep before citing (the same warning
> `docs/reference/MODEL_ACCESS_INVENTORY.md:24` gives, having measured 24% drift in itself).

---

## 0. Verdict first

**Recommend shape (c): no tools in the ingestion path.** Keep the model producing
**grammar-constrained structured JSON** against an explicit schema, applied by a deterministic
enactor; make the "conversation" a small number of **additional structured passes** over the same
note, each a fresh constrained call; and make "ask the owner when unsure" what it already is — a
**review card written by the deterministic layer from flags the model set**, never a live turn
that parks a 69 GB model on a human's reply.

Five reasons, each a measured number below:

1. **The streamed tool-call path on this exact model is ~44% reliable, and fails silently.**
   `backend/src/jbrain/llm/errors.py:26-45` records: *"gpt-oss-120b streamed its reasoning, the
   `tool_calls` deltas and the finish/usage chunks never arrived, and nine of one agent's sixteen
   turns were recorded as successful empty turns instead of the tool calls they actually were"*
   (observed 2026-08-27). `backend/src/jbrain/llm/router.py:912`: *"measured on the box: 12/12
   versus ~44%"* — non-streaming is reliable, streaming is not. An ingestion agent would use the
   non-streaming path, so this is survivable — but it is the reliability floor the design starts
   from, and it was invisible until someone went looking. `[REPO]`
2. **The constrained-JSON path has a repair loop; the tool path has none.** `router.complete`
   re-asks once on unparseable JSON and then raises (`router.py:735-751`); `router.converse`
   takes no `json_schema`, has no re-ask, and its docstring says so
   (`router.py:768-783`). Under llama.cpp you cannot have both: tool calling builds its own
   internal GBNF and a user grammar/`response_format` cannot be layered on it (`[WEB]`,
   [llama.cpp discussion #15341](https://github.com/ggml-org/llama.cpp/discussions/15341);
   llama-server answers *"Either 'json_schema' or 'grammar' can be specified, but not both"*,
   [issue #11847](https://github.com/ggml-org/llama.cpp/issues/11847)). **Choosing tools is
   choosing to give up `maxItems`, `strict`, and the re-ask** — all three of which this repo
   currently relies on to survive gpt-oss's known looping (`analysis/intent_parse.py:48-58`).
3. **This question was already asked and answered on this box, with 121 cases.**
   `docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md:585-613` (§16) records **full agentic ingestion
   evaluated and rejected on evidence**, alongside multi-tier decomposition and an
   entity-lookup-tool specialist tier. §15 (`:515-583`) is the battery: genuine model-error rate
   **8.3% (10/121)** on the *structured* path, and *"in every genuine identity failure the
   colliding entities were already in the injected context; the model lacked restraint, not
   information, so a lookup tool answers a question the context already answers."* `[REPO]`
4. **gpt-oss cannot emit parallel tool calls**, so an ingest conversation is strictly serial:
   N facts ⇒ N round trips, each paying decode at ~31 tok/s and a prefill of the whole growing
   transcript. `[WEB]` + `[DERIVED]` — §4 below puts a 10-round note at **3–13 minutes** against
   today's ~1.5–3 minutes for the whole two-call pipeline, on a box where that model is also the
   owner's chat model and there is one KV slot.
5. **No graph-write tool exists today, and building one moves the safety spine inside the
   model.** Every one of the 117 `.tool` sidecars (`backend/src/jbrain/agent/tools/`) that
   touches owner truth *stages* rather than writes: `remember.tool` (*"This NEVER writes on its
   own"*), `propose_correction`, `propose_merge`, `file_correction` (writes a **note**, not a
   fact). Facts reach the DB only through `apply_intent` behind the deterministic arbiter. Shape
   (a) requires ~8–15 net-new write tools whose arguments are the firewall boundary — against
   CLAUDE.md #3 and against the plan's own I1–I9 invariants.

**Fallback ladder, in order, if a spike shows (c) is not enough:**
(c) → **(c+) two-pass constrained**: a second constrained call that sees pass-1's output plus a
*deterministically retrieved* wider graph slice (no model-chosen retrieval, so re-run determinism
survives — `ENTITY_GRAPH_INGEST_V2_PLAN.md:240-258` §6) → **(b) one fat `submit_graph_changes`
tool called exactly once**, which is (c) wearing a tool's clothes and buys nothing except the
loss of `strict` → **(a) many small tools**, only if a spike shows ≥95% well-formed-call rate
over ≥100 notes *and* the owner accepts non-deterministic re-runs. Today's evidence does not
support reaching (a).

---

## 1. The serving stack, verified

| fact | evidence |
|---|---|
| Gateway is **llama-swap fronting llama.cpp (Vulkan/RADV, gfx1151)**, pinned by digest | `deploy/docker-compose.yml:374-419` — `["llama-swap","--listen",":8080","--config","/models/llama-swap.yaml","--watch-config"]`, `LOCAL_LLM_BASE: docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:cea7c7…` `[REPO]` |
| Not vLLM, not Ollama | same; the vendor evaluation is preserved at `docs/reference/MODEL_ACCESS_INVENTORY.md:231` |
| Tool calling is served through the model's own chat template | `backend/src/jbrain/llm/llama_swap_config.py:286-292` — *"Tool calling needs the model's own chat template: `--jinja` … Without it the OpenAI `tools` we send have no grammar"* `[REPO]` |
| Prefix reuse is on | `llama_swap_config.py:433` — `--cache-reuse 256` `[REPO]` |
| Reasoning is split onto its own channel | `llama_swap_config.py:435-440` — `--reasoning-format deepseek` `[REPO]` |
| A vendored harmony chat-template override is in play | `llama_swap_config.py:449-455`, `deploy/chat-templates/README.md` (moves harmony's `Current date` to the prompt tail so it stops invalidating the KV prefix) `[REPO]` |
| gpt-oss-120b served at 131,072 × 1 slot, **69.26 GB measured peak** (predicted 68.55) | `MODEL_ACCESS_INVENTORY.md:290-296` `[REPO]` |

### Constrained decoding: available, used, and mutually exclusive with tools

`[REPO]` The adapter sends OpenAI `response_format: {type: json_schema, …, strict: true}` at
`backend/src/jbrain/llm/openai_compat.py:196-200`. llama.cpp converts that to GBNF. This repo
relies on it working, and has validated it on-box:

> *"Hard array ceilings so grammar-constrained decoding can't run away. gpt-oss-120b
> intermittently LOOPS on this task — re-emitting the same fact until it nears the token cap and
> TRUNCATES the JSON… `maxItems` is honored by the local grammar backend (validated on-box: an
> array capped at N stops at exactly N)."* — `backend/src/jbrain/analysis/intent_parse.py:48-58`

Users of the schema path: `intent_parse.INTENT_SCHEMA` (integrate.note), `wiki/rewriter.py:146,173`,
`wiki/lint.py:745`, `agent/deep_research.py:2423,2849`.

`[WEB]` The limits that bound the design:

- **You cannot combine a user grammar with tool calling.** *"I don't think you can use grammar
  with function calling; I've seen evidence in the code that function calling internally uses its
  own grammar"* — [ggml-org/llama.cpp discussion #15341](https://github.com/ggml-org/llama.cpp/discussions/15341).
  llama-server rejects both at once: *"Either 'json_schema' or 'grammar' can be specified, but not
  both"* — [issue #11847](https://github.com/ggml-org/llama.cpp/issues/11847).
- **Grammar enforcement can silently vanish when thinking is on.** [issue #20345](https://github.com/ggml-org/llama.cpp/issues/20345)
  (open, filed 2026-03): with `response_format` + `enable_thinking: true`, *"grammar not enforced
  for ANY model with thinking ON"*; tested on Qwen3.5-35B-A3B and Qwen3-VL-8B. gpt-oss/harmony was
  not tested there, and this repo's on-box `maxItems` validation is evidence it *does* hold on the
  harmony path here — **but the Qwen hybrids this box also serves are exactly the affected family**
  (`MODEL_PROMPTING.md` "How a thinking level reaches each reasoner"). Worth re-verifying whenever
  the pinned base moves.
- **Grammar failure fails open, not closed.** [issue #19051](https://github.com/ggml-org/llama.cpp/issues/19051):
  when schema→GBNF conversion succeeds but grammar *parsing* fails, llama-server logs it and
  returns HTTP 200 with **unconstrained** output. A structured-output guarantee that can evaporate
  with a 200 is a guarantee the enactor must re-validate — which this repo already does
  (`intent_parse.parse_intent` is *"strict on the top-level shape … lenient on individual items"*,
  `intent_parse.py:1-14`).

---

## 2. Does the Full Brain agent already run on the local model? **Yes.**

`[REPO]` The static tables say cloud and the live box says local; both are true and the difference
has burned prior plans.

- All 20 `TASK_DEFAULTS` entries are the identical string `"xai:grok-4.3"`
  (`backend/src/jbrain/llm/router.py:51-120`), and so are all three `TIER_DEFAULTS`
  (`router.py:175-179`).
- `_resolve` orders **env pin → strength tier → task default** (`router.py:374-389`), and the
  agent loop passes `SYSTEM_STRENGTH` on **every** call (`agent/loop.py:643,658,1071,1481`; the
  value is `high`, from `agent/prompts/system.prompt:5`). So an un-pinned agent turn resolves
  through `TIER_DEFAULTS` and **never reads its `agent.turn` entry**
  (`docs/plans/LOCAL_MODEL_ACCESS_PLAN.md:60-66`).
- `_resolve_live` then folds in the DB `llm_task_overrides`, which outrank the env pin, the tier
  and the default (`router.py:428-500`, docstring at `:431-436`).
- **Live box, read 2026-08-22:** *"All 19 selectable tasks resolve to on-box models — 16 to
  `gpt-oss-120b`, 3 vision to `qwen3.8-27b-abliterated` … `provider_choices()` returns 12 local
  models and no cloud entries."* — `MODEL_ACCESS_INVENTORY.md:72-79`.

**So jerv — 37–44 tools, ReAct chains up to 500 steps supervised — has been running on
`gpt-oss-120b` in production for months.** Everything in §3 is evidence from that, not
speculation about it.

---

## 3. Observed tool-calling reliability of `gpt-oss-120b` in this tree

### 3.1 The failure catalogue, with citations

| # | failure | evidence | mitigation in tree |
|---|---|---|---|
| F1 | **Streamed tool-call rounds vanish silently.** ~44% of streamed tool turns; 9 of 16 turns for one agent recorded as successful *empty* turns. Non-streaming: 12/12. | `llm/errors.py:26-45`; `llm/router.py:908-912` | `LlmStreamTruncatedError` raised on a missing `finish_reason` (`openai_compat.py:437-455`); router re-issues once **non-streaming** and replays (`router.py:908-940`) |
| F2 | **No parallel tool calls.** One call per assistant turn. | `[WEB]` [HF gpt-oss-120b discussion #151](https://huggingface.co/openai/gpt-oss-120b/discussions/151) — `<|call|>` (200012) was removed as a stop token to stop post-tool-call hallucination, which *"inadvertently blocks parallel tool calling functionality entirely"* | loop iterates `turn.tool_calls` (`loop.py:872-900`) so it *would* handle a batch; the model does not send one |
| F3 | **A JSON-Schema `enum` on a many-optional-property tool object deterministically segfaults the harmony GBNF path** (turn returns HTTP 500). Bisected as `enum × full-optional-field-set`, not tool count or size. | `docs/runbooks/STRIX_HALO_SETUP.md:592-602`; `backend/src/jbrain/draw/scene.py:20-21`; `agent/visiontools.py:11-12`; regression test `tests/unit/test_agent_readtools.py:1578` | **allowed values live in prose, validated in the handler** — a permanent tax on schema strictness for every tool gpt-oss may see |
| F4 | **A rolling llama.cpp build once shipped a harmony tool-grammar regression that crashed every tool-carrying turn.** | `llm/smoketest.py:1-34`; `docker-compose.yml:388-398` (why the base is digest-pinned) | post-upgrade smoke test + rollback — but *"the probe no longer targets gpt-oss"* (loading 69 GB mid-update froze the box), so *"a harmony-specific regression can now ship unnoticed"* (`smoketest.py:22-38`) |
| F5 | **Tool-selection accuracy is materially below 100% and wording-sensitive.** Live `gpt-oss-120b`, `/api/debug/tool-probe`: 7-case fixture **5/7** with full descriptions; widened 16-case fixture **14/16**. One case wrong **5/5**. One summary line in three phrasings scored **1/5, 2/5, 4/5**. | `scratchpad/prefill-probe-results.json`; `docs/plans/TOOL_CATALOG_PLAN.md:164-192` | none; the plan's conclusion is *"n=1 is noise… any gate needs n≥5"* |
| F6 | **A prompt-stated tool budget is not self-enforcing** — gpt-oss does not reliably count its own tool calls and runs to the step cap. Capping `web_search` moved the runaway to `web_fetch`. | `docs/reference/MODEL_PROMPTING.md` "Prompt budgets need an engine backstop"; `agent/loop.py:186-206` (`ToolCallBudget`) | engine-enforced per-tool ceilings; *"Prompt = intent; engine = ceiling."* |
| F7 | **Empty final turns.** *"gpt-oss occasionally returns an empty final turn (a 0-token completion at high context, or all content stranded in the reasoning channel)"* — one child produced 21 tool calls then an empty answer. | `agent/loop.py:852-859`; `agent/jmolt_night.py:70` | forced-final synthesis at `FINAL_ANSWER_EFFORT = "none"` (`loop.py:167-171`) |
| F8 | **Harmony leaks a tool round's *analysis* onto the content channel**, which then glues itself in front of the real reply. | `agent/loop.py:578-583, 1033, 1115, 1470`; `agent/contracts.py:218`; `agent/transcript_accumulator.py:40` | reclassified into the thinking trace by the loop |
| F9 | **Toolless turns still emit tool calls as prose/JSON.** A step-capped child's "answer" comes back as raw `{"query": …}`. | `agent/loop.py:172-180` (`FINAL_ANSWER_DIRECTIVE`) | an explicit "emit no tool call, no JSON" directive appended as the last user turn |
| F10 | **Discriminated-union / many-optional-field argument objects are hard for it to fill.** *"gpt-oss-120b could not fill that parameter"* (`agent/jmoltscratchtools.py:36`); umbrella tools were only adopted *after* per-family probes confirmed it fills the `action` reliably (`agent/agents.py:198,208`, `grokipediatools.py:10`, `researchtools.py:9`, `publicrecordstools.py:9`). | as cited | tool shapes are chosen by probe, not by taste |
| F11 | **It loops on the integrate task**, re-emitting the same fact until the token cap truncates the JSON. An anti-duplication *prompt* line made it emit **more**, not fewer. | `analysis/intent_parse.py:48-58` | `maxItems` in the **grammar** — a mitigation that **does not exist on the tool path** (§1) |
| F12 | **A grammar can force a key present but not non-empty**; the model emits `""`. | `agent/jmoltscratchtools.py:190` | handler-side validation |
| F13 | Structured-path judgment errors on the ingest task: **8.3% genuine (10/121)** on an adversarial battery; the two clusters are inferred-sensitive escalation and same-name ambiguity, plus **2 flag-strips** (a safety class: the engine can add review, never remove it). | `docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md:515-583` | deterministic nets I5 / namesake-ambiguity / I2 catch exactly those; the A/B prompt+schema fix cleared 8/10 and both flag-strips |

### 3.2 What the loop does about it, and what that costs

`agent/loop.py` treats every tool problem as an *observation*, never a crash: an unknown or
out-of-allowlist name returns `"tool not available: {name}"` (`:1663-1673`), a raised handler
exception returns a generic actionable string plus the sidecar's first authored example
(`:1674-1694`), and `max_consecutive_tool_errors = 3` (`:133`) ends a wedged chain with
`too_many_errors` (`:905`, `:1294`). There is **no argument-repair path** — no re-ask, no schema
re-prompt, no partial-parse salvage. A tool call whose arguments are not a JSON object raises
`LlmBadResponseError` and kills the turn (`openai_compat.py:353-360`, `:475-481`).

**That asymmetry is the design decision.** The JSON path gets a re-ask and a hard failure the
caller can retry (`router.py:735-751`). The tool path gets a soft observation and a 3-strike
counter. For a *conversational* agent that is right — a wrong tool call is recoverable by talking.
For an *ingestion* path that must be re-runnable and must not half-write a graph, it is the wrong
shape: three bad calls and the note is silently under-extracted, with no exception for the job
queue to retry.

---

## 4. Throughput reality

### 4.1 Measured on this box `[REPO]`

| quantity | value | source |
|---|---|---|
| Prefill (prompt processing), gpt-oss-120b, 44-tool payload | **~416 tok/s** — 22,704 tokens in **54.6 s** (3 cold runs: 55.82 / 56.12 / 56.42 s) | `scratchpad/prefill-probe-results.json` `prefill_timing_2026_08_17`; `STRIX_HALO_SETUP.md:186-188` |
| Warm `--cache-reuse` hit on the same payload | **0.87–1.28 s** | same |
| 280-token control | 1.52 s | same |
| Decode, gpt-oss-120b | **~31 tok/s** | `STRIX_HALO_SETUP.md:1451` |
| End-to-end tok/s including prefill, one observed forced-final | **~3 tok/s** (~74 s for a low-effort synthesis) | `agent/loop.py:167-170` |
| Cold **weight load** of gpt-oss-120b | **104 s** to a 200; a load+warm measured at **198 s** (warm alone 118 s of it) | `LOCAL_MODEL_ACCESS_PLAN.md:212-214`; `LOCAL_MODEL_LEDGER_PLAN.md:219` |
| Unload (llama-swap graceful stop) | **11.0 s** | `LOCAL_MODEL_ACCESS_PLAN.md:218` |
| Disk KV-slot restore of a primed prefix | ~100 ms–2 s vs a ~60 s prefill | `STRIX_HALO_SETUP.md:196-230`; `llm/kv_prefix.py` |
| jerv's tool block alone | **27,787 tokens** of rendered schemas | `llm/router.py:327`; `TOOL_CATALOG_PLAN.md:38-42` (44 tools ≈ 111.0k chars) |

Two corroborations that the prefill number is hardware, not fault: a hybrid 27B on the same box
measures **~243 tok/s** prefill, and *"Published figures for a 27B-class Q4 on Strix Halo Vulkan
are 250–330 tok/s"* (`STRIX_HALO_SETUP.md:393-409`) — gpt-oss's 416 tok/s is the MoE's lower
active-parameter count showing up, as expected.

### 4.2 Wall-clock for one agentic note `[DERIVED]`

Assume a lean ingest surface: system prompt ~4k tokens, 12 small graph tools ~6k tokens
(a tenth of jerv's 27.8k), the note ~1k, retrieved graph context ~3k. Static prefix ≈ 10k,
per-note tail ≈ 4k.

- **Turn 0 prefill.** The static prefix can be primed and reused (`--cache-reuse 256`,
  WarmKeeper, `kv_prefix`) → ~1 s if warm. The per-note 4k tail is never in any cache →
  4,000 / 416 ≈ **10 s**.
- **Per round trip.** Decode is the pole. gpt-oss bills its reasoning trace against the same
  budget (`loop.py:81-86`, `TURN_MAX_TOKENS = 16384`); the integrate probes ran **~950 output
  tokens each** (`ENTITY_GRAPH_INGEST_V2_PLAN.md:492`). Take 300–1,200 tokens per ReAct step →
  **10–39 s decode**. Incremental prefill of the previous round's tool call + result (say
  300–1,500 tokens) → **1–4 s**.
- **5 rounds:** ≈ 10 + 5×(11 to 43) ≈ **65 s – 3.7 min.**
  **15 rounds:** ≈ 10 + 15×(11 to 43) ≈ **3 – 11 min.**
- **Plus the slot-clobber tax.** gpt-oss runs **one KV slot** by default, and it was measured
  holding **2,164 tokens against a ~36k jerv prefix** because `note.extract`,
  `entity.disambiguate` and `fact.adjudicate` all route to the same model and take the slot in
  turn (`STRIX_HALO_SETUP.md:530-536`). Every clobbered round re-prefills the **whole**
  conversation: at 20k accumulated tokens that is **48 s**, once per clobber. Two clobbers in a
  15-round note put it past **12 min**.

**Against today's two-call pipeline `[DERIVED]`:** `note_extract.prompt` is 30,688 bytes and
`integrate_note.prompt` 11,207 bytes (≈ 8k + 3k tokens of system text), `max_tokens: 16384` each,
observed outputs ~950–2,000 tokens. Two calls ≈ 2 × (prefill 20–35 s + decode 30–65 s) ≈
**1.5–3.5 min per note**. **The agentic version is roughly 2–10× that**, and its variance is much
wider because the round count is model-chosen.

### 4.3 Is that acceptable?

**Capture-to-searchable is not at risk.** `docs/reference/ANALYSIS.md:13,34` — *"chunk + embed +
FTS (local, no LLM — searchable within seconds) … Capture-to-searchable never waits on a cloud
LLM"*, and `:468` — *"Embedding is never gated"*. Facts and entities are explicitly async
enrichment. So the honest statement is **"a captured note has *facts* in 3–13 minutes instead of
1.5–3.5"**, not "search gets slower".

**What *is* at risk is the box.** Three second-order costs, all `[REPO]`:

1. **The owner's chat model is the ingest model.** Every ingest round takes the one slot and
   evicts jerv's primed ~33k prefix; the next chat turn pays a cold **~56 s** prefill.
   Longer ingest turns ⇒ more collisions.
2. **Serialisation is enforced.** `spawn._effective_max_parallel` forces parallelism to **1** on a
   local route (`agent/spawn.py:901-910`) — *"A single-GPU local model serializes every call."*
   A backlog of N notes is N × (3–13 min), strictly serial.
3. **An "ask the owner" turn cannot be synchronous.** A live question parks a 69 GB model and a
   held conversation on a human reply; the agent wall-clock ceiling is already
   `_MAX_TURN_WALL_CLOCK_S = 3600` (`agent/runlog.py:36-38`). The existing mechanism — a
   `review_items` card raised by the deterministic layer — is the correct shape and is
   asynchronous by construction.

---

## 5. Context budget

`[REPO]` gpt-oss-120b serves **131,072** tokens on this box (catalog `-c`, per-model window
picker; `MODEL_ACCESS_INVENTORY.md:290`). Occupancy for a jerv-shaped turn today:

| component | tokens | source |
|---|---|---|
| Tool schemas (44 tools) | **27,787** | `router.py:327` |
| Persona + harness preamble | ~5k | `agent/prompts/system.prompt`, `priming.py` |
| Measured primed prefix in practice | **~33–36k** | `STRIX_HALO_SETUP.md:520,530` |
| Remaining working context | ~95k | `[DERIVED]` |

For an ingest agent this is *better* than jerv (a dozen tools ≈ 6k, not 27.8k), and the 131k
window is not the binding constraint — **prefill time is**. Four facts that shape the design:

- **The tools block sits at the front of the prompt, so anything that changes it invalidates the
  KV prefix from the tools block onward** (`TOOL_CATALOG_PLAN.md:53-58`). A dynamic per-turn tool
  array (mode (a) in that plan) *"has a real latency cost on exactly the model it most wants to
  protect."* An ingest agent must therefore send a **fixed** tool array for the whole note.
- **Prefix caching exists and is a latency mechanism only.** `--cache-reuse 256`
  (`llama_swap_config.py:433`), the WarmKeeper (`llm/warm_keeper.py`), the disk KV-slot store
  (`llm/kv_prefix.py`, 25 GiB budget, fingerprinted over launch line + system text + tool schema +
  reasoning effort), `prefill.watch` for the PWA's "Reading your prompt…" row (`llm/prefill.py`).
  There is **no billed prompt caching** anywhere in this repo — *"zero `cache_control` breakpoints
  anywhere in `backend/`"* (`TOOL_CATALOG_PLAN.md:47-53`).
- **The cache is fingerprint-keyed and brittle.** Any drift in the rendered system text, tool
  JSON, launch flags, `n_ctx`/`n_slots` or reasoning effort changes the filename and the restore
  simply misses (`STRIX_HALO_SETUP.md:477-484`). The harmony template's live `Current date` had to
  be moved to the prompt tail for this reason (`STRIX_HALO_SETUP.md:230-233`,
  `deploy/chat-templates/README.md`).
- **A second slot is the fix for ingest-vs-chat contention and costs KV RAM.** `-np 2` doubles the
  model's KV (~10.0 GB per 128k for gpt-oss with `kv_full_history`,
  `STRIX_HALO_SETUP.md:370-382`); llama-server routes each request to the slot with the longest
  matching prefix, so chat and ingest stop evicting each other
  (`STRIX_HALO_SETUP.md:526-546`). **If any agentic-ingest design ships, the interactive slot is
  a prerequisite, not an optimisation.**

---

## 6. The vision path — the premise is partly wrong, and that is good news

The brief states the vision model means unloading the 120b. `[REPO]` says otherwise for the
configurations this box actually serves:

| model | measured resident | source |
|---|---|---|
| `gpt-oss-120b` @131,072×1 | **69.26 GB** (peak-across-warm) | `MODEL_ACCESS_INVENTORY.md:296` |
| `qwen3-vl-30b-q4` @32,768×1 | **22.06 GB** | `MODEL_ACCESS_INVENTORY.md:294` |
| `qwen3-vl-30b` (Q8) @16,384×1 | **33.42 GB** | same |
| `qwen3.8-27b-abliterated` @**262,144** | **36.92 GB** (pre-flight reserved 20.29 — 1.8× light) | `llm/local_catalog.py:1275-1278` |

Box total reads **121.2 GB** usable; the live free-RAM fraction is **0.05**, so the ceiling is
**~115 GiB**, not the ~103 GiB the 0.15 default implies (`MODEL_ACCESS_INVENTORY.md:86-88`,
`LOCAL_MODEL_ACCESS_PLAN.md:212`).

`[DERIVED]` 69.26 + 22.06 = **91.3 GB**; 69.26 + 33.42 = **102.7 GB**; 69.26 + 36.92 =
**106.2 GB**. All three fit under ~115 GiB. And the docs say so explicitly: the Q4 vision twin
*"co-resides with gpt-oss-120b under the free-RAM floor instead of evicting it"*
(`STRIX_HALO_SETUP.md:632-636`), the abliterated entry *"co-resides beside gpt-oss-120b"*
(`STRIX_HALO_SETUP.md:656-658`), and the compose default is sized for exactly that pair
(`docker-compose.yml:123-125`).

**Swap cost, if a config does force one** `[DERIVED]` from `[REPO]` numbers: unload 11.0 s
(`LOCAL_MODEL_ACCESS_PLAN.md:218`) + vision cold load + work + unload 11.0 s + gpt-oss cold load
**104–198 s** + prefill of the ingest prefix (~1 s if the KV-slot file restores, up to ~56 s if
it does not, `STRIX_HALO_SETUP.md:186-230`). **A single round trip through vision costs 2.5–5
minutes of pure overhead** — 40–100× the cost of the vision call itself.

**Recommendation for the vision path:**
1. **Pin the co-residing pair** (`gpt-oss-120b` + `qwen3-vl-30b-q4` @32k, ~91 GB) so no swap
   happens at all in the ingest path. Verify with a live footprint read before committing.
2. **Batch attachments regardless.** OCR/caption per note, all attachments in one sweep, *before*
   the text pass — which is already the architecture (`ANALYSIS.md:455-470`: the work-gate holds
   the extract until OCR lands, so *"an image note is extracted once, with its OCR text"*).
3. **Never let the text model call a vision tool mid-conversation.** `agent.vision` exists so a
   text-only model can delegate a read (`router.py:64-67`), but on an ingest path that is exactly
   the mid-turn second-model load the memory doctrine refuses: `tool_in_turn` is classified
   **refused, with the reason** — *"a second model mid-turn is the co-residency threat; a person
   is waiting, so it cannot defer"* (`LOCAL_MODEL_ACCESS_PLAN.md:364`).
4. Note that the abliterated build **prepends its own "never refuse" system prompt above ours,
   with no API switch** (`MODEL_PROMPTING.md` header note; `STRIX_HALO_SETUP.md:645-652`). It is a
   red-team probe, `recommended=False`, and *nothing routes to it by default*
   (`local_catalog.py:824-828`). Using it as the ingestion vision model means every ingest prompt
   is displaced by a jailbreak prompt. **Use `qwen3-vl-30b-q4`.**

---

## 7. The three tool-surface shapes, judged against this model

### (a) Many small tools with strict schemas

**Failure modes on gpt-oss-120b, all measured:** no parallel calls (F2) ⇒ one write per round ⇒
a 12-fact note is 12+ round trips at 11–43 s each; `enum` is unusable on any many-optional-field
argument object (F3), so type discipline moves into prose and into handlers; selection accuracy
is 14/16 at best and wording-sensitive (F5); union/`action` parameters are a known weak spot
(F10); no `maxItems`, no `strict`, no re-ask (§1, `router.py:768-783`); the loop's only recovery
is a 3-strike counter (F-§3.2). Plus the structural costs: ~8–15 net-new write tools whose
arguments *are* the firewall boundary (CLAUDE.md #3), and model-chosen retrieval breaks re-run
determinism, which JBrain depends on because it **re-extracts on model/prompt upgrades**
(`ENTITY_GRAPH_INGEST_V2_PLAN.md:240-258`, `:589-593`).

**Verdict: reject.** This is the shape §16 already rejected on evidence.

### (b) One fat `submit_graph_changes`, called once

**What it buys:** the model's output is a single validated payload; the applier stays
deterministic; injection surface and round count are the same as today.
**What it costs:** you give up `response_format: json_schema` + `strict` + `maxItems` (§1) and get
llama.cpp's internal tool grammar instead — which is the grammar with the **known segfault**
(F3) and no `maxItems` to bound the documented integrate loop (F11). You also lose
`router.complete`'s re-ask (`router.py:735-751`). And F1's silent-empty-turn class becomes a
silently *skipped* note rather than a visible error.

**Verdict: (b) is (c) with the safety rails removed.** It is only worth it if the model is
measurably better at filling a tool argument than at filling a `response_format` body — which
nothing in this repo suggests, and F3/F10/F11 suggest the reverse.

### (c) No tools — constrained JSON + deterministic applier, "conversation" = more passes

**Failure modes, honestly:** the same 8.3% genuine judgment error rate (F13); looping (F11,
bounded by `maxItems`); truncation at the token cap (mitigated by the same ceiling and by the
`llm.json_reask` retry); the fail-open grammar bug (`[WEB]` #19051, re-validated by
`parse_intent`); and the loss of *adaptive* retrieval — the model sees the graph slice a
deterministic ranker chose (`analysis/graph_context.rank_and_bound`), not one it asked for.

That last one is the only real capability loss, and §15 measured it as not mattering: *"in every
genuine identity failure the colliding entities were already in the injected context; the model
lacked restraint, not information"* (`ENTITY_GRAPH_INGEST_V2_PLAN.md:600-604`).

**Verdict: recommend.** It preserves everything the local model is good at (the battery calls
cross-subject attribution, assertion status, injection resistance and near-determinism strengths),
uses the one reliability mechanism this stack actually provides (GBNF via `strict` + `maxItems`),
keeps re-run determinism, keeps the firewall deterministic, and costs 1.5–3.5 min/note instead of
3–13.

**How to get the redesign's *spirit* without tools:**
- *"asks the owner when unsure"* → the model sets flags (`inferred`, `domain`, `ambiguous`,
  `needs_review`), the deterministic nets escalate, the owner answers a **card**. Already built;
  §15's A/B shows the flag discipline is prompt-fixable.
- *"a conversation, not a single shot"* → **passes**, not turns. Pass 1 constrained extract; pass 2
  constrained integrate against a deterministically retrieved slice; optional pass 3 only for
  notes a deterministic trigger marks hard (cross-subject, namesake collision, sensitive-inferred).
  Each pass is a fresh `router.complete` with its own schema and its own re-ask — three
  independently retryable jobs instead of one 15-round transaction that can half-fail.
- *"the model writes the graph"* → it writes an **intent**; `apply_intent` writes the graph. That
  separation is what makes an RLS isolation test possible per CLAUDE.md #3.

---

## 8. The spike — cheap, one owner session, run before committing

**Everything below runs through `/api/debug/*` with a minted capability token — no shell, no
redeploy, no production write** (`docs/runbooks/DEBUG_ACCESS.md:225-240`;
`backend/evals/box/README.md`). Precedent: this is exactly how §15's 121-case battery and
`TOOL_CATALOG_PLAN`'s 16-case fixture were run.

### S1 — Well-formed-call rate (the make-or-break, ~1 hour of box time)

**Corpus:** N = **40 notes** — 20 from the graded corpus (`backend/tests/eval/corpus/`) and 20 from
`backend/tests/harness/scenarios/` chosen for fact density and at least one cross-subject and one
namesake case. Each run **×3** (the model is non-deterministic; `evals/box/README.md` insists on a
pass *rate*), so 120 calls.

**Arm A — tools.** `POST /api/debug/tool-probe` with `raw_tools` carrying a **5-tool** draft write
surface, deliberately minimal and enum-free (F3):

```
resolve_entity(mention_ref, mode:"existing"|"new"|"ambiguous", entity_id?, new_kind?, new_name?)
assert_fact(entity_ref, predicate, kind, assertion, statement, value_json?, object_entity_ref?,
            inferred:boolean, self_confidence:number)
supersede_fact(fact_id, superseded_by_ref, reason)
flag_for_review(entity_ref?, fact_ref?, reason)
done(summary)
```

`system` = the production `integrate_note.prompt` text, minus its "emit JSON" tail, plus one line:
*"Call one tool per turn. Call `done` when the note is fully recorded."*
`user_text` = the rendered note + the deterministically ranked graph context (build both with
`analysis.graph_context.render_graph_context`, exactly as `evals/integrate_runner.py` does).
`task="integrate.note"` so the live local route and its reasoning effort apply.

`/tool-probe` runs **no handler**, so a multi-round conversation must be driven client-side:
feed each proposed call back as a synthetic tool result (`"ok"`), re-post, repeat, cap at 25
rounds. Use `/complete-async` + `/jobs/{id}` for anything slow — the tunnel dies at ~100 s and
`/upstream/…` at 180 s (`STRIX_HALO_SETUP.md:466-474`).

**Arm B — constrained JSON (the control).** Same system + user text, `POST /api/debug/complete`
with `task="integrate.note"` and today's `INTENT_SCHEMA`. This is the current production path;
it is the baseline the new design must beat.

**Metrics, per note, per repeat:**

| metric | Arm A | Arm B |
|---|---|---|
| M1 **well-formed** | every proposed call names a real tool and parses as a JSON object matching its schema | body parses and `parse_intent` returns non-None |
| M2 **complete** | `done` reached within 25 rounds without 3 consecutive malformed calls | no truncation, no `llm.json_reask` exhaustion |
| M3 **judgment** | score the reconstructed intent with `jbrain.evals.integrate_runner._score` — identical scorer both arms | same |
| M4 **flag-strip** (safety) | any fact that should carry `inferred:true` / `expected` and does not | same |
| M5 **wall-clock** | seconds, submit→`done` | seconds |
| M6 **rounds / tokens** | round count; summed input+output tokens | 1; tokens |

**Pass/fail — the numbers that decide it:**

- **Kill (a) and (b) if M1 < 95%** over 120 Arm-A calls. Below that, every note needs a human or a
  retry, and the loop has no repair path (§3.2). *Prior: F5's best fixture result is 14/16 = 87.5%
  on selection alone, before argument correctness — so this is the likely outcome.*
- **Kill (a) and (b) if M3(A) does not beat M3(B) by ≥5 points**, or if M4(A) > M4(B) at all. A
  safety regression is disqualifying regardless of judgment gain — *the engine can add review,
  never remove it* (`ENTITY_GRAPH_INGEST_V2_PLAN.md:559-566`).
- **Kill on latency if median M5(A) > 5× median M5(B)**, or if p90 M5(A) > 10 min. §4.2 predicts
  3–13 min; anything past 10 min at p90 makes a backlog un-drainable at 1 note at a time
  (`spawn.py:901-910`).
- **Kill on context if median M6(A) tokens > 4× Arm B.** Every extra token is prefill on a
  single-slot box that also serves chat.

**Cost of the spike:** ~160 model calls, ~2–4 h of exclusive box time (serial —
`evals/box/README.md` rule 2), zero code changes, zero writes.

### S2 — Two cheap pre-checks, ~20 minutes, run first

- **S2a — segfault survey.** `POST /tool-probe` with each draft write tool's schema in turn, then
  all five. If any returns `"local: HTTP 500"`, F3 has already bitten and the schema needs
  reshaping before S1 is meaningful. (This endpoint *"was built to bisect gateway segfaults, and it
  found one"* — `TOOL_CATALOG_PLAN.md:228`.)
- **S2b — grammar-still-honoured check.** `POST /complete` with `task="integrate.note"` and an
  `INTENT_SCHEMA` variant whose `facts.maxItems` is **2**, against a note with ten obvious facts.
  If more than two come back, `[WEB]` issues #20345 / #19051 have reached this build and the
  **constrained** path — the recommendation in §0 — has lost its main guarantee. This is a
  cheap standing regression check worth adding to the nightly eval regardless of the redesign.

### S3 — If S1 passes (only then)

Re-run S1's Arm A at **N = 100 notes × 3** including the owner's real-corpus snapshot, and add
**re-run determinism**: run each note twice and diff the resulting intents. §6 of
`ENTITY_GRAPH_INGEST_V2_PLAN.md` requires re-run determinism by recomputation; a model-chosen
read order breaks it. **If the two runs disagree on >10% of facts, the design is incompatible
with re-extraction on prompt/model upgrade** — which is not a tuning problem, it is an
architectural one.

---

## Open questions for the owner

1. **Is the async review card an acceptable "ask me"?** The redesign's appeal is the agent asking
   mid-note. On this box that means holding a 69 GB model and a live conversation across a human
   reply, on a one-slot server that is also your chat model. Is a card in the review inbox — the
   mechanism that exists — actually worse for you, or just less exciting?
2. **How much slower per note can you live with?** §4.2 puts an agentic note at 3–13 min against
   today's 1.5–3.5, serial, with your chat turns paying a ~56 s cold prefill behind each one.
   Search is unaffected; *facts* are late. Where is the line?
3. **§16 already rejected full agentic ingestion on a 121-case on-box battery (2026-07-24).**
   What changed since — new evidence, a new goal, or dissatisfaction with the Levers A/B/C
   outcome? If it is the last, the cheaper move is finishing Wave 2's prompt+schema fixes, which
   the same battery's A/B showed cleared **8 of 10** genuine errors and **both** flag-strips.
4. **Will you buy the interactive slot?** `-np 2` on gpt-oss costs ~10 GB of KV and removes the
   ingest↔chat cache clobber. Any version of this design — even (c) — is better with it, and (a)
   is unworkable without it.
5. **Which vision model in the ingest path?** `qwen3.8-27b-abliterated` prepends a jailbreak
   system prompt above ours with no switch and is `recommended=False`. Should the three vision
   tasks move to `qwen3-vl-30b-q4` (22.06 GB, co-resides with gpt-oss at 91.3 GB total) so the
   ingest path never swaps and never runs under a displaced prompt?
6. **Re-run determinism: still a requirement?** JBrain re-extracts on model/prompt upgrades. A
   tool-using agent that chooses what to read produces a different graph each run. If that
   requirement is negotiable, say so explicitly — half the objection in §7(a) is downstream of it.
7. **What happens to the ~8.3% the model gets wrong, in the new world?** Today the deterministic
   nets catch exactly those two clusters. If the model writes the graph directly, what replaces
   them — and how is that tested per CLAUDE.md #3 (an RLS isolation test per new table/write
   path)?
8. **Do you want S2b as a permanent nightly check** regardless of this redesign? Grammar
   enforcement failing open (HTTP 200, unconstrained output) is a live upstream bug class, and
   this repo's structured-output guarantees rest on it holding.
