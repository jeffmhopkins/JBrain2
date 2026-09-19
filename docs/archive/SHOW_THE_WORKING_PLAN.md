# Show the working — the code-run surfaces, and the question that reaches the chat

> **Status:** Shipped · **Last verified:** 2026-09-19 · **Waves:** W1✅ W1b✅ W2✅ W3✅ W4✅

## Thesis

`calculate` and `run_python` shipped (`../archive/EXACT_MATH_TOOLS_PLAN.md`) and closed the
number-invention failure class *mechanically* — the arithmetic is now done by something that
cannot be fluent and wrong. But they shipped **with no view of their own**: the code that
produced a number is persisted on the turn and shown nowhere, so the one tool whose entire
value is that its answer can be **checked** produced answers you still had to take on trust.

This plan builds the surfaces that finish that job, plus the one that prevents the same
failure one level up:

- **You can see where a number came from** — G, the cited computation.
- **You can see what ran, in what order, and what failed** — H, the Worked-panel ledger.
- **It asks instead of guessing** — the question surface, reaching the conversation.

The three are one idea: *the model's confidence is not evidence.* Two of them make its work
inspectable after the fact; the third stops it manufacturing work from a premise it invented.

## What main landed while this branch was in the GUI gate

Merging main before writing the first line of code changed one of the three waves
substantially, and the honest thing is to say so rather than build a second version of
something that ships:

**`ask_owner` exists** (`agent/asktools.py`, `agent/tools/ask_owner.tool` v4). A note
conversation's agent asks a batched **set** of questions, each with the question, what it
blocks in the owner's words, and candidates; the questions become durable **inside the ask
transaction** (not at the turn seam, because the reply path reads them back); the
conversation goes to `waiting_on_owner`; and the turn ends — via `ToolOutput(halt=...)`
and the loop, deliberately *not* by asking the model in prose to stop. One open set at a
time; a second ask is refused and reports the set that is open.

**`QuestionBlock` exists** (`agent/QuestionBlock.tsx`, `.fb-q-*`), and it already renders
the thing the ask-user gate was called to settle — one row per question, *blocks · why*,
tappable candidates, and a text field.

**Two of that component's decisions are better than the ones the gate reached,** and this
plan adopts them rather than arguing:

- **It cannot start a turn.** Selecting a candidate is local state; nothing posts. The
  submit is the composer's, because three answers that each posted their own turn would be
  three turns, three clarification blocks and three re-reads — the exact cost a *batched*
  ask exists to remove. Mock A posts its own turn, `InlineProposal`-style. **A is wrong on
  this point and the shipped component is right.**
- **A question set, not a question.** Everything the pass is stuck on goes in one call.

**What the gate got right is already in the shipped code too.** The owner's condition on
variant A — *a multiple-choice question must always let me write a custom answer* — is
satisfied by `QuestionRow`: whenever a row has candidates and is not frozen, the component
appends **"Something else"**, which reveals the same text field. The component appends it;
the model does not supply it and cannot suppress it. That is the invariant, already
enforced where it belongs.

**So `ask_user` is not a tool to build.** It is `ask_owner` + `QuestionBlock` reaching a
*conversation* instead of only a note thread — see W4, which is now much smaller and much
better founded than the one the gate implied.

## What is already true, and therefore not in scope

Reading the live code shrank this plan considerably. Recorded here so no wave re-litigates it:

- **The tools work.** `agent/mathtools.py`, `agent/pythontools.py`, `pysandbox/`. No backend
  computation work here — this is the surface over what they already return.
- **The panel exists.** `StepRow` / `.fb-step-*` in `FullBrainSurface.tsx` is where every tool
  already reports, from the settled `../mocks/assistant-tooluse-1-inline-accordion.html`. H is
  **not a new panel and not a new disclosure** — it is what each tool renders into the one that
  is already there.
