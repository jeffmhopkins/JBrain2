# GUI gate — browse trace and inline tool marks

> **Status:** Living · **Last verified:** 2026-10-06
>
> **Decided 2026-10-06 (owner pick): A — "timeline"** is the binding spec for what a
> `browse` step shows when expanded (`a-timeline.html`). **Inline tool marks** in the answer
> are binding too (owner request 2026-10-06, `inline-tool-marks.html`). The reasoning lands
> in `docs/reference/DESIGN.md` with the frontend.
>
> **Both are built (2026-10-06).** The trace is the `browse_trace` step view
> (`backend/src/jbrain/agent/browse_trace.py`, `frontend/src/agent/views/browseTrace.tsx`); the
> marks are `frontend/src/agent/toolMarks.ts` with `markdown.placeMarks`. Where the build
> departs from these mocks (no *show all* link, the *JS shell* badge's source, a tapped group
> opening every step it stands for, `N tools used` for a mixed group) DESIGN.md says why.

Today a jerv turn that used `browse` shows one Worked row: *Browsed a site · On
epictheatres.com's "Epic Thea… · ● verified · 6 steps*. Expanding it shows only the web
source card and the raw tool text. Nothing says what the agent did on the site. These
mocks fix that, and also mark in the answer where the model stopped to use a tool.

Both files use the shipped stylesheet (tokens, `.fb-act-*`, `.fb-step-*`, `.fb-res-*`,
pasted from `docs/mocks/code-run/h-worked-ledger.html`'s BLOCK 1). New CSS is in clearly
marked blocks, and new UI is outlined in dashes. They are dark and phone-width. The worked
example is Epic Theatres of Titusville, showtimes for Tue Oct 6.

## A — `a-timeline.html` — the browse trace (binding)

A `browse_trace` step view (a `STEP_VIEWS` member, `frontend/src/agent/views/registry.tsx`)
renders inside the expanded "Browsed a site" step, above the shipped sources rung.

- **Level 1, the rail.** One rung per step, in plain words: *Clicked "Titusville with Epic
  XL"*, the page it landed on, the time it took, and badges (*JS shell*, *1 re-found*,
  *1 refused*, *1 not run*). Dot colours: green ok, amber partly refused, rose failed.
  Host-only work is drawn differently from the model's: the opening navigation has a
  hollow dot, and the closing check has a ringed dot. The last rung is the answer and the
  host's fact check (*verified: 35 of 35 times and prices, 34 of 36 names and numbers on
  the page*). It also shows whether extraction ran.
- **Level 2, inside a rung.** Each command with the element line it acted on
  (`[41] button "Tue Oct 6"`), the re-find or refusal under it, and the timings and tokens
  (model, browser, settle, page tokens, prompt with cached tokens, output). Below that is
  the shipped Thinking/Worked chip strip, reused as a segmented control over one panel:
  - **Thinking.** The step's reasoning. The fast loop runs with thinking off, so it says
    *No thinking — fast mode*. The B1 loop shows a short trace.
  - **Actions.** The exact call (`act({"commands":[…]})` or `click({"ref":"e318"})`), then
    what the host did with each command: resolved to a ref, re-found by role and name after
    a stale ref, refused with the gate's text, not run, page moved to a new address.
  - **Page it saw.** A capped excerpt of the indexed view the model chose from: the full
    page or only the changes, with the token count and a *show all* link.
- **Scenario panel.** *Fast loop* or *B1 loop*. *Check passes at done* or *Fails →
  extraction*: B1's `finish` always extracts, and fast only extracts when the check fails.
- **The pane cap.** An open step view lifts the shipped `.fb-steps` cap
  (`320px × font-scale`), via `:has()`. Otherwise the trace would be a scroll pane inside
  a scroll pane.

### Data the backend must carry (per step, on the browse `ToolOutput`'s `view`)

Today `BrowseStep` (`backend/src/jbrain/agent/browse.py`) holds one row per command. The
view groups them by `n`. Per step:

| Field | Source today |
|---|---|
| plain-language summary (*Clicked "Titusville with Epic XL"*) | built from `action` and the element's role and name, host-side |
| `url` and page title after the step | `url` exists. The title does not yet. |
| ok / refused / not-run, per command, with the note text | `ok` and `note` exist. *Not run* is only in the model-facing text. |
| `model_ms`, `browser_ms` (and the settle time) | exist |
| `prompt_tokens`, `cached_tokens`, `output_tokens`, `snapshot_tokens` | exist |
| reasoning text (empty in the fast loop) | **new**: not kept today |
| the raw tool call as the model sent it | partly: `args` is the per-command brief |
| per-command host result: resolved ref, re-found after a stale ref, refused with the reason, page moved | partly: the ref is in `args`. The re-find is not recorded. |
| page excerpt the model saw (capped, about 12 lines), full or changes-only | **new** |
| run-level: outcome, verified, the fact-check `describe()` text, extraction ran (time, tokens), elapsed | `BrowseRun` has outcome, verified and elapsed. The check text is on the done step's note. Extraction stats are **new**. |

Everything comes from the trace the run already builds. **No screenshot capture is needed.**
Keep the view payload bounded: excerpts are capped, and the full page text stays out.

## Inline tool marks — `inline-tool-marks.html` (binding)

When the model writes some text, calls tools, then writes more, the answer gets a small
mark at that point. *Searched the web ×2*, *Read a web page*, *Browsed a site · verified*.

- **Placement.** Each tool's `textOffset` (`frontend/src/agent/transcript.ts`, persisted as
  `text_offset`). A tool at offset 0 or at the very end of the text gets no mark, because
  the ledger already lists it.
- **Grouping.** Consecutive tools at the same offset share one mark. If the names fit, it
  shows them (*Searched the web ×2*, or up to three names). Otherwise it reads *N tools
  used*.
- **Tap.** Opens the bubble's Worked panel, expands the step (the first one, for a group),
  scrolls it into view and lights the step(s) briefly. The tapped mark takes the steel tint
  of the open Worked chip.
- **Quiet.** Text-3, no fill, a hairline border and smaller than the prose, so it reads as
  punctuation rather than a link. The mock opens on the tapped state, with the ledger at
  the browse step and its timeline. *Untapped* in the scenario panel shows the resting
  look.

## Rivals considered (not built)

- **B — filmstrip.** A's rail plus a page screenshot thumbnail per step, with the clicked
  element highlighted. It would need new screenshot capture and storage, and add about
  0.3–0.5 s per step. It could be added to A later if screenshots are ever wanted.
- **C — transcript.** A compact monospace log, one line per command. It is dense and
  precise, but harder to scan on a phone. A's *Actions* panel keeps that precision one
  tap down.
