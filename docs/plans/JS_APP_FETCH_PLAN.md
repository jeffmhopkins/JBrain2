# JS-App Fetch — stop reading an un-rendered SPA as an empty page

> **Status:** In progress · **Last verified:** 2026-08-15 · **Waves:** J1✅ J2◻️

`web_fetch` walks a four-tier ladder (direct → reader → byparr → Tavily) whose
escalation was built entirely around a page being **blocked**: a 403/429, a challenge
interstitial, a paywall. A single-page app is none of those. It answers 200 with a valid
HTML skeleton and paints the article from JavaScript, so the static extractor reads it as
a successful fetch of a page with nothing on it.

Observed live on `qwen.ai/blog?id=…` — 92 KB of HTML, 53 `<script>` tags, **zero**
extracted text. jerv reported "the page didn't render its content (because it's a JS
app)" and went looking elsewhere: the right call, reached by guessing, because nothing in
the tool result said so.

## Two defects, one shape

**1. The escalation trigger was emptiness, not thinness.** Recovery fired only on
`not result.text.strip()`. Every *recovery* tier, meanwhile, has to clear
`_MIN_RECOVERED_CHARS` (200) to win — a deliberate rule from the challenge-solver work, so
a reader that renders a blocked origin as its bare title (`reuters.com\n===`) doesn't
short-circuit the stealth browser. The direct tier alone was exempt: it won at **one
character**. So the common SPA shape — a shell leaking a cookie line, a "Loading…"
placeholder, a splash-screen script's stray words — cleared the bar the reader would have
failed, and the ladder never ran at all. The strictly-empty case (qwen.ai) did escalate;
it was the *nearly*-empty case that silently didn't.

**2. Nothing told the model what it was holding.** An unrecovered fetch reported "That
page (url) had no readable text" — indistinguishable from "this topic has no page", and
silent about the fact that a headless renderer and a stealth browser had already tried.
The model was left to infer the cause from the URL and either give up or spend more calls
re-fetching the same URL at a different offset.

## Design

**Detection (`_looks_like_js_app`, `backend/src/jbrain/web/fetch.py`).** Evidence-based,
not a text-length rule, because the two mistakes cost differently: missing a shell wastes
only a page we already failed to read, while flagging a genuinely short real page (a
three-line status notice) burns the heavy solver and the **paid** Tavily tier on every
fetch of it. Two independent signals, either sufficient, both gated behind a thin
extraction so a real article never reaches them:

- the classic SPA hydration markers (`__NEXT_DATA__`, `__NUXT__`, `id="root"`,
  `data-reactroot`, `window.__INITIAL_STATE__`, a `<noscript>` telling you to switch
  JavaScript on, …);
- the shape that catches a framework we don't carry a name for — a lot of HTML and a lot
  of `<script>` with almost no readable text to show for it.

Unlike a challenge or a paywall this is **flagged, never raised**: the origin isn't
refusing us, the page is real and simply needs a browser.

**Escalation.** A first-page fetch that is empty *or* thin-with-JS-evidence walks the
existing recovery ladder, with the shell seeded as the thin fallback so escalation can only
improve on what we hold, never lose it. Held to the same `_MIN_RECOVERED_CHARS` bar as
every other tier — the asymmetry that caused the miss is gone.

**Telling the model (`JS_SHELL_MESSAGE` / `JS_SHELL_NOTE`, `web/fetch.py`, beside the
detector and `_SEARCH_FORM_MESSAGE`).** `FetchResult.js_shell` rides back on a result no
tier could paint, and the tool says three things the old message didn't: the page is a
JavaScript app, the renderer and stealth browser **were already tried** (so don't re-fetch
this URL), and this is not evidence the topic doesn't exist — go find it on a site that
serves real HTML, or hit the feed/JSON endpoint behind it. A shell that leaked a few words
gets the same warning appended to its text, because that is the more dangerous half: it
reads as a successful short page, and the model will otherwise quote chrome as content.
Both fetch surfaces carry it — jerv's `web_fetch` tool and the jcode sandbox's `web-fetch`
bridge (`api/jcode_llm.py`), which shares the fetcher and so inherited the same blind spot.

**Diagnosability.** `web.js_shell_unrecovered` (with the char count) marks a page the whole
ladder failed to render, alongside the existing `web.challenge_blocked` /
`web.solver_used`.

## Waves

- **J1 ✅ (this PR)** — `_looks_like_js_app`, the widened escalation trigger + `_richer`
  seeding, `FetchResult.js_shell`, the two tool messages, the
  `web.js_shell_unrecovered` log, and the same two messages on the jcode `web-fetch`
  bridge. Unit coverage in `test_web.py` (11 cases: leaky-shell
  escalation to reader and to solver, shape-only detection with no framework marker, the
  unrecovered/thin-recovery flag, the offset-page guard, and the two precision cases that
  must NOT escalate — a genuinely short page and a hydrated framework page — plus the two
  tool-message cases), web fetch faked via `MockTransport`, plus two bridge cases in
  `test_jcode_web_bridge.py`.
- **J2 ◻️** — live validation on-box via `POST /api/debug/fetch`: confirm a known SPA
  (`qwen.ai/blog`) either renders through a tier or comes back honestly flagged, and check
  whether the on-box reader needs a hydration wait (`X-Timeout` / `X-Wait-For-Selector`)
  to render a client-routed page at all — the plain render currently returns nothing for
  qwen.ai, which no amount of better routing fixes. Tune
  `_JS_APP_MIN_HTML` / `_JS_APP_MIN_SCRIPTS` against real traffic if the shape signal
  proves loose, and decide whether a JS-shell domain should be learned the way
  `solver_failed` domains are.

## Honest ceiling

Detection is a **correctness** fix: it stops a JS app being read as an empty page and stops
the model guessing at why. It does not itself render anything — recovery is only as good as
the reader/solver/Tavily tiers, and a site those three can't paint stays unread. That is
the honest outcome the message now reports, instead of a blank page passed off as content.