- **The ledger's slot exists, and main has started filling it.** `.fb-step-cnt` is styled and
  sits exactly where a result belongs, between the status dot and the caret. When this gate
  opened it was populated by **two** tools, `search` and `web_search`; main has since added the
  entity-write and resolve phrases (`fbw-cnt fbw-*`), for the reason this plan gives — *"the one
  call that creates the owner's records read as a call that did nothing."* So the ledger is not
  a new idea to sell; it is an established one to **finish**, and main has already set the
  convention (a modifier class per result kind) that D1's `.fb-step-cnt.res` follows. The
  remaining work is that the slot is filled by four hardcoded per-tool branches rather than by
  every tool.
- **The view registry exists.** `views/registry.tsx` maps a `view` name to a first-party
  component and renders **nothing** for an unknown name (DESIGN.md invariant #1/#9). A new
  tool-view is a deliberate edit to that map, like adding a tool.
- **The whole ask-and-answer path exists** — `ask_owner`, the loop's `halt`, `QuestionBlock`,
  and the reply that consumes an open set. It is bound to a note thread, and only to a note
  thread. W4 is that binding, not a rebuild.
- **Per-call duration is already carried and persisted.** `ToolResultEvent.duration_ms`.

## The settled surfaces

The GUI gate is closed on all three. The mocks are the binding spec, built on the shipped
stylesheet (tokens and `.fb-*` / `.ip-*` rules pasted verbatim), so a row that looks wrong in a
mock looks wrong in the app.

| | Mock | Answers | Entry |
|---|---|---|---|
| **G** | `../mocks/code-run/g-cited-floating.html` | *"Where did **this number** come from?"* | an `ƒn` marker on the number, in the prose |
| **H** | `../mocks/code-run/h-worked-ledger.html` | *"What ran, in what order, and what failed?"* | the Worked panel, collapsed to one button |
| **A** | `../mocks/ask-user/a-inline-card.html` — **partly superseded**, see above | *"Which one did you mean?"* | a question card in the transcript |

H is what closes G's hole: **a call that failed produced no number, so it earns no marker and
is invisible in G.** The panel is the only place it ever appears, and it opens itself there the
way a failed step already does.

## Design decisions

The load-bearing calls, made here so a wave does not have to make them under time pressure.

### D1 — the ledger's right-hand side is authored by the handler, not derived in the client

`ToolResultEvent` gains `result_brief: str = ""` — a short, already-truncated string the tool's
handler supplies: `3 notes`, `5.15% · 30yr`, `Thu 18 Sep, 20:54`, `2568`.

**Why not derive it in `toolSummary.ts`.** Only the handler knows which part of its own result
was the answer. A client deriving `5.15% · 30yr` from a note read would have to parse the
model-facing summary text — which is exactly the brittle fallback parsing that already exists
for `search`/`read_note` and that the structured `sources` field was added to replace. Making
the client guess the salient result is the same mistake one layer down.

It is a **separate field, not `summary`**: `summary` is the model-facing text and is free to be
long, sectioned, and full of things the owner should not have to read.

Defaulted to `""`, so every existing construction and every stored turn without it still loads —
the pattern `duration_ms` already set.

**And it is enforced, not requested.** `test_tool_step_polish.py` already fails a `.tool` that
ships as a raw snake_case row with no visible target. It gains the mirror rule: **every tool in
the roster declares a result-brief policy** — a handler that fills it, or a named entry in an
explicit exemption list with a reason. That is how *"every row carries a right-hand side"*
becomes a property of the codebase instead of an aspiration in a README. A tool that cannot
fill it is a tool whose result was never worth surfacing, and saying so in a list is cheap.

**One rule governs the row:** the argument truncates and **the result never does**. The answer
is what you came for, so it is the half that survives a narrow screen.

### D2 — a step renders its tool's registered view, which resolves H's open question

H's README left one real build question: `run_python`'s code listing does not fit `StepRow`'s
fixed detail cascade (args → error / sources / entities / web sources / summary → raw), so
either add a `code` rung to the cascade, or let a step render its tool's registered view inline.

**Take the second.** It is barely larger and it is the one that makes G and H render *the same
component* rather than two components that must be kept saying the same thing.

It is also less new machinery than it sounds: `ToolViewEvent` is already streamed keyed by
`tool_call_id`, which is the same id the step carries. The association already exists; nothing
today joins on it. The cascade gains **one rung** that renders `<ToolView>` when the step has a
payload, above the raw rung.

**The risk, stated:** a view designed for the bubble may be too big for a step. `ViewPayload`
already carries `surface` (`inline | sheet | dialog`), and `ToolView` already emits it as a
class — so a component can render compactly in a step without a second component or a prop
drilled through. Any view that renders badly in a step is a component bug with a visible test,
not a reason to fork the design.

### D3 — `ƒn` is a marker namespace; the model authors the marker and nothing else

G's marker follows the shipped `[^n]` source-citation mechanism in `markdown.tsx` — the model
writes a marker in prose, the renderer resolves it positionally against targets the *surface*
built from the turn's tool calls.

**A separate namespace from `[^n]`, deliberately.** A computation is not a source note. Sharing
one numbering would put "this came from your note" and "this came from arithmetic I did" behind
the same glyph, and those are different claims with different failure modes.

**The model authors the marker and never the popover.** The popover's contents — expression,
code, stdout, result, duration, containment — are read from the persisted call, not from
anything the model wrote at render time. A marker that resolves to no call **renders as nothing**,
the same rule `ToolView` applies to an unknown view name. This is what keeps G from becoming a
way for model output to assert a computation happened that did not.

### D4 — the question surface is widened, not duplicated

`ask_owner` is bound to `note_ingest` by allowlist, and `FullBrainSurface` feeds
`QuestionBlock` only inside a note thread (*"Absent on every turn that asked nothing, which
is every turn outside a note thread"*). Everything else it needs already works.

**So W4 widens the binding and builds no second question system.** The tool, its halt, its
durability-at-ask-time, its one-open-set rule, the block, the candidates and the written
escape are all reused as they stand.

**The one thing that does not carry over is the store.** `ask_owner`'s questions hang off a
**note conversation** — `waiting_on_owner` is a note-conversation state, and the reply path
consumes an open set against that row. A `/chat` conversation has no such row. W4's real
work is that binding, and it is the wave's one genuine design question rather than a
formality (see O1 below).

### D5 — a conversational answer mints no note

The ingest plan's D7 makes an owner's answer mint an owner-authored note so the graph stays
re-derivable, and in a note thread the answer is appended to the note as source text and
re-ingested. That is right *there*: the question was about a note, and the answer is a
correction to it.

Applied to a conversation it is wrong. *"The cardiology one"* is not a fact about the world;
it is a disambiguation scoped to the turn that asked, and minting it would fill the corpus
with answers that mean nothing away from their question. So: **a conversational answer
returns to the model as a tool result and mints nothing.** An answer that asserts a fact
goes through the agent's normal commit path, where it mints a note like any other fact —
because that is a claim the graph should carry, not because it arrived as an answer.

This is a boundary, not an exception to D7: D7 governs a note conversation's answers, which
keep behaving exactly as they do today. **Ratified by the owner** (O2).

### D6 — the written escape gets a test that names it, because it is load-bearing

`QuestionRow` already appends **"Something else"** to any row with candidates. What it does
not have is a test that says *this is a rule*, so the next refactor that makes the escape
conditional — on candidate count, on a model flag, on "the model said these are exhaustive"
— would pass CI.

A closed set of choices is a new way to get a wrong answer: if none of the options is right,
the owner picks one anyway, and a confident wrong answer is worse than the guess the tool
exists to prevent. That is worth one test asserting that a row built from candidates alone
still offers the written escape, and that the escape is not derived from anything the model
sent.

## Waves

### W1 ✅ — the ledger

Every row gets a right-hand side.

- **`result_brief`** on `ToolOutput` and `ToolResultEvent`, through `_Dispatched`, both event
  seams, the live stream (`transcript.ts`), the persisted step (`transcript_accumulator.py`,
  `loop._step`) and its replay (`useFullBrain.ts`), into `ToolStep.result`.
- **`stepLedger.ts`** decides the phrase, and `StepRow`'s four hardcoded per-tool branches —
  the `search` / `web_search` counts and the two `fbw-cnt` write phrases — become one call.
  The order is the design: an authored answer beats a count, because a count is what the
  client could have worked out and the answer is what only the tool knew.
- **A failed call now says `failed` for every tool**, not just the writes. The status dot
  already said it; nothing said it in the column the eye scans down, which is what makes a
  failed call findable in a strip of twelve. That one rule covers all 127 tools.
- **`calculate` and `run_python` author their answers** — the reason this plan exists. Both
  read the answer off a VALUE rather than off the text they just built: `evaluate` returns a
  `Rendered` str subclass carrying `.brief` (the same idiom `ToolOutput` itself uses, so all
  47 existing tests keep treating it as a str), and `run_brief` reads `Ran`'s fields. Parsing
  one's own rendered prose would be the exact mistake `result_brief` exists to stop, one layer
  further down.
- **Two CSS rules**: `.fb-step-cnt.fbl-res` (a computed answer, `--ok` and monospace) and
  `.fb-step-cnt.fbl-bad`. Plus `flex: none` on the column, so on a narrow row the label and
  the argument give way first — the argument is what you asked, the result is what you came
  for.
- **The gate**: `test_tool_step_polish.py` requires every tool to be in exactly one of
  `_AUTHORS_BRIEF` or `_NO_AUTHORED_BRIEF`, so a new tool cannot ship without deciding, and
  a second test checks that the tools claiming to author an answer still pass `result_brief=`
  (verified by removing the argument and watching it go red).

**The honest gap W1 left, now closed.** W1 shipped the mechanism with 2 of 127 handlers
authoring an answer; W1b finished the sweep.

### W1b ✅ — the long tail authors its answers

**108 of 127 tools author a brief; the other 19 render from structured fields; none render
blank.** That last clause is the wave's actual acceptance test, and it is now a gate rather
than a claim.

The sweep was chosen by what would otherwise be EMPTY, which turned up the thing the plan had
not noticed: **a `view` does not fill the ledger.** `stepLedger` reads `sources`, `facts`,
`entities` and `web_sources` — not `view` — so every view-only tool (the charts, the canvas,
the crops, the plan, the video and stream analyses, the rendered HTML) had a rich card in the
bubble and nothing at all in the column the eye scans.

Where a phrasing could mislead, the phrasing is the work:

- **A staged write says it is staged.** `manage_appointment`, `save_place`, `prefs_write`,
  `propose_merge`, `merge_entities`, `propose_correction`, `make_intake_link`, the connector
  lookups and `remember` all read *staged, not applied* / *not merged* / *not saved* /
  *nothing sent*. `merge_entities`' own result text ends "do not say they are merged"; the row
  is held to the same rule, and it is the surface the owner reads without opening anything.
- **A capped count keeps its `+`.** A bare number where `gmail_count` hit its cap would report
  a cap as a total.
- **A full-replace write reports the size CHANGE** (`archivist_memory_write`, `scratch_write`),
  because a silent shrink is how work disappears.
- **`render_html` says `rendered, clipped`** — a clipped render looks complete in the image.
- **Attacker-controlled sources get a count, never a line.** `aprs_recent`, `jmolt_observe` and
  the `moltbook` read umbrella put how much came back in the row; the text itself is only safe
  inside the data/instruction fence its result already carries.
- **A fire-and-forget run says so** (`analyze_stream`, `deepest_research`), so a turn that
  ended on purpose does not read like a call that did nothing.

**Reuse over reinvention.** The brief calls the module's own renderer — `_when`,
`_place_phrase`, `_format_duration`, `_STATUS_LABEL`, `pace.headroom` — rather than growing a
second one free to disagree with the sentence beside it. Where the answer had to come out of a
formatter, the formatter returns it (`Rendered.brief`, `_signal_brief`, `_deliver`'s tuple)
instead of the handler re-parsing prose it had just built.

**The second gate is the one that matters.** `_STRUCTURAL_RESULT` says "this row is already
filled", which is a claim about a handler, not a note to self — so it is checked: the handler's
module must surface one of the four fields `stepLedger` phrases from, with the roster files
(`agents.py`, `spawn.py`, `toolregistry.py`) excluded, since either would otherwise vouch for
every tool in the repo. **It immediately caught three tools that would have rendered blank
behind a label saying they were fine** — `weather_history`, the `moltbook` umbrella and
`merge_entities`. Without that assertion the set becomes the place a tool goes when nobody
could think what its answer was.

### W2 ✅ — the `code_run` view, rendered in the panel

- **One component, two tools.** `code_run` (`views/codeRun.tsx`) renders a run's code,
  stdout, stderr, result and verdict; `calculate` fills the same slots with an expression in
  place of the code and its exact/decimal pair in place of the output. They are the same act,
  and a second component would be a second place for them to disagree.
- **The component owns the highlighting.** `language` selects a closed set of token classes
  the stylesheet colours — the model fills data-only slots and authors no span, colour or URL
  (DESIGN.md #1/#9). The code is TOKENIZED into React nodes rather than rendered as HTML, so
  there is no path from tool output to the DOM as markup. A test feeds the view a snippet
  containing `<span class="k">` and an `<img onerror>` and asserts both arrive as text.
  **Output is never highlighted**, because colouring a program's own stdout would let it
  print something that reads as syntax.
- **`ok` is whether the code RAN, not whether it was right**, so the chip says *ran clean* or
  *raised* and never *correct*.
- **The containment is stated and CHECKED.** `SANDBOX_SEALS` are the chips the owner is shown
  under every run, and `test_pysandbox_server.py` ties each one to the declaration that makes
  it true — `internal: true` for *no network*, `read_only` + the tmpfs for *scratch only*,
  `USER nobody` + `cap_drop: ALL` + `no-new-privileges` for *no root*. Verified by deleting
  the `read_only` block and watching it go red. A reassurance nobody checks is the kind of
  claim that outlives the thing that made it true. **`calculate` does not borrow those
  chips** — it runs in-process on a restricted AST and never enters the container, so it says
  *exact arithmetic · no code executed*.
- **D2, implemented.** `StepRow`'s cascade gains one rung that renders the step's own
  registered view, above the fixed args → sources → summary ladder. A `STEP_VIEWS` set in the
  registry decides which views live in a step rather than the bubble, and BOTH sides read it —
  the bubble filters them out, the step renders them — so a view can never appear twice or
  nowhere. The view rides its `tool_call_id` onto its step on the live path and replays off
  the persisted step, which already carried it.

### W3 ✅ — G, the cited computation

- **The marker is `[=n]`**, an ASCII sibling of `[^n]` in its own namespace, rendered as a
  small `ƒn` chip. Separate numbering on purpose (D3): `[^1]` means *this came from your
  note* and `[=1]` means *this came from arithmetic I did* — different claims with different
  failure modes, so one numbering would hide which was which.
- **The model authors the marker and nothing else.** `[=n]` names a POSITION; the popover's
  contents are read from the persisted call, built by the surface from the turn's own steps.
  **A marker that resolves to no call renders as plain text** — the same rule `ToolView`
  applies to an unknown view name, and what stops a marker asserting a computation that never
  happened. Tested directly.
- **The popover is F's, unchanged**: fixed-position, clamped to the viewport on both axes,
  flipping above the marker when there is no room below, and closing on Escape, on an outside
  tap, or on any scroll — a panel pointing at a number that has moved is worse than no panel.
- **It opens compact and expands in place**, the expansion capped at 46vh and scrolling. That
  is the answer to the one flaw recorded against F (*"a popover is a poor place for eight
  lines of code"*): small content stays a popover, large content stops being one. The expanded
  body renders **the same `code_run` component the Worked step does**, which is what D2 bought
  — one component, two entry points, no second place for them to disagree.
- **Prompt guidance** in the three prompts that carry the arithmetic block (`system` v10,
  `jerv` v50, `archivist` v9), including the clutter rule the gate flagged: mark the FIRST
  computed figure in a sentence and leave the rest, because three markers in one clause reads
  as clutter rather than rigour. That was the live-use risk G could not settle on a mock, and
  it is cheap to reverse — a prompt change, not a rebuild.

### W4 ✅ — the question reaches the conversation

Not a new tool and not a new card. It turned out to be **smaller than the plan expected**,
because the frontend was already generic: `FullBrainSurface` builds its `ask` from the
MESSAGE'S OWN STEPS (`asked.askStep`), which never read a conversation row. The comment
saying `ask` is "absent on every turn outside a note thread" described what `ask_owner`'s
binding allowed, not a restriction in the surface. **No frontend change was needed to render
a question in a plain chat.**

So the wave is three things:

- **The handler stops refusing a conversation.** With no note conversation behind the
  session, `ask_owner` records the set on the TURN — `recorded_args` puts the questions and
  their server-minted ids on the transcript step, which is the only thing the question block
  is built from — and halts exactly as it does in a thread. It writes no ledger row and
  flips no state, which is asserted by exploding if it tries.
- **One open set still holds, with no state to keep it.** The turn ENDS, so nothing can ask
  again until the owner replies, and `askStep` renders the LAST recorded ask on a turn — so
  a model emitting two calls in one message leaves one block, not two.
- **The grant.** `curator` (via `extra_tools`, past the NEVER_DEFAULT gate) and `teacher`,
  whose case is the same one level up: a tutor that guesses which reading a student meant
  teaches the guess. Prompt guidance in both (`system` v11, `teacher` v3), each carrying the
  bar — ask only what you cannot settle, never to have a reading approved, never as a hedge.

**Never `intake`, and now mechanically.** Three independent assertions fail on that grant,
and the test is written over `NON_OWNER_PERSONAS` rather than the one persona that exists
today, because W4 is precisely the change that could widen it by accident. Verified by
granting it and watching all three go red. The reasoning is `agents.py`'s own: an intake
question is model-authored from stranger-controlled text, reaches the owner in his own
agent's voice after the review step that is the whole trust boundary, and his typed answer
is appended as source and re-ingested — an unreviewed inbound message channel with a
stranger at its source.

**D6, the written escape, is now a rule.** `QuestionRow` already appended *"Something else"*;
what it lacked was a test saying that is a rule rather than behaviour that happens to be
there. Two now pin it: the escape appears on EVERY candidate row whatever the model supplied,
and a candidate the model LABELS "Something else" does not stand in for it. Verified by
making the escape conditional on candidate count and watching both fail. Without them, a
refactor keyed on "the model said these are exhaustive" would pass CI — and a closed set of
choices is a new way to get a wrong answer, which is the failure this tool exists to prevent.

## Binding constraints

1. **Model output never authors markup, URL or colour.** DESIGN.md invariant #1/#9. Applies to
   `code_run`'s slots, to G's popover, and to a question's candidate labels.
2. **An unresolvable reference renders as nothing** — an unknown view name, a marker with no
   call. Never a placeholder, never an error the owner has to interpret.
3. **Asking never blocks the agent loop.** `ask_owner` already does this correctly — the handler
   returns `ToolOutput(halt=...)` and the loop finishes the turn, rather than the prompt asking
   the model to stop. The answer arrives as a new turn. Anything else puts a human in a timeout
   path, and a prose obligation is not a mechanism.
4. **One question model.** One tool, one block, one open-set rule, whether the question came
   from a note or a conversation. A second question system is the failure W4 is shaped to
   avoid, and it is now an easy failure to have: the tool it would duplicate already ships.
5. **New fields are defaulted.** Stored turns predate them and must still load.
6. **No new runtime dependency.** Syntax highlighting is component-owned token classes, not a
   highlighting library.

## Security posture

`run_python`'s containment is unchanged by this plan — no new execution path is added, and the
surfaces are read-only views over calls that already happened. Two things are genuinely new and
worth naming:

**A question block is a first-party surface rendering model-authored text,** and that text can
be induced by a note body or a fetched web page ("confirm your account to continue"). A surface
that *asks for input* is a better target than one that displays output. `QuestionBlock` is
already built to the right shape — it renders no link and no model-authored markup — and W4
must not widen it. The exposure W4 genuinely adds is **reach**: a conversation can hold web
content a note thread cannot, so the tool that was previously asked only about the owner's own
note can now be asked from a turn that just read the internet. `agents.py`'s existing argument
for dropping `ask_owner` from the third-party set is the precedent, and **a red-team pass on the
widened binding is part of W4's gate**, not a follow-up.

**G's marker is an assertion that a computation happened.** Resolving it against the persisted
call rather than against anything written at render time (D3) is what keeps a model unable to
claim arithmetic it never did. Worth a test that says so directly.

W1–W3 touch neither RLS, the domain firewall, nor principal scope. W4 touches principal scope
by definition — it decides who may put a question in front of the owner — and if its open set
needs a new table, that table needs an RLS isolation test per CLAUDE.md #3.

## Decisions ratified by the owner (2026-09-18)

Both of the plan's open questions are settled. Nothing here is left for a wave to decide.

**O1 — the conversational open set lives in the turn, shape (b).** No new column on the
conversation and no reuse of the note reply path: the set is recorded on the turn that asked,
and an unanswered question dies with the conversation.

The consequence, stated so a wave does not discover it as a surprise: **a question asked in
chat can never reach the notes-tab queue.** That is the point rather than a cost — a chat
question has no note to correct and no expiry ladder to climb, so a queue entry for it would
be a row nothing can ever resolve. The ingest plan's ladder (D2) and its decay-to-assumption
keep governing note-thread questions, which are unchanged.

It also keeps `ask_owner`'s one-open-set rule honest for free: one conversation, one turn
holding the set, and the reply that resumes the turn consumes it.

**O2 — a conversational answer mints no note.** D5 as written is ratified. An answer that
resolves a reference is scoped to the turn that asked and is re-derivable from it; a fact
worth keeping lands through the agent's normal commit path, because it is a claim about the
world and not because of how it arrived. D7 is unchanged for note conversations, where the
answer still appends to the note as source text and is re-ingested.

The practical rule for W4: **the handler that resumes on an answer has no note-writing path at
all.** Not a policy the model is asked to observe — there is nothing there to call.

## Docs to reconcile at merge

- `../reference/DESIGN.md` — `code_run` added to the tool-view registry; the question card's
  closed affordance set; `.fb-step-cnt` as a universal slot rather than a two-tool one.
- `../reference/ASSISTANT.md` — `ask_owner`'s widened persona grants and the conversational
  question surface.
- `../mocks/code-run/README.md`, `../mocks/ask-user/README.md` — gate status → built.
- `../ROADMAP.md` — this plan's status.
- `AGENT_INGEST_CONVERSATION_PLAN.md` / `AGENT_INGEST_REWRITE.md` — the open-set shape the
  owner picks, and D5's narrowing of D7 to the note-thread context.
- This plan archives to `../archive/` in the PR that lands its last wave.
