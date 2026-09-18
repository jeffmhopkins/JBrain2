# Code run — seeing what `run_python` and `calculate` actually did

> **Status:** GUI gate **open** — three variants, awaiting the owner's choice. Nothing
> built. The tools themselves shipped in `../../archive/EXACT_MATH_TOOLS_PLAN.md`;
> this is the surface that makes their working legible.

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

## The judgement

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

The data in all three is the **real output of a real run** — the electricity-bill question
from the plan's demo, through the actual `pysandbox` container. `27875/937`, `44ms` and
the quarterly running totals are what the tools returned, not invented for the mock. C's
failed `243.15 * 12mo` call is fabricated to show the retry case; everything else is real.
