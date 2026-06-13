# B · Verbose / progressive per-tool logging & drill-down

Research lens for the Full Brain "Worked" panel. Companion to the broader
tool-use-UX research; this file owns **one question**:

> When a casual reader flips a bubble to its "Worked" panel and wants *more*,
> how should an individual tool step expand to reveal its detail — the arguments
> that went in, the raw result that came out — and how many levels of verbosity
> do we offer before it becomes a debug console?

The owner's own framing: *"maybe I can expand it further to get individual tool
results in the actual pulldown."* Today each step is a friendly label
(`Searched your notes`) plus tidy source cards. There is **no way to drill into a
single tool call** to read its arguments or its full raw result string. This is a
deliberate gap to close — but on a phone, for a non-developer, under the binding
DESIGN.md rules (one-thumb, ≥44px, near-monochrome, color=information,
honest-status-always-visible, lowercase-calm voice).

---

## 1. Lens framing — who is the reader, and what is "verbose" for?

The Worked panel serves two appetites that pull in opposite directions:

- **The glancer** (default): "did it actually look, and where?" — answered today
  by the step label + source cards. Verbose detail must never tax this reader.
- **The auditor** (occasional, same person on a different day): "show me exactly
  what it searched for and what came back, verbatim." This is the owner's ask.
  It is *trust tooling*, not decoration — and DESIGN.md elevates honest status to
  a principle, so it belongs in the product, not hidden behind a dev flag.

