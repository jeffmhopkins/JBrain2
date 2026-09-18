# Code run — seeing what `run_python` and `calculate` actually did

> **Status:** GUI gate **settled** — the owner chose **D's marker with F's popover**, built
> as **variant G** (`g-cited-floating.html`), which is the binding spec. A–F are retained as
> the record. Nothing is built yet; the build plan is the next artifact. The tools
> themselves shipped in `../../archive/EXACT_MATH_TOOLS_PLAN.md`; this is the surface that
> makes their working legible.

## The problem

`run_python` and `calculate` shipped with **no view of their own**. A run currently
renders in the Worked strip as the tool's model-facing text — labelled sections of
`stdout:` / `result:` prose — which is what the *model* needed, not what the owner needs.
The code that produced the number is in the call's arguments, persisted on the turn's
`agent_turns` row, and shown nowhere.

That is the wrong way round for the one tool whose entire value is that its answer can be
**checked**. A number you cannot trace is a number you have to trust, which is the state
the tool existed to get out of.

## What all three share

- A registered **`code_run` tool-view** (DESIGN.md's closed registry): the model fills
  data-only slots — code, stdout, stderr, result, duration, ok — and authors **no markup,
  URL or colour**. Syntax highlighting is a closed set of component-owned token classes,
  applied by the component from the language, never sent by the model.
- **The containment is stated, not assumed.** Every variant surfaces *no network ·
  scratch only · stdlib only*, because "where did this run?" is a fair question to ask of
  something that executed code, and the answer is a selling point.
- **`calculate` uses the same view**, with the code section replaced by the expression and
  the exact/decimal pair. One component, two tools — they are the same act.

## The three

| | Shape | Best at | Worst at |
|---|---|---|---|
| **A** `a-inline-accordion.html` | The Worked row expands in place into code + output | Zero navigation; code sits beside the sentence it produced; several runs open at once | A long snippet makes a tall bubble the transcript must scroll past |
| **B** `b-tabbed-card.html` | A `code_run` card with **Code · Output · Run** tabs | Constant bubble height whatever the snippet; matches the settled `weather_card`/`hurricane_card`/`chart` frame; the **Run** tab gives containment a home | Can't see code and output at once — comparing them is a tap back and forth; heavy frame for a one-line `calculate` |
| **C** `c-run-sheet.html` | One calm transcript line → full-height sheet with **every call in the turn** as a timeline | The only one that shows a **failed call and its retry together**; transcript stays one line however much ran | Most new surface; the working is behind a tap and leaves the conversation |

## Round two — collapsed at rest

The owner's read of A–C, in their words: *"I want them to basically have a collapsed view
and not take up so much space unless I expand them."*

That is a real objection and it lands hardest on A, whose expanded snippet makes a bubble
the transcript has to scroll past. It also reframes the gate: the question is no longer
"how should a run be displayed" but **"what does a run cost when nobody is looking at
it?"** — which for a tool called on most answers is the cost that actually accumulates.

Three more variants, constrained to collapsed-at-rest, and deliberately differing in what
they cost when closed rather than in how they look when open:

| | Shape | At rest | Taps to code | Layout shift |
|---|---|---|---|---|
| **D** `d-cited-computations.html` | A computed number carries a `ƒ1` marker and cites its working the way a fact cites its note | nothing (inline marker) | 1 | pushes content down |
| **E** `e-collapsed-ledger.html` | One 30px row per turn → a ledger (expression ↔ answer) → that line's code | one row per turn | 2 | pushes content down |
| **F** `f-tap-the-number.html` | No component; the number itself is the affordance, and the popover floats | nothing at all | 1–2 | **none** |

**D** is the one with a real idea behind it: a computation *is* a source for a number, and
the repo already renders sources as tappable `[^n]` markers, so this reuses a paradigm
rather than adding one. **F** is the most elegant and photographs best. **E** is the
dullest and is probably right.

## The decision — G, the hybrid

The owner's call: *"I like D, but I want it to have the pop-up of F."*

That resolves the gate, and it resolves it better than any of the six on their own, because
it takes the two variants whose weaknesses were **exactly complementary**:

| | Findable? | Moves the page? |
|---|---|---|
| D — cited computations | **yes** (a visible `ƒ1`) | yes, the card pushes content down |
| F — tap the number | no, invisible until guessed at | **no**, the popover floats |
| **G — both** | **yes** | **no** |

Neither half was compromised to get there — the marker is D's unchanged, the floating,
clamped, tail-anchored popover is F's unchanged. `g-cited-floating.html` is the binding
mock.

**It also fixes the one flaw I recorded against F.** *"A popover is a poor place for eight
lines of code"* was true and was not hand-waved: in G the `run_python` popover opens
compact (expression → result), expands in place with its body capped to 46% of the frame
and scrolling, and carries a **promote-to-sheet** control for when that still isn't room.
Small content stays a popover; large content stops being one.

**What G does not settle,** and what live use will have to: whether `ƒ1 ƒ2 ƒ3` inside one
sentence reads as rigour or as clutter. Three markers in a single clause is the densest
case the tool will routinely produce, and no mock can answer it — if it reads as clutter,
the fallback is to mark only the *first* computed figure in a clause and let its popover
list the rest.

### Why not E, which I had recommended

E is still the best *forensic* view — the ledger's expression-beside-answer list, with the
failed call visible among them, is the fastest way to audit a whole turn. G trades that for
per-number attribution: it answers "where did **this** number come from" immediately, and
"what ran in this turn" not at all.

That trade is defensible and probably right — the question people actually ask is about a
specific number, not a turn. But it leaves a real gap: **a failed call that was retried has
no marker of its own**, because it produced no number, so nothing in G surfaces it. If that
matters, E's ledger is the natural second surface, reachable from a turn-level affordance —
and this is the one place the build plan should revisit.

### The round-two judgement (kept for the record)

**E.**

D and F both make the working *invisible until guessed at* — F entirely, D nearly so. That
is a bad property for the one feature whose whole job is making a number checkable: a
reader who does not already suspect a number will never find the affordance, and the
failure this tool exists to catch is precisely the number nobody suspected.

E's middle level is the one that answers the real question. Not "show me this snippet" but
**"is this right?"** — and that is answered by five expressions beside five answers,
readable at a glance, with the failed call visible in the list. It costs one thin row per
turn to keep that one tap away, and it never loads code nobody asked for.

F is the better screenshot. E is the one you want at 11pm when a number looks wrong.

## The round-one judgement (kept for the record)

**B is the house answer; C is the better answer to the question actually asked.**

"Expand the code, view and see everything that ran" reads *plural*. The failure mode that
matters is not "I want to see this one snippet" — it is **"this number looks wrong, what
happened?"**, and that question is about the whole turn: which calls ran, in what order,
which one failed, what the retry changed. Only C shows that. A and B both show one call at
a time and silently drop the failed attempt that preceded the good one.

Against that: B is the paradigm three shipped views already use, and consistency is worth
real money in a one-person system.

**A hybrid is available and probably right:** B's card in the transcript (the house frame,
predictable height, containment stated) with a **"see the whole turn"** affordance opening
C's sheet. That costs one extra component and gets the calm default plus the forensic
view. Say the word and the fourth mock is the one to build.

## Fidelity

The data in all six is the **real output of a real run** — the electricity-bill question
from the plan's demo, through the actual `pysandbox` container. `27875/937`, `44ms` and
the quarterly running totals are what the tools returned, not invented for the mock. The
failed `243.15 * 12mo` call in C and E is fabricated to show the retry case, as is F's
fixed-tariff exchange; everything else is real.
