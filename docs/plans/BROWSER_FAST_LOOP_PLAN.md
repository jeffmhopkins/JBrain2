# Browser fast loop — a generic browse that a local model finishes in seconds

> **Status:** Scheduled · **Last verified:** 2026-10-05 · **Waves:** L1◻️ L2◻️ L3◻️

The B1 `browse` sub-agent (`BROWSER_AGENT_PLAN.md`) works and is fenced, but on Flash-Next it
takes 110–240 s for a simple "showtimes at a cinema with a location picker" goal, sometimes
times out, and jerv has called it twice in one turn. The owner's bar: a simple page fact should
not take minutes, and the fix must be **generic** — no site-specific scrapers or reverse-
engineered APIs. The research (`../research/browser-agent/LOCAL_MODEL_BROWSER_AGENTS.md`) says
we built the textbook cloud-model loop — one verbose tool call per step, thinking on, a ~5k-token
page view, clicking through the visible UI — and that local-model setups that work invert each
of those. This plan supersedes B1's host loop; the fence (B0), the fetch-first gate, the
quarantine, the risk tiers and the extraction + host fact check all stay.

## 1. Where the time goes, and the target

Measured per step on the box: ~12 s prefill for a new ~5k-token page (~400 tok/s), 16–32 s of
capped thinking (10–20 tok/s decode), a few seconds of tool-call JSON. Five to twelve steps.

| | Today | L1 | L1+L2 | L1+L2+L3 (repeat task) |
|---|---|---|---|---|
| Model calls | 5–12 | ~4 | 2–3 | 1 |
| Worked example, warm | 110–240 s | ~40–60 s | ~20–30 s | ~5–10 s |

These are research estimates; every wave is measured on the box before the next starts, against
a fixed **benchmark set** (L1) rather than one site.

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Thinking on action steps | **Off.** One retry of the same step with low effort + the existing 320-token cap only after a failed action, a refused action, or a no-progress step. | Thinking is most of the decode; no evidence it helps small models act (Qwen3 report, browser-use `flash_mode`). Measured caveat: thinking off *with the old 5k view and one action a step* wandered (13 steps). So it ships only together with the smaller view and batching, and the benchmark decides. |
| Page view | **Indexed interactive view**: one line per actionable element `[n] role "name" (value)`, numbered from 1 per snapshot, viewport plus one screen below first, repeated rows collapsed, text clipped (~80 chars), a short heading outline. Target ≤1.5–2k tokens. Full readable text only via an explicit `read` action. | browser-use/AgentOccam: pruning the observation is the single biggest accuracy lever for small models, and prefill scales with it. Built host-side from playwright-mcp's snapshot (refs map to our short indexes); no new browser dependency. |
| Actions per call | **2–5 commands per call**, executed in order; the batch stops at the first one that changes the URL or the page structure, or that the gate refuses; the model then sees the new page. | browser-use's default; pickers and filters become one call. |
| Action format | **Compact text commands** (`click 12`, `select 4 "Titusville"`, `type 5 "Titusville"`, `enter 5`, `goto <url>`, `read`, `back`, `done "<where>"`), constrained by a **GBNF grammar** sent to llama-server, instead of native tool calling. Cloud models (if ever used) get the same commands without the grammar. | ~10–15 output tokens a command vs 50–100 for a tool call; a grammar makes malformed output impossible. Every command still passes the same host gate as today (refs must be on the latest page, type/select only into search/filter/location/date fields, commit buttons refused, public-http(s) navigation only). |
| History | Append-only, as today (cache reuse proven on the box): the prompt only grows. Each past step is the commands + one-line outcomes; past *views* stay as sent (never edited); compaction past the cap as today. | Keeps the measured KV reuse. Smaller views make the growth cheap. |
| Data sources (L2) | **Generic, ordered:** (1) embedded structured data on the page — JSON-LD/microdata and framework hydration blobs; (2) JSON responses the page fetched during the browse; (3) the readable page text. Fed to the existing no-thinking extraction as *quoted data*; the host fact check verifies against the union. | Most location-picker sites load the answer as JSON; scrapers read it this way. Data, not instructions — quarantine unchanged. |
| Response capture | A thin host-controlled capture, not a playwright-mcp fork: an init script installed by the host through `browser_evaluate` that wraps `fetch`/XHR and keeps same-site `application/json` 2xx bodies (size-capped, analytics hosts dropped) in page memory, read back by the host. If that proves unreliable, a minimal wrapper tool in our own small sidecar image instead. Decided by a spike at the start of L2. | Stock playwright-mcp lists requests but not bodies. |
| Start point (L2) | Before driving a home page, one web search for a **direct link** (the location/item page) when the goal names a place or item; the fetch-first gate still applies to whatever URL browse starts on. | Skips home-page and picker navigation on most sites. |
| Site memory (L3) | After a **verified** run, save a per-site **recipe**: the URL template reached, the commands with element *descriptions* (role + name, never indexes), and which data source held the answer; plus the cookies/localStorage diff the run created. Next run with a matching goal shape replays it **without the model**, re-checking each element by description; any mismatch falls back to the normal loop. | Stagehand/browser-use replay. Repeat questions become seconds. |
| What is never done | No site-specific code, no vendor API knowledge, no stored credentials or logged-in state, no replay of anything but GETs and the same gated commands. | Owner: generic tool. Rule of Two and the B0 fence unchanged. |

