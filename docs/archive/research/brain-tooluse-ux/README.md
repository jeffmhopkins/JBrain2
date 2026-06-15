# Full Brain tool-use UX — research + mockups

The owner's brief: *"In full brain mode, when I flip the card over to see the
assistant's tool use — I'm really not digging how the interface works. … I also
want more verbose logging over the tool use itself, as in maybe I can expand it
further to get individual tool results in the actual pulldown."*

Three usability researchers each took one lens. Their full reports:

- [`A-disclosure-patterns.md`](A-disclosure-patterns.md) — *what disclosure model
  reveals tool use beside an answer?* (information architecture)
- [`B-verbose-logging.md`](B-verbose-logging.md) — *how should an individual step
  expand to show its arguments-in and result-out, and how deep?* (the verbose ask)
- [`C-gesture-ergonomics.md`](C-gesture-ergonomics.md) — *is swipe-to-flip the
  right interaction on a one-thumb phone?* (gesture + accessibility)

## Where they converged

1. **Retire the 3D flip.** It makes the answer and its work mutually exclusive
   faces (you can't see a claim and its source at once), hides the most important
   reveal behind a 10px corner cue, and its horizontal swipe **collides** with the
   surface's Sessions/Proposals swipes on the same axis. No shipping AI product
   (ChatGPT, Claude.ai, Perplexity, Cursor) uses a flip — they all keep the answer
   visible and disclose work inline or in a panel.
2. **Disclose in place, by tap, with a real affordance.** A labelled ≥44px
   "Worked" control under the answer is honest status (DESIGN.md Principle 5), the
   sanctioned "inline expansion for row detail" paradigm, and frees the swipe axis.
3. **Make every step its own pulldown** (the verbose ask). A four-rung ladder:
   *answer → step list → step expanded (arguments + result) → raw payload + copy*.
   Per-step expand, **not** a global verbose firehose (the Feb-2026 Claude Code
   "too much noise" backlash is the anti-pattern). A `verbose` switch may pre-open
   all steps to the args+result rung, never to raw.
4. **Status on every rung, errors never hidden.** In-flight = steel shimmer;
   ok = quiet mark; error = rose mark + auto-expanded failure text, paired with the
   word "failed" (never color alone).

## The three interactive mockups

All three house the **same per-step drill-down** so the choice is about the
*container*, not the detail. Open them in a browser (self-contained HTML, real
dark tokens); each fixes the missing `prefers-reduced-motion` rule.

| # | Mockup | The idea | Best when |
|---|---|---|---|
| 1 | [`assistant-tooluse-1-inline-accordion.html`](../../mocks/assistant-tooluse-1-inline-accordion.html) | A 44px "Worked" button under the answer expands the steps **in place**; each step is a pulldown. **Consensus pick** (A·D1 + C·A + B). | The default — calm, discoverable, scales 1→many. |
| 2 | [`assistant-tooluse-2-trace-sheet.html`](../../mocks/assistant-tooluse-2-trace-sheet.html) | A persistent rail opens a **bottom sheet** with the full timeline + a `verbose` switch (A·D3 + C·B + B·alt). | Heavy/verbose runs; most one-thumb-friendly. |
| 3 | [`assistant-tooluse-3-citation-first.html`](../../mocks/assistant-tooluse-3-citation-first.html) | Provenance lives **in the prose** as tappable `[n]` footnotes + a sources tray; the step list is demoted to a toggle (A·D2). | Source-heavy answers; provenance-first reading. |

## The one wire change the verbose drill-down needs

The drill-down's *arguments* row needs data the frontend currently throws away:
`ToolCallEvent` carries `arguments: Record<string, unknown>`, but
`applyEvent`'s `tool_call` case in `frontend/src/agent/transcript.ts` builds only
`{ id, name }` and **drops `arguments`**. Threading it through is a small,
unit-testable reducer change plus one `ToolActivity.args` field — no backend work.
The *raw result* rung needs no new wire: `tool_result.summary` is already the raw
backend text for search/read tools (see `toolSummary.ts`). A truly separate
verbatim field and replay-persistence on `TranscriptTurn.tools[]` are optional and
deferrable.
