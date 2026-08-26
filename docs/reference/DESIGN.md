# JBrain2 — GUI Design System

> **Status:** Living · **Last verified:** 2026-08-26

Binding reference for all UI work. Derived from the owner-supplied JBrain v1
reference screens (dark composer, knowledge hub, calendar, medical entry).
Components use **tokens only** — no raw hex values outside the token sheet.

## Principles

1. **Phone-first, one-thumb.** Primary actions live in the bottom half of the
   screen. Touch targets ≥ 44px. Bottom nav is the spine.
2. **Minimal / utilitarian.** Near-monochrome surfaces; color is *information*
   (state, domain), never decoration. No gradients, no glass, no shadows
   heavier than a hairline border.
3. **Comfortable density.** Generous padding and type sizes; fewer things per
   screen, each easily hittable. Data-dense surfaces (logs, lab tables,
   location history) may use the compact variants noted below.
4. **Color codes the domain.** Accents are muted and contextual: an active
   medical surface tints rose, research tints amber, general/info tints
   steel blue. The accent tells you *where you are and what kind of data
   you're touching*.
5. **Honest status, always visible.** Connectivity, sync, and server state are
   surfaced persistently (status dot + banner), never hidden behind a tap.
   The app must feel trustworthy about what it is and isn't doing.

## Theming

Dual theme, dark-first. Implementation:

- All colors are CSS custom properties on `:root`, overridden by
  `[data-theme="light"]`. Components reference tokens only.
- Default follows `prefers-color-scheme`; a Settings option overrides it
  (`system | dark | dark-bright | light`), persisted locally and (later) as a
  user setting.
- `dark-bright` ("Dark+") is a dark variant that inherits every dark neutral
  and only lifts `--border` to `#464A52` for stronger, brighter hairlines.
