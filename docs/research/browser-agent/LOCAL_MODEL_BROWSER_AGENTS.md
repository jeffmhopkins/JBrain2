# Browser agents on local models — what works, and where ours differed

> **Status:** Research · **Last verified:** 2026-10-05 · Feeds `../../plans/BROWSER_FAST_LOOP_PLAN.md`.

Two research passes on 2026-10-05, after the shipped B1 loop (`../../plans/BROWSER_AGENT_PLAN.md`)
measured 110–240 s for "tonight's showtimes at a cinema with a location picker" on Flash-Next.
The question was generic: how do people drive a browser with a small local model today, and
what is our loop doing differently. Epic Theatres is only the worked example.

## Where our time went (measured on the box, B1 loop)

Before prefix caching and the thinking cap (B1's first runs): ~25–35 s a step, 5–12 steps,
110–240 s. After both (#1571–#1573, warm): 117–124 s — four navigation steps ~31 s, the
`finish` decision 45–52 s (its first read of the result page), extraction ~21–25 s. Prompt
totals of 4.6–8.7k tokens include the system prompt and history; a pruned page view itself is
~0.7–3k tokens.

## Findings

- **No framework publishes a quality benchmark for small local models.** browser-use's 89% on
  WebVoyager is GPT-4o (9.7–36 steps a site,
  <https://browser-use.com/posts/sota-technical-report>). A company blog post (anecdotal, one
  hands-on test, no fresh benchmark) reports browser-use on a local qwen3:4b hitting 75–180 s
  model timeouts where a scripted browser took 4.9 s
  (<https://lite.ego.app/article/browser-use-local-llm>). Fine-tuning on synthetic web
  trajectories lifted Qwen3-14B by +9.2 on WebArena (<https://arxiv.org/html/2602.14721v1>).
- **Text beats screenshots for small text models**, but only pruned: AgentOccam gained +26.6
  points from aligning the observation and action space (pruning the view, a small action set)
  (<https://arxiv.org/html/2410.13825>); Agent-E switches between text-only, inputs-only and
  all-fields views (<https://arxiv.org/pdf/2407.13032>).
- **Observation size.** browser-use's distilled DOM keeps interactive elements only, indexed
  `[12]<button>…`, ~1.5–3k tokens a page
  (<https://dev.to/ifnodoraemon/under-the-hood-of-browser-use-100k-stars-dom-tree-distillation-vision-grounding-and-5363>);
  raw playwright-mcp snapshots run 50–200k on app-like pages
  (<https://github.com/microsoft/playwright-mcp/issues/1233>; not independently checked).
  Ours, pruned, is ~0.7–3k a page.
- **Thinking.** Nothing published shows reasoning helps small models act on the web, but the
  evidence against it is weak too: the Qwen3-report claim (<https://arxiv.org/pdf/2505.09388>)
  is unverified here and concerns Qwen3, not Qwen3.8; browser-use's `flash_mode` drops its own
  reasoning fields rather than measuring small models. On our box, thinking off wandered twice
  (13 and 12–14 steps). Treat the thinking budget as something to measure.
- **Fewer model calls.** Several actions per call — browser-use defaults to
  `max_actions_per_step=5`, stopping the batch when the page changes
  (<https://docs.browser-use.com/open-source/customize/agent/all-parameters>). Deterministic
  replay of a successful run — browser-use's cached script
  (<https://docs.browser-use.com/cloud/agent/cache-script>), workflow-use
  (<https://github.com/browser-use/workflow-use>), Stagehand's action cache keyed on the
  instruction and page (<https://docs.stagehand.dev/v4/basics/observe>). Plan-once-then-execute
  (<https://arxiv.org/pdf/2604.09718>, 80–94% zero-shot — with five frontier models, so not
  transferable as-is to a ~6B-active model).
- **Data off the wire.** Most location-picker sites (cinemas, retail) render an empty shell and
  load the data as JSON for the chosen location; the clicks only pick which request the page
  sends. Scrapers (Crawlee, Firecrawl, browser-use forks) record `application/json` responses
  during the browse and hand them to the extractor, and read embedded data first (schema.org
  JSON-LD, `__NEXT_DATA__`, `__NUXT__`, `__APOLLO_STATE__`, Gatsby `page-data.json`). Locations
  usually have their own URL (in `sitemap.xml`, in links, or found by a web search), so the
  picker can be skipped. A chosen location is often kept in cookies/localStorage, so a saved
  per-site `storageState` skips it next time. *Example:* Epic's site loads a theater's schedule
  as unauthenticated JSON after the pick, and every theater has its own URL.
- **Gap in our stack.** playwright-mcp's `browser_network_requests` lists requests but not
  their bodies. An init script via `browser_evaluate` would run after load and be lost on
  navigation; the workable hook at v0.0.82 is a host-only `browser_run_code_unsafe` call that
  attaches `context.on('response')` before the first navigation.

## Estimated effect for the worked example (research arithmetic from pre-cache numbers, not measured; superseded by the plan's L0)

| Configuration | Calls | Estimate |
|---|---|---|
| Shipped B1 loop | 5–12 steps | 110–240 s (measured) |
| No-thinking actions, ~1.5k-token indexed view, 2–5 actions a call, compact grammar-bound commands | ~4 | ~40–60 s |
| + response/embedded-data capture, direct-link search first | 2–3 | ~20–30 s |
| + replay a saved site recipe | 1 (extraction) | ~5–10 s |
