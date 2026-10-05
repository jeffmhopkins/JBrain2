# Browser fast loop — a generic browse that a local model finishes in seconds

> **Status:** Scheduled · **Last verified:** 2026-10-05 · **Waves:** L0◻️ L1◻️ L2◻️ L3◻️

The B1 `browse` sub-agent (`BROWSER_AGENT_PLAN.md`) works and is fenced, but on Flash-Next a
simple fact behind a location picker still takes about two minutes, and jerv has called it
twice in one turn. The owner's bar: seconds, not minutes, and **generic** — no site-specific
scrapers or reverse-engineered APIs. The research
(`../research/browser-agent/LOCAL_MODEL_BROWSER_AGENTS.md`) says we built the textbook
cloud-model loop — clicking through the visible UI one decision at a time — while setups that
work on small models read the data the page already has, take fewer and cheaper decisions, and
replay what worked. This plan supersedes B1's host loop. The B0 fence, the fetch-first gate,
the action gate, quarantine, budgets, the no-thinking extraction and the host fact check stay.
An adversarial review (2026-10-05) reshaped the first draft; its points are folded in below.

## 1. Where the time goes now (measured, warm, after #1573)

Worked example (a cinema with a location picker), B1 loop with prefix caching, page deltas and
the 320-token thinking cap: **117 s and 124 s**, answered and verified.

| Phase | Time |
|---|---|
| 4 navigation steps (one is a stale-ref retry) | ~31 s |
| `finish` decision (reads the result page for the first time, then decides) | 45–52 s |
| Extraction (fresh ~2.7k-token prompt, thinking off) + host fact check | ~21–25 s |

So the cost is spread across *how many decisions*, *the first read of the data page*, and *the
separate extraction prefill* — not one knob. Targets per wave are stated as measured medians on
the benchmark set (L0), not this one site.

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Measure first | **L0** sweeps the knobs that already exist before building anything. | The draft's diagnosis was stale; thinking-off already failed twice on the box (13 steps; then 12–14 steps, 216 s). Decide from numbers. |
| Thinking on action steps | The **smallest `reasoning_budget` that keeps benchmark success**, chosen by the L0 sweep (0 / 64 / 128 / 320); "off" is one arm, not the default. Fallback if small budgets wander: **planner-executor** — one capped-thinking call at the start writes a short plan (which control, which value), then no-think execution steps. | Evidence for "off" is weak for this model (the Qwen3 claim is unverified and Qwen3 ≠ Qwen3.8; browser-use's `flash_mode` drops its own reasoning fields, not evidence about small models). |
| Action format | A **native single `act` tool** whose argument is `commands`: an array (max 5) of a tiny enum schema (`click`, `select`, `type`, `enter`, `goto`, `read`, `back`, `done`) with an index and an optional value. llama-server already constrains tool calls; no adapter change; cloud parity. A raw GBNF grammar is an L0 A/B arm only. | At pin 869034b a custom grammar cannot be combined with tools (`server-common.cpp:1349` throws "Cannot use custom grammar constraints with tools"; `:1254` rejects json_schema with grammar), and whether it constrains thinking is unverified. A tool call is already ~30 tokens (~1 s). Dropping tools would let the extraction append to the cached history — the one real upside, kept as an L0 measurement. |
| Several actions per call | Up to 5 per call. **Before each next command** the host takes a fresh snapshot after a short settle and re-resolves the command's target by role + name; the batch stops if the target is gone or different, on any gate refusal, or on navigation. The model sees the executed prefix and the first failure. | "Stop when the page changes" is unreliable on single-page apps (late XHR, modals, autocomplete). |
| Element indexes | **Per-run monotonic** indexes bound to playwright refs (an element keeps its number across steps); "the index must be on the latest page" is enforced as today. | Append-only history keeps old views; per-snapshot numbering would collide with them. |
| Page view | The **indexed interactive view**: one line per actionable element `[n] role "name" (value)`, viewport plus one screen first, repeated rows collapsed, text clipped; full text via the `read` command. Built host-side from the snapshot. | Smaller views help small models choose (AgentOccam: pruning the observation and action space gave +26.6 points). Today's pruned pages are already ~0.7–3k tokens, so the gain is accuracy and fewer steps more than prefill. |
| Read data before clicking (L1) | Generic and ordered: (1) **embedded structured data** — schema.org JSON-LD, microdata, framework hydration blobs (`__NEXT_DATA__`, `__NUXT__`, `__APOLLO_STATE__`, Gatsby page-data) — read by **`web_fetch` itself** (`web/fetch.py` already detects these shells but does not read them); (2) when browse starts because the fetch was a **JS shell**, one **extraction attempt on the rendered start page** (text + captured JSON from L2 when available) **before any action step**. | Cheapest generic wins; many pages answer without a single click. |
| Response capture (L2) | The host calls playwright-mcp's `browser_run_code_unsafe` **once, before the first navigate**, with a **fixed constant script** that attaches `context.on('response')` and keeps capped bodies of first-party `application/json` 2xx responses **in Node memory** (out of the page's reach); a second constant script reads them back. Host-only code: the model can never reach `run_code` (already pinned by `test_browse.py`). | An init script via `browser_evaluate` runs after load, misses the load-time requests that matter, is lost on navigation and is writable by the page. |
| Fact check with captured data | **Each answer line must be backed by one source** — the page text or one captured response — and the trace names the source. | Checking against the union lets a multi-location JSON "verify" another location's times. |
| Direct link to a location/item | **jerv's job, not browse's**: search for the location/item page first, then `web_fetch` it; a server-rendered page needs no browse, a JS shell passes the existing fetch-first gate. No gate change, no new egress path inside browse. | Keeps the browse sub-agent single-purpose and the gate unchanged. |
| Second browse in a turn | Refused for the same registrable domain once a browse returned (answered, page text or timeout) this turn; jerv answers from what it has. | Seen live: a timeout followed by a second full browse. |
| Site memory (L3) | **Bookmarks, not scripts**: after a *verified* run, remember the final URL per (registrable domain, entity) — e.g. the location page reached — plus which data source held the answer. Next time browse starts there. Replay = navigation only, through `check_url`, same registrable domain; **answers are never cached** (extraction + fact check always run). | Simplest thing that removes the picker on repeat questions; avoids replaying clicks or storing scripts a page could shape. |
| Saved browser state (L3, optional) | Only after an **explicit B0 amendment**: keep first-party cookies/localStorage created during a verified run for that domain (never third-party/tracker state, never anything from a login — the browser never logs in), injected by a host-only constant script. Ships only if bookmarks alone leave pickers unsolved on the benchmark. | B0 promises an `--isolated` browser where nothing survives a run; changing that is an owner decision, not a side effect. |
| Where L3 lives | A **DB table with RLS and an isolation test**, classified as **location-firewalled** (non-negotiable 3): keys like "a cinema in Titusville" reveal where the owner goes. Settings off switch; list/delete in Ops and the debug API (rule 10). | Decided now, not in the wave. |
| Fallback | A Settings/debug switch selects the **B1 loop** until L1 meets its criteria on the box. | Roll back without a deploy or a terminal. |
| Never | Site-specific code, vendor API knowledge, stored credentials or logged-in state, `run_code` reachable by the model, replay of anything but same-domain navigation through the gate. | Owner: generic. Rule of Two and the B0 fence unchanged. |