- The PWA `theme-color` meta updates with the active theme.
- **Text size**: every type token is `calc(px × var(--font-scale))`; a
  Settings "Text size" control (65 / 75 / 90 / 100%) sets the scale,
  persisted locally. **Default is 75%** of the drawn px values (settled in
  Phase 1 polish — the doc's sizes read large on real devices).

## Color tokens

### Neutrals (dark / light)

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0E0F11` | `#F7F7F5` | App background |
| `--surface` | `#17181B` | `#FFFFFF` | Cards, tiles, composer |
| `--surface-2` | `#1E2024` | `#F0F0EE` | Nested surfaces, inputs, inactive segments |
| `--border` | `#26282C` | `#E2E2DF` | Hairline borders (1px) |
| `--text` | `#E6E7E9` | `#1A1B1E` | Primary text |
| `--text-2` | `#9A9DA3` | `#5C5F66` | Secondary text, descriptions |
| `--text-3` | `#5C5F66` | `#9A9DA3` | Muted: placeholders, disabled, out-of-month days |

### Accents (identical across themes, tuned for both)

Muted, desaturated pastels — never saturated/neon. Each has a `-tint`
(translucent background for active segments, badges, banners).

| Token | Value | Tint | Meaning |
|---|---|---|---|
| `--steel` | `#7FA7C9` | 13% alpha | Brand (wordmark dot), Full Brain mode, links, focus ring, info |
| `--green` | `#8FBC9A` | 13% alpha | Entry mode / "saved", success, healthy |
| `--amber` | `#C9A36A` | 13% alpha | Research mode (read-only), pending/in-progress, warnings |
| `--rose` | `#CF8A8F` | 13% alpha | Medical domain, errors, destructive |
| `--violet` | `#A493C9` | 13% alpha | Financial domain |
| `--location` | `#6FB6B1` | 13% alpha | Location domain (teal) — map trail/fence/start, location tool-views |

Semantic aliases: `--ok: var(--green)`, `--warn: var(--amber)`,
`--danger: var(--rose)`, `--info: var(--steel)`, `--location: #6FB6B1`. The
location domain's color is **teal `--location` (`#6FB6B1`)** — settled by the
L3 location-assistant GUI gate (owner chose Option B + the teal location accent;
see `docs/mocks/location-views/README.md`). It is distinct from the five mode
accents and shares the teal hue with the MedicalProcedure entity disc (a
type-axis use, not a domain one — the two axes are independent). The inline
location tool-views (`location_map`/`place_card`) and their Leaflet trail/fence/
start markers ride this token (the steel `loc-lf-*` classes on the full-screen
map are unchanged unless separately re-decided).

**Mode/domain coding rule** (settled in the Phase 1 omnibox review):
green=entry/save, amber=research/read-only, steel=full-brain/agent,
rose=medical, violet=financial. A surface's active segment, status dot,
send button, and section markers all take its mode color — you can *see*
which mode and firewall you're inside.

### Entity-type accents

A separate axis from domain color: the entity-type icon disc is tinted by the
entity's *type*, while domain still rides its own dot on the same row. The five
accents above are reused where a type maps naturally; five muted tones plus a
neutral slate fill the rest, all in the same desaturated register so the disc
never out-shouts the chrome. `Entity.kind` is free text — anything outside this
set normalizes to **Thing**.

| Type | Token | Value | Type | Token | Value |
|---|---|---|---|---|---|
| Person | `--steel` | `#7FA7C9` | Animal | `--sage` | `#A8BD7E` |
| Organization | `--violet` | `#A493C9` | CreativeWork | `--rose` | `#CF8A8F` |
| Place | `--green` | `#8FBC9A` | MedicalCondition | `--terracotta` | `#D0917F` |
| Event | `--amber` | `#C9A36A` | MedicalProcedure | `--teal` | `#6FB6B1` |
| Product | `--periwinkle` | `#8F9FD0` | Drug | `--orchid` | `#C98AB4` |
| | | | Thing | `--slate` | `#9AA0A8` |

The disc is `color-mix(in srgb, <accent> 16%, transparent)` background with the
accent as the glyph color — one tint formula, no per-type `-tint` tokens.

## Typography

- System font stack: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- Scale: 12 (micro/labels) · 14 (secondary) · 16 (body, inputs) · 18 (card
  titles) · 22 (screen titles) · 28 (wordmark/hero). Line-height 1.4.
- Weights: 400 body, 500 titles/buttons, 700 wordmark only.
- Section headers (e.g. KNOWLEDGE, AUTHORING): 12px, uppercase, letter-spacing
  0.08em, `--text-3`.
- The wordmark is `JBrain` + a `--accent` period: **JBrain.**

## Spacing & shape

- 4px base unit. Common steps: 4 / 8 / 12 / 16 / 24 / 32.
- Screen gutter 16px; card padding 16px; grid gap 12px.
- Radii: 16px cards/composer/tiles, 12px inputs/segments, 999px pills/dots.
- Borders: 1px `--border` on every raised surface; no drop shadows in dark,
  optional `0 1px 2px rgba(0,0,0,.06)` in light.
- Touch targets ≥ 44×44px; compact-variant rows may reduce to 36px height but
  never shrink tap areas below 44px including padding.

## Core components

**Top bar** — wordmark (or back chevron + screen title) left; right cluster: the
**vitals chart** (below). Height 56px. The chart is a readout that is also the *only*
control in the cluster: it opens the vitals detail card. Nothing else there is
tappable, and the chart never became a launcher — the launcher stays on the omnibox
swipe-up.

**Status banner** — full-width strip under the top bar for connectivity/sync
problems: `--rose` text on rose-tint background, e.g. *"Browser online, but
JBrain server unreachable — retrying…"*. Auto-dismisses on recovery. Never
use modals for connectivity.

**Status dot** — 8px circle: green=healthy, amber=degraded/retrying,
rose=error, `--text-3`=unknown. Used in the composer footer. It no longer
appears in the top bar — see the vitals chart, which carries sync as its
baseline rule.

**Vitals chart** (settled in a two-round GUI gate — chosen **E "instrument"** over
D "typographic" and F "stateful block"; binding mock
`docs/mocks/topbar-vitals/e-instrument.html`, rivals `{d-typographic,f-stateful-chip}.html`).
The top bar's right cluster is a **12-second strip chart** of the box's vitals — the
newest twelve seconds of the same client-side ring the vitals detail plots
(`hostVitals.ts`), re-read once a second. It deliberately keeps no buffer of its own: a
private strip resampled the published reading on a second clock, so the seconds a
reconnect backfilled into the ring stayed holes in the strip while the detail graph
filled — the "empty bars after coming back to the PWA" symptom.

- **GPU busy %** is a row of hairline columns that ticks left each second. Columns
  fade with age so the eye reads direction of travel, and go `--warn` past 85%.
  When the box exposes no amdgpu gauge, a **dashed mid-line** stands in — a flat row
  of zero-height columns would misread as an idle GPU.
- **Tokens/sec** is a `--steel` trace over the same axis, drawn only while a turn
  streams and **broken by a gap** wherever nothing was generated (a tool call). The
  two channels share a box but not a y-scale, so each is comparable against itself
  over time and never against the other.
- **Sync is the chart's baseline rule**, and **a healthy connection is silent**
  [decided]: synced draws a neutral `--text-3` rule with no word at all. Only
  `pending` / `unreachable` colour the rule (`--warn` / `--danger`) and show the
  word beneath it. This is what replaced the top bar's 8px status dot. Note the
  order of that decision: the word is dropped *and* the colour with it, because a
  green rule on its own would make colour the only encoding of "healthy" — the one
  thing the pairing rule forbids. Silence, not a green light, is the healthy state;
  the aria-label still reports it. A stalled stream — unreachable server, suspended
  app — **freezes and dims** the trace rather than draining it: the strip's window is
  anchored at the ring's newest sample, because advancing the axis with nothing
  arriving would draw blanks that read as "the box went idle" when they mean "we
  stopped being told". The em-dash readout and the sync word carry the staleness.
- Digits sit in fixed tabular slots and the t/s row keeps its space when idle, so
  no state change can nudge the ellipsizing session title.
- **A reading nobody has is an em dash, not a blank** [decided]. Three states, three
  renderings: a figure when the box answered, `no gpu` when it answered that it has no
  amdgpu gauge, and `—` when the stream is merely down. Blank was the original answer for
  that third case, and it is indistinguishable from an idle box: when the stream died the
  chart drew no columns and the readout showed nothing, so a dead meter and a quiet one
  looked identical. The meter also **reopens a fatally-closed stream itself**: EventSource
  retries a dropped connection but not a fatal one (a 502 while the box redeploys), and
  leaving that stream in place blanked the top bar for the rest of the session while the
  detail screen — reading the server's ring over plain fetches — carried on showing
  numbers. **A silent stream is also treated as dead** [decided]: the route sends a frame
  every second whether or not the reading moved, precisely so silence is diagnostic, and a
  stream can stop delivering while the socket stays OPEN — a proxy buffering
  `text/event-stream`, a half-open connection after a network change. Neither raises an
  error event, so the fatal-close recovery never fires and the meter just sits blank. Six
  missed frames replaces the stream. The server also asks proxies not to buffer
  (`Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`); the watchdog is the
  backstop for one that ignores the ask.
- **The meter reports on itself** [decided]. The stream's own health — frames seen, opens,
  errors, reopens, `readyState`, time since the last frame — is on the detail surface
  behind a collapsed row, and beaconed to the box so it can be read through the debug
  token. It is there because the failure is invisible from the server: the top bar sat
  blank while the box served 97% quite happily, and *frames not sent*, *sent but not
  arriving*, and *arriving but dropped* need different fixes yet look identical from the
  box — a connection the browser declines to open leaves no trace there at all. Collapsed,
  because it is a diagnostic and not part of the reading.
- **Every retry clears its own handle** [decided]. The pending-retry handle is not just a
  timer — the reopen path reads it as *an attempt is already scheduled, don't add
  another*. So a callback that fired without clearing it did not merely leak a handle: it
  left the slot occupied forever, and from that moment the silence watchdog could tear a
  dead stream down but never bring one back. The meter stayed blind for the rest of the
  session with no reconnects reaching the box at all. One failed probe was enough to arm
  it, and a probe fails on every deploy, when the route is briefly gone. All retries now
  go through one helper that nulls the handle before running the attempt.
- **The chart and the GPU figure share one grid row** [decided]. They are the pair
  the eye reads together, and laying them out as two independently-centred columns
  put the chart's middle 11px above the figure's — the reserved t/s slot pushed the
  figure down while the sync word's line box pushed the chart up. Row sharing holds
  the two on one line at any `--font-scale`; the sync word gets its own row beneath,
  dropped from flow when healthy so the cluster doesn't hang high in an empty bar.

The honest signal this buys: **streaming fast while the GPU stays cold means a
cloud model answered, not the box.** The first round (A/B/C) folded the launcher
into the readout as a tap target; the owner ruled that out — the bolt is gone
entirely and nothing in the cluster is tappable.

**Vitals detail** (settled in a three-way GUI gate — chosen **I "drill-down"** over
G "instrument panel" and H "dossier"; binding mock
`docs/mocks/vitals-detail/i-drilldown.html`). Tapping the top bar's vitals chart opens
a **full-screen card** — the paradigm table's answer for a graph plus a drillable list
plus expandable detail — with **two levels**:

- **One gauge, one sampler, every surface** [decided]. Inside the API there is exactly
  ONE reader of `gpu_busy_percent`: `vitals_ring.sample_loop`, once a second, on a thread
  with a timeout. The probe, the SSE stream and the roster all answer from its newest
  sample rather than taking their own read. Before this there were three readers — the
  supervisor's (feeding the Ops tile every 30s), the API's inline read per stream frame,
  and the ring's — so two surfaces both labelled "GPU" could legitimately disagree, which
  is exactly what the owner kept seeing. Two consequences follow: a stalled read can no
  longer block a request path (it is off the loop, and there is one of it rather than one
  per client per second), and `latest()` returns None once its sample ages past a few
  ticks, so a dead sampler reads as *no reading* instead of a confident frozen number. The
  supervisor keeps its own copy on purpose — separate deployable, no shared package — but
  it feeds only the 30-day history, never a live figure shown next to a live one.
- **The whole surface runs on ONE clock, at 1 Hz** [decided]. The graph ticked at 1 Hz
  while the roster refetched every 3s and an open turn every 2s, so three things
  describing the same instant disagreed on screen — a turn's load appeared in the plot
  before its row appeared in the list. A page that claims to be live at 1 Hz is live at
  1 Hz throughout, streamed or polled. The polls chain from completion rather than sitting
  on an interval: at 1 Hz a read slower than the period would stack requests, and the
  moment the box is busiest is exactly when this screen is being watched. Bounded by being
  owner-only, open-only and foreground-only.
- **Level 1** is the graph at **1 / 5 / 15 minutes** plus a roster of the turns in that
  **same window**, children indented under the turn that spawned them, split into
  *Running now* and *Finished, last N*. The range drives **both halves** [decided]: the
  roster first shipped as "runs in flight" only, which emptied within seconds of a turn
  settling and left fifteen minutes of GPU history sitting above a list with nothing in
  it. A settled row shows its **total duration** rather than a ticking elapsed time, and
  a hollow dot rather than a filled one — on a 15-minute window most rows are finished,
  and a screen of solid dots reads as "all of this is running right now".
- **Under load, every list is capped at four rows** (settled in a second three-way GUI
  gate — chosen **J "capped lists"** over K "collapsing graph header" and L "segmented
  lists"; binding mock `docs/mocks/vitals-scrolling/j-capped-lists.html`) [decided]. At
  the 15-minute range a working box posts thirty model loads above a roster nobody can
  reach, so each section — the box's own work, *Running now*, *Finished, last N* — shows
  four rows and offers the rest on a **"Show all N" footer row** inside the list, with a
  **"Show fewer"** to put it back. The cap counts ROWS but never splits a parent from the
  fan it spawned, and one fan always survives however wide: half a research fan reads as a
  fan that lost children, not as a list that was trimmed. The **ordering is what makes the
  cap safe** — a load still in flight and a running turn sort first, so the cap can only
  ever take from the settled tail. Section headings are **sticky**, since an expanded list
  is the only place on the app where thirty rows can pass under one label. K was rejected
  for leaving the scroll as long as it found it, L for putting the box's work and the turn
  that provoked it behind different tabs.
- **The screen's sections may not shrink** [decided]. `.vitals-detail` is a column flex
  container of a definite height, so a section is a flex item — and a flex item whose own
  overflow is not `visible` has an automatic minimum size of ZERO. Both list cards clip
  their corners with `overflow: hidden`, so the screen answered a busy box by squashing
  ten events into one and a half rows and a *Running now 1* card into 20px, and did not
  scroll at all. Every section is pinned at its natural height (`flex-shrink: 0`), and
  anything added here must be a real child of the scroller — a `display: contents` wrapper
  hands its children up to the flex container and puts them out of that rule's reach.
- **Level 2** is a pushed layer that is entirely one turn: what it is doing now, its
  children, **the call** (model, provider, reasoning effort, context window, tools,
  persona), **the run** (id, parent, started, trigger, session, domain, ran-as), the
  triggering message, the **step trail**, and the **raw output** — last, so it can run
  as long as it likes. It climbs back with the chevron, the down-swipe and the platform
  back gesture, like every other stacked layer.
- **Raw output has two sources, and says which** [decided]. A parent `/chat` turn still
  in flight has an in-process render accumulator — the only way to see a turn
  mid-answer — and its output is badged *streaming*. Everything else (a sub-agent, which
  never registers a live handle, or any settled turn) reads its stored transcript and
  says so. Collapsing the two would let a finished answer masquerade as one still
  arriving.
- **The graph opens with a past** [decided]: GPU load is sampled once a second into an
  in-process ring server-side (`backend/src/jbrain/vitals_ring.py`) and the browser
  seeds its shared ring from it — on every stream (re)open (`hostVitals.ts`, sized to
  the hole the dead stream left, so a resume backfills the time away for the top bar
  and this screen alike), and on a slow re-seed poll while this screen is open. A
  reload no longer starts the plot empty. Deliberately not
  `app.host_metrics`, which samples every 30s for a 30-day graph — a one-minute window
  read from it is two points, and writing a row a second to serve a fifteen-minute view
  would multiply that table thirtyfold for a question that stops mattering a quarter of
  an hour later. The **token rate stays session-local**: it is measured in the browser
  off the chat stream, so its trace still begins empty after a reload.
- **One sample per wall-clock second, stamped on the box** [decided]. The trace showed
  occasional one-second dropouts — a gap in a line where nothing had actually failed to
  record. Nothing was dropping frames; the second was being *aliased away*. The plot gives
  each whole second one slot, and three separate places each stretched the cadence past a
  second: the sampler and the stream loop both slept a full second *after* doing their
  work (so the period was a second plus the work, slipping a whole tick every minute or
  two), and the browser bucketed each reading by the instant its frame ARRIVED, where a
  few tens of milliseconds of network jitter is enough to put two readings in one slot and
  none in the next. All three are fixed at their source: both loops now sleep to the next
  whole second rather than for a whole second, each sample is stamped with the second it
  is a reading **of**, and a frame carries every sample since the last frame sent, so
  neither loop's phase can drop one. The browser plots those stamps, reconciled to its own
  clock by an offset estimated from the frames themselves (the minimum `arrival − stamp`,
  re-based only on a step larger than a wobble). A gap on the trace is therefore a second
  the box genuinely has no reading for — which is the whole point of drawing gaps rather
  than zeroes. The screen also re-reads the box's ring on a slow timer, because the
  seconds a *reconnect* costs are seconds the box recorded and the browser did not.
- **The box narrates what it is doing, not just how loaded it is** [decided]. Between the
  graph and the roster sits *On the box, last N* — model loads, the evictions that made
  room for them, image renders — because the surface's commonest reading was a trace
  pinned at 94% above an empty roster, and the heaviest work this box does is not a turn.
  Three properties make it an explanation rather than a log. It is **written when the work
  starts**, so a load in flight reads "loading gpt-oss-120b…" *during* the spike instead
  of being accounted for a minute after it passed. It carries **why**, supplied by whoever
  knew — "to make room for gpt-oss-120b", "an image render needs the whole memory pool",
  "you unloaded it" — which is the difference between two unexplained events and one
  comprehensible swap. And it is **cross-process** (`app.box_events`, not an in-process
  ring like the samples): the worker loads models of its own for deferred transcription
  and ingest, and those are precisely the loads nothing on screen asked for, so a row
  the api narrated is badged only when it wasn't — a *background* chip on the worker's.
  The card is hidden entirely when there is nothing to report; a permanently empty card
  teaches the eye to skip the place the answer appears. A row left open by a process that
  died mid-load ages out as *stale* rather than claiming to still be loading. A **running**
  load also carries **how far in it is** — "loading gpt-oss-120b… 43%", measured as the
  **device-memory delta** against the catalog's projected footprint. The elapsed count
  beside a row answers *how long has this been going*; only the fraction answers *how much
  longer*, which on a load that reads tens of GB is the question actually being asked. No
  figure before the first sample lands, or on a box with no device probe (never a `0%`,
  which reads as stuck), and none on a settled row — "loaded gpt-oss-120b 100%" would put a
  progress figure on a row whose point is that it is over.
- **A load says so in the chat too, on the line above the composer** [decided]. The same
  reading, deliberately duplicated onto the conversation surface's status line
  (`AgentStatusLine`): *Loading **gpt-oss-120b**… 43% · 12s*. A cold 120B takes the better
  part of a minute to read in, and for every second of it that line said "Thinking it
  through" with a climbing timer — the agent looking hung during the heaviest work the box
  does. It **outranks** both the turn's own phase and the plan's between-steps countdown,
  because until the weights are resident neither of those can move. It shows with **no turn
  running** as well: a load the owner started from Settings, or one the worker started, is
  why the next thing they type will sit there, and saying so before they type it is the
  point. Its clock is the **load's**, from the box's own record, not the phase it replaced.
- **The fraction is measured, not narrated — and it covers the whole wait** [decided]. A
  load span is **two phases with two different sources**, because MEASURED on the box one
  cold gpt-oss-120b span ran 198.7 s and split 80 s reading the weights, then 118 s in the
  priming warm-up. The second is 60% of the wait the owner sits through.

  Phase one, the weights read, is measured by the **page-cache sweep's own accounting**
  (`llm.local_gateway._sweep_page_cache_during_load`): every GiB that appears in `Cached`
  is a GiB that came off disk, so the running total survives the drops that keep taking it
  back out. Phase two, the warm-up, is measured by its **prefill** against `/slots` — the
  same mechanism a real turn's prefill uses, so the two cannot drift apart. The fraction is
  mapped into 0–40% and 40–100% by their measured shares, so the bar keeps advancing across
  the phase boundary rather than restarting.

  It is **not** the load watchdog's device-memory samples, which is what shipped first and
  is what the earlier version of this entry claimed. Device memory is the **reservation**:
  llama.cpp commits the whole GTT buffer before it reads a byte (measured — 57 GB committed
  up front), so that reading was already at 0.78 four seconds into a 198 s span, then flat
  at 0.99 for the last two minutes. It was not wrong about the load — it was accurately
  describing a load that had finished while a phase nothing measured ran on. The watchdog is
  a watchdog again: a question about the booking, which is exactly right for catching a
  runaway and wrong for "how far in is this".

  Before that it was a parse of llama-swap's log buffer, which had **no source on this
  build** at all: the model loader prints nothing at the default verbosity, so every
  consumer had only ever received `null`. Three attempts, each shipped without being checked
  against a real load; the pattern that ended it was measuring on the box first.

  It is written where it is read from, so the vitals list, the chat line, and the code-mode
  loading bar cannot report different percentages. The denominator is the catalog's
  projection, which drifts (measured 69.26 GiB against a predicted 68.55 on that load), so
  the fraction is clamped — a model that outgrows its estimate reads as arrived rather than
  as 118%, and one the catalog over-predicts sits short of full, which is honest: the rest
  is not known.
- **The wait after the load says so too** [decided]. Once the weights are resident a long
  prompt still spends tens of seconds in **prefill** before the model can say a word, and
  that silence is the same failure the loading line was built to end, one step later in the
  turn. `llm.prefill` reads it from llama-server's `/slots`, on a schema **measured on the
  box** rather than guessed:

  - `n_prompt_tokens_processed` is the numerator — exact, monotonic, advancing one batch
    (2048) at a time.
  - `n_prompt_tokens` is **not** the total. While a slot is busy it reads
    `processed + batch + cache`, a window trailing the work by one batch; the true total
    appears only once the slot settles. Used as the denominator it reads 0.75 where the real
    answer is 0.50.
  - `next_token[0].n_decoded` separates "still eating the prompt" from "answering", which
    `is_processing` alone cannot.

  So the numerator is exact and **the denominator is not in the body**. It comes from us:
  the prompt's own character count over a chars-per-token ratio that every completed turn
  corrects from its real `usage.prompt_tokens`. Approximate on purpose and clamped, and
  cheaper than the exact alternative — llama-server's `/tokenize` would cost a second full
  send of the prompt per slow turn to remove an error a handful of turns removes anyway.
  Nothing is drawn for a turn that answers inside three seconds, which is what keeps a
  prompt-cache hit — measured returning instantly on an identical repeat — from ever
  flashing a bar.

  On screen it is worded as *Reading **your prompt*** rather than by model name: while the
  weights are still arriving the model's name answers "why is nothing happening", and once
  they have arrived it explains nothing.
- **It rides the stream that is already open** [decided]. The load is a field on the 1 Hz
  vitals frame (`/ops/vitals/stream`), not a poll or a socket of its own. That stream is
  already open on every screen, already foreground-gated, already access-probed, and
  already ticking at exactly the cadence a load indicator wants — so the chat line costs no
  new connection and cannot disagree with the trace beside it about what second it is.
  The answer is read **once per second for the box**, not once per client per surface: a
  screen with several readers open would otherwise make a database round trip each, every
  second, during the one minute the box has nothing to spare.
  Every path that gives up on the gauge — a fatal close, a silent socket, backgrounding —
  drops the load with it, so a line can never sit there naming a load that finished four
  minutes ago.
- **The detail plot is the shared Ops sparkline** [decided], not a private drawing:
  `components/TimeSeriesPlot.tsx`, the same component the Ops screen renders every host
  metric with. It began as hand-rolled bar columns, which meant the box's load was drawn
  one way here and another way three taps along. Each channel gets its own panel, since
  they share no unit: **GPU busy pinned to 0–100%**, the **token rate fitted to its own
  window**. Pinning the percentage is the point of the `scale` option added to that
  component — a bounded unit fitted to its own range draws an idle box hovering at 1–3%
  as the same dramatic mountain range as one that was pinned all minute. The peak/low
  axis still reports the data's true range, so nothing is hidden by the pinning.

Three things this surface is careful about:

- **Full resolution, on a grid anchored to absolute seconds** [decided]. One point per
  second, every reading the ring holds — no bucketing at any range. Both earlier schemes
  are superseded: peak-only columns, then mean-plus-peak-band. Bucketing threw away
  resolution already in hand (900 samples squeezed into 60 columns), and because the
  bucket edges were derived from `Date.now()` on each tick, the whole partition slid a
  fraction of a bucket every second and samples visibly hopped between columns — the line
  reshaped itself once a second while the data behind it sat still. Anchoring to whole
  seconds makes a sample's slot a property of the sample, so the window scrolls by exactly
  one slot per tick instead of re-partitioning. With every sample drawn, mean-vs-peak
  stops being a question.
- **A value at the floor still draws a line** [decided]. The plot reserves a stroke's
  width inside its own box at both ends, because a series sitting exactly at its scale
  minimum — a token rate of zero, the commonest case — landed on the viewBox edge with
  half its stroke clipped away, and a real zero became indistinguishable from no data.
  That is the one distinction these plots exist to keep: a **gap** means "we were not
  told", a **floor** means "it was zero". Nulls still draw nothing at all.
- **The GPU area is shaded, the token rate is not** [decided]. Shading reads as "how much
  of the available capacity was used", which is only true when the baseline is a real
  zero — so it is opt-in (`PlotLine.fill`) and paired with the pinned 0–100 scale. The
  token rate is auto-scaled to its own window, where the floor is the window's minimum
  rather than zero, and shading to that floor would imply a quantity that isn't there.
- **The verbatim prompt is shown, from memory only** [decided]. It is captured as each
  model call goes out and held in a process-lifetime ring
  (`backend/src/jbrain/agent/prompt_capture.py`), never written to a table. An assembled
  agent prompt is a verbatim copy of everything the turn retrieved — including whatever
  crossed the health, finance and location domains — so persisting it would duplicate
  data whose protection is row-level security into a new artifact every backup then
  carries, to answer a question about what the box is doing *now*. A restart empties it
  and the screen says the prompt was not recorded. It is collapsed by default (the
  largest thing on the surface) and states its own clipping when a prompt is too long
  to render whole.
- **A busy GPU with an empty roster is a real state**, not a bug: GPU busy covers the
  whole box, image generation and model loads included. The empty state explains that
  in words instead of looking broken.

**Making the chart a control** [decided]: the drawing is untouched at 48×20, and only
the hit area grows to clear the 44px minimum. It also stops being an `<output>` when
tappable — a live region that is also a control would announce the whole reading on
every 1 Hz tick — so it becomes a button with a stable name.

**Segmented control** — pill row on `--surface-2`; inactive segments
transparent with `--text-2` label + icon; active segment gets the
*context-appropriate* accent tint background with accent icon and `--text`
label (Entry=accent, Research=amber, Medical=rose, Financial=green).

**Card / tile** — `--surface`, 16px radius, hairline border. Hub tiles:
3-column grid, outline icon top, 16px/500 title, 12px `--text-2` description.

**Composer** — the signature surface: card with mode segments on top, large
16px placeholder body, footer row with status dot + context microcopy left
(*"Files to notes/medical/ · PDFs staged."*) and paperclip + send icons right.

**Buttons** — primary: accent-tint background, accent text (no solid fills);
secondary: `--surface-2` + border; destructive: rose-tint + rose text;
ghost: text-only. All 12px radius, 44px min height. Destructive actions get
an inline confirm (button morphs to "Tap again to confirm") — `window.confirm`
is a Phase-0 placeholder to be replaced.

**Inputs** — `--surface-2` fill, hairline border, 12px radius, 16px text;
focus = 2px `--accent` ring (`:focus-visible` only). Selects match.

**Lists** — full-bleed rows inside cards, 1px `--border` separators, 44px+
rows; leading icon optional, trailing chevron for navigation.

**Badges** — 12px text on the relevant tint, pill radius (e.g. `running`,
`healthy` in the Ops screen).

**Meters** — 6px pill-radius track on `--surface-2`; fill is `--ok`, turning
`--warn` above 80% and `--danger` above 92%. Always paired with a text
value — the bar is a glance aid, never the only encoding.

**Status-card grids** — glanceable per-item status (Ops containers and
similar) uses **half-width cards** in a 2-column grid, not full-width rows
(settled in Phase 1 polish); names/images truncate with ellipsis rather
than wrapping.

**Ops Host settings card** — the one card that **opens itself**. Every other
Ops card is collapsed by default and expands on tap; this one is `defaultOpen`
when any checked setting fails, and collapses to a one-word summary ("all
good") when they all hold. The asymmetry is deliberate and is the reason the
card exists: `ttm.pages_limit` sat misconfigured for weeks and nothing said so,
so a panel the owner has to think to open would not have helped. A failing row
earns the space to state what breaks and what to do; a passing row is one quiet
line behind the fold. Remedies that need a shell are prefixed **"Needs host
access:"** rather than being styled the same as the ones an Update fixes — the
owner has no terminal (CLAUDE.md #10), so "press the button" and "plan a
reboot" must be distinguishable at a glance.

**Ops Data card** (settled in a three-way review — inline card won over a
backup-vault list and a guided transfer sheet): a "Data" section with two
inline buttons. **Export backup** runs a supervisor one-shot that bundles
the database dump + blob files + manifest into one `.jbrain.tar`, then the
browser downloads it. **Import backup…** picks a file, shows
`name · size`, and arms a rose tap-again confirm that names the
consequence ("current data is overwritten"); a safety backup is taken
first, the stack restarts mid-import (the card tolerates the api being
unreachable, like Server update), and success offers **Reload app**.
**Reset DB** (right of Import, danger-styled) is a testing convenience
with the same double-press confirm — tap arms "Tap again — erases ALL
notes and data" (3s auto-disarm) — that takes a safety backup first, then
truncates all content data (notes, attachments, chunks, jobs, the entity
graph, facts, review items, analyses) and empties the blob volume while
auth/identity, domains, and llm_usage telemetry survive; the worker
restarts and success offers **Reload app**.
Progress is phased text + the one-shot's log tail, matching the update
card — no fake progress bars. **The Data flows now live on their own
card-launcher screen, not Ops** — see "Data screen" below; the behavior
(one-shots, tap-again confirms, reload-on-done) is unchanged.

**Data screen** (settled in a three-way review — reference mock
`docs/mocks/data-screen/data-c-segmented-tasks.html`; rivals A "action
list" and B "status dashboard"). The export / import / reset flows, lifted
off the Ops screen in the B3 redesign, get their own **card-launcher
destination** (a Data tile under SYSTEM; `DataScreen`). Chosen **C —
segmented tasks**: a **Backup · Restore · Reset** segmented control shows
**one focused task at a time**, the active segment taking that task's accent
(backup = steel, restore = amber, reset = rose, via the shared `.seg-on`
`--mode`/`--mode-tint`). Each panel is a guided surface — a task lead
(icon + one-line intent), then either an at-a-glance **summary** (Backup
shows live db size / notes·files / blob footprint from ops metrics; Reset
lists what it erases vs. keeps) or **numbered steps** (Restore), then a
single primary action. The destructive paths keep their settled
confirmations: Restore arms a rose "Tap again — current data is
overwritten"; Reset arms "Tap again — erases ALL notes and data" (3s
auto-disarm). C won for putting the dangerous actions behind a deliberate
tab rather than one long scroll of buttons (A) and for not leaning on a
"last backup" freshness signal the backend doesn't track (B).

**Ops screen — collapsible System + role groups (settled in a B-variant
review; reference mock `docs/mocks/ops-redesign/ops-redesign-b3-system-open.html`,
rivals A "status board" and C "health triage").** The flat full-width
container list didn't scale past a handful of services, so the screen is now
a stack of **collapsible cards** built on one shared disclosure shell
(`OpsCard`: a header button with caret + `aria-expanded`, body mounted only
when open — so a collapsed group never fetches its logs):

- **System card** (the one section **expanded by default**): the four vitals
  — Memory, Disk, Database, Load — as labeled rows; collapsed, its header
  shows a one-line recap (`mem 55% · disk 14% · load 0.55 · up 5h 40m`).
  **Server update lives on the Load row** (owner request — it's a system
  concern, not a footer afterthought): a steel info bar with the
  tap-again-to-confirm button, expanding to the running/done log exactly as
  the old card.
- **Service groups** — services are **grouped by role** (Core / AI / Infra,
  frontend-only mapping; anything unrecognized falls into a trailing Other),
  each a **collapsed** card whose header carries a count and a **roll-up
  state** (green "all up" / amber "degraded" / rose "down", worst-wins over
  its members). A service **row** shows a level dot, name, state/health
  badges, image·since meta, and memory; tapping it expands **its own log
  tail** — the per-service viewer (Follow toggle = the SSE stream, scoped to
  that one service) plus a one-tap **Copy logs** (writes the tail to the
  clipboard; button reads "Copied" for 2s) and a Restart.
- **AI usage moved off Ops to the LLM Settings screen** (`AiUsageCard`,
  `frontend/src/screens/aiUsage.tsx`): token spend belongs with the model
  config that drives it. It is a self-fetching collapsible drawer in the
  same register as the Local-models drawer — collapsed, its header shows the
  month's `in · out · ~$` recap; expanded, the today/month totals and the
  per-task breakdown. Telemetry still fails quietly (a missing/malformed
  payload reads "no usage data yet", never an exception).

Everything starts collapsed except System. Status colors stay paired with
text (the dot's level is also the badge). Rejected rivals: A's one-screen
tile board (status legible but logs/detail cramped) and C's health-triage
bar + per-service sheet (the filter+sheet added navigation for a list that
groups solve inline).

**Runs — filtering (settled in a three-way mock review; chosen **B —
multi-select show/hide chips + filter sheet** over A "kind segmented lanes" and
C "collapsible groups, agent pinned"; reference mocks
`docs/mocks/runs-filter/{a-segmented,b-chips-sheet,c-grouped}.html`).** The run
log is dominated by the scheduler's ~0-token housekeeping (`reconcile_*`,
`geofence_sweep`), which buries the runs that carry signal — agent turns and
integrations — and the surface shipped with no filter. B won for doing exactly
what was asked ("show/hide pipeline and other runs", agent turns one tap away)
with the most expressive control and the least new paradigm:

- A **multi-select chip row** (reusing the settled `.filter-chip` pattern), one
  chip per kind — **Agent · Integration · Pipeline** — each carrying a live
  count and its kind tint (steel / green / violet). A `subagent` run rides the
  Agent chip. Tapping a chip strikes it out and hides that kind; **any
  combination is legal** (additive, not one-lane-at-a-time). Agent is always a
  first-class chip, so a turn is never buried.
- A **filter button** (sliders glyph, with a non-default-count badge) opens the
  shared `Sheet` for the lower-frequency controls: a **date range** (`.seg-row`
  Today / 7d / 30d / All), a **result limit** (25 / 50 / All), and a one-tap
  **Hide reconcile sweeps** switch that drops the seeded housekeeping
  (`reconcile_*` / `geofence_sweep` / `purge_deleted_artifacts`) without hiding
  real pipelines like `nightly_predicate_sweep`.
- **Filtering is server-side and opt-in.** `GET /api/runs` takes `kinds` /
  `exclude_sweeps` / `since` / `limit`, so picking a kind fetches *that kind from
  the whole history*, not just whatever survived the reconcile noise in the recent
  page — the first cut shipped client-side over the fetched 50, but the sweeps fire
  every few minutes and saturate that window, so agent turns never reached the app
  to be filtered (the reported failure). The **status tiles + chip counts come from
  a separate `GET /api/runs/stats`** aggregate over the whole log — so the tiles
  stay honest while the list is filtered, and "tokens today" reflects the day
  rather than the last 50 rows. The default shows everything (the surface reads as
  before until a control is touched); a `N of M runs · <range> · <kinds> hidden ·
  sweeps hidden` count line and a **reset** link report what's applied; an emptied
  list names the fix rather than reading as "no runs".

Rejected rivals: A's exclusive segmented lanes (can't see agent + integration
together without re-mixing the noise) and C's structural grouping (loses the
strictly-chronological cross-kind default).

**Cause, not just symptom — idle sweeps are reaped [shipped].** The reconcile /
geofence sweeps fire every few minutes; a fire that reconciles *nothing* used to
still write a 0-work run, and that flood is what saturated the window in the first
place. The worker now **reaps an idle sweep's run right after the job completes**:
the four housekeeping handlers (`REAPABLE_IDLE_SWEEPS` in `workflow/scheduler.py`)
return their work count, and `runlog.reap_idle_run` deletes the run when a fire did
zero work (guarded to a lone-step, 0-token, done run, so a real pipeline is never
touched; needs the `app.runs` DELETE grant, migration `runs_reap_delete_grant`).
So no-op reconcile fires no longer appear in the log at all, while a fire that
actually picked up work still shows. Server-side filtering (above) is the display
control; reaping is the upstream cleanup — together the log reads as real activity.

**Calendar** — Day/Week/Month/List segments; month grid with hairline cell
borders, out-of-month days in `--text-3`, today = accent ring around the day
number; selected-day panel below with `+ Add` (accent link) and `Open day →`.

**Home stream** (settled in the Phase 2 home review): home is NOT an
infinite timeline — it shows the **last 2 days** of notes with an
"older notes live in Search" pill above. The stream area is
**mode-scoped**: Entry shows the note stream; Research / Full Brain show
that mode's **conversation cards** (title, last-message preview, time,
mode dot) — tapping one descends the tree into the conversation layer;
typing in those modes always starts a NEW conversation. With no
conversations yet, the mode shows a one-line empty state.
**Swiping a note bubble left** slides it to reveal an
**icon action rail — Delete · Edit · Hide** (settled in the entry-mode swipe
review; **Move domain** was dropped from the rail to the note-view ⋯ menu and
**Hide** added — three 64px buttons, RAIL_WIDTH 192, each an outline icon over
a lowercase label). **Edit** opens the full-screen **focused-writer** editor (— settled in the
Phase 2 edit review against two rival designs: chrome fades to a whisper
context line (domain dot · date) with a quiet ✕; the note is the screen at
`--fs-editor` (20px-scale) with 1.7 line-height and a 38em measure, steel
caret/selection; the thumb bar holds live `words · chars` (+ amber
`· unsaved`) and a 44px **done** button — surface-2 until savable, then
green-tint per the green=save rule — riding above the keyboard; dirty ✕
arms an inline rose "discard edits?" that auto-disarms in 3s or on typing;
saving PATCHes the body and re-triggers ingestion; the editor also owns
**attachment management** — a paperclip in the thumb bar adds files, chips
above the bar list them with a tap-again rose remove; adds/removals apply
immediately to the note, independent of the text's done/cancel). **Delete**
uses an inline tap-again confirm (the button arms to a filled-rose state).
**Hide** removes the note from the home stream **without deleting it** — a
persisted per-note `hidden_at` flag (not a local view filter), so it survives
reload and syncs across devices; the note's chunks are untouched, so it stays
in Search and openable from there. Hiding offers a single **undo** toast
(green=save rule does not apply — undo links steel); there is **no persistent
hidden tray** and **no swipe-right gesture**. Hide/unhide are dedicated
endpoints (`POST /notes/{id}/hide|unhide`), never a PATCH, so visibility
toggles never re-ingest. Tapping a bubble opens the note sheet.

**Capture location** (settled in the Phase 2 review): a Settings toggle,
**on by default** (browser permission prompt on first launch; denial just
means location-less notes). While on, the app keeps a warm geolocation fix
and attaches lat/lng/accuracy to a note at send **only if the fix is under
2 minutes old** — capture is never blocked or delayed waiting for GPS.
Note-location is owner-eyes metadata: Phase 7 scoped tokens never receive
location fields, regardless of the note's domain.

**Image analysis** (Settings): a segmented control, **ocr only | full
analysis**, default **full**. Full = verbatim transcription plus a salient
description (objects, people, context, relationships visible — the text the
fact pipeline mines); ocr only skips the description call. This is the
**first server-synced setting** (GET/PUT `/api/settings` over
`app.settings`, owner-only RLS) because the worker reads it per job — theme
and text size deliberately stay device-local for now. Either way, capture
never waits: vision runs after sync.

**Note view** (settled in the Phase 2 review; Attachments tab settled in a
later three-way review — **manifest** won over gallery and inline-viewer
designs): entry-stream bubbles clamp at **3 lines**; tapping opens the
**note view layer** (slide-up tree level, swipe-down back) with a
**Note / Attachments / Analysis tab split**:

- *Note tab*: full markdown body only. No attachment chrome (files live
  in their own tab) and no action buttons — note actions live in a
  **⋯ menu right-aligned on the domain/date line** (same affordance as
  the attachment rows' ⋯; kept out of the top bar, which stays
  navigation-only) opening the shared bottom sheet with **edit**
  (amber-tint), **move domain**, and **delete** (rose, tap-again confirm
  "tap again — deletes this note"); the ⋯ hides for not-yet-synced
  outbox notes.
- *Attachments tab* — the **canonical attachment manager** (the editor
  keeps its quick paperclip for capture-time adds). The tab label carries a
  count pill. Layout is a **manifest**: a one-line summary
  (`N files · total size · how many searchable / indexing / awaiting ocr`),
  then one bordered card of rows — type icon, filename,
  `size · media type` meta line, and a **pipeline status chip** derived
  client-side (`indexing…` amber while the note's ingest is pending,
  `text extracted` green-tint for text/PDF, `ocr queued…` amber for an
  image whose vision cache is empty, `text extracted (ocr)` for an
  OCR-only image, `text + description` once full analysis also cached a
  description). Each row ends in a 44px `⋯` that opens the shared
  bottom sheet with **open** (new tab) and **remove** — remove uses the
  tap-again confirm and spells out the consequence ("removes file + its
  extracted text"). The card's last row is a steel **add files** row
  (multi-select) with the hint "pdfs and images become searchable";
  adds/removals apply immediately and re-trigger ingestion.

  **Image extracts moved out** (settled twice: first a three-way review
  chose inline expansion in the manifest [mock C]; then the Sources-card
  review [decided: **variant B** of three mockups] relocated viewing +
  the analyze re-run to the **Analysis tab's Sources card**): Attachments
  is a **pure manifest** again. The status chips stay; rows are **inert**
  — no caret, no tap expansion, no pdf-hint line; the per-file ⋯ sheet
  (open / remove) is untouched.
- *Analysis tab* (lights up by phase): generated title + 3-6 tags (P3 —
  pre-P3 the header shows only domain + date, **no title fallback**);
  salient facts with kind badges (measurement/state/event/preference),
  status chips (active / pending-review / **pinned**) and confidence;
  entity chips → entity pages; wiki backlinks → articles (P6). At the
  bottom, the **Sources card** (settled review — variant B, "sources
  provenance card") frames analysis as a pipeline:
  - A **note-text row** (char count, always ✓), then **one row per image
    attachment** with a per-stage status line (`ocr ✓ · description ✓`;
    amber spinners for in-flight stages, `queued` while a stage waits on
    OCR, `skipped` when the mode is ocr only).
  - Image rows carry a disclosure caret and **unfold in place** — a small
    thumbnail strip with `open full image →`, the verbatim OCR in a quiet
    monospace inset (clamped ~6 lines, "show all N lines" grows in place,
    `[illegible]` rendered muted-italic and never reworded), the
    description beneath when present with tool/confidence micro-meta and
    the "mined for facts in analysis" provenance line
    (`ocr · xai:grok-4.3 · 70%`). A row lacking a description in ocr mode
    reads *"no description — image analysis is set to ocr only."*
    Extracts are fetched eagerly when the card mounts (the stage line
    needs them up front).
  - Each image row's **⋯ opens the shared bottom sheet** with **re-run
    image analysis** — an on-demand full analysis for THAT attachment
    regardless of the global mode; in flight the row reads a calm
    *"analyzing image…"* and the fresh result polls in without reopening
    the note.
  - The **card footer unifies provenance with the note-level re-run**:
    the "analyzed Jun 11 · xai:grok-4.3" line (the former provenance
    foot — it has exactly one home, this card) next to a steel **re-run
    analysis** button (`POST /notes/{id}/analyze`, 202; a 409 means a
    run is already in flight and reads the same). After posting, the tab
    polls the analysis (~3s, cleared on unmount/tab switch) until
    analyzed_at moves, then swaps the fresh result in.
  - **Gated empty state**: the backend gates analysis on image extracts,
    so when analyzed_at is null and ≥1 image still lacks extracts the
    facts area is absent with the quiet line *"waiting on image analysis
    — facts extract once every source below is in."*, the Sources card
    renders mid-flight (per-stage spinners/pending), and the footer's
    re-run is disabled (*"analysis waits here — runs automatically when
    every source is in."*). Plain not-analyzed (no images outstanding)
    keeps the existing quiet line. With no images at all, an analyzed
    note's card collapses to the note-text row + footer.

  Gating makes the lifecycle-chip sequence **truly one-way** — indexing…
  → reading image(s)… → analyzing… → quiet. `analyzed` suppresses the
  chip ahead of the awaiting-images check (the backend's analyze-anyway
  paths can leave an image without extracts forever), and a note-level
  re-run flips analyzed back to false — the chip resumes at "analyzing…"
  without re-indexing.

Search results and stream taps open the same surface — this *is* the
former "note sheet", upgraded.

**Analysis tab + entity pages** (settled in the Phase 3 three-way review —
**graph-forward** won over a dense dossier and soft cards): the analysis
tab renders facts as **literal property-graph edges grouped by subject
node** (`me.blood_pressure → 128/82 mmHg`,
`appt:patel-follow-up.scheduled_time → Sep 2026 ±`), predicate paths in
monospace; subject headers double as entity navigation. Tapping a fact
cites back to the **highlighted source words**. The **entity page is a
hub**: centered node with kind/alias/domain meta, current facts as
outbound edges, inbound edges from other entities, provisional state
marked. **The page is current-only [decided: declutter]**: each property
shows its live value (a `pending_review` value stays on the page — it needs
the owner), and prior **once-true superseded** values collapse behind a
quiet `N earlier →` disclosure that opens that property's **revision
timeline rail** (each dot a supersession link citing its note) in the
shared `Sheet`. Muting stale values inline only dimmed them while keeping
their full footprint, so a multi-revision entity clogged; the rail is the
same settled paradigm, just relocated off the default view. **Retracted**
facts (machine extraction errors — never true) are excluded from the value
view entirely (audit-only, a later opt-in surface), never shown beside
once-true history. Correction is never a direct edit —
facts route to **review / pin** with tap-again confirms; the pipeline owns
the data. Temper the raw notation toward the lowercase-calm voice during
implementation (the chosen mock's `~provisional`/`.96` chrome reads too
developer-facing — keep paths, soften the meta). The launcher's **Entities
tile** opens a browse list of the graph — the search screen's live filter
input plus kind chips over standard list rows in a card, each row opening
the entity page — pure reuse of settled paradigms, so it shipped without a
variant review.

**The graph "Map" — Focus + Sheet, 2-hop local view** (settled in a
mobile-first review against five rivals; reference mocks under
`docs/mocks/entity-graph/` — desktop renders A "constellation", B "orbits",
C "clusters", then the mobile-first trio D "focus + sheet", E "orbit deck",
F "cluster drill"; **owner chose D, 2-hop**, `graph-d-focus-sheet-2hop.html`).
The earlier force-directed overview/focus map was a desktop port — it leaned on
hover, wheel-zoom, and sub-44px chrome, and a whole-graph hairball is unreadable
on a phone (the convergent finding of the UX research pass). The Map is now a
**local view centred on one focal entity**, never the whole graph at once:

- **Deterministic 2-hop layout** — focal at the centre, 1-hop neighbours on an
  inner ring, 2-hop clustered just outside each parent (capped: 8 first-ring,
  4 second per parent). No force simulation, so nodes never overlap tap targets
  and the layout never fights gestures. A floating **1 / 2 hops** toggle
  (bottom-left, one-thumb) sets depth; default is 2.
- **Tap is the only affordance** (no hover): tapping a node or a relationship
  row **re-centres** on it, pushing a **breadcrumb** that walks the path back.
  Every node carries a ≥44px target (the disc *is* the button).
- **A persistent bottom panel** (not the modal `<Sheet>` — it co-occupies the
  screen, Google-Maps style; drag the handle to resize) shows the focal's
  type-disc, name, kind · domain · link count (firewalled domains flagged rose),
  and its relationships as fat tappable rows. The single footer action is
  **Open entity →**.
- **Search is the front door** — a top input filters the in-memory graph; a
  result drops you into that entity's local view.
- One dataset backs it: the whole graph (centred on "Me") by default, or a
  named root's 2-hop ego; re-centring explores within what's loaded. Pinch /
  drag pan and focal-anchored zoom stay as a bonus, never required (the layout
  fits the stage). Edge labels show only on the focal's own edges, and the
  density-aware label grid keeps the rest legible.

**Former / past relationships — the interval timeline** (settled in a three-way
review — **variant C** won over an inline "former" chip [A] and a current/
previously section split [B]; reference mock:
`docs/mocks/legacy-links-c-interval-timeline.html`). A relationship/state is
**current** only when it is `active` **and** open (`valid_to IS NULL`); a closed
interval (`valid_to` set) is **former**, even when nothing replaced it (the
two-axis model — `docs/archive/research/legacy-links-handling.md` §3.1). A former edge
stays **visible on the default view** (it is not superseded history to hide
behind the `N earlier →` rail), rendered with a compact **validity track** under
the edge: a `--green` open span to **now** for the current value, a faded/dashed
`--slate` span for a former one — and **bounds the note never gave stay vague**
(an undated "used to" reads `former`/`ended ≤ <capture>` at era precision, never
an invented date). Tapping the row opens that property's **revision rail** in the
shared `Sheet` — the same settled history paradigm — where each dot cites its
note (so source citation lives in the rail, not a separate inline expansion).
Concurrent former values with no stated order are **co-equal** (neither
supersedes the other); the rail lists them without implying a sequence. A closed
relationship has **no derived inverse** (so a former `worksFor → X` never shows
`X employs Me`).

**Review inbox** (resettled in review — the **split inbox** won over the
original one-at-a-time triage: you couldn't move between items, and a
proposal that was only *reject*-able was a dead end): a segmented filter
**pending · deferred · decided** with live count pills splits the screen
into three lanes, and the list is **browsable** — every item in a lane is
listed (kind badge, domain dot, one-line summary, confidence badge,
when), not metered out one card at a time. A **select** toggle turns rows
into checkboxes with a contextual bulk bar (**defer all · approve all**),
and a one-tap **"approve N high-confidence"** suggestion clears the easy
volume; bulk actions resolve through one batch call and raise the same
undo. Tapping a row **pushes a detail** view (back to inbox + **N of M** +
**prev/next** chevrons, so you move between items without returning to the
list). The detail leads with the proposal: a **before→after value diff**
for collisions/conflicts (struck `current` over green `from this note`),
a **proposed-fact panel** (the `predicate → value` edge it would write,
rendered exactly as the entity page) for **every fact-bearing card** — a
low-confidence inference hold, and (beside the before→after diff) a fact
conflict or attribute collision — so it's clear what the decision records, and
that fact is **editable in place** (*correct in place*,
docs/mocks/review-inference-c-correct-in-place.html): the predicate via a
weighted picker (the canonicals nearest the proposed relation, plus free
entry), the value as a free-text chip→input or a member picker for a **typed
(closed-enum) predicate** like `gender → {male, female, unknown}` whose members
ride on the card payload, and the modality (the assertion stance). Deciding
unchanged records the pick (an inference's *approve*, a conflict's chosen
side); an edit flips the primary to *approve correction*, which files a
correction note (the #7 channel — the wiki stays machine-written) instead of
the footer's *correct it* detour (dropped for every editable fact card,
replaced by the inline edit). Or a what-happens panel for the rest;
then a one-line rationale, a
confidence badge, the **cited evidence** snippet (provenance), and the
**proposals to choose among** as stacked buttons (destructive ones —
splits, `distinct_from` — keep the armed tap-again). Two universal escape
hatches sit in the footer — **defer** (park for later) and **talk it
over** (hand to the assistant) — so *reject is never the only way out*:
the ambiguous-mention case that used to advertise only reject now always
offers defer and talk-it-over beside it. Every decision raises an **undo
snackbar** (undo is the server's own unwind — clean for a parked item, a
reopened tombstone for a real decision). Item kinds unchanged: fact
conflicts, attribute collisions, merge proposals, ambiguous mentions,
domain promotions, low-confidence extractions, splits.

**Deferred & decided lanes** (**reopen = full unwind** [decided]): the
**deferred** lane lists parked items (a *defer* or a *talk-it-over*, the
latter tagged **with assistant**); its detail offers **resume**, a clean
re-queue to pending with no tombstone — parking is not a decision. The
**decided** lane is the reverse-chronological log: each row carries **what
was decided in plain language** (the chosen option's own copy), dismissed
rows muted. Its detail shows the cited evidence, the **proposals that were
offered with the chosen one marked**, and an amber **reopen** (armed
tap-again) whose consequence text **names the unwind** per kind. Reopening
returns the item to pending (count pills update) and reverses the
resolution's recorded graph effects; the decided row stays behind as a
**struck-through "reopened" tombstone**. The one permanent exception is a
rejected merge: the `distinct_from` edge survives by doctrine. Empty lanes
read as one calm `--text-2` sentence each.

*Edit model:* "approve with edits" has two shapes, neither of which writes
the graph by hand (honoring non-negotiable #7 — facts aren't edited
directly). **Choose-among-proposals** picks among the values the pipeline
already proposed. **Correct in place** edits the proposed fact's predicate,
value, or modality directly on the card — available on **every fact-bearing
card** (inference holds, fact conflicts, attribute collisions), since each now
carries its structured proposed fact in the payload; an edit files the same
**correction note** rather than a verbatim pick, so the inline editor *is* the
correction channel for these kinds (their footer *correct it* is dropped).
For the kinds that carry no editable fact (merges, ambiguous mentions, domain
moves, …), **correct it** opens a composer that files the human's
fix as a real **correction note** (the #7 channel) in the item's domain and
resolves the item as *corrected*; the pipeline applies it when it processes
that note (**re-adjudicate**, never a hand-written fact), so the wiki stays
machine-written and the value lands once extraction runs — reopening keeps
the note (it's the human's own). The note is filed with
`provenance=owner_correction` (`POST /api/review/{id}/correction`, owner-gated
like the wiki path), so its facts *force-supersede + pin* what they correct;
filing it as a plain `human` note instead let a same-value correction of a
prose-valued attribute read as a fresh conflict and spawn another collision
card — the correction spawned reviews rather than resolving one. The planned third mode, **talk it over
with the assistant**, is the conversational version of the same — the
assistant drafts that correction-note body from your intent; until that
handoff is wired the footer affordance parks the item for the assistant.
Whether a human may ever pin a typed value directly, short-circuiting the
pipeline, stays the open #7 decision.

*Detail composition: the block registry [decided].* The review detail is
**assembled from a sequence of typed, reusable blocks**, not a per-kind
conditional screen — so a new review kind is "declare a block sequence", not
"add a branch". The vocabulary is `header`, `claim:{inference,diff,notice,contradiction}`,
`trace`, `action`, `evidence`, plus a lane-driven `footer` appended to every
detail. A `kind → block-sequence` table (`frontend/src/review/blocks/registry`)
declares each kind's blocks in a canonical order (e.g. a collision is
`header · trace · claim:diff · action · evidence`; an inference is
`header · claim:inference · trace · action · evidence`; a wiki_contradiction is
`header · claim:contradiction · action · evidence`, the source-grounded
side-by-side that makes a `wiki_lint` clash decidable in place — the raw source
is the hero, each paired record's facts hang beneath it, chosen via the
three-mock GUI gate, `docs/mocks/review-wiki-contradiction-{a-ledger,b-source,c-verdict}.html`);
listed blocks
**self-gate** — they render nothing when their payload data is absent — so a
sequence can be generous and reads as the kind's intent. The polymorphic
`action` block carries the per-lane fork (pending controls / decided record /
deferred park) and the per-kind controls (collision choices, inference
approve-reject, new_predicate map/keep/rename); the inference's edit state is
hoisted to the detail so `claim:inference` (the editable proposed-fact panel)
and `action` (the approve button that flips to *approve correction* on an
edit) share it. **The block-to-kind mapping is frontend-only** [decided]: it is
derived from `kind` + payload-field presence, leaving the backend display
contract (`display.py`, which emits card fields, not layout) and its tests
untouched — layout iterates without a wire migration. A future kind that needs
an ordering `kind` can't express may add an optional `payload.blocks` the
frontend prefers; until then the table is the single source. (Rejected:
backend-declared block sequences — couples the Python display contract to a
React layout vocabulary for no present gain.)

**Search** (settled in the Phase 2 review; input mode revised on-device):
**live as-you-type** — results update per keystroke behind a 250ms
debounce, stale responses sequence-guarded, previous results stay visible
while the next query is in flight; enter / the Search button forces an
immediate run. **Passage-first results** — the matched chunk is the hero text with
`--amber-tint` highlight marks, the source note is a one-line context row
beneath; domain-colored dot + date in the head; every result carries its
**match badge** (`semantic` steel-tint / `keyword` surface-2) — retrieval
transparency is a feature, not debug chrome. Domain filter chips under the
search bar. Degraded mode shows the amber "keyword-only results — semantic
search recovering…" banner (never an error page). Tapping a result opens
the **note sheet** — a minimal full-note view (body, attachments, metadata)
as a slide-up layer; swipe down returns to results. The omnibox Research /
Full Brain modes drive agent conversations; passage search lives behind the
Search tile.

**Empty states** — one `--text-2` sentence with the action inline: *"Nothing
scheduled — tap to add."* No illustrations.

**Toasts** — bottom-anchored above the nav, `--surface-2`, auto-dismiss 4s,
single action max.

## Motion

Fast and physical: 120–180ms ease-out for state changes; segment/theme
changes crossfade ≤150ms; no springs, no parallax. Honor
`prefers-reduced-motion: reduce` by disabling all non-essential animation.

## Iconography

One outline set (Lucide), 1.5px stroke, 20px in controls / 24px in tiles and
nav. No filled icons except the status dot. No emoji in UI chrome.

**Entity-type icons** — a cohesive Lucide-style set, one glyph per canonical
entity type (Person, Organization, Place, Event, Product, Animal, CreativeWork,
MedicalCondition, MedicalProcedure, Drug, Thing). Rendered in a round disc
tinted by the type's accent (see "Entity-type accents") on entity rows and the
entity hub header. The glyph carries the *type*; the row's dot still carries the
*domain*.

## Voice & microcopy

Terse, factual, lowercase-calm with em-dashes; say what the system is doing
and what it won't do: *"Ask anything about your notes — I only read; I won't
change anything."* Errors state the situation + the recovery: *"Server
unreachable — retrying…"*. Never blame the user; never exclamation marks.

## Accessibility

- Text contrast ≥ 4.5:1 against its surface in both themes (the muted accents
  are for chrome/tints; body text is always `--text`/`--text-2`).
- Visible focus rings on `:focus-visible`; full keyboard operability on
  desktop layouts.
- Status conveyed by dot color is always paired with text.
- Respect safe-area insets (`env(safe-area-inset-*)`) in top bar and bottom nav.

## UI development process

Binding workflow for every new screen or significant UI change:

1. **Mock-first, approval-gated.** UIs are built and reviewed against mock
   data before any backend wiring. The frontend ships a mock mode
   (`npm run dev:mock`) where the typed API client is backed by fixtures —
   realistic, varied data including empty, long, error, and offline states.
   Backend endpoints are implemented only after the owner approves the
   mocked UI.
2. **Options before commitment.** New surfaces are presented as **3–4
   distinct variants** (layout, interaction pattern, or visual treatment —
   not color-swaps of one idea). The owner picks; the *reasoning and chosen
   pattern* are added to this document in the same PR, so the next surface
   reuses the decision instead of re-litigating it.
   **No reuse exemption [decided]:** every NEW screen or surface gets an
   interactive mockup round before implementation, even when it composes
   entirely from established paradigms — "it's just a list" is not a
   waiver. Paradigm reuse shapes the variants; it does not skip the review.
   Small in-place changes to an existing surface (a chip state, a button on
   an existing card) remain exempt.
3. **Decisions accrete here.** If a review settles anything reusable — a
   list pattern, a modal flow, an empty-state style — it gets a subsection
   in this doc immediately. This document is the memory; "we decided this
   already" must be checkable by reading it.

## The omnibox home (approved Phase 1 review — reference mock: `docs/mocks/phase1-omnibox-approved.html`)

The home screen is a **bottom-docked omnibox** with a day-grouped transcript
stream above it (newest at the bottom). Capture is message-send: instant
local append with an amber "pending sync" chip until the outbox clears.

- **Modes**: one segmented row carries Entry / Research / Full Brain.
  **Tapping Entry while it is active morphs the other two slots into the
  entry sub-types (Medical / Financial); tapping it again morphs back.**
  The row is a full-width bordered rect with hairline dividers; the active
  segment takes its mode tint, colored icon, and bold label.
- **Fixed box height across all modes** (~300px). Medical/Financial show a
  destination row inside the box — mode icon, path (`notes/medical/`),
  destination select, `+ New` — and the text area absorbs the difference.
- **Footer**: mode-colored dot + mode microcopy left ("Saved to your wiki ·
  no AI." / "Read-only — nothing gets written." / "Files to notes/medical/ ·
  PDFs staged."); right icons are paperclip + send (Research swaps the
  paperclip for the bolt). Send button tint follows the mode.
- **Type sizes**: composer body/placeholder 17px (the 22px draft read too
  big), segments 15px/500, footer 14px, destination row 15px.
- Research / Full Brain sends hand off to the (Phase 4) conversation
  surface; in Phase 1 they explain themselves via toast.
- **Conversation-surface foot** (added post-Phase-1): a live context-window
  meter fills the foot's left, with the action icons hard right. When the open
  conversation has a **per-conversation model pick** (below), a small mode-tinted
  model chip sits beside the meter as a reminder the turn isn't on the default route.
- **Per-conversation model pick** (long-press a conversation tab): long-pressing
  (or right-clicking) the **Research** or **Full Brain** tab opens a bottom sheet
  listing the on-box models **currently loaded**, plus an **Automatic** row that
  clears back to the default route. When the default route (or a listed model)
  reasons, the sheet adds a **Reasoning** radio pill row (None / Low / Medium / High,
  styled like the chat picker's Today/Older/Archived segments) — no separate "Auto":
  the route's effective default level carries a small **"(default)"** marker under its
  label and reads selected while no override is set; tapping it clears the override so
  the route's own effort keeps applying. The reasoning level is an **independent**
  per-conversation override — **not** bundled into the model pick: tapping a level
  persists it immediately (without closing) whether or not a model is pinned, so the
  owner can dial reasoning on **Automatic** with no model change, and the backend
  applies it only when the turn's resolved model is reasoning-capable. The foot's model
  chip shows whichever picks are set, joined by `·` — a model (`GPT-OSS 120B`), a level
  on the default route (`high`), or both (`GPT-OSS 120B · high`). Both picks are scoped
  to **that conversation only** — they ride every turn of that chat and are kept in
  memory (a reload reverts to Automatic); they never touch the global task routing in
  Settings. Only conversation tabs arm the gesture; capture tabs keep their native
  tap/right-click.
- **Read-aloud (per turn)**: when the owner has enabled read-aloud (the
  `brain_read_aloud` setting), each settled answer carries a **three-state** play
  control just left of its copy button. Tapping it speaks that one turn and the glyph
  flips to **pause**; tapping pause (or playing another turn, or leaving the surface)
  stops it. A **long-press** arms **auto-play** (a violet loop-marked triangle — the
  third state): every new turn then speaks itself *as it streams in*, fed
  sentence-by-sentence so it starts talking without waiting for the whole answer;
  long-press again to disarm. Auto-play is a device-local, persisted preference. The
  **engine** is chosen in **Settings → Read-aloud voice** (`brain_read_aloud_engine`):
  **Kokoro** renders each sentence on the box in the chosen voice (`brain_answer_voice`,
  streamed back over the api's `/api/brain/tts` proxy and played back-to-back) and
  falls back to the device's native voice when the box is unreachable, has no Kokoro weights,
  or a clip fails to render (the failure is logged on the box, since a silent fall back reads
  as the wrong voice); **Native** uses
  the device's own Web Speech voice. In Kokoro mode the same card offers the voice picker
  — any installed `kokoro-<voice>` — a *play sample* button, and a *read custom text* button;
  that voice also reads the wall display's answers. **Read custom text** opens a full-screen
  surface that is mostly a text area (paste a note or a book chapter, or **Upload .md** to drop
  a `.md`/`.txt` file's contents into the area to review and edit) with an **Upload .md**,
  **Play/Stop**, and **Export audio** foot — Play renders the text on the box clip-by-clip and
  plays it gaplessly in the chosen voice; Export renders the whole thing and downloads it as a
  single WAV. The text is normalized on Kokoro's profile, so custom text reads exactly as a chat
  answer in that voice does. On-box (Kokoro) only, since it needs the box to render capturable
  audio; a back button is its explicit exit. With read-aloud on, the copy button drops its "Copy" label
  to just the icon so the pair fits on the foot line.

## Navigation: the card launcher (no bottom nav)

There is **no bottom tab bar**. Navigation is a full-screen **card
launcher** (the v1 knowledge-hub tile grid: 3-column tiles under uppercase
section headers — KNOWLEDGE, AUTHORING, SYSTEM):

- Opened by **swiping up on the omnibox** (settled when the top bar's right
  cluster became the vitals chart — the bolt icon that used to open it is gone).
  The chart itself is tappable, but it opens the vitals detail card, never the
  launcher: one gesture, one destination.
- Slides up over the home screen; dismissed by the **explicit ✕ button or
  tapping the handle row** (primary paths — gestures proved unreliable on
  real devices and are an enhancement only), swipe down, or Escape. It is a
  navigation surface, not a modal — no scrim-tap dismissal needed, it owns
  the whole screen.
- Every overlay surface must have a visible, tappable exit; a gesture is
  never the only way out (settled in Phase 1 polish).
- **Navigation is a tree, and swiping down climbs it** (settled in Phase 1
  polish): card screen → (swipe down at scroll-top) → launcher → (swipe
  down) → home. Swipe up on the omnibox descends into the launcher. The
  down-swipe on scrollable screens arms only at scroll-top so it never
  fights content scrolling; the top-bar chevron still jumps straight home.
- **Levels are stacked slide-up layers**: card screens animate exactly like
  the launcher — rising from the bottom over the still-open launcher,
  sinking back down to reveal it (150ms ease-out, disabled under reduced
  motion). Each card carries its own top bar (chevron + title); the chevron
  jumps home and the down-swipe climbs one level.
- **The platform back gesture climbs one level too** (`useBackGesture`), exactly
  like swipe-down — closing the topmost layer in z-order (sheets, then reading
  layers, then card/launcher, then the Full Brain Proposal/panel and the Tasks
  return-to-card). It **never exits the app**: the open-layer stack is mirrored into
  the History API with one real entry per layer plus a permanent "root trap" beneath
  them, so backing out of any layer is a native history pop that lands on another of
  our entries — never the app's base entry — and a back at the bare chat only reaches
  the root trap, which re-arms (exit is via the home button / app switcher). One entry
  per layer (not a lone trap) is what keeps a layer-back a full step above the base, so
  Android's gesture/predictive back can't race it into an exit. The bare home stream
  and the Full Brain conversation are both "the main screen" — back always reaches one
  of them before it stops.
- **Native host path.** Inside the owner Android app (a WebView; `OwnerActivity`), the
  system back button is a native callback, not the History API — so `useBackGesture`
  detects that host (a `JBrainOwner/` UA marker), skips the history trap entirely, and
  publishes `window.__jbrainBack()` (closes the top layer, returns whether it did). The
  activity calls it on back and **backgrounds** the app (`moveTaskToBack`) when nothing
  was open, so back is deterministic where the web trap can't be. Same layer logic, one
  source of truth — the native side only routes the button.
- Tiles for phases not yet built render disabled with their phase label.

### Full Brain lateral shortcuts (Sessions ← chat → Proposals)

In **Full Brain** mode (steel/agent) the conversation is the center of a
three-pane lateral model: **Sessions** to the left, **Proposals** to the right —
the mnemonic is temporal/actional (past sessions left, pending approvals right).
Both are first-class **card-launcher destinations** (tiles, under a SYSTEM/
ASSISTANT group) — that is their canonical, tappable home and the required visible
way in and out. The **Proposals** page is the unified review queue focused on the
agent's staged Proposal trees (see `docs/reference/ASSISTANT.md`); **Sessions** lists past
and active agent sessions with their selected read scope.

**Inline approval card (settled — binding mock `docs/mocks/inline-approvals/d-one-tree.html`,
chosen from a four-variant round `docs/mocks/inline-approvals/{a,b,c,d}.html`; build plan
`docs/archive/INLINE_APPROVALS_PLAN.md`).** A Proposal staged mid-conversation is acted on
**in the transcript**, not only from the side panel: the answer bubble renders the staged
Proposal as **one interactive card** (`agent/InlineProposal`, `.fb-inline-prop`) — a tree
of leaves each **approved (✓) / declined with a reason (✕) / corrected in place** (tap the
value to edit; the leaf turns `corrected` amber and files the owner's edit as a `human`
correction). A leaf whose prerequisite is declined reads **held** (amber, fail-closed).
**One Enact at the foot is a double-tap** (arm → "tap to enact N" green → run), which runs
the approved, unblocked leaves and **returns a single server-authored outcome to the
assistant** as a data-framed turn, so it follows up ("Enacted N — … · declined 1 (reason)").
This is the settled **double-press-to-confirm** paradigm and the green=save/steel=info
tokens; only `correction · knowledge · appointment · merge · egress` kinds render inline —
**`wiki-restructure` (a large multi-op tree) and `intake-link` (a bespoke mint editor)
keep the navigational chip** to the panel. The **side panel is now for browsing older /
cross-session proposals**, no longer the way to act on the one in front of you; the
`ProposalTree` panel view is unchanged.

As an **enhancement only** (never the sole path — the gesture rule above binds), a
**horizontal swipe on the omnibox / text-entry box** is a shortcut, following the
natural drawer convention — **the panel slides in from its own side to cover the
screen, in the direction your finger moves:**

- **Swipe right → Sessions** (the left panel shuttles in from the **left** edge to
  cover the screen).
- **Swipe left → Proposals** (the right panel shuttles in from the **right** edge).

Rules:

- **No edge chrome on the main screen** — there are no handles, tabs, or peek
  affordances flanking the composer. The conversation surface stays clean; the
  gesture is discovered, and the **card-launcher tiles** (under a SYSTEM/ASSISTANT
  group) are the canonical, always-visible tappable way to both pages.
- The gesture is anchored to the **composer**, not to transcript bubbles, so it
  never competes with message content; the recognizer favors the dominant axis, so
  it never fights the vertical nav-tree gestures (up → launcher, down → climb).
  Horizontal is available precisely because modes switch by *tap*, not swipe.
- Sessions and Proposals open as **standard full-screen cards** (own top bar + back
  chevron, which satisfies the required visible tappable exit; the down-swipe climbs
  too). The panel tracks the finger and snaps in past threshold; disabled under
  reduced motion.
- **Full-Brain-only:** Entry/Research composers do not carry these shortcuts (Entry
  keeps its transcript-item action rail).

Reference mocks: `docs/mocks/assistant-lateral-swipe.html` (the gesture, no edge
chrome), `docs/mocks/assistant-sessions-view.html` (the Sessions page + start-
session read-scope picker), `docs/mocks/assistant-proposals-view.html` (the
tree-structured Proposals page with whole/subtree/leaf approval and dependency
holds).

**Chats picker — segmented buckets + compact rows (settled in a three-way density
review; chosen **C — segmented micro rows** over A "expandable list" and B "swatch
tiles"; reference mocks `docs/mocks/session-picker/{a-expandable-list,b-swatch-tiles,
c-segmented-micro}.html`).** The tall chat cards (title + multi-line preview + a
footer chip row) didn't scale, so the picker is now:

- A **`Today · Older · Archived` segmented control** (the shared `.seg-row`/`.seg-on`,
  steel `--mode`) with a per-segment **count pill**, showing **one bucket at a time**
  so the list stays short. **Older** folds yesterday and everything before it (off
  `last_active_at`); **Archived** replaces the old "Show N archived" disclosure as its
  own segment. Until the owner taps a segment, the picker **follows the data** — it
  shows the first non-empty bucket, so it never lands on an empty Today while chats
  load into Older/Archived.
- **Micro rows (~44px)** packed into one bordered card with hairline separators: a
  **scope-tinted dot** (the domain color it reads — green when this is the open chat,
  its `reads <scope>` label on the dot's `title`), the **title** (ellipsized), then
  **turns / a staged badge**, and a trailing chevron. **The preview and the visible
  scope chip are dropped** for density — the dot carries scope at a glance.
- **New chat**, the segments, and the search field (shown once chats pass the
  `SEARCH_THRESHOLD`) pin in a non-scrolling header; only the row list scrolls. Search
  filters the rows and the count pills together.
- The **swipe-left rail** keeps the home-note paradigm but now carries **four**
  actions — **rename · scope · archive · delete** (`rail-4`, 48px each across the same
  `RAIL_WIDTH`). **Re-scope moved onto the rail** (its own sliders glyph) since the
  tappable scope chip left the row; rename still edits inline, delete still arms a
  tap-again confirm. C won for the most aggressive vertical density with the
  bucketing the owner asked for; A (tap-to-unfold preview) and B (two-line swatch
  tiles + filter chips) are retained as the record.

**Live-turn activity glyph on the row (in-place addition; chip-state exemption — owner
chose the *stateful mini-glyph* over a pulsing dot and a spinner ring; reference mock
`docs/mocks/session-active-turn-glyph.html`).** A chat with a turn streaming right now
replaces its leading scope dot with an **accent (`--steel`) activity glyph** so an
in-flight thinking/render is visible from the picker even while another chat is open
(the turn is detached from the SSE connection and keyed to its own session — see
`docs/reference/ASSISTANT.md`). The glyph is **stateful**: **three bouncing dots while thinking**
(any non-image activity — reasoning, tools, answering), a **twinkling spark while an
image tool renders** (`generate_image`/`edit_image`). The row's `turns / staged` meta is
replaced by a calm accent **`thinking…` / `rendering…`** word for the duration. The glyph
is **decorative** (`aria-hidden`) — the visible status word carries the state and rides
the row button's accessible name, so a screen reader hears it without a nagging live
region. Honors `prefers-reduced-motion` with a steady glyph. At most one chat shows it at
a time (a single turn is in flight — `busy` gates sends).

## The image launcher — standalone generate/edit screen (settled in a four-way mock review; chosen **B + gallery shortcut**, reference mock `docs/mocks/image-launcher/launcher-b-gallery.html`; rivals A "composer-dock studio", C "pinboard gallery", D "render console / darkroom" retained in `docs/mocks/image-launcher/README.md`)

A **card-launcher destination** for on-box image generation/editing that drives ComfyUI
**directly** — the headline property is that the **language models stay unloaded**. This is
distinct from today's only path (`generate_image`/`edit_image` as jerv tool calls, which need
the LLM resident); the screen is the "I just want a picture — don't wake the brain" path. Its
accent is **`--violet`** (image models ride violet on the residency ladder); it is **not** a
chat surface. The screen path and the jerv path coexist — the screen carries only a one-line,
unobtrusive "ask jerv in chat" note, never a chat affordance.

- **Segmented Generate | Edit form** (the settled Data-screen segmented-tasks paradigm): one
  focused task panel at a time, the active segment taking the violet image tint. A persistent
  **honest residency line** ("renders on-box · language models stay unloaded").
- **Configuration is explicit, in a collapsible card** with a one-line summary when collapsed:
  **speed** (`dreamshaper` · `fast` · `quality`, default quality), **aspect** (square / portrait
  / landscape / tall / wide), **resolution** (small / medium / large, default medium), **steps**
  (20–40, **visibly locked** with a "fixed N steps" hint when speed ≠ quality), **negative
  prompt**, **seed** (blank = random, the resolved seed recorded and shown). speed implies the
  model (no model-id picker on this surface). Edit inherits the source's aspect.
- **Edit** leads with a **source**: a dropzone (upload) **or** "pick from gallery"; plus up to
  **2 reference** slots (compositing/style). The result shows the edit's **before→after
  swipe-compare** (the same paradigm as the in-chat `generated_image` view,
  `docs/mocks/genimage-c-edit-aware.html`).
- **Render is synchronous and honest**: queued → rendering… (shimmer, no fake progress bar;
  reduced-motion shows steady phased text) → the sized result with its meta
  (`dimensions · model · seed`) and small actions (use as edit source, copy seed).
- **The gallery shortcut** — a grid icon in the top bar with a live count — opens a full-screen,
  scrollable **image-only pinboard** of every render (a 2-column masonry, kind badge per tile,
  newest first). New renders flow in at the top; tapping a tile opens a **large view** with its
  meta and **use as edit source**. An empty board is one `--text-2` sentence with the action
  inline. The board is the workshop's shelf, not a separate destination — creation stays on the
  form, never behind a modal.

**Build sequencing (binding UI process — mock-first):** the screen is implemented first against
the **mock API client** (fixtures) so the working mocked UI is owner-approved before any backend
wiring. The **direct, non-agent render endpoints are a follow-up wave** and are **escalation-
worthy** (a non-agent surface that drives ComfyUI renders) — owner-only RLS, security-100%, and
the shared render logic extracted from the jerv tool handlers so the two paths never diverge.
See `docs/archive/IMAGE_LAUNCHER_PLAN.md`.

## Surface paradigms (which container for which job)

| Job | Paradigm |
|---|---|
| Primary tasks (capture, reading an article, chat) | Full screen with top-bar back chevron |
| App-wide navigation | Card launcher (swipe up on omnibox) |
| Contextual quick forms & actions (add list item, edit appointment, filters) | **Bottom sheet** — the workhorse modal on phone |
| Confirmation of a destructive/irreversible act | Center **confirm dialog**, destructive variant |
| Row-level detail that doesn't warrant navigation | Inline expansion within the list |
| Outcome feedback (saved, restarted, queued) | Toast |
| Connectivity / sync state | Status banner + dot — **never** a modal |

## Modal system (one implementation, reused everywhere)

- A single shared **`<Sheet>`** (bottom sheet) and a single shared
  **`<Dialog>`** (center confirm) component own all modal behavior: scrim
  (`--bg` at 60% alpha), focus trap, body-scroll lock, Escape/back-gesture
  dismiss, swipe-down dismiss for sheets, safe-area padding, 16px top radius.
  New modals compose these shells — building a bespoke modal is a design-doc
  violation.
- **One modal at a time, never nested.** If a flow seems to need a modal
  over a modal, the first one should have been a full screen.
- Sheets carry a 32×4px drag handle, a 18px/500 title, and at most one
  primary action; longer flows are full screens.
- Dialogs are for confirmation only: one sentence of consequence, two
  buttons max (destructive variant on the right), no scrolling content.

## Agent tool views (registered components, never bespoke markup)

Agent tools render rich UI — lab plots, tables, timelines, appointment cards,
confirm sheets — but **only through a closed registry of first-party
components**, never by emitting HTML, scripts, or markdown URLs (that would be the
exfiltration channel `docs/reference/ASSISTANT.md` invariant I-9 forbids, and would let
model output drive the render). The contract:

- A tool result may carry a **`view`**: a schema-validated, **data-only** payload
  naming a registered component and filling its typed slots
  (`{ view:"lab_plot", series:[…], ref_fact_ids:[…] }`). The PWA looks the name up
  in a fixed component registry and renders the vetted React component; an
  unknown name renders nothing.
- A `surface` hint (`inline | sheet | dialog`) places it: inline in the chat
  transcript, or into the **shared `<Sheet>`/`<Dialog>`** shells. **This is not a
  bespoke modal** — the component is the *content*; the modal-system rules above
  still bind. Adding a component is a deliberate, versioned design+code change
  (extend this document in the same PR), exactly like adding a tool.
- View payloads are **data, not instruction** (I-1) and **render no external
  resources** (I-9); slots are escaped by the component. Data in a view came from
  an RLS-scoped tool call, so domain firewalls hold at the source; views carry
  `fact_id`/`entity_id` refs for citation hover-cards (pointers-not-copies).
- **Interactive views never mutate directly.** A button dispatches a tool call or
  stages a **Proposal** under the session's action policy — the agent proposes,
  the pipeline disposes.
- **One view names one component** (no nested trees/dashboards; multiple views in a
  turn render as sequential inline cards), and components express **`tone`/`flag`/
  `kind` enums, never colors or hex** — the model conveys meaning, the component owns
  the token mapping.

**The registry** (starter set; spec in `docs/archive/research/self-improving-agent/G-tool-
view-components.md`). Three composable primitives hold the count down —
`data_table`, `stat_block`, `citation_card` (the shared pointer-not-copy citation
surface every view reuses). **MVP:** those three + `lab_plot` + the interactives
`record_list`, `appointment_card`, `confirm_panel`. **Standard:** `entity_card`,
`timeline`, `wiki_preview`, `med_card`, `txn_table`. **Refused** (anti-bloat, tied
to invariants): no `form` (input flows through composer/sheets/review inbox), no
`markdown`/`html`/`image`/`iframe` (I-9), no external **map tile ever**, no free
`button`/`link`, no generic `chart` kitchen-sink (purpose-built plots only), no
dashboard/layout components.

**Proxy-tile carve-out for the location domain (L3).** The "no external map tile
ever" rule above was written when location had no on-box basemap; it is
**superseded for the location domain** by the registered Leaflet tool-views
`location_map` (#3) and `place_card` (#4). These are the *sanctioned* Leaflet
tool-views: they render tiles only from the **on-box `/api/tiles` proxy**
(`leafletMap.ts`), not an external host — so the exfiltration/I-9 concern that
motivated the ban (a model-authored URL reaching a third-party host) does not
apply. The invariants still bind: coordinates are **render-only** (lat/lon enter
only the Leaflet layers via the map glue, never model-facing text or a view
caption), a GPS gap is never bridged (the trail splits into separate polylines),
and derived `place_card` stats are owner-gated. The data still comes from an
RLS-scoped, full-owner-gated tool call. No other domain gets a tile view without
its own decision.

### `generated_image` tool-view (settled in a three-way GUI review — reference mock: `docs/mocks/genimage-c-edit-aware.html`)

The in-chat card jerv shows after a `generate_image` or `edit_image` call
(`docs/archive/IMAGE_GEN_PLAN.md`, Wave G3). A registered, data-only view like every
other: the model fills `{image_id, kind ('generate'|'edit'), prompt, width,
height, model}` and **authors no markup and no URL** — the component builds the
image source as `/api/images/generated/${image_id}` and sizes the frame from
`width`/`height` so the bubble reserves space (no layout shift while the blob
loads). Tokens-only `.tv-genimg-*` classes; the card frame matches the live
`.tool-view`.

Chosen **C — edit-aware before/after** (`docs/mocks/genimage-c-edit-aware.html`)
over A (result-only) and B (result + a collapsible prompt/seed/Regenerate
disclosure). A *generate* renders like A — just the sized image, a `kind` badge,
and a dimensions·model caption — but an **edit** renders the source→result link
as a draggable swipe-compare with a Before/After/Compare toggle, pulling the
"before" image from `/api/images/generated/${image_id}/source` (the owner-gated
edit-source route). C won because **`edit_image` is in scope**, so "what did the
edit do?" is the key question and the source→result provenance must be legible;
A/B render an edit no differently from a fresh generate. A and B are retained as
the record in `docs/mocks/genimage-README.md` (B/C both subsume A's generate-only
layout, so this choice still fixes the generate rendering). Owner-only (the table
mirrors `wiki_*` RLS); never a note, never RAG-indexed — a chat artifact.

#### Canvas renders — a deliberate palette deviation, inside the image

A canvas (`show_canvas`, `docs/plans/AGENT_CANVAS_PLAN.md`) reuses this same card: it
persists as a `generated_images` row stamped `provenance="canvas"`, so it renders through
the component above with the origin caption "drawn on the canvas" and no seed line, exactly
like a grabbed frame. **No new component, no new payload shape.**

What *is* new, and is recorded here because it deviates from the palette rules above: the
marks drawn INSIDE that image do not obey the UI contrast assumptions. The model still
authors meaning, never colour — canvas ops carry a `tone` enum (`auto | danger | warn | info
| ok | accent | neutral`) and hex is rejected at the op boundary, so I-1 holds. But the
renderer resolves those tones over an arbitrary photograph rather than over `--surface`, and
the muted accents (`--rose` and friends) are tuned for chrome on a flat ground: on a busy
photo a 3px `#CF8A8F` stroke and dark label text are both close to invisible.

So the annotation layer adds two affordances the UI itself must never need:

- a **dark outer stroke** under every accent stroke (halo), and
- a **`--surface`-tinted backing plate at ~85%** behind label text.

Both are **photo-only**. On a blank sheet they are actively harmful — a dark plate behind
dark text is worse contrast than no plate — so the renderer paints neither, and `tone: auto`
resolves to near-black instead of rose. A filled rect stays an ~18% tint, never opaque,
because the point of boxing a thing in a photo is to keep seeing it.

#### `render_html` renders — one frame, not two

`render_html` (`docs/plans/AGENT_CANVAS_PLAN.md` §3b) reuses the same card again: model-authored
HTML + CSS goes to the egress-free `htmlrender` sidecar and comes back as a `generated_images`
row stamped `provenance="html"`, captioned "rendered page", no seed line. **No new component, no
new payload shape** — and, as with the canvas, the model authors no markup that reaches the DOM.
The PWA receives pixels, so I-1 holds by construction rather than by validation.

Two rules exist because the app already frames what it is handed, and a render that carries its
own frame reads as a bug:

- **The page ground is a token, not a model choice.** The sidecar knows two grounds by NAME,
  `dark` and `light`, and they are exactly this sheet's `--surface` values. The card paints
  `--surface` behind the image, so a matching ground makes the render sit flush; a ground merely
  *close* to it draws a visible second rectangle. A colour string is never accepted for it.
- **The image is measured, not padded to a guess.** Height is omitted by default and the page is
  shot at its own content height, so a short card is short. A guessed height leaves a band of
  empty ground inside the frame, which is the same second-rectangle failure by another route. The
  gutter is the page's (`pad`, default 28) so the markup needs no padded outer `<div>` either.

The tool contract states the matching rule for the markup: no outer border, panel, radius or
shadow around the whole page. Inside that, the render is free — it is an image, and unlike the
canvas layer above it sits on a flat known ground, so it needs neither halo nor backing plate.

### `video_analysis` tool-view (settled in a GUI review — binding mock: `docs/mocks/analyze-video-approved.html`)

The in-chat card jerv shows after an `analyze_video` call (`docs/archive/VIDEO_ANALYSIS_PLAN.md`,
Wave 4). A registered, data-only view: the model fills `{attachment_id, source
('chat'|'note'), media:'video', filename, summary, duration_ms, frames:[{t_ms,
caption, thumb_id}], transcript:{text, words:[{text,start_ms,end_ms,confidence}]}|null}`
and **authors no markup and no URL** — the component builds the media source from the
id + source (`/api/chat-attachments/${id}` for jerv's tool, `/api/attachments/${id}`
for a note). One `<video>` drives **one shared clock** across a **filmstrip scrubber**
(the sampled frame thumbnails ARE the timeline — tap a frame to seek, the active frame
lifts and the strip auto-scrolls to it; a live "now" line under it shows the active
frame's caption) and two tabs — **Summary** and **Transcript** (the approved
`AudioTranscript` reader, reused verbatim: confidence-gradient words, steel-pill
karaoke, tap-to-seek). The Transcript tab is omitted when the clip has no speech (then
no tab bar, just the summary); the filmstrip is always shown when there are frames.
Tokens-only `.tv-vid-*` classes; the Transcript tab reuses `.atx-*`.

Chosen **D — combined** (filmstrip scrubber + tabs) over A (filmstrip), B (moment
feed), and C (tabs), with the owner's edits: **no Frames tab**, the Transcript tab
reuses the audio-transcript card, and — after owner review — **no Moments tab**: the
horizontal filmstrip is the only timeline (the vertical moment feed was redundant with
it + the now-line + the Transcript), so per-frame "said" snippets are dropped (the full
speech lives in Transcript).

**Thumbnails + the firewall.** The frame stills are real, but a frame JPEG is a
content-addressed blob with **no per-blob domain firewall**, so it is never served by
raw sha. Instead the `analyze_video` tool **caches its result on the chat-attachment
row** (`turn_attachments.analysis`, migration 0084 — which also makes a re-ask free),
and the thumbnail endpoint `GET /api/chat-attachments/${id}/thumb/${thumb_id}`
validates the requested `thumb_id` against THAT row's stored frame list **under the
attachment's domain scope** (`TurnAttachmentRepo.frame_thumb`, RLS): a sha that isn't
one of the attachment's analysed frames — or any frame of an out-of-scope attachment —
is a 404, so the firewall (invariant #3) holds and no URL rides the payload (#9). The
component builds each thumbnail src from `attachment_id` + `thumb_id` exactly as it
builds the media src. The same shape supports a future note-attachment card
(`source:'note'`) once a note thumbnail route validates against `attachment_extracts`.
Owner-facing chat artifact; never a note, never RAG-indexed.

### `weather_card` tool-view (settled in a four-way GUI review — reference mocks: `docs/mocks/weather-view/`)

The in-chat card jerv shows after a `weather` tool call — the glanceable replacement
for the old web-search-and-scrape-into-a-markdown-table weather flow. A registered,
data-only view like every other: the model fills
`{place, as_of, tz, range:('today'|'week'), alert:({event, tone:('warning'|'watch'|
'advisory'), headline, more}|null), now:{temp_f, feels_f, feels_hi_f, feels_lo_f,
cond, is_day, label, humidity, wind_mph, wind_dir}, hi_f, lo_f, hours:[{label, temp_f,
feels_f, cond, is_day, pop, wind_mph, wind_dir}], days:[{label, cond, hi_f, lo_f,
feels_hi_f, feels_lo_f, pop, wind_mph, wind_dir}]}` and **authors no markup, no URL, and
no color** — `cond` is a closed enum (`clear|partly|cloudy|rain|storm|snow|fog`) and
`is_day` a flag the component maps to an **inline SVG glyph + token** (the night variants
for clear/partly skies live in the component, not the payload). Tokens-only `.tv-wx-*`
classes; weather is non-personal jerv info, so the card rides the **steel** info accent
and a high heat index reads **amber** (the warn tone). Every `feels_*` value is the **NWS
heat index** computed on-box from temperature + humidity (the figure a Heat Advisory turns
on — the same Rothfusz math `weather_history` uses, shared via `jbrain.web.heatindex`),
never Open-Meteo's own apparent temperature, which read a few degrees under. `feels_f` is
the current heat index; `feels_hi_f`/`feels_lo_f` are the day's **peak/coldest** heat index
(reduced from the hourly series) — the heat-advisory number a raw high/low hides (at dawn
"feels 88°" reads mild on a day the afternoon heat index tops 110°). The component owns the heat-tone decision, not the model:
a current feels-like ≥ 100° reads amber, and when the day's peak feels-like clears that
warn line **above** where it feels now the hero surfaces a **"Feels like up to N° today"**
callout (the week list flags each heat day's peak instead). The optional **`alert`** slot
is the card's one official watch/warning surface — the governing active **NWS** alert for
the place (the same feed the hurricane card reads, but kept for **every** event kind, not
just tropical, so a Heat Advisory shows on the ordinary forecast). It banners atop the
card: a `warning` reads **rose** (danger), a `watch`/`advisory` **amber** (caution), with
`more` folding any other active alerts behind it. `null`/absent off-box or where NWS
doesn't cover the point (non-US), so the forecast always renders. The card frame matches
the live `.tool-view`.

Chosen **A — hero + hourly strip** (`docs/mocks/weather-view/weather-a-hero-strip.html`)
over **B** temperature curve (`lab_plot`-style SVG — most distinctive, heaviest new
component), **C** compact dossier rows (the `data_table` upgrade — most complete,
tallest), and **D** segmented Now/Hourly/Rain-&-wind facets (the settled segmented-tasks
pattern — compact, but hourly numbers a tap away). A won as the lowest-friction answer to
the literal "what's the weather now → midnight" question: a big current-conditions hero
(place · time, temperature, condition glyph, feels-like, H/L, wind) over a finger-
scrollable hourly row (time · glyph · temp · precip %), reading in one glance. B/C/D are
retained as the record in `docs/mocks/weather-view/README.md`.

**Two ranges, set by the tool's `range` param (`today` | `week`).** `today` (default) is
the hero + hourly strip above. `week` keeps the same hero and swaps the strip for a
**daily list** (`docs/mocks/weather-view/weather-a-week.html`): one row per day —
weekday (first reads "Today") · condition glyph · precip % · a **temp-range bar scaled to
the week's own min/max** (a steel→amber fill, so the warm and cool days read at a glance)
· the day's high/low. The component picks the layout from `range`; only the matching
detail list (`hours` or `days`) is populated. Open-Meteo gives a usable daily forecast to
~16 days; the tool exposes a 7-day week and deliberately offers **no month-or-longer
outlook** (climate-normal territory, not a forecast — jerv falls back to a web search if
asked). The daily list reused the established list paradigms, so it shipped without its
own mock round (owner call).

**The location firewall holds at the tool, not the view.** A named place is forward-
geocoded by name; the owner's "here" fix is resolved to a nearest-city **name** on-box
(the offline geocoder) and only that public name is geocoded — so the coordinate that
reaches Open-Meteo is a city centre, the same coarseness as naming the city, never the
precise fix. Coordinates never ride the data-only payload (#9). Owner-facing chat
artifact; never a note, never RAG-indexed.

### `hurricane_card` tool-view (binding mock: `docs/mocks/hurricane-view/hurricane-combined-tabs.html`; build plan `docs/archive/HURRICANE_TABS_PLAN.md`)

The in-chat **tabbed** card jerv shows after a `hurricane` tool call. A persistent
storm hero + an official watch/warning banner sit above a tab bar: **Timeline** (the
local hour-by-hour wind/gust/rain strip), **Track** (the forecast cone + path), and
**Impact** (the hazard grid). A registered, data-only view like every other — the model
**authors no markup and no color**; every enum maps to a glyph + token in the component.
The **Track** tab draws the storm on **real map tiles** (the on-box `/api/tiles` proxy,
via Leaflet — pannable/zoomable), so this card is the **scoped exception** to the no-URL /
no-raw-lat/lon rule (#9): its geometry carries real coordinates (the public NHC track +
cone, plus the city-centre `you` pin — never the owner's precise fix), and it carries one
URL — `nhc_url`, the storm's public NHC graphics page. The shape (full schema in
`docs/archive/HURRICANE_TABS_PLAN.md` §2):
`{place, as_of, active_count, coverage, storm:{name, kind, cat, sustained_mph,
sustained_level, gust_mph, gust_level, pressure_mb, pressure_level, moving},
distance_mi, bearing, proximity, alert, track[], cone[], you, nhc_url, timeline[],
arrival, impact}`.

- `kind` is a closed enum (`hurricane|typhoon|tropical-storm|tropical-depression|
  subtropical-storm|subtropical-depression|post-tropical|potential|low|cyclone`); `cat`
  is the Saffir-Simpson number ("1".."5"), the badge when it applies, else the kind label.
- `sustained_level`/`gust_level`/`pressure_level` are **computed** severity tiers
  (`low|moderate|high|extreme`, same enum as `impact.*.level`) so the Storm-stats
  gauges track the real vitals rather than a fixed decoration; the component maps the
  tier to a gauge fill + tone (movement is a heading, so it shows no gauge).
- `proximity` (`near|regional|distant`) is a **computed** how-close tone (amber caution
  when `near` + threatening, else steel info).
- `alert` is the **official NWS watch/warning** for the place (`{level: warning|watch,
  kind, event, headline}`) or `null` — the **only** legitimate watch/warning surface. A
  real `warning` is the one case the card shows the **rose danger** banner (a watch reads
  amber); the headline renders as **escaped text content only**, never markup.
- `track[]`/`cone[]`/`you` are geometry in **real `{lat, lon}`** (the track points also
  carry `label`/`cat`/`past`); Leaflet frames and draws them on the tiled basemap. The
  track + cone are public NHC data; the `you` pin is the geocoded **city centre** (`hit`),
  the same coarseness the projected pin already revealed — never `ctx.here`. This scoped
  #9 relaxation is documented in `backend/.../hurricanetools.py`.
- `nhc_url` is the storm's **public NHC graphics page** (keyed by the feed's `binNumber`,
  e.g. `graphics_at2.shtml`) shown as a "National Hurricane Center forecast" link, or `""`
  when the feed carried no slot.
- `coverage` is `us` (NWS served the point → timeline/alert/impact present) or `global`
  (the point is outside NWS coverage → hero + Track only; the component hides the empty
  Timeline/Impact tabs). `timeline[]`, `arrival`, and `impact` are NWS-derived; `impact.surge`
  is the NHC banded estimate. Tokens-only `.tv-hu-*` classes; the frame matches `.tool-view`.

**Honesty boundary.** Official watches/warnings come only from the NWS `alert` slot
(US & territories); the card never invents one and shows no banner when `alert` is null.
Surge is a **banded** estimate, and arrival/impact **timing is approximate** (derived
from the local forecast crossing TS/hurricane-force thresholds, not official onset
grids). The `.tool` prose binds the model to those limits and to **never** issue an
evacuation instruction from the card — evacuation follows official orders. (This
supersedes the v1 "position + strength only / never rose" framing: the rose banner is
now legitimate *because* it is NWS-sourced.)

**The location firewall holds at the tool, not the view.** The NHC active-storm + GIS
track/cone feeds carry **no** location (queried by storm identity). The two coordinate
egresses — the NWS API (alerts + gridpoint) and the NHC surge MapServer — receive only
the **geocoded city centre** (`hit`), the same coarseness `weather` already sends to
Open-Meteo, never the owner's precise fix (`ctx.here`); the surge query fires only for
an in-coverage US point. Map geometry is projected on-box, so the most an inversion of
the `you` pin against the public track coordinates can recover is that city centre.
Owner-facing chat artifact; never a note, never RAG-indexed.

### `chart` & `lab_chart` tool-views (settled in a three-way GUI review — reference mock: `docs/mocks/chat-charts/c-tabbed-card.html`; build plan `docs/archive/CHAT_CHARTS_PLAN.md`)

The in-chat **interactive time-series chart** — the answer when a "chart / graph / plot
this over time" or "show my lab trend" question is better read as a shape than a wall of
numbers. Two registered, data-only views over one shared **`InteractiveChart`** engine
(SVG, X-axis **pinch/wheel zoom** + **drag pan** + tap-to-scrub readout + reset — the
numeric analogue of the Leaflet `location_map` zoom/pan; `touch-action: pan-y` so a chart
never traps vertical scroll). The model authors **no markup, URL, or color** — it fills
numbers + closed enums only (invariants #1/#9):

- **`chart`** (generic) — `{kind:('line'|'area'), unit, x_kind:'time', title, sub,
  series:[{label, key?, points:[{x:epoch_ms, y, flag?}]}], y:{min,max,ticks:[…]}}`. The component
  owns the palette from the categorical token order (the general domain reads **steel**).
  **Multi-series:** more than one `series` entry overlays several lines on one card — a legend
  appears, each line is colored by its `key` (0→steel…5→amber, component-owned), the y-scale spans
  every series, a tap reads all lines at the selected date, and the Table/Stats tabs go per-line
  (one column / one tile each). `render_chart` emits this when handed `series:[{name, points}]`
  instead of a lone `points` list (e.g. launches per month split by vehicle). Single-series and
  `lab_chart` render exactly as before (flag toning + reference band are single-series only).
- **`lab_chart`** — `chart` + `ref:{lo,hi,label}` (the reference band, drawn as a
  `--green-tint` zone with a dashed edge) and per-point `flag ∈ {normal,low,high,critical}`
  (a closed enum the component tones: **amber** low/high, **rose** critical). Health-domain,
  so the card reads **rose**; every draw carries a `fact_id`/`note_id` ref (pointers-not-
  copies) for the Table view and citation. Superseded / preliminary draws are excluded from
  the plotted current series (marked in the Table), matching `read_labs`' own prose rule.

Chosen **C — tabbed multi-view card** (`docs/mocks/chat-charts/c-tabbed-card.html`) over
**A** answer-first card → fullscreen explorer (the `location_map` pattern — calmest
transcript, zoom behind one tap) and **B** direct-inline-manipulation (the chart *is* the
bubble — fewest taps, but a taller bubble and gestures sharing the scroll surface). C
reuses the settled `hurricane_card`/`weather_card` tabbed-card paradigm: one inline card
with a compact headline (current value · delta) over a **Trend · Table · Range** tab row
(the generic card's third tab is **Stats**). **Trend** is the zoom/pan chart + the scrub
readout; **Table** is the raw rows (date · value · ref · flag · source); **Range** (lab
only) gauges each recent reading against the reference band so the in-range judgement is
glanceable; **Stats** (generic) is a min/max/avg/change grid. The chart mounts on a fresh
node each time the Trend tab is shown, so pointer listeners never double-bind. C won for
surfacing the raw rows and the in-range read **without leaving the bubble** — the lab case
wants the number and the range beside the shape — while keeping the house tabbed-card
frame. A and B are retained as the record in `docs/mocks/chat-charts/README.md`.

**The safety frame binds the view, not just the tool.** A `lab_chart` shows *what the
record says* — values, reference range, flags, dates, cited — never a diagnosis or a
recommendation (it inherits `read_labs`' rule). RLS holds at the tool that produced the
data (a non-health scope reaches no lab rows and emits no `lab_chart`); the view is a
render of an already-firewalled read. Tokens-only `.tv-cc-*`/`.tv-plot-*` classes; the
frame matches the live `.tool-view`. Owner-facing chat artifact; never a note, never
RAG-indexed.

### `bar_chart` tool-view (settled in a three-way GUI review — binding mock: `docs/mocks/bar-charts/c-tabbed-card.html`)

The categorical sibling of `chart`: the answer when a "how many X by Y", "compare X across
Y", or "which Y has the most X" question is a **breakdown or ranking**, not a value over
time (counts per month, notes by domain, top tags). One registered, data-only view; the
model authors **no markup, URL, or color** — it fills labels + numbers only (invariants
#1/#9): `{title, unit, domain, stacked?, categories:[{label, note?}], series:[{name, key,
values:[…]}]}`. One series draws one bar per category; several series draw **grouped**
(side-by-side) or **stacked** bars. Each series is colored by its **`key`** (0→steel,
1→rose, 2→violet, 3→teal, 4→green, 5→amber) — a component-owned index into the domain
accents, assigned by the producer, never a model hex; the palette caps at six series. The
bar canvas is zero-baselined (the axis only dips below zero if a value is negative); a
value label rides each single-series bar and each stacked total.

Chosen **C — tabbed multi-view card** (`docs/mocks/bar-charts/c-tabbed-card.html`) over
**A** answer-first card → fullscreen explorer and **B** direct-inline ranked horizontal
bars, mirroring the `chart`/`hurricane_card`/`weather_card` decision so the bar view reads
as one system with the line view. One inline card: a total headline over a **Chart · Table
· Stats** tab row. **Chart** is vertical bars with a **grouped↔stacked** toggle (multi-
series) and a tap-a-bar readout (value · per-series split · share of total · the category's
`note` citation when present); **Table** is the raw counts (category · per-series columns ·
total · source); **Stats** is a total/biggest/smallest/mean/peak-share grid plus the top
series. B was the strongest for a pure ranking but the weakest for grouped/stacked multi-
series; A hid the raw counts behind a tap the line view already settled against. A and B are
retained as the record in `docs/mocks/bar-charts/README.md`.

Produced by **`render_bars`** — the categorical twin of `render_chart`: a **general-domain**
artifact for a breakdown the model assembled itself (it renders exactly the numbers passed,
so provenance is model-retyped and the reply must say where they came from). It carries no
`fact_id` refs; a cited count-by-category producer over `app.facts` is a possible follow-up.
Tokens-only `.tv-cc-*`/`.tv-bar-*` classes; the frame matches the live `.tool-view`. Owner-
facing chat artifact; never a note, never RAG-indexed.

### `plan_card` tool-view (build plan `docs/archive/JERV_PLANNING_TOOL_PLAN.md` — binding mock: `docs/mocks/jerv-planning-mock.html`)

jerv's per-conversation **plan** — the surface for owner-initiated planning-and-auto-resume.
jerv drafts a plan only when the owner **asks for one** (its `write_plan` tool), the owner
**approves** it, and jerv then works the checklist **across turns**, pausing after each step
so the owner can steer and **auto-continuing** if they don't. One registered, data-only view;
the model authors **no markup, URL, or color** (invariants #1/#9) — the tool result fills
`{session_id, body, status, updated_at}` only, `status` a **flag enum** (`not_approved |
approved | in_work`) the component maps to a theme class (the `flag-${status}` pattern, like
`appointment_card`), **never** a model-sent color. The card parses `body` (plan markdown):
the first heading is the title, the `- [ ]`/`- [x]` lines render as the **styled checklist**
(done = filled green box + strike; the first unchecked step while working spins a dashed
steel box), and any remaining prose renders through `<Markdown>` — the same escaped-envelope
path as an assistant turn, so no model markup reaches the DOM.

**Owner-only Approve is the one transition jerv can't make** (web content it reads can never
talk it into self-approving): while `not_approved` the foot shows an **Approve** button
(`POST /api/plans/{id}/approve`) and an **Edit** affordance that opens the raw body in a
textarea and, on save, corrects it in place (`POST /api/plans/{id}/edit`) before approving.
Once **`in_work`**, the plan surface polls `GET /api/plans/{id}` and, when
`continuation_due_at` is set, shows the **auto-resume countdown** ("continuing in m:ss", a 1s
ticker anchored to the server timestamp like `TaskStatus`) with **Continue now**
(`POST …/continue`, arms it due-now) and **Stop** (`POST …/stop`, cancels the window). The
continuation fires **server-side** (a background sweep) but now **streams live** into the
chat — the reattach broker publishes the step's thinking + tool calls token-by-token — so the
owner watches it work, not just its settled answer. When `awaiting_owner` is true jerv paused
for the owner's call: a distinct **violet "Waiting for you"** state with **no countdown**. When
every step is `- [x]` the chip reads **"Plan complete"** (steel). Tokens-only
`.plan-card`/`.plan-chip.flag-*`/`.plan-approve`/`.plan-resume`/`.plan-await` classes; the card
brings its own frame, so the generic `.tool-view` wrapper drops its border like `tv-task`.

**Where the plan renders — draft inline, in-work behind the pill.** To keep the plan from
crowding the transcript (fighting deep-research and other tool views), the **inline
`plan_card` is draft-only**: `FullBrainSurface` filters it out once the live `plan_status`
leaves `not_approved`. The in-work plan then lives behind the composer pill: the shared plan
state + render (`usePlanState` + `PlanBody`, `agent/views/registry.tsx`) power both the inline
draft card and a bottom **`PlanSheet`** (the shared `Sheet`) opened by tapping the pill —
status, checklist, countdown, Continue/Stop — dismissed like any sheet.

**Out-of-card status surfaces.** `plan_status` (`SessionOut.plan_status ∈ not_approved |
approved | in_work | null`) drives two always-visible twins of the chip: the **composer-foot
plan pill** (`.plan-pill.flag-*`, beside the model pill — now a **button** that opens the
`PlanSheet`, reading the **live derived state** so it flips to **"plan complete"** instead of
stalling on "working to plan"; the in_work dot pulses) so the owner sees the plan state and can
pop it open without scrolling, and the **Chats-picker plan badge** (`.stat.plan.flag-*`, beside
the staged badge on the session row). Both are the same flag enum the theme colors, never a
model color; the after-turn session reload keeps them fresh, and an in-card action
optimistically refreshes them.

### `deep_research_report` tool-view (build plan `docs/plans/DEEP_RESEARCH_TOOL_PLAN.md`, Wave D3 — **mock-gate sign-off pending**)

jerv's `deep_research` run returns its finished report as this registered view. Data-only
slots: `{question, complexity, report_md, sub_agents, rounds, analyzed, revised,
coverage_limited, truncated, source_mode, web_sources:[{url, title}],
children:[{label, persona, ok, summary, session_id}]}`. It renders a provenance strip (the
`complexity` label, a source count, the round count, a **`source_mode`** badge for a
library-scoped run (`library` → "video library", `library_first` → "library + web"; the
default `web` shows none — docs/archive/DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md), and the `analyzed`
"cross-checked" / `revised` / `coverage_limited` / `truncated` flags — closed
enums/booleans the theme colors, never a model-sent color), the report body, and a
collapsible sub-agent roster
whose rows deep-link to each child's own session on reopen (reusing the `.tv-syn-*` roster
classes). **Citations are tracked end-to-end:** the real URLs the sub-agents reached are
collected into `web_sources` (the global registry the synthesizer numbers against), and
the report's `[^n]` markers render as tappable **favicon** citations — `[^n]` →
`web_sources[n-1]`, the same on-box favicon standard jerv's own web answers use (the URLs
came from the children's tool calls, never model prose; #9). A synthesizer on **gpt-oss**
cites in its native harmony notation (`【410†L1-L8】`) instead of `[^n]`; the report render
path opts into `harmonyToFootnotes` (`Markdown harmonyCitations`), which rewrites `【N†…】` →
`[^N]` so those land on the same favicon path — a browse turn, where `【N†…】` is the model's
private cursor, still strips it. During the run each stage streams a live
`ToolProgressEvent` phase line (Planning → Researching → Cross-checking → Checking coverage
→ Filling gaps → Writing → Reviewing → Revising), rendered by `DeepResearchProgress` as a
**vertical timeline** (`.fb-drp` — the eight stages stack down a rail; done reads steel spine
+ ✓, the live stage pulses, the rest stay dim, so the pipeline never wraps or spills on a
narrow bubble). The **active stage opens a slot** that hosts its detail line, the
`<SubagentFan>` it spawned (the roster + budget meter nest inside the stage, not as a loose
block below the bubble), and — at Write / Revise — the report streaming into `.fb-drp-report`,
so the owner watches the orchestration in one scannable column
(`docs/plans/DEEP_RESEARCH_TOOL_PLAN.md` v8).
The report body is **`report_md` rendered through the shared `<Markdown>` path** — the same
renderer an assistant turn uses — which is safe because the Markdown came from the
synthesizer model over the escaped-envelope findings and carries no model-authored markup,
URLs, or scripts (I-9); its `[^n]` footnotes render as the report's own numbered chips (the
report's `## Sources` section maps them). Tokens-only `.tv-dr-*` classes; the frame matches
the live `.tool-view`. Like every view it is **data, not instruction** (I-1) and renders no
external resource (I-9). The non-happy states (coverage-limited / truncated / thin-sources)
and a reference mock go through the mock gate before this is marked settled.

### Deepest research — the in-flight surface (GUI gate settled: **A — backgrounded card**; reference mock `docs/mocks/deepest-research/compare.html`, build plan `docs/archive/DEEPEST_RESEARCH_TOOL_PLAN.md` R8)

`deepest_research` is a no-holds **background** run (two agent tiers, minutes-to-hours), so
unlike `deep_research`'s live in-turn card it is surfaced as a *backgrounded* run whose
progress arrives as coarse per-round ticks (R6). A three-way GUI review (A backgrounded
card · B run banner + reopen · C two-tier emphasis) settled on **A** (owner, 2026-07-22):
the surface **is the `deep_research` timeline, backgrounded** — `DeepestRunCard`
(`FullBrainSurface.tsx`) wraps the unchanged `DeepResearchProgress` timeline + its
`SubagentFan`, adding only an **amber "deepest" identity badge** (amber = the research
accent, distinguishing it from a live deep_research card) and a coarse per-round meta line
(`round N · sources · coverage · elapsed`) that advances per checkpoint tick, not per token.
The finished run resolves to the **same `deep_research_report` view** — its provenance chips
carry the deepest extras (`2 tiers`, task/sub-agent counts, `resumed once`) and the roster
nests sub-agents under their task agent, each deep-linking to its session. Tokens-only
`.fb-deepest-*` classes over the reused `.fb-drp-*` / `.fb-sa-*` machinery; data, not
instruction (I-1). The losing variants B/C are retained in `compare.html` as the record.

## Research Library (settled in a three-way GUI review — binding mock: `docs/mocks/research-library/b-segmented-tabs.html`; build plan `docs/archive/RESEARCH_LIBRARY_PLAN.md`)

The owner-facing browse surface over the two **`external`-corpus** artifacts jerv
produces on its own turns — **deep-research reports** (`app.research_reports`, the
`deep_research` tool) and **video analyses** (`external.sources`, `analyze_stream` /
external-video ingestion). Both persist server-side and are already reachable to
*jerv* via corpus tools (`list_/search_/read_/show_/remove_research_report`, the
`search_external_video` family); this is the **human's** door to the same corpus — a
card-launcher destination (a **Research** tile under KNOWLEDGE; `ResearchLibraryScreen`)
that lets the owner **search, view, and delete** what's been researched, without going
through a jerv turn. It rides the **amber** research accent (the read-only/research
domain, per §Principles 4); reports carry an amber file type-disc, video analyses a steel
video disc — the type axis, distinct from the amber domain dot.

Chosen **B — segmented tabs** over A (unified feed + swipe rail) and C (search-first +
bulk select); rivals retained as the record in `docs/mocks/research-library/`. B won for
giving each artifact a **purpose-built list** rather than forcing two very different
shapes into one row: a **Reports · Videos** segmented control (`.seg-row`/`.seg-on`, the
settled Data/Locations pattern, active segment taking the amber research tint) switches the
surface, and **search filters within the active tab**. Reports lead with a **short
LLM-generated title** (the `title` column, migration 0143 — the raw `question` is often a
whole paragraph, so it heads the card only as the fallback until the `title_research_report`
job lands), a **complexity** badge (simple `--green` / comparative `--steel` / deep
`--violet`), and a **provenance chip row** (`sub_agents` · `rounds` · `sources`, plus the
`analyzed` "cross-checked" / `revised` / `coverage_limited` / `truncated` flags the theme
colors — closed booleans, never a model-sent color). Videos are **grouped into collapsible
per-channel sections** (a `⌄` section head with the channel name + count, sorted by channel;
default expanded; rows within a section run **newest-published first**, so a channel reads as
a timeline rather than in analysis order), each row leading with the provider **thumbnail** (a `i.ytimg.com` still
for a YouTube source, falling back to the camera glyph on load failure) carrying the
duration pill and the title + date. A/C's single mixed stream made the report provenance and
the video thumbnail fight for the same row; B's two lanes let each read at a glance. (A's
swipe rail and C's bulk-select + passage-first search stay available paradigms for other
surfaces — they lost here on fit, not quality.)

- **The report row is a compact list row [decided 2026-07-20 — variant round
  `docs/mocks/research-card-density/`].** The first card stacked four vertical bands (a 38px
  amber type-disc beside a two-line title, a complexity pill, a provenance chip row, a
  `research · date` footer) and read too tall — only ~4 cleared the phone fold. Chosen **D —
  list row** over three lighter rivals (A tightened-current / B text-meta / C edge-accent rail):
  a **two-line** clamped 13px title (the full text stays in the DOM for search + assistive tech)
  over **one muted meta subline** — complexity as a colour-coded word (deep `--violet` /
  comparative `--steel` / simple `--green`), then `sub_agents` · `rounds` · date — led by a
  small **amber** file glyph, with the row's chevron and `⋯` kept. ~7 reports now clear the
  fold. The full provenance chip row (`sources` plus the `analyzed` / `revised` /
  `coverage_limited` / `truncated` flags) lives on the **detail** screen, not the list. Videos
  keep their taller thumbnail-led row (the thumbnail needs the height).
- **Tap a title → a full-screen detail layer** (the settled slide-up layer; back
  chevron + title, swipe-down climbs the tree). A report renders its `report_md` through
  the shared `<Markdown>` path (the same renderer an assistant turn and the
  `deep_research_report` view use — safe model-authored-over-escaped-findings Markdown,
  I-1/I-9) with the provenance strip on top; a video renders a still + filmstrip + the
  summary and transcript, reusing the `video_analysis` card's shape (`VideoAnalysis.tsx`).
  The detail layer is **pure reading** — item actions live on the list's `⋯` menu, not here.
- **One consolidated action menu on the list (`⋯` bottom sheet)** — the shared `Sheet`
  with the settled `.actrow` rows, opened from each row's `⋯`, listing only what applies to
  that source: **View** (opens the detail), **Open in jerv conversation** (both — seeds the
  owner's current Research/jerv chat, the agent that produced these artifacts, with a
  reference to the item), **Move to folder** (reports only — opens the move sheet below),
  **Copy** (report for a report; summary + transcript for a video),
  **Download report (.md)** (reports only), **Open source ↗** (videos with a URL), and
  **Delete** (both). Copy/download **fetch the full item on demand** (the listing carries no
  body); a transient feedback toast reports the result.
- **Reports fold into owner-named folders [decided 2026-07-27 — parity with the Tasks
  surface].** The Reports tab mirrors the Tasks grouping (Direction B — `docs/mocks/
  task-grouping/`): a report is filed into a **folder** via its `⋯` → **Move to folder**
  sheet (existing folders as ticked rows, an **Ungrouped** escape, a **New folder…** row that
  creates + files in one tap — never a drag), and browse mode renders each folder as a
  **collapsible section** (the same `⌄` head + count as the video channels) over a trailing
  **Ungrouped** section that hides when empty. Collapse is **device-local + persisted**
  (`jb.research.collapsedFolders`, like the Tasks `tasks/collapsed`). An **Organize folders**
  toggle arms inline **rename / delete** on each folder header (headers force-expand while
  organizing); deleting a folder drops the folder only — its reports fall back to Ungrouped
  (server FK `SET NULL`), never deleted. Folders are **owner-only browse metadata**
  (`app.report_groups` + `research_reports.group_id`, migration 0149) kept off the corpus
  firewall — the `group_id` is opaque to jerv (its corpus tools never select it), written
  only under the full-owner context. An **active search flattens across folders** into one
  result list (folders + the Organize toggle show only while browsing), matching the video
  tab. A library with **no folders** stays a flat list — the folder headers appear once the
  owner files the first report.
- **Delete is owner-initiated and direct** (not a staged Proposal — that path is jerv's).
  It uses the **tap-again confirm** on the sheet's rose Delete row (`window.confirm` is not
  used), spelling the consequence ("deletes this report/video"), and raises the standard
  **undo snackbar** (steel undo link; the delete is soft-held for the toast's life, then
  committed). The corpus rows carry no graph/notes footprint, so deletion is a plain row
  delete under the `external` scope — no cascade, no review.
- **Search is an instant in-tab client filter [decided].** For a personal-scale corpus the
  as-you-type filter narrows the *already-loaded* rows of the active tab client-side (matching
  the binding mock, keeping the rich browse rows, no debounce/spinner) over the title/question
  + channel fields. The owner-gated **server** search endpoints (`GET
  /research-library/{reports,videos}/search`, hybrid FTS + embedding, `degraded` signal) are
  the tested API for a future *whole-library* search affordance (matching in `report_md` /
  summary / transcript beyond the loaded page) — not wired into the browse filter today.
- **States (DoD fixtures):** empty ("Nothing here yet — reports and video analyses jerv
  saves show up here."), filter-no-match (names the fix), long lists, a stream/LIVE video,
  a truncated/coverage-limited report, and an error line on an unreachable server (never an
  error page).

Everything composes from settled paradigms (segmented control, list rows, the `Sheet`
action menu, the slide-up detail layer, the `<Markdown>` + `video_analysis` renderers, the
undo toast), so the review shaped the *composition*, not new primitives.

## Wiki Talk board (settled in a three-way GUI review — reference mock: `docs/mocks/wiki-talk-b-topics.html`)

The article's editorial board (Phase 6) — the wiki's second surface after the reader. Chosen
**B — threaded topics** over A (single chat thread) and C (claim-anchored annotations): discrete
collapsible topics with **open/resolved** badges (amber/green), signed + timestamped posts in
**three voices** — `You` (owner), `Editor` (the agent, violet signature), `Builder` (the batch
builder) — a **New topic** composer, a per-topic reply box, and an auto **Build log** topic
(`auto · N entries`) the builder posts a one-line decision summary to on every rebuild
("Created/Rebuilt article …; N facts across M domains", "Merged in X"). B won for the durable,
scannable archive it gives across many editorial threads over time; A/C are retained as the record
(`wiki-talk-{a,b,c}`). Owner-only; tokens-only; same shell as the reader (TopBar +
swipe-down-to-close). The wiki stays **machine-written** — Talk is the front-end over the sanctioned
levers (correction note, source exclusion, rebuild). **Wave T1** shipped the board + the Builder voice
+ owner topics/replies; **Wave T2** shipped the live **Editor** (agent) reply — an owner reply draws an
agent turn (`AgentLoop` + the wiki tools, a dedicated Editor system prompt) that explains sourcing and
enacts via the levers, posted as an `editor` post with an outcome chip. Reachable from the reader's
**Discussion** affordance (the quick-fix correction sheet stays beside it until T2 unifies them). DoD
fixtures: empty (Build-log only) / long-thread / pending-action / error / offline.

## Locations surface (the owner's place views — Phase 7)

The location domain's accent is the **`--location` teal (`#6FB6B1`)** (settled in L3);
amber (`--warn`) carries the stale/"last known" tone (matching the GPS-gap marker).
`LocationScreen` is a 3-tab segmented control (Map · Timeline · Phones) on `.seg-row`/
`.seg-on`. Two L7 affordances sit on it, both **names + times only — never a
coordinate** (this is why neither needs a basemap):

- **Inline digest panel (L7a — chosen Option C, reference mock `docs/mocks/location-l7/
  option-c.html`).** A **compute-on-read** place digest renders as a **collapsible
  inline panel ABOVE the Map** inside the Map tab — *not* a fourth tab (Options A/B's
  extra "Digest" segment was rejected). It is a **per-day place-track**: each local
  civil day is a horizontal bar of named place-segments (home teal, other places
  steel, a dashed amber "no signal" gap), with a headline summary (nights home, time
  at a place, longest trip), a compact legend, a first/last-seen line, and an
  owner-only footnote. It **defaults to the WEEKLY period**, with a nightly⇄weekly
  pill toggle (nightly expands a single day's hour-track) and a "computed just now ↻"
  recompute affordance that keeps it honestly compute-on-read (`GET
  /api/locations/digest?period=week|night`, owner + full-owner gated — the digest reads
  WEAK-RLS `app.events`/`place_geofence`, so the endpoint gate is the barrier, not RLS).
  The panel is a regular surface element (not a modal): it follows the inline-expansion
  paradigm, collapsed/expanded by its header.

- **App-open presence toast (L7b — chosen Option C).** On app/chat open a small
  **corner toast** rises bottom-anchored above the nav (the existing toast paradigm —
  bottom-anchored, auto-dismiss, single action), showing the owner's OWN latest place.
  It is **freshness-honest**: a fresh fix reads teal "Currently at <place>"; a stale
  fix reads amber "Last known: <place> · N ago · may have moved", **never "here now"**.
  It self-dismisses after a few seconds and carries one **"open"** action (jump to the
  Locations surface); it is **absent entirely when there is no usable fix**. Names +
  times only — no coordinate. This is a **distinct presence toast**, NOT the
  connectivity status banner DESIGN reserves for sync state (it uses `role`/live-region
  semantics via an `<output>` element). The toast reads `GET /api/locations/presence`
  (owner + full-owner gated). The SAME presence read also reaches the assistant — but
  as a **data-framed `UserMessage` prepended to the conversation** in
  `api/agent.py::chat` (inside the agent's data/instruction boundary — ASSISTANT.md
  non-negotiable #1), **not** the system prompt and **not** the toast — owner-gated (present
  only for a location-scoped full-owner session), so a narrowed session gets neither.

### Phones tab — paired-phone management (settled in a three-way review; chosen **B — swipe rail** over A "family roster + device-hub sheet" and C "inline accordion + credential strip"; reference mocks `docs/mocks/phone-management/{a-family-roster,b-swipe-rail,c-inline-accordion}.html`)

The location surface is **phones only** — the manual "Add device (OwnTracks)" path
is retired (a JBrain360 phone never pastes a key). The old Devices tab had two
gaps: no way to **roll the pairing token** once a phone was paired, and a
**"Rotate key" that couldn't reach a paired phone** (a phone receives credentials
only by redeeming a pairing code). The redesign collapses both into one action and
renames the tab **Phones**:

- **Layout:** an **Active / Revoked** filter (count pills, `--steel`) over a
  **swipe-left rail** list — the settled home-note / chats paradigm (`notes/swipe.ts`
  `RAIL_WIDTH`, the shared `.rail-btn`/`.rail-edit`/`.rail-delete`/`.rail-armed`).
  Active rows carry **re-pair · rename · revoke · delete** (`rail-4`, 48px each);
  a revoked row carries **restore · delete** (`rail-2`, 96px each). **Tapping a
  closed row also opens its rail**, so the actions are reachable without the gesture
  (the gesture-is-never-the-only-way rule). One rail open at a time.
- **Re-pair (the unifying fix):** "roll the token" and "rotate the key" for a phone
  are **one action** — mint a fresh one-time code **bound to the existing device**
  (`POST /api/pairing/codes` with `device_id`); on redemption the device's key
  **rotates in place** (old key revoked, new minted) while its identity + history
  stay attached, with **no lockout window** (the old key works until the phone
  redeems). The same flow **restores a revoked phone**. The code rides the device's
  **current** name. Backend: `pairing_code.subject_id` + a re-pair-aware
  `app.redeem_pairing_code` (migration 0077).
- **Rename** edits the label inline on the row (`POST /api/devices/{id}/rename`,
  the active key principal's label follows). **Revoke** suspends the key (history
  kept); **delete** hard-removes the phone + its history
  (`DELETE /api/devices/{id}`, cascading fixes/geofence state). Both destructive
  actions arm a **tap-again confirm** on the rail button, disarmed when the rail
  closes. Re-pair / new-pair show the QR via the existing `PairCodeSheet`.

B won for the most aggressive vertical density and muscle-memory reuse of the
existing swipe rail; A (a per-phone management sheet grouped by family member) and
C (an inline accordion led by a credential-lifecycle strip) are retained as the
record. **Family-member grouping is deferred** — it needs the device→Person graph
link surfaced in the device list payload, out of scope for this round.

## JBrain360 app — member live-map surface (Phase 7, owner-approved)

The **member dashboard** (`/dash`, served into the Android app's WebView) is a
**full-screen live map with a collapsible bottom dock** — reference mock
`docs/mocks/app-live-map-v2.html` (owner-approved directly; the three-way GUI gate was
waived by explicit owner choice of this direction). It replaces the earlier
Devices/Timeline/Map tab shell: the map is the whole surface; chrome floats over it on
`backdrop-filter` panels. Default basemap is **CARTO Dark Matter** (dark/minimal, via
the `/api/tiles` proxy). Location domain stays **`--location` teal**; live = `--green`,
stale = `--amber`. The v2 refinement (collapsible dock + drag-both-ends window +
center-on-select) supersedes the original `app-live-map.html`, kept for history.

The elements, all owner-/family-scoped (never a scoped link — L8):

- **Person switcher (top).** A horizontally-scrollable row of avatar chips —
  **Everyone** + each family member — each with a green/amber **live/stale** presence
  dot. Selecting a person **recenters the map on them** (`centerOn`, no auto-fit) and
  drives the overlay; tapping their pin selects them too. Everyone mode shows all
  current pins (auto-fit, no trail/heat).
- **Current location.** A person-colored map pin with an upright initial.
- **Collapsed bottom dock (map-first default).** A slim persistent **bar** shows the
  selected person (avatar · name · live/last-seen) and **two pull-up tabs**, opened
  **one at a time**:
  - **Details** — the person's **last-actions** timeline (Today / Yesterday / N-days
    arrived/left transitions; names + times only), or the **roster** in Everyone.
  - **History** — the **Trail/Heat** toggle + a **drag-both-ends time window**
    (two thumbs over now → 7 days; relative labels "5d ago → now") that drives the
    trail/heat and filters the activity list. Disabled in Everyone (no single trail).
- **Live.** Live fixes move each visible person's pin and extend the focused trail
  (the server scopes the stream to self + group).

The surface honours the firewall: a member session sees only **its own subject + its
family group** (RLS `viewer_may_see`/`view_scope`), the basemap is self-hosted, and
the Details/History content is **names + times only — never a raw coordinate in
prose**. Build plan + wave breakdown: `docs/archive/PHASE7_APP_MAP_PLAN.md`.

## jcode — code mode (GUI gate settled; build plan `docs/archive/JCODE_PLAN.md`, Wave J3)

Code mode is a sandboxed coding session fronted by the PWA (a launcher tile →
launcher → live session). The two surfaces went through the mock-first gate
(three variants each; rivals retained under `docs/mocks/jcode-*`):

- **Launcher — `jcode-launcher-b-resume-first.html` [chosen].** The **session list is
  the hero**, reusing the **Chats-picker paradigm** (`Today/Older/Archived` segmented
  buckets, ~44px micro rows with a scope/live dot, repo `@ branch · when`, turn count,
  and the bouncing-dots live-turn glyph for an in-flight turn). A single prominent
  **New session** button opens setup as a **bottom sheet** (workspace = clone-repo /
  scratch, work branch, the pinned on-box model card, Start). Chosen over A
  (compose-first form always open) and C (a `New·Sessions` toggle) for consistency with
  the settled Chats picker — you resume far more than you start.
- **Session — `jcode-session-2tab-a-fullbleed.html` [chosen, supersedes the 4-view
  `jcode-session-c-tabbed.html`].** Code mode was gutted to a **terminal-first** session
  (owner decision, build plan `docs/archive/JCODE_2TAB_PLAN.md`): the PWA chat, the diff
  placeholder, and the read-only terminal-log view are gone. One session, **two views:
  Terminal · Preview**. The Terminal is the workhorse — a real shell in the sandbox
  (xterm.js) where the owner runs `grok` against the on-box coder; Preview is the
  ephemeral tunnel. **Variant A — full-bleed** maximizes the terminal: a slim one-line
  header (back · status dot · `repo @ branch` · model chip `qwen3-coder · 256k · on-box`),
  owner actions (Reset / Share / **Stop** / Delete) in a `⋯` menu, two compact labelled
  tabs, then the terminal fills everything to the bottom with the mobile key row docked
  beneath it. The coder loads at its **full native 256k context**, and switching to an
  already-resident coder never reloads it. **Exiting the shell pauses the session**
  (processes killed, checkout kept); it shows a **Restart** prompt and can be restarted
  from the launcher (a paused row reads `stopped`). Share recipients get both tabs.
  Chosen over B (tabs-in-header) and C (bottom tab bar) for the cleanest, most
  terminal-maximizing chrome; rivals retained under `docs/mocks/jcode-session-2tab-*`.

Both reuse settled paradigms (Chats picker, segmented control, the preview tunnel), so
they implement rather than re-litigate them.

## Tasks — the result band (settled in a three-way review; reference mock `docs/mocks/task-session-nav/c-result-band.html`, rivals A "inline latest-run line" + B "unread-inbox reframe" retained under `docs/mocks/task-session-nav/`)

The Tasks screen (saved prompts that spawn an agent session on a schedule;
authoring + history live in `docs/mocks/tasks-launcher-README.md`) had no way to
reach a task's latest session, or to tell which task had a fresh one, without
expanding a card and comparing run timestamps. The fix is a **two-zone card**:

- **The card splits into a config header and a docked "result band".** The header
  is unchanged (health dot, name, agent badge + schedule, enable toggle, expand
  chevron). Below it, a **full-width tappable band** — recessed on `--surface-2`,
  ≥56px — shows the **latest run** as a mini session row: a status dot, the run's
  summary (2-line clamp), and `N turns · <ago> ›`. Tapping the band **opens that
  session in one tap**; the band is the primary call-to-action. A task that has
  never run shows an inert "No runs yet" placeholder; a run without a session
  (an early failure) renders inert.
- **Unviewed recognition rides `--steel`** (info/notification — distinct from the
  green health dot). An unviewed result gets a **3px steel left-edge bar, a NEW
  pill, and a full-`--text` summary**; once its session is opened the band relaxes
  — bar and pill gone, summary to `--text-2`, an **"opened ·"** meta prefix.
  Failures keep a rose dot regardless.
- **Viewed-state is device-local** (like theme / text size): a
  `jb.tasks.viewedRunAt` map (task id → the newest opened run's `started_at`),
  shared by the card band and the launcher's **Tasks tile badge** — both read
  from the same map (`frontend/src/tasks/viewed.ts`), so the badge counts exactly
  the tasks whose bands still say "new". A task reads "new" until its latest run's
  session is opened **on this device**; opening Tasks does not clear it (only
  opening the session does), so the badge tracks unopened results, not screen
  visits. Cross-device read-sync would need a server column and is deliberately
  deferred.
- **The latest run is embedded in the task payload** (`Task.latest_run`,
  server-computed via one `DISTINCT ON (task_id)` query) so every band renders
  from the single `GET /api/tasks` — no per-card fetch. Mutations that return a
  task (PUT / PATCH) re-embed it so a toggle never blanks the band.

Chosen over **A** (a subtle inline latest-run line — too easy to miss as the
recognition signal) and **B** (an unread-inbox reframe with a `New · All`
segmented sort — more screen surgery than the problem warranted, and it buried
the config behind the disclosure). C keeps the per-card model while making the
result a first-class, always-visible dock.

## Tasks — grouping & reordering (settled in a four-way review — chosen **B, "chips + move sheet"** over A "drag handles + groups", C "swimlane board", and D "organize manager"; reference mocks `docs/mocks/task-grouping/{a-drag-groups,b-chips-move-sheet,c-board-lanes,d-organize-manager}.html`, `docs/mocks/task-grouping/README.md`)

The Tasks screen auto-bucketed tasks into two **fixed, system-defined** sections
(Scheduled / On demand) with no owner control over membership or order. Grouping
is now **owner-defined**, and **B keeps the two concerns separate** — membership
is a deliberate menu pick, order is an opt-in drag — which is why it won over the
one-gesture rivals (A's cross-group drag, C's board) and the button-only manager
(D):

- **A group is an owner-named bucket.** A **chip row** (the settled `filter-chip`
  pattern) is the group switch: **All** shows every task under its group header,
  each chip narrows to one bucket. Tasks with no group fall to a trailing
  **Ungrouped** section (so existing tasks migrate untouched — grouping *augments*
  the old split rather than forcing a backfill). The Scheduled / On-demand headers
  are retired.
- **Filing is a `⋯ → Move to…` sheet, never a cross-screen drag** (composes the
  shared `Sheet`, mirroring `MoveDomainSheet`): the owner's groups as rows (current
  one ticked), an **Ungrouped** escape, and a **New group…** row that creates a
  bucket and files the task into it in the same tap.
- **Order is an opt-in "Organize" mode** (the reorder toggle in the top bar). It
  arms an in-place **grip** on each card — drag to reorder **within a group only**,
  or focus the grip and use **↑ ↓** (the accessible/keyboard path, the D insight
  carried across as the drag equivalent). Organize mode also reveals **inline
  rename** and a **tap-again delete** on each group header; deleting a bucket
  **SET-NULLs its tasks back to Ungrouped**, never deleting them.
- **Persistence.** A task gains `group_id` (FK, `ON DELETE SET NULL`) + a 0-based
  `position` within its bucket; groups are their own owner-only RLS table
  (`app.task_groups`, migration 0137). One `POST /api/tasks/reorder`
  `{group_id, task_ids}` is the authoritative write behind **both** a within-group
  reorder and a move (the client sends the destination's full ordered id list with
  the moved id appended). `GET /api/tasks` returns tasks pre-sorted by position;
  `GET/POST/PATCH/DELETE /api/task-groups` manage the buckets.

Rejected: **A** (one drag does membership + order, but cross-group drag over a long
list fights autoscroll and offers no non-drag path), **C** (a swimlane board — one
group visible at a time loses the overview and horizontal paging fights vertical
drag; over-built for a handful of tasks), and **D** (button-only organize manager —
reliable and accessible, but less tactile; its ▲▼/keyboard path was folded into B's
Organize mode instead of shipping as its own screen).

## Sub-agent spawning surfaces (settled; build plan `docs/archive/SUBAGENT_SPAWNING_PLAN.md`)

When `jerv` fans out web-sandboxed research/review/summarize sub-agents (the
reserved `spawn_subagent` hatch, `docs/reference/ASSISTANT.md`), two surfaces show them. The
**layouts** were chosen in a three-way review (rivals retained as the record); an
adversarial review then re-opened the gate, and the revised mocks added the
failure / cancel / long-fan / budget-exhausted states (scenario switchers) and
dropped the persona-as-color scheme. The owner **re-confirmed** the revised mocks —
the gate is settled.

**Persona is a `kind` enum, rendered as a NEUTRAL text tag (or a per-persona glyph
on a neutral disc) — never a color.** The earlier "research=steel / review=violet /
summarize=green" scheme is **rejected**: it violates the registry rule (components
express `tone`/`flag`/`kind` enums, never colors) and collided with three reserved
meanings simultaneously — green=live/ok, violet=Financial domain, steel=agent/focus/
live-glyph. Semantic color on these surfaces stays fixed: **steel=live, green=done,
rose=failed**; persona never borrows an accent.

**In-chat live panel — chosen A "accordion step list"** (rivals B "agent cards",
C "live fan/tree"; reference mock `docs/mocks/subagent-chat-mock.html`). Below
jerv's answer bubble, the running fan renders as a bordered group ("Researching ·
N agents") of **collapsible step rows** — the same disclosure register as the
existing `ActivityLine`/`StepRow` "Worked" foot strip. Each row carries the
**stateful glyph** (steel bouncing dots while running → green `✓` done → **rose `✕`
failed**; `aria-hidden`, the status word carries state, honors
`prefers-reduced-motion`), the **label** (title-forward — it owns the row and wraps
to two lines rather than sharing it with a persona pill), a live **status word**, and
a thin progress bar; tapping a row expands its neutral **persona tag**, its **trace**
(thinking + tool calls), and the final **summary** — rendered as rich markdown in a
bounded scroll region so a long answer doesn't push the fan open (a failed row
auto-expands its error like `StepRow`). A depth-2
sub-sub-agent nests one indent deeper. **Required non-happy states:** a **Stop** on
the group header (cascade-cancel, mirroring the image-render Stop); the
**tree-budget meter** goes `--danger` at the ceiling (paired text value) with a
**truncated** synthesis variant on `tree_budget_exhausted`; the header rolls up
`done · N ran · M failed`; a **row cap + "show N more" + max-height scroll region**
contains a long fan. **Accessibility:** one polite live-region summary for the whole
fan (not N live rows — avoids the announcement storm). The fan-out result is a
**registered `subagent_synthesis` tool-view** (added to the registry list in the
same PR; composed from `stat_block`/`citation_card`, standard tool-view frame — no
bespoke green panel). A won for density and continuity; B/C retained.

**Session manager nesting — chosen B "always-nested rail"** (rivals A "caret
disclosure", C "inline chips"; reference mock `docs/mocks/subagent-sessions-mock.
html`). A spawning chat shows its sub-agents **nested beneath it under a vertical
connector rail** (a depth-2 agent indents one rail deeper). **Children are excluded
from top-level bucketing** (filtered by `parent_session_id != null`) — they never
appear as their own top-level rows; the rail **collapses by default once
`subagent_count` exceeds a threshold** (and for any archived parent) so a large fan
doesn't bury the dense Chats list. The **group toggle is a real `button`** with
`aria-expanded`, and the tree uses `role="tree"/"treeitem"` + `aria-level`. Each
child row reuses the live-turn glyph + neutral persona tag + status (incl. **failed
rose**); the parent badge distinguishes `N running` / `done · N ran` / `… · M
failed`. **This is the one place the "at most one chat shows the live glyph" rule is
lifted** — `activeTurn` becomes a session-keyed **set for the row glyphs only** (it
does **not** gate sends; the parent turn stays the single gated turn, and the
in-chat accordion reads the parent turn's `subagent_*` events while the tree reads
child session rows — see the build plan's "Execution model").

## Implementation rules

1. Tokens live in one file (`frontend/src/styles/tokens.css`); components
   never hard-code colors, radii, or font sizes.
2. New components follow this document; if a needed pattern is missing, extend
   this document in the same PR that introduces the component.
3. Screenshot-test significant surfaces in both themes once Playwright lands.
4. The mock fixtures are maintained alongside the API client; a screen's
   mock states (default, empty, error, offline) are part of its definition
   of done.
