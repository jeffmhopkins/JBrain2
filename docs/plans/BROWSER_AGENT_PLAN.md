# Browser agent — a real browser jerv can drive, and search off Tavily

> **Status:** In progress · **Last verified:** 2026-10-05 · **Waves:** B0🟡(built; on-box fence tests + Epic run pending) B1🟡(built; bake-off pending) B2◻️ B3◻️

`web_fetch` reads pages; it cannot *use* them. Epic Theatres is the case that started this
(2026-10-04): every page reads "Please select a location" until a visitor picks a theater,
because the site keeps that choice in browser storage. Direct fetch, the reader and Byparr
all return the same 221-character template, and jerv spent 48 steps on aggregators before
giving up. A browser that can click "Titusville" and read the result does it in about five.

The prior-art survey is `docs/research/browser-agent/SELF_HOSTED_BROWSER_AGENT_OPTIONS.md`.
Its conclusion, which this plan adopts: **don't build a browser agent — adopt a browser
tool server and run our own small loop over it.**

## 1. Decisions

| Question | Decision | Why |
|---|---|---|
| Browser layer | **Microsoft's `playwright-mcp`**, official image `mcr.microsoft.com/playwright/mcp`, pinned by tag, HTTP transport | Apache-2.0, actively maintained, bundles its own Chromium. Observes pages as a pruned accessibility tree with element refs, so each step is one small tool call (`browser_click ref=12`) — no pixel coordinates, no large JSON schema. That is the shape a ~6B-active model can do. |
| Who runs the loop | **Our backend**, through the LLM adapter (non-negotiable 1) | browser-use and Stagehand own their model loop, bypass the adapter, and their own docs/maintainers say small Qwen models fail their schemas. playwright-mcp has no model of its own — the loop is ours, logged in the run-log. |
| Where browsing happens | **A sub-agent with its own context** (owner decision 2026-10-04) | jerv calls `browse(goal, start_url)` (start_url required since the 2026-10-05 fetch-first gate) and gets back a short text answer with source URLs. Page snapshots never enter jerv's context. |
| Runner-up | Vercel `agent-browser` (Apache-2.0) | Richer built-in policies (action allowlists, confirmations, delta snapshots); no confirmed official image. Measured against playwright-mcp in B1's bake-off. |
| Stealth | Stock Chromium first | A 2026 benchmark across 31 Cloudflare sites from a residential IP: vanilla Playwright cleared 24, the best stealth tool 28. A theater picker needs JavaScript, not evasion. Byparr cannot be driven (FlareSolverr API, not CDP); if stealth matters later, a zendriver-launched Chrome over CDP is the upgrade path. |
| Egress proxy | **Squid** (Canonical's `ubuntu/squid`, Ubuntu-LTS build, digest-pinned), built on the box with the deny list baked in and a build-time `squid -k parse` | Resolves DNS itself and checks the resolved address, so a public name pointing at a private address is refused. Smokescreen (Stripe) was the prior-art pick but publishes no official image; Squid's `dst` ACLs do the same job from a maintained distro package. Verified locally against the deny list (169.254/16, RFC1918, loopback, CGNAT, `db`, `searxng`, `localtest.me`, IPv4-mapped v6, CONNECT) — every one 403, a public site 200. |
| MCP client | **Hand-written over httpx** (`web/mcp_client.py`), not the `mcp` SDK | Four operations (open, list, call, close) against one server we run; the SDK would bring anyio/starlette/sse into the api for one caller. Handles JSON and SSE replies; verified against the real v0.0.82 image. |
| Model route | **`browse.step`**, its own task, following `agent.turn`'s model, pinned to **its own `browse` slot** (Flash-Next slot 8) | First shipped on the research slot to avoid a ninth slot; moved 2026-10-05 (owner): research agents will search and browse a lot, so sharing their slot would let a research turn evict a browse run's cache mid-run. The ninth slot reserves no pool cells (FLASH_NEXT_ENGINE_PLAN §4a). Following agent.turn means a local box never sends page text to a cloud default. |
| Search | **SearXNG primary**, metered APIs as backstop, Tavily optional | The box ran a pre-2026-09-04 SearXNG; the pinned build's curl_cffi client fixed this exact block pattern elsewhere. See B3. |

## 2. The fence — security is the network, not the tool flags

Playwright's own docs call its origin allow/block lists "not a security boundary"; a
playwright-mcp setup has been steered into screenshotting a cloud metadata endpoint.
Injection into browsing agents succeeded 42–68% of the time in 2026 tests, worse on weaker
models. So the rules are structural:

- **Own network.** The browser joins a new `browser` network, which only the api and an
  egress proxy also join. It never touches `internal`, where `db`, the supervisor and the
  model servers live. **Accepted risk (B0/B1):** because the api is on `browser` to reach the
  MCP port, a *compromised* Chromium (it runs `--no-sandbox`) could open TCP to `api:8000`
  directly, past Caddy — the same shape `pysandbox` and `jcode` already have. A page cannot:
  every request goes through the proxy, which refuses private addresses. It takes a browser
  exploit first. B2 closes it (below).
- **Egress proxy that resolves DNS itself** (so DNS rebinding can't slip past) and denies
  RFC1918, loopback, link-local incl. 169.254/16, CGNAT 100.64/10, IPv6 ULA and link-local,
  and every compose service name.
- **Rule of Two.** The sub-agent sees untrusted web content and nothing else: no notes, wiki
  or firewalled domains, no credentials, an `--isolated` in-memory profile.
- **Quarantined result.** Its answer returns to jerv as data — text plus source URLs, with
  links and images stripped — and can never pick a side-effecting tool.
- **Tiered actions, not a blanket read-only** (owner decision 2026-10-04: form filling is
  allowed, behind approval). Every state-changing action passes the risk gate in §2a before
  it runs; the gate lives in the host loop, never in the prompt.
- **Budgets.** ~20 steps, a wall-clock cap, a page cap, loop detection. Success is checked
  against the final page, never taken from the model's "done".
- **Existing sidecars.** `reader` and `byparr` also take untrusted pages but sit on
  `internal` beside the database. Moving them behind the same fence is **deferred from B0**:
  each needs its own proxy plumbing verified (the reader's headless Chromium and byparr's
  Camoufox take a proxy differently, and byparr's FlareSolverr API passes one per request),
  and a mistake silently breaks two `web_fetch` tiers on a box the owner cannot reach by
  shell. It is a follow-up with its own on-box check, not a compose one-liner.

## 2a. The risk gate — mundane actions run, risky ones ask

The owner wants routine work done without a prompt for every click, and anything touching
personal data or commitments to wait for approval. Prior work converges on the same shape:
**deterministic rules first, a separate judge for the gray zone, and the person approves
with the evidence in front of them.**

Prior work this borrows from:
- **OpenAI Operator** — asks before "significant" actions (purchases, sending), and hands
  control to the person for passwords and payment ("takeover") rather than typing them.
- **Claude in Chrome / computer use** — per-site permissions, always-confirm categories
  (purchases, publishing, sharing personal data), and an injection classifier on page
  content.
- **Vercel `agent-browser`** — declarative action policies with confirmation prompts.
- **Microsoft Presidio** (MIT) — detects PII in text: names, emails, phones, addresses,
  card and account numbers, IDs. Used on every value the agent is about to type.
- **The page itself** — HTML `autocomplete` tokens (`cc-number`, `email`, `tel`,
  `street-address`, `current-password`, `one-time-code`), `type=password`, payment iframes,
  a form's method and the submit button's label are strong, cheap signals.
- **Meta LlamaFirewall's AlignmentCheck, Google's Conseca, CaMeL, Progent** — a separate
  check that the next action still serves the owner's original goal, judged without the
  page's text in its instructions (so an injected page can't argue for itself). This is
  also how this repo's own Claude Code auto-mode classifier works.

The gate, in order (first match wins):

| Tier | Examples | What happens |
|---|---|---|
| **Never** | typing a password, card number, CVV, bank/ID number, one-time code; downloads that execute | Refused. The card tells the owner to do that step themselves. |
| **Ask** | any submit carrying PII (Presidio hit or a PII `autocomplete` field), creating an account, sending or posting a message, booking, reserving, ordering, agreeing to terms, a value that came from the owner's own data | Paused. A PWA card shows the screenshot, the site, the exact fields and values to submit, and Allow / Deny / Always allow this on this site. |
| **Auto** | navigation, search and filter forms (GET, `role=search`), choosing a location, date or tab, dismissing cookie banners, a site the owner has said "always allow" for this action kind | Runs, and appears in the trace. |
| **Judge** | anything the rules don't place | A separate small model call sees the owner's goal, the pending action and the field labels — never the page's prose — and answers auto or ask. Unsure means ask. |

Owner choices ("always allow on this site") are stored per site and action kind, are
listed and revocable in Settings, and never extend to the Never tier.

## 3. Waves

### B0 — The fenced browser, and the instrument 🟡 (built 2026-10-05; on-box checks pending)
- Compose: the `browser` service (playwright-mcp, pinned tag, `--isolated`, HTTP port,
  `shm_size`, `mem_limit`), the `browser` network, the egress proxy service. Move `reader`
  and `byparr` behind the proxy too.
- A compose test asserting the network membership (like the pysandbox one): the browser
  and the web sidecars cannot reach `internal`.
- Debug route `POST /api/debug/browse` `{goal, start_url?, max_steps?}` + `debug-connect.sh
  browse`: runs one goal and returns the step trace (action, snapshot tokens, latency), the
  answer and the final URL. Async with job polling (a run outlasts the tunnel's ~100 s).
- **On-box, with notice:** the fence tests must fail closed — 169.254.169.254, an RFC1918
  address, `db:5432`, `searxng:8080`, via a redirect and a rebinding hostname — plus an
  injection canary page. Then the Epic Theatres goal end to end.
- **Built:** `browser` (playwright-mcp `v0.0.82`, pinned by tag and digest, `--isolated`,
  `--proxy-server=http://egress:3128`, `--allowed-hosts=browser:8931`, `--no-webmcp`,
  `--image-responses=omit`, no ports or env, only its read-only launch config mounted, `cap_drop: ALL`) on the `internal: true`
  `browser` network, which only `api` and `egress` also join; `egress` (`deploy/Dockerfile.egress`
  + `deploy/egress/squid.conf`) on `browser` + `browser_out` only. `test_browser_compose.py`
  pins the membership, the flags, the pins, and that every compose service name is in the
  proxy's deny list. CI builds the proxy image. `POST /api/debug/browse` (a job, polled at
  `/jobs/{id}`, `web.browse` scope) and `debug-connect.sh browse` return the step trace, the
  answer, whether it was verified, the final URL and the exact text jerv would read; `--spec`
  runs one goal on a chosen model for the bake-off. `/api/debug/fetch` now also reports `gated`.
- **Hardened after review (2026-10-05):** the browser runs `read_only` with size-capped tmpfs
  for `/tmp` (profile + MCP output, `--output-max-size`) and `/home/node`, so nothing a page
  does can fill the box's disk; WebRTC may not send UDP around the proxy (a Chromium flag via
  `deploy/browser/config.json`, verified applied on the real image); Squid caps a plain-HTTP
  reply at 50 MB and also refuses `.localhost`, 6to4 and Teredo. CI now RUNS the built proxy
  and asserts its 403 matrix (`deploy/egress/fence-check.sh` — verified locally against the
  real image: 20 refusals, a public site through).
- **Deferred:** `reader`/`byparr` behind the proxy (see §2). Ops does not yet group the two
  new containers (they show under "Other") — B2's Ops work.
- **Pending on-box (owner notice):** the fence list above, through `debug-connect.sh browse`
  (the host gate refuses literal private addresses and internal names before the browser sees
  them, so the proxy's own refusal is exercised with a public name that resolves privately, a
  redirect, and a rebinding host); the injection canary; the Epic Theatres goal.

### B1 — The `browse` sub-agent 🟡 (built 2026-10-05; bake-off pending)

> **Superseded host loop:** the step loop below is being replaced by `BROWSER_FAST_LOOP_PLAN.md` (L0–L3); the fence, gates, quarantine and extraction described here stay.
- Host loop in the backend: the read-only tool allowlist mapped onto playwright-mcp's
  tools, snapshot pruning (interactive-only, delta where available), budgets, loop
  detection, final-page verification, the quarantined result. Its own task route and slot
  role (research-sized), so its prefix caches apart from jerv's.
- `browse` tool for jerv; jerv's prompt says when to reach for it (a page needs a choice
  made, a click, a tab, a form filter) and that its result is quoted data.