## 3. Waves

### L1 — The fast action loop ◻️
- Indexed interactive view builder (from the snapshot; refs ↔ indexes per snapshot; viewport-first;
  collapsing; clip) and the `read` action for full text.
- Compact command language + GBNF grammar through the LLM adapter (non-negotiable 1): a
  `grammar` field on the request, local-only on the wire, like `reasoning_budget`.
- Batched execution (2–5) with stop-on-change; every command through the existing gate;
  per-command outcomes in the trace.
- Thinking off on steps; one low-effort, capped retry after a failed/refused/no-progress step.
- Jerv side: after a browse that returned page text or a timeout for a site, a second `browse`
  of that registrable domain in the same turn is refused (answer from what you have).
- **Benchmark set**, generic and fixed, run through debug `/browse` before and after: a cinema
  with a location picker, a store/stock lookup with a location, a search box → result page, a
  paginated list, a tabbed detail page, a page needing one filter. Recorded: success, steps,
  model calls, time, tokens. A wave ships only if success does not drop and time falls.
- The trace keeps per-step model ms, prompt/cached/output tokens, and now commands per call.

### L2 — Read the data, not the screen ◻️
- Spike: init-script capture vs a wrapper tool; pick one, documented.
- Embedded-data reader (JSON-LD, microdata, hydration blobs) on every page; response capture
  during the browse; both summarised into the extraction prompt as fenced data with sizes capped
  and ranked by overlap with the goal's terms.
- Direct-link search before the first navigation when the goal names a place/item.
- Fact check verifies against page text ∪ captured data.

### L3 — Site memory ◻️
- Recipe store per registrable domain (storage abstraction or a table with an RLS isolation
  test — decided in the wave), with the goal-shape key, element descriptions, URL template,
  data-source hint, cookies/localStorage diff, last-verified time.
- Replay without the model; mismatch → normal loop; a failed replay marks the recipe stale.
- Owner controls: list and delete recipes (debug API + Ops), and an off switch.

## 4. Security — what must not move

The B0 fence, the fetch-first gate, the action gate (refs on the latest page, field-type rules,
commit refusal, public URLs only), quarantine of everything page-derived, budgets and the
semaphore all apply unchanged to batched commands, the `read` action, captured data and
replays. Captured JSON and embedded data are *data handed to the extractor*, never prompts the
action model sees as instructions. Replays use the same gate as live commands. Saved site state
holds no credentials (the browser never logs in) and stays per-site.

## 5. Risks and open questions

1. **Thinking off may still wander** with the new view; the retry-with-thinking rule and the
   benchmark guard it. If success drops, keep low effort on the first step only.
2. **Grammar + llama-server.** Per-request `grammar` is long-standing in llama-server, but it
   must be confirmed at the Flash-Next pin together with chat templates and reasoning off.
3. **Indexes vs refs.** Short indexes are per snapshot; a batch that crosses a page change
   stops before using stale indexes.
4. **Response capture** misses data loaded before the init script or via WebSocket; the
   page-text path still covers it.
5. **Recipes go stale** when sites change; replay re-checks every element and falls back.
6. **Cloud models**: commands without a grammar; not a target of this plan.