## 3. Waves

### L0 — Measure with the knobs we have ◻️
- **Benchmark set** (generic, fixed, run through debug `/browse`): a cinema with a location
  picker, a store/stock lookup by location, a search box → result page, a paginated list, a
  tabbed detail page, a page needing one filter.
- Arms: `reasoning_budget` 0 / 64 / 128 / 320 on steps (debug `/browse` takes a per-run
  `reasoning_budget`, `debug-connect.sh browse --budget N`); grammar
  vs native tool call on a single-step probe; extraction appended to history vs fresh prompt.
- Recorded per run: success (fact check), steps, model calls, and time per phase (navigation,
  finish decision, extraction). The result restates §1 and sets L1's numeric targets.
- **Done when:** a table of arms × benchmark sites is in this plan and the step budget is chosen.

### L1 — Fewer, cheaper decisions ◻️
- `web_fetch` reads embedded structured data; browse tries extraction on the rendered start page
  before any action (JS-shell starts).
- The `act` tool with batched commands, per-command re-resolution, monotonic indexes, the
  indexed view and `read`; thinking per L0; planner-executor if L0 shows small budgets wander.
- jerv: search-for-the-page-first guidance; second-browse refusal (tests).
- Fallback switch to the B1 loop (Settings + debug).
- **Done when (on the box, benchmark):** success ≥ L0's best arm and median run time ≤ half of
  L0's best arm; the worked example ≤ 45 s warm.

### L2 — Read the data, not the screen ◻️
- Host-only response capture via `browser_run_code_unsafe` (constant scripts, Node memory, caps,
  first-party JSON only, analytics hosts dropped); captured data and embedded data handed to the
  extraction as fenced, quarantined data, ranked by overlap with the goal.
- Single-source fact check, source named in the trace.
- **Done when:** success ≥ L1 and median run time ≤ L1 − 30%; no answer verified across sources.

### L3 — Remember where the answer was ◻️
- Bookmarks table (RLS, isolation test, location-firewalled), written only after verified runs,
  read at browse start; Settings switch; Ops + debug list/delete.
- Optional saved first-party state, only with the B0 amendment approved by the owner.
- **Done when:** a repeat benchmark question runs with no picker interaction and ≤ 2 model calls.

## 4. Security — what must not move

The B0 fence, the fetch-first gate, the action gate (an index on the latest page, field-type
rules, commit refusal, public URLs only), quarantine of everything page-derived, budgets and the
semaphore apply unchanged to batched commands, `read`, captured data and bookmarks. Captured
JSON is data for the extractor, never instructions to the action model. `run_code` is host-only
with constant scripts. L3 data is location-firewalled.

## 5. Risks and open questions

1. Small thinking budgets may still wander; planner-executor is the named fallback.
2. Per-command re-resolution costs a snapshot per command; settle time is tuned in L1.
3. Response capture misses WebSocket and service-worker-served data; page text still covers it.
4. Bookmarks go stale when sites move pages; a bookmark that fails `check_url` or the gate is
   dropped and the normal loop runs.
5. Cloud models (if ever used) get the same `act` tool unchanged.