- `web_fetch` hands off: a thin page that reads as a location/store gate or a JS shell says
  so, and suggests `browse`, instead of leaving jerv to guess "JavaScript". The 200-char
  bar that let Epic's 221-char template through as a success is revisited here.
- **Bake-off** on 20–30 of the owner's real tasks, playwright-mcp vs agent-browser behind
  the same loop: success (verified on the final page), steps, time, snapshot tokens,
  tool-call parse failures. The winner ships; the numbers go in this plan.
- **Built:** `agent/browse.py` (the loop), `agent/browse_policy.py` (the gate, the page view,
  the quarantine), `agent/browse_actions/*.tool` (the eleven actions the model may pick:
  navigate, click, type_text, select_option, press_key, go_back, wait_for, tabs, snapshot,
  finish, give_up — mapped onto eight playwright-mcp tools plus `browser_snapshot`; nothing
  else on the server is reachable), `prompts/browse.prompt`, and jerv's `browse` tool
  (`tools/browse.tool`, `agent/browsetools.py`) with jerv prompt guidance (`agent-jerv-v56`).
  Budgets: 20 steps (ceiling 30), 240 s, 12 distinct pages, the same action refused on the
  third try and stopping the run on the fourth, six actions in a row that leave the page
  unchanged stop it. `finish` only says "this page answers it"; the host reads the answer off
  that page in one no-thinking call and checks it against the page (the fast finish, below).
