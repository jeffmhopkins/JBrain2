# Browser agents on local models — what works, and where ours differed

> **Status:** Research · **Last verified:** 2026-10-05 · Feeds `../../plans/BROWSER_FAST_LOOP_PLAN.md`.

Two research passes on 2026-10-05, after the shipped B1 loop (`../../plans/BROWSER_AGENT_PLAN.md`)
measured 110–240 s for "tonight's showtimes at a cinema with a location picker" on Flash-Next.
The question was generic: how do people drive a browser with a small local model today, and
what is our loop doing differently. Epic Theatres is only the worked example.

## Where our time went (measured on the box)

Each step: ~5k tokens of page view prefilled at ~400 tok/s (~12 s on a new page), up to 320
thinking tokens decoded at 10–20 tok/s (16–32 s), then a verbose tool-call JSON. ~25–35 s a
step, 5–12 steps a run. Decode — mostly thinking — dominates, then step count, then prefill.

## Findings

- **No framework publishes a quality benchmark for small local models.** browser-use's 89% on
  WebVoyager is GPT-4o (9.7–36 steps a site,
  <https://browser-use.com/posts/sota-technical-report>). browser-use on a local qwen3:4b hit
  75–180 s model timeouts where a scripted browser took 4.9 s
  (<https://lite.ego.app/article/browser-use-local-llm>). Small open models become competitive
  only after fine-tuning on web trajectories (<https://arxiv.org/html/2602.14721v1>).
- **Text beats screenshots for small text models**, but only pruned: AgentOccam gained up to
  +15.8% purely by pruning the observation and shrinking the action set
  (<https://arxiv.org/html/2410.13825>); Agent-E switches between text-only, inputs-only and
  all-fields views (<https://arxiv.org/pdf/2407.13032>).
- **Observation size.** browser-use's distilled DOM keeps interactive elements only, indexed
  `[12]<button>…`, ~1.5–3k tokens a page
  (<https://dev.to/ifnodoraemon/under-the-hood-of-browser-use-100k-stars-dom-tree-distillation-vision-grounding-and-5363>);
  raw playwright-mcp snapshots run 50–200k on app-like pages
  (<https://github.com/microsoft/playwright-mcp/issues/1233>). Ours, pruned, is ~5k — 2–3×
  browser-use.
- **Thinking.** Nothing published shows reasoning helps small models act on the web; Qwen3's
  report finds no consistent gain for small models (<https://arxiv.org/pdf/2505.09388>), and
  browser-use's fastest mode (`flash_mode`) turns it off. The usual pattern is thinking off for
  actions, on only after a failed step.
- **Fewer model calls.** Several actions per call — browser-use defaults to
  `max_actions_per_step=5`, stopping the batch when the page changes
  (<https://docs.browser-use.com/open-source/customize/agent/all-parameters>). Deterministic
  replay of a successful run — browser-use's cached script
  (<https://docs.browser-use.com/cloud/agent/cache-script>), workflow-use
  (<https://github.com/browser-use/workflow-use>), Stagehand's action cache keyed on the
  instruction and page (<https://docs.stagehand.dev/v4/basics/observe>). Plan-once-then-execute
  (<https://arxiv.org/pdf/2604.09718>, 80–94% zero-shot).
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
  their bodies, so capturing response JSON needs our own hook (a thin wrapper server, or an
  init script installed through `browser_evaluate`).

## Estimated effect for the worked example (research arithmetic, not measured)

| Configuration | Calls | Estimate |
|---|---|---|
| Shipped B1 loop | 5–12 steps | 110–240 s (measured) |
| No-thinking actions, ~1.5k-token indexed view, 2–5 actions a call, compact grammar-bound commands | ~4 | ~40–60 s |
| + response/embedded-data capture, direct-link search first | 2–3 | ~20–30 s |
| + replay a saved site recipe | 1 (extraction) | ~5–10 s |