The whole industry has converged on **progressive disclosure** to reconcile these
([NN/g](https://www.nngroup.com/articles/progressive-disclosure/),
[Primer](https://primer.style/product/ui-patterns/progressive-disclosure/)): a
calm summary up front, detail one tap away, raw payload one more tap down. Our job
is to pick the *right number of rungs* and the *right affordance per rung* for a
phone. The data already exists on the wire for most of it (see §6) — the missing
piece is mostly UI plus a one-field reducer fix and one new result field.

A note on direction-of-travel risk: in Feb 2026 Claude Code **over-collapsed** —
it replaced filenames with `Read 3 files (ctrl+o to expand)` and drew loud
developer backlash that "searched for 2 patterns, read 3 files" conveys "no useful
information," and that an all-or-nothing verbose mode "is not a viable alternative,
there's way too much noise"
([devclass](https://www.devclass.com/development/2026/02/16/claude-code-gets-more-opaque-devs-want-more-transparency/4091233)).
The lesson for us: **detail must be reachable per-step and incrementally**, not
behind a single global firehose toggle.

---

## 2. Survey — how others present per-tool-call detail

### Claude.ai / Claude Code tool-use blocks
The chat product renders each tool use as an inline, collapsible block titled by
the tool, expanding to show the input and the returned content. Claude Code's CLI
is the cautionary tale above: a collapsed one-liner with a single expand chord
(`ctrl+o`) and a separate verbose mode. The takeaway is the *failure mode* —
collapsing too aggressively and offering only a global toggle frustrated exactly
the auditor we're designing for
([devclass](https://www.devclass.com/development/2026/02/16/claude-code-gets-more-opaque-devs-want-more-transparency/4091233)).
What worked: a per-action summary line with a **clear, consistent expand chord**,
and surfacing the *most decision-relevant* field (file paths) when expanded rather
than a raw dump.

### Vercel AI SDK UI — the cleanest state model to borrow
Tool parts carry an explicit `state`:
**`input-streaming` → `input-available` → `output-available` / `output-error`**
(plus `approval-requested` for tools needing authorization)
([ai-sdk.dev](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-with-tool-calling)).
The guidance is to render *per state*: a "preparing…" placeholder while inputs
stream, the finalized input once available, the result when it lands, and
`Error: {errorText}` on failure. This is precisely the in-flight / ok / error
ladder we need, named and battle-tested. We don't get streamed *arguments* on our
wire (they arrive whole in `tool_call`), so we collapse to three live states:
**in-flight → ok → error**.

### LangSmith — the "raw payload" rung done right
The trace viewer renders inputs/outputs as an **expandable tree**: nodes show
field names, array counts `(3)`, indices `[0]`/`[-1]`, and inline preview values
for strings/numbers; arrows expand nested objects; a detail panel shows exact
inputs/outputs and metrics
([LangChain docs](https://docs.langchain.com/langsmith/configure-input-output-preview),
[deep dive](https://medium.com/@aviadr1/langsmith-tracing-deep-dive-beyond-the-docs-75016c91f747)).
This is the gold standard for *developer* JSON inspection — and it's **too much**
for our deepest phone rung. We borrow the *idea* (key/value with inline previews,
collapsed nesting) but flatten it to a one-level key/value list, not a recursive
tree.

### OpenAI Agents/Responses traces
The Traces dashboard records a run as LLM generations, **tool calls with their
arguments**, handoffs, and outputs, viewable per-workflow
([OpenAI Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/),
[MLflow OpenAI tracing](https://mlflow.org/docs/latest/genai/tracing/integrations/listing/openai/)).
Same pattern: a step list where each step opens to args + output. Confirms the
**args-in / output-out pairing** as the universal unit of tool transparency.

### Cursor / Cline tool cards
Cursor's **Compact mode** collapses diffs by default, hides tool icons, and
auto-hides input for long sessions, with expand/collapse-all in a `⋯` menu
([Cursor changelog 1.4](https://cursor.com/changelog/1-4),
[3.0](https://cursor.com/changelog/3-0)). Cline shows "explicit tool calls" and
terminal output as reviewable cards prized for auditability
([Cline](https://github.com/cline/cline)). Lesson: a **per-step collapsed card**
that opens to detail, plus an optional **expand-all** for the power user — but
defaulting to collapsed.

### Perplexity Pro Search / Deep Research
Shows the plan executing step-by-step with **expandable sections; clicking an
individual step reveals more detail**; citations hover to source snippets
([LangChain case study](https://www.langchain.com/breakoutagents/perplexity)).
Notably, Perplexity found users tolerate latency *better* when intermediate steps
are visible — directly relevant to our in-flight treatment (§5). This is the
consumer-grade precedent: per-step expand, not a global dump.

### OpenTelemetry GenAI semantic conventions
Standardizes tool-execution spans carrying **inputs and outputs** as span content;
viewers (Arize Phoenix, etc.) render them as expandable span detail
([OTel GenAI agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/),
[Phoenix](https://arize.com/docs/phoenix/tracing/concepts-tracing/translating-conventions)).
Reinforces the **(name, args, result, ok, timing)** tuple as the canonical
tool-call record — a useful checklist for what to *thread through the wire* (§6).

**Convergent finding.** Everyone uses the same unit — *name + arguments-in +
result-out + status* — and the same pattern — *summary line that expands to
detail*. The only axis of disagreement is **depth**: dev tools (LangSmith, OTel)
go to recursive raw JSON; consumer tools (Perplexity, Claude.ai chat) stop at a
readable summary. JBrain2 is consumer-first but owner-is-also-builder, so we want
a ladder that **starts consumer and bottoms out at raw**, with the raw rung kept
out of the way.

---

## 3. The progressive-disclosure ladder (recommendation)

Four rungs. Rung 0–1 exist today; this lens adds rung 2 and 3. Each rung is one
tap deeper; the reader never has to descend to understand the rung above.

| Rung | Name | What it shows | Affordance |
|---|---|---|---|
| **0** | Flip card front | The answer + a `2 tools` cue | swipe / tap cue (exists) |
| **1** | Worked panel | Step list: glyph · friendly label · status · per-step count; source cards under read/search steps | flip to back (exists) |
| **2** | **Step expanded** | **arguments (key/value) + result summary**, per step | tap the step row → inline accordion |
| **3** | **Raw payload** | full raw result string (and raw args JSON), monospace, copyable | a `raw` toggle inside rung 2 |

Key decisions:

- **Per-step expand, not a global verbose switch.** Each step row becomes its own
  disclosure (tap to open rung 2). This is the direct answer to "expand it further
  to get individual tool results in the actual pulldown." It sidesteps the
  Claude-Code firehose complaint: you open *the one step you doubt*, not all of
  them. A **`verbose` toggle still exists** but only as a convenience that
  *pre-opens all steps to rung 2* (never rung 3) — equivalent to Cursor's
  expand-all. Default off; remembered per session.
- **Rung 2 is the workhorse.** Args + result-summary is what 95% of the auditor's
  curiosity wants ("what did it search *for*?"). It must read like prose-adjacent
  data, not a console.
- **Rung 3 is opt-in and quarantined.** The verbatim raw result/args live behind
  a small `raw` text toggle inside the open step, rendered monospace, clamped,
  with copy. This honors "honest status always visible" (the truth is reachable)
  without letting UUID soup leak into rung 1 the way today's "Before" mock does.
- **Status rides every rung.** A per-step status mark (ok / error / in-flight)
  appears on the rung-1 row itself, so failure is visible *before* you expand —
  paired with text per the a11y rule, never color alone.

---

## 4. Rendering args + raw result on a phone

### Arguments — flat key/value, not a tree
`tool_call.arguments` is `Record<string, unknown>`. In practice these are shallow
(`{query: "born", domain: "general", limit: 5}`). Render a **one-level key/value
list**, borrowing LangSmith's inline-preview idea but *not* its recursion:

- key in `--text-3` (12px), value in `--text-2` (12–13px), monospace value font
  so a UUID or date is legible and copyable.
- string values rendered with surrounding quotes stripped for calm; numbers/bools
  as-is; an array shows `[3]` with the first item inline (`["born", …]`); a nested
  object shows `{…}` collapsed — tapping it reveals it in the **rung-3 raw block**
  rather than expanding inline (keeps rung 2 strictly one level deep).
- long string values clamp to 2 lines with a tap-to-grow, matching the Analysis
  tab's OCR-inset pattern already settled in DESIGN.md ("show all N lines" grows
  in place).

### Result — summary at rung 2, verbatim at rung 3
- **Rung 2** shows the human `summary` we already have (for `search`, the parsed
  source cards; for others, the summary string clamped to ~3 lines).
- **Rung 3** (`raw` toggle) shows the **verbatim result string** in a quiet
  monospace inset (`--surface-2`, 11–12px, `white-space:pre-wrap;
  word-break:break-word`), clamped to ~8 lines with **"show all N lines"** growing
  in place. This reuses the exact treatment DESIGN.md already blessed for image
  OCR insets — no new paradigm.
- **Copy.** A 44px copy-icon button top-right of the raw inset copies the full raw
  string (`navigator.clipboard.writeText`), confirmed with the standard bottom
  toast ("copied"). Copy is the auditor's escape hatch to paste into a note or a
  bug report — and it satisfies "reachable truth" even when the inset is clamped.

### Markup sketch (illustrative — do NOT build from this verbatim)

```
<div class="tw-step" role="button" aria-expanded="false">   <!-- rung 1 row, now tappable -->
  <Glyph/> Searched your notes
  <span class="tw-status ok">·</span>          <!-- ok dot, paired with sr-only "ok" -->
  <span class="tw-count">4 results</span>
  <Caret/>                                       <!-- rotates on open -->
</div>

<div class="tw-step-detail">                      <!-- rung 2, accordion -->
  <dl class="tw-args">                            <!-- arguments in -->
    <dt>query</dt><dd>born</dd>
    <dt>domain</dt><dd>general</dd>
    <dt>limit</dt><dd>5</dd>
  </dl>
  <div class="tw-result">                          <!-- result summary -->
    … existing source cards / clamped summary …
  </div>
  <button class="tw-raw-toggle">raw</button>       <!-- rung 3 trigger -->
  <pre class="tw-raw" hidden>                       <!-- rung 3, clamped + copy -->
- note 1a2f [general] 2026-06-12: I was born …
    <button class="tw-copy" aria-label="copy raw result"><CopyGlyph/></button>
  </pre>
</div>
```

Spacing/тokens all from DESIGN.md: caret rotates 90° (matches the "A · Collapsed"
mock's `.caret` behavior), insets are `--surface-2`, hairline `--border`, 12px
input radius, tap targets ≥44px including padding even where the visual row is
36px (compact-row rule).

---

## 5. Error and in-flight treatment

- **In-flight** (`ok === undefined`): the step row shows the calm pulsing mark
  already used by the bottom status line (`--steel`, shimmer), label reads present
  tense (`Searching your notes…`). No expand caret until the result lands — there
  is nothing to drill into yet. (Perplexity's finding: showing the live step makes
  the wait feel shorter.)
- **Success** (`ok === true`): quiet `--text-3`/`--green` status mark + the count;
  expandable.
- **Error** (`ok === false`): the row takes a **`--rose` (danger) status mark and
  a rose hairline**, label stays factual (`Search failed`), and the row is
  **auto-expanded to rung 2** showing the error text from the result `summary`
  (Vercel's `output-error` rendering, `Error: {text}`-style but in our
  lowercase-calm voice). Errors are the one case where detail is *not* hidden — an
  auditor's most important moment. Color is paired with the word "failed" per a11y.

This maps cleanly onto Vercel's `input-available → output-available /
output-error` and onto our existing three-valued `ok?: boolean`.

---

## 6. Minimal data / wire changes (context for the owner — kept brief)

Most of rung 2/3 is already on the wire; two small gaps:

1. **Arguments are thrown away.** `ToolCallEvent` carries
   `arguments: Record<string,unknown>` (types.ts) but `applyEvent`'s `tool_call`
   case builds `{ id, name }` and **drops `arguments`** (transcript.ts:59). Fix:
   add `args?: Record<string, unknown>` to `ToolActivity` and thread
   `event.arguments` through. Pure-reducer change, unit-testable, no backend work.
2. **No verbatim raw result on the wire for rung 3.** `tool_result.summary` is a
   *human* string (already shown at rung 2). Rung 3's "verbatim" is, for `search`/
   `read_note`, *already* that summary (the source-parsing in toolSummary.ts proves
   it's the raw backend text). So **rung 3 can ship with zero backend change** by
   treating `summary` as the raw payload for tools that emit raw text, and simply
   not offering a `raw` toggle for tools whose summary is already friendly. If we
   later want a *truly* separate verbatim field (e.g. structured tool JSON), add an
   optional `raw?: string` (or `result_json?`) to `ToolResultEvent` + the persisted
   `TranscriptTurn.tools[]` so it replays on session reopen. Defer this.
3. **Persistence for replay.** If args should survive a session reopen, add
   `args` to `TranscriptTurn.tools[]` (types.ts:99). Optional; the live-stream
   experience works without it.

Net: **rung 2 (args) = one reducer field + one type field; rung 3 (raw) = pure UI
over existing `summary`.** A richer verbatim result field is a *later, optional*
backend change, not a blocker.

---

## 7. RECOMMENDATION

**Make every step row in the Worked panel its own progressive disclosure.** Tap a
step → it accordions open (rung 2) to show **arguments as a flat key/value list**
and the **result summary** (existing source cards / clamped summary). Inside the
open step, a small **`raw` toggle** reveals (rung 3) the **verbatim result string**
in a clamped monospace inset with a **44px copy button** and "show all N lines".
Per-step **status marks** (in-flight shimmer / ok / rose-error) sit on the rung-1
row; **error rows auto-open to rung 2** with the failure text. A session-scoped
**`verbose` toggle** in the panel header pre-opens all steps to rung 2 (never rung
3) for the power user — off by default.

This is the consumer-to-raw ladder the whole field converged on (Perplexity/Claude
chat at the top, LangSmith/OTel at the bottom), it directly answers the owner's
"expand it further to get individual tool results in the actual pulldown," it
reuses paradigms already settled in DESIGN.md (clamped monospace inset + show-all,
caret-rotate disclosure, toast confirm, status-mark-with-text), and its data cost
is one reducer field plus one type field.

**Alternative A — Global verbose mode only (rejected as primary).** A single
panel-level toggle that flips every step from "friendly" to "raw args + raw
result," like Claude Code's verbose mode. Simpler to build, but it's the exact
all-or-nothing pattern developers revolted against ("way too much noise") — it
buries the one step you care about under nine you don't. Keep it *only* as the
convenience layer above per-step expand, never as the sole path.

**Alternative B — A dedicated full-screen "trace" sheet per turn.** A `⋯ → view
trace` on the Worked head opens a bottom `<Sheet>` (the settled phone modal) with
the LangSmith-style step list, each expandable to args+result+raw, with copy. This
keeps the inline Worked panel pristine (rung 1 only) and gives the auditor a roomy,
scrollable surface for long runs. Strong second choice and arguably *better for
very long tool runs* — could ship as a follow-on to the inline rung-2/3 work,
reached from the same `verbose`/`raw` intent. Worth mocking alongside the inline
accordion.

Build the inline per-step accordion (rung 2 + rung 3) as the primary; mock the
full-screen trace sheet (Alt B) as the rival in the variant round.

---

### Sources

- [NN/g — Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [Primer — Progressive disclosure UI pattern](https://primer.style/product/ui-patterns/progressive-disclosure/)
- [devclass — Claude Code more opaque, devs want transparency](https://www.devclass.com/development/2026/02/16/claude-code-gets-more-opaque-devs-want-more-transparency/4091233)
- [Vercel AI SDK UI — Chatbot Tool Usage (tool states)](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-with-tool-calling)
- [LangChain docs — Configure run input/output preview](https://docs.langchain.com/langsmith/configure-input-output-preview)
- [LangSmith tracing deep dive](https://medium.com/@aviadr1/langsmith-tracing-deep-dive-beyond-the-docs-75016c91f747)
- [OpenAI Agents SDK — Tracing](https://openai.github.io/openai-agents-python/tracing/)
- [MLflow — Tracing OpenAI](https://mlflow.org/docs/latest/genai/tracing/integrations/listing/openai/)
- [Cursor changelog 1.4 — agent tools & usage visibility](https://cursor.com/changelog/1-4)
- [Cursor changelog 3.0 — new interface / compact mode](https://cursor.com/changelog/3-0)
- [Cline — autonomous coding agent](https://github.com/cline/cline)
- [LangChain — Perplexity Pro Search case study](https://www.langchain.com/breakoutagents/perplexity)
- [OpenTelemetry — GenAI agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/)
- [Arize Phoenix — Translating semantic conventions](https://arize.com/docs/phoenix/tracing/concepts-tracing/translating-conventions)