- **The interim action rule** (until B2's risk gate): typing and selecting only into search,
  filter, location and date fields (or anything inside the page's `search` landmark) — no bare
  "address", "state", "type" or "format" — never a field whose label names an email, password,
  phone, card, account, name, message or code, and never a value shaped like an email or a long
  number; `Enter` only after such a field on the same page took the text; buttons that buy,
  book, sign in, send, submit, continue, proceed or go "next" are refused; links always pass
  (navigation is a GET). Numeric host spellings (`127.1`, octal, hex, one big decimal) and
  CGNAT are refused before the browser sees them. Dialogs and file choosers are dismissed by
  the HOST; the model is never offered either. One run at a time (a semaphore shared by jerv
  and the debug route).
- **The result jerv reads** is host lines plus the answer, last, on ONE line between
  `<<<BROWSE ANSWER BEGIN>>>`/`END>>>` markers (which are stripped from the answer), so a page
  cannot forge "Outcome:" or "Checked:" lines; error text and URLs are sanitized too (a URL that
  could carry a line break or hidden text is dropped). (The original `finish` also had to quote
  20+ characters or three words of evidence; the fast finish below replaced that check.)
- **web_fetch hand-off:** the 200-character bar is kept for recovery, but a first page whose
  wording is a location/store picker (`fetch.looks_like_location_gate`, under 1,500 chars) is
  now flagged `gated` whatever its length, says so, and — for a caller holding `browse` —
  suggests it, as an unrendered JS shell now does too.
- **First live run, and the step-cost fix (2026-10-05).** jerv's browse for "Epic Titusville
  showtimes tonight" on Flash-Next (`qwen3.8-flash-next`, research slot) ran 8 steps and hit
  the 240 s wall with outcome `timeout` and NO answer — while its final page was the
  showtimes page. Per-step `browse.step` input grew 2.7k → 6.8k → 7.9k → 6.7k → 13.3k →
  14.5k → 13.5k tokens, at 4, 15, 18, 17, 49, 40 and 58 s, writing 50–631 tokens each; a
  debug run of the same goal took 165 s over 6 steps, its `finish` alone 92 s for 1,443
  tokens. Three causes, three fixes:
  1. **The whole prompt was re-prefilled every step.** (Superseded the same day by the
     append-only fix below — the one-line-note design here still missed the cache.) The current page sat in the FIRST
     message, so each step's prompt diverged at its start and llama-server (Flash-Next is a
     hybrid; it reuses only a stable prefix) prefilled everything again. Now the messages are
     append-only: the opening is the goal and static instructions, every older page is its
     one-line note, and the page in full rides only on the last message. Step N's messages
     minus the last are a prefix of step N+1's (pinned by a test). That is prefix stability
     at the MESSAGE level; reuse at the TOKEN level is **unmeasured**. The prompt still
     diverges where the previous page (in full last step, a one-line note now) begins, and
     on the hybrid a cache can only be resumed from a context checkpoint at or before that
     point: the end-of-prompt checkpoint sits after it, and the mid-prefill ones
     (`--checkpoint-min-step 1024`, 8 per slot) can be evicted by a big page's own prefill.
     The research slot is shared, so concurrent research tasks can evict it too. Check it on
     the box: the debug `/browse` step trace now reports each step's `prompt_tokens`,
     `cached_tokens` and `output_tokens` (from `LlmUsage`), and llama-server's log says
     "restored context checkpoint" or "forcing full prompt re-processing" per request.
  2. **Every click thought at `xhigh`.** `browse.step` sat in the medium bucket, which sends
     no level, and Qwen3.8's template then defaults to `xhigh`. It is now in the **low**
     bucket (as `research.title`, which also follows the chat model, is), and — though it has
     no picker row — it is listed by name on the Flash-Next reasoning card so the owner can
     raise it; the `low` tier's level applies too. On a cloud `agent.turn` (Grok) the step
     now sends `reasoning_effort=low` as well — intended: a click needs no deep thought on
     any model. The prompt (`agent-browse-v2`) asks for a
     compact answer (a short list, under ~150 words) and to finish at once when the page
     already answers; once two steps or 60 s remain, the page carries a note to finish now.
     `STEP_MAX_TOKENS` (4,096) is left as is: at low effort a step's reply is the tool call.
  3. **A stop threw the page away.** A run that ends on `timeout`, `step_budget`,
     `page_budget`, `no_action`, `loop` or `stuck` now returns its final page's text
     (quarantined like the answer, capped at 6,000 chars, one line between
     `<<<BROWSE PAGE TEXT BEGIN/END>>>` markers after the host lines, labelled UNVERIFIED),
     with the final URL — that page's own, blank when it reported none, never an earlier
     page's — so jerv can answer from it (`browse.tool` v2 says so). The forgery test covers
     this path. `gave_up` and `error` carry none. The quarantine now NFKC-folds and strips
     every Unicode format (Cf) character — zero-width, bidi, word joiners, BOM, the Tag
     block — plus variation selectors, before its address check; marker stripping folds
     too and repeats until no marker is left, so fullwidth, split and nested markers cannot
     pose as an END line (regression tests for each).
  The wall stays at 240 s: with cache reuse and low effort a step should cost seconds, not a
  minute, and a run that still runs out now hands back the page. Re-measure on the box.
- **Second live run, and the append-only fix (2026-10-05).** After #1570 the Epic Titusville
  goal on Flash-Next answered and verified, but took 233–249 s. Per step the prompt was 4.6k–
  8.7k tokens and only ~2,200 were ever cached (the system prompt and the opening), so each
  step re-prefilled 2.4k–6.5k tokens at ~400 tok/s — 8–17 s for a ~30-token reply — and the
  `finish` step wrote 359–1,750 tokens (32–93 s). Effort `none` cut the thinking but the run
  wandered (13 steps, repeated clicks), so effort stays `low`. The cause: step N's full page
  became a one-line note in step N+1, so the prompt diverged at the start of the previous
  page, and on the hybrid a resume needs a context checkpoint at or before that point — none
  survived there. Four owner-approved fixes:
  1. **Strictly append-only.** Each step's messages are EXACTLY the previous step's plus a
     tail (the assistant's action and what it left); nothing sent is ever shortened or edited.
     The budget note is written into the new tail and stays, unedited. The divergence is then
     always the end of the last prompt, which is where llama-server keeps a checkpoint, so a
     step prefills only its tail. Pinned by a test that step N's whole message list is a prefix
     of step N+1's, across a navigation, a same-page change, a refusal, a nudge and a bounced
     `finish`. Growth is bounded by smaller views (2) and a total cap of 96k characters
     (~24k tokens): past it the run starts ONE fresh compacted prompt (the opening, a line per
     recent step, the page in full) — one full prefill — and extends that strictly again.
     Accepted cost: on a cloud `agent.turn` model (browse follows it) every page now stays in
     the input, so a step's billed input grows with the run rather than staying near one page.
     Local is the default, and a cloud provider's own prefix cache bills the repeated prefix
     at its cached rate, so this was not gated by provider.
  2. **Smaller views.** playwright-mcp v0.0.82 has no incremental snapshot (`--snapshot-mode` is
     `full` or `none` and governs action replies, not `browser_snapshot`), so the delta is
     host-side: the host still takes a FULL snapshot each step and gates every ref against
     it, but when the URL is the one the model last saw it is sent only what changed
     (`browse_policy.page_delta`: changed and new lines, each under the heading it sits in and
     the line above it, so a time still says which film it is; the lines and refs that went
     named; the whole page when the changes span more than one heading, run to more than six
     hunks, or would be over 60% of the page). A navigation sends
     the new page whole; a refusal re-sends nothing ("The page is as shown above."). The view
     cap dropped from 24k to 16k characters, a text line is clipped at 400, and over the cap the
     pruning keeps controls, headings and the two lines either side first, filling with
     far-off text only as room allows (it used to keep the first 24k, in order).
  3. **Short finish.** `answer` is the raw facts, one per line, no prose (`agent-browse-v3`,
     `finish` v2, ~80 words); jerv writes them up (`browse.tool` v3). The answer cap is 1,200
     characters (from 2,000); evidence verification is unchanged.
  4. **Its own slot.** `browse.step` moved from the research slot to a new ninth Flash-Next
     slot, `browse` (slot 8, 131,072 cap, freed second). The owner first kept the shared
     research slot (accepting that a concurrent research task could evict a run mid-way),
     then reversed it: research agents will be searching and browsing a lot, so browsing
     should not share their slot. Memory: none up front (FLASH_NEXT_ENGINE_PLAN §4a). One
     side effect on rollout: `-np` is in the F4 fingerprint and restore-gate key, so the saved
     role prefixes and conversation files start over and restores wait for the slot probe to
     pass again.
  **Expected effect, to re-measure on the box** (debug `/browse` per-step `prompt_tokens` /
  `cached_tokens`; llama-server's "restored context checkpoint"): every step after the first
  caches all of the previous prompt, so it prefills only its tail — ~30 tokens of action plus
  a new page (~1–3k tokens for a pruned Epic page, ~2–5 s) or a delta/refusal (~50–300
  tokens, under a second). The Epic run's four page-bearing steps should cost ~10–15 s of
  prefill in all instead of ~60–100 s, and a short `finish` (~100–300 tokens) ~10–25 s instead
  of 32–93 s — so ~60–90 s for the run, if the model wanders no more than it did. The prompt
  at `finish` grows to ~10–14k tokens (all pages kept), still far under the compaction cap.
  Security is unchanged: the gate reads the host's full page, never the delta; quarantine,
  forgery, type/select/Enter, URL, budget and semaphore tests all still pass.
- **Third live run, and the fast finish (2026-10-05).** After #1571 caching worked: each step's
  prompt was cached up to about the previous prompt, and the four navigation steps of the Epic
  Titusville goal (Flash-Next, effort low) took 8–10 s each. Then `finish` spent **148 s writing
  2,942 tokens** (thinking out the answer) and was REJECTED because its evidence quote, "7:45
  PM", was under the 20-character/three-word minimum; the re-finish took 29 s (604 tokens). Run
  total 234 s. (The answer also listed the whole day's times though the goal said "only
  upcoming this evening" — accepted: jerv filters.) Owner-approved fix:
  1. **The model navigates; the host reads.** `finish` (v3) carries no answer — only an
     optional `note` of where on the page the answer is (`agent-browse-v4`). The host then
     makes ONE extraction call: its own small prompt (`prompts/browse_extract.prompt`,
     `agent-browse-extract-v1`), one user message with the goal, the note (labelled as where to look — "The browsing
     agent says to look at: …" — never as the answer) and the final page's
     readable text (quarantined, its strings one per line, up to the 16k view cap, fenced as
     data), **no tools, reasoning effort `none`, 700 max tokens**, same `browse.step` task and
     `browse` slot. Its reply is raw facts, one per line, quarantined and capped at 1,200
     characters; "NOT FOUND" ends the run as `not_found` with the page text handed back. One
     extraction only — never a bounce and retry; a failed call is `not_found` with the error,
     not `error`, so the page still goes back.
     **Why a fresh prompt, not the history plus a request:** appending to the step history
     would reuse its cache only if the call kept the action tools (they render at the head of
     the prompt, so dropping them diverges at token ~0 and re-prefills the whole ~10–14k-token
     history, ~30 s), and a tools-present call can still answer with a tool call instead of
     text. A fresh prompt is ~2–4k tokens (system + goal + one page), ~5–10 s of prefill at
     ~400 tok/s, and at effort none the reply is the facts themselves (~100–300 tokens). It
     is a FRESH prompt: no cache reuse is assumed for it (its whole prefill is in the
     estimate). The step history is untouched, so the strict-extension test still pins every
     STEP call.
     **Its time is reserved.** No step starts inside the last 30 s of the wall
     (`EXTRACT_RESERVE_SECONDS`), and the extraction runs after the browser session closes,
     outside the drive's timeout, given at least that reserve — so a `finish` chosen right at
     the deadline is still read; one that runs out of time is `not_found` with the page text.
  2. **Host-side verification replaces the evidence quote** (`browse_policy.facts_on_page`).
     From each answer line the host takes its salient tokens — clock times (folded so "7:15PM",
     "7:15 p.m." and "7:15 PM" are one), standalone numbers, prices and dates, and capitalised
     words of 3+ letters other than a few function words — and looks each up, as a whole token,
     in the final page's FULL text (NFKC, casefolded, single-spaced). Verified needs EVERY
     time and price found (one invented showtime fails the answer), ≥80% of the names and
     other numbers, and every line backed by at least one match. A bare one- or two-digit
     number is not evidence (a stray "7" is on every page): it is not counted, and a line
     whose only numbers are such, with no time or price beside them ("Dune: 7, 10"), is
     unbacked. An answer with nothing checkable is UNVERIFIED. jerv's line reads "Checked: the answer's names, times and numbers
     are on the final page." or UNVERIFIED (`browse.tool` v4). The `extract` step in the trace
     carries the tally ("verified: 2 of 2 times and prices, 3 of 3 names and numbers on the page") and its own
     call's prompt/cached/output tokens.
  3. **A stopped run reads its last page too.** A run that ends on `step_budget`,
     `page_budget`, `loop`, `stuck`, `no_action` or the reserve's `timeout` with ≥10 s of the
     wall left runs the same extraction on its final page (bounded by the time left); a
     verified result turns the run into `answered`, an unverified or failed one is dropped
     (logged, not shown as the run's error) and the page text goes back as before. The hard
     cut (wall + 30 s, a hung browser) leaves no time and is not tried.
  4. **Effort `none` per call.** The extraction passes `effort_override="none"`: on Flash-Next
     (a hybrid) that is `chat_template_kwargs.enable_thinking=false`. On a cloud `agent.turn`
     model it is sent as `none`, exactly as any other per-call none.
  Security: the extraction sees only the goal and the page (Rule of Two unchanged); its output
  takes the answer's path — quarantine (markup, links, invisibles), one line between the
  answer markers, markers stripped — and a forgery test drives a poisoned extraction end to end.
  **Expected:** navigation unchanged (~35–40 s for the Epic run's four steps), `finish` a few
  seconds (a ~30-token tool call), the extraction ~10–20 s — **~60–70 s** for the run instead
  of 234 s. Re-measure on the box (debug `/browse`: the `finish` and `extract` steps'
  `model_ms` and tokens).
- **Fetch first, enforced (owner decision 2026-10-05).** `browse` is refused — a fast tool
  error telling jerv to `web_fetch` the URL first and use its result — unless, earlier in the
  SAME turn, a `web_fetch` of the same registrable domain (eTLD+1 via the bundled Public
  Suffix List, the `tld` package; `www.` and other subdomains match their parent, `bbc.co.uk`
  is not every `.co.uk`; a suffix the list does not know falls back to the host less `www.`;
  never down to a dotless name; an IP or dotless host never passes) came back needing a
  browser: `gated` (location/store picker), `js_shell` (unrendered JavaScript app), thin (a
  plain read from the top whose whole page is under the fetcher's own 200-character recovery
  bar, `fetch.THIN_PAGE_CHARS`), or `blocked` — a fetch that FAILED on a bot wall, challenge
  page, paywall or other hard block (`_block_reason`), or was refused because the host is on
  the 24h skip list, which is exactly where a real browser can get through (a 404, a glitch
  or a search form does not count). The gate keys on "some page of this domain, this turn, was
  gated/JS/thin/blocked"; a fetch that redirected to another site records only the site it
  ENDED on. Only `start_url` is checked: where the run navigates afterwards is governed by the
  browse policy (`check_url`), not this gate.
  `start_url` is now required. The tools decide it: web_fetch records the verdict from the
  result's own flags on the turn's `ToolContext.browser_needed` (domain → reason; one per
  turn, so an earlier turn's fetch does not count), and `agent/browse_gate.py` reads it before
  any model call or browser session — never the model's judgment, never the result text. The
  web_fetch browse hint uses the same test, so it never suggests a call the gate would refuse.
  There is **no "needs interaction" bypass**: the owner's ruling is that the agent cannot be
  trusted to judge it. **Accepted trade-off:** a page that fetches fine but needs a click to
  reveal its data (a tab, a "show more", a date picker over a readable page) is refused —
  revisit if it bites. The debug `POST /browse` route is NOT gated (it is the measuring
  instrument); an optional `require_fetch_gate` flag was skipped because the route has no turn
  whose fetches it could consult. `jerv.prompt` `agent-jerv-v57`, `browse.tool` v5.
- **Fourth live run, and a thinking cap on each step (2026-10-05).** After #1572 the Epic
  Titusville goal (warm, effort low) ran **161 s**, answered and verified: four navigation
  clicks ~33 s in all, the extraction 25 s — and the step where the model decided to call
  `finish` took **83 s writing ~1,543 tokens** of thinking (86 s / 1,593 in a second run).
  Effort `none` on every step was tried and made the model wander (12–14 steps, 216 s), so
  steps stay at low. Fix (owner-approved):
  1. **A per-request thinking budget.** llama.cpp at the Flash-Next pin (`869034b`) takes one
     per request: `tools/server/server-common.cpp` reads `reasoning_budget_tokens` (alias
     `thinking_budget_tokens`, default the server's `--reasoning-budget`) and
     `reasoning_budget_message` from the body (~L1412–1426) and passes them to the sampler
     whenever the template has a think-end tag (the autoparser finds Qwen's `</think>`,
     `common/chat.cpp` ~L1356–1363); `common/reasoning-budget.cpp` counts the tokens after the
     think-start tag (the generation prompt's own `<think>` included, `common/sampling.cpp`
     ~L311–322) and at the cap forces the message plus `</think>`, so the reply continues to its
     tool call. No server flag changes. `Sampling` gained `reasoning_budget` (it is a sampler
     there): the OpenAI-compatible client sends it, with a short "enough thinking; act now"
     line as the message, to `local` only — never to xAI, never to Anthropic — and a server or
     template without a think tag ignores it (gpt-oss's harmony template, the standard
     engine's older pin). `browse.prompt` declares `config: sampling: reasoning_budget: 320`
     and every STEP call passes it; the extraction does not (thinking is off there). It is a
     sampling field, not part of the prompt, so the strict-extension prefix cache is
     untouched. The debug `/complete` `sampling` object takes it too, so the cap can be A/B'd
     from the PWA's debug console.
  2. **Firmer finishing guidance** (`agent-browse-v5`): the moment the page shows what the goal
     asks, `finish` is the whole decision — no re-checking, no working out or weighing the
     answer (the host reads and filters it).
  Why not the host-side alternatives: a stream-and-abort-then-retry spends the wasted tokens
  first and adds a second call; a "page looks like the answer" heuristic switching effort to
  none per step needs a target-shape guess and, at none, risks the wandering measured above.
  The server-side cap needs neither. **Expected:** the finish step ~320 thinking tokens plus a
  ~30-token call, ~18–20 s at the measured ~18.6 tok/s instead of 83 s; navigation steps
  (~100–200 tokens of thinking each) are rarely capped. **~95 s for the run**, toward ~90 s
  if v5's guidance shortens the decision further. Re-measure on the box: the `finish` step's
  `output_tokens` in the debug `/browse` trace should read at most ~360.
- **Pending:** the bake-off is an on-box measurement and has not been run; agent-browser is
  untested. The snapshot pruning measured ~3x on Epic's home page (9.0k → 2.8k chars) and
  ~1.4k tokens on wikipedia.org against the real image (before the tighter cap above).

### B2 — Seeing it in chat, and approving ◻️ (GUI gate: three mocks, owner picks)
- A **tool card** in the chat that pops up while `browse` runs (owner decision 2026-10-04:
  screenshots, no live stream): the latest screenshot and readable step rows ("Opened
  epictheatres.com", "Picked 'Titusville'", "Read today's showtimes"); it settles into the
  Worked pane when the run ends.
- The **approval card** for the Ask tier, in the same place: screenshot, site, the fields
  and values about to be submitted, Allow / Deny / Always allow on this site.
- The risk gate (§2a) ships here with its rules, Presidio on typed values, and the judge
  call; its decisions are logged per step.
- Settings: browsing on/off, step budget. Ops: the browser container's health and the last
  runs' traces.
- **Close the api hop** (the accepted risk in §2): put an MCP-only relay between the api and
  the browser, or a network shape where the api reaches the browser without the browser being
  able to reach the api, so a compromised Chromium has nothing on its network but the proxy.

### B3 — Search off Tavily ◻️
- **Done ahead of the plan (PR #1557):** SearXNG, reader and byparr pinned to dated/digest
  images — the update never re-pulled `latest`, so all three had frozen at install.
- SearXNG settings: enable Bing and the structured engines (Wikipedia, Wikidata, GitHub,
  StackExchange, arXiv); drop or down-weight DuckDuckGo (it bans the IP); shorten
  `suspended_times` (defaults bench an engine for a day on one captcha); surface engine
  suspensions on Ops.
- A rate-limited queue in front of SearXNG for deep-research bursts, on top of the
  existing one-hour cache.
- Provider chain with per-provider budgets: SearXNG → **Brave Search API** (owner decision
  2026-10-04; about 1,000 queries/month on its free credit, then $5/1,000) → optionally
  Mojeek (£2/1,000). Tavily becomes an optional last tier the owner can switch off.
- The Brave key is set **in Settings**, never `.env` — a panel mirroring the Tavily one
  (`api/tavily_settings.py`): stored key with the env as fallback, enable toggle, a "Test
  key" button, and this month's usage against the budget.
- **Brave provider and Settings panel landed in PR #1557:** `BraveSearch` + a per-month query
  budget (default 900, counted in `app.settings`), the chain reordered to SearXNG → Brave →
  Tavily, and the **Brave Search** Settings panel (`api/brave_settings.py`). The rest of B3
  is still open.
- A week of per-engine health on the new pin (including a 50-query burst) decides whether
  Brave's free credit covers the overflow.

## 4. Decided (owner, 2026-10-04)
1. **Forms are allowed, behind approval** — the tiered risk gate in §2a.
2. **Screenshots, no live view** — shown as a tool card that pops up during a run.
3. **Brave Search API is in**, its key set in Settings.

## 5. Open questions
1. **The judge's model.** Flash-Next itself at low effort in a small slot, or a smaller
   dedicated classifier? Decided by B2's measured false-auto rate.
