# GUI gate — Flash-Next per-task reasoning

> **Status:** Plan · **Last verified:** 2026-10-03
>
> **Awaiting the owner's pick** among A, B and C. Nothing is built yet. Once a variant is
> chosen, this header records it as the binding spec and keeps the other two as the rivals.
> The reasoning then goes into `docs/reference/DESIGN.md` along with the frontend. Behaviour
> source: the owner's request of 2026-10-03, on top of the engine remap in
> `docs/plans/FLASH_NEXT_ENGINE_PLAN.md` §4c.

Three interactive mocks of the per-task routing tiers in **LLM settings**. While the
**Flash-Next** engine serves, every local task is remapped onto Qwen3.8 Flash-Next. That
part is unchanged. What's new is a **separate reasoning level used only on Flash-Next**. It
is stored apart from the Standard picks, so switching back to Standard brings those back
untouched. Open each `.html` in a browser. Each has a dark/light toggle, a caption, and a
dashed **scenario panel** above the phone that drives every state. A dashed outline in the
screen marks the new part. Everything else is the real screen as it ships:
`settings-meta`, the collapsed On-box models card, the five tier cards (Provider select,
the *→ Flash-Next (engine active)* remap marker, Reasoning segments, and Per-task overrides
with a provider select and N/L/M/H on each row), then Code mode and AI usage.

## The model (identical in all three)

- **Levels:** Default · None · Low · Medium · High, settable **per tier** and **per task**.
  The precedence is: the task's own Flash-Next level, then its tier's Flash-Next level, then
  Default.
- **Default is today's behaviour**, and every variant shows what it resolves to:
  - the Standard pick's effort, when the Standard model takes a reasoning level
    (*from the Standard pick*);
  - otherwise the task's reasoning bucket, when its Standard model takes no level
    (*task default*: JPet reply on Qwen3.5 4B → Low);
  - otherwise Flash-Next's own Medium, for a task with no bucket on a non-reasoning model
    (*Flash-Next default*: Vision OCR on Qwen3-VL 30B → Medium).
- Only **local** picks move to Flash-Next. A task on a cloud provider (Intake materialize on
  Grok 4.3, JPet statue sculptor on Claude) keeps running there. Each variant says so and
  offers no Flash-Next level for it.
- A tier's Flash-Next level leaves task overrides in place. The tier shows *N set
  individually*, with **Reset to tier**.
- Edits save immediately, like every pick on this screen, and a toast confirms each one. A
  Standard edit made while Flash-Next serves says *used when Standard serves*. Changing a
  Standard effort also changes what a Flash-Next Default resolves to.

## Shared states (identical data in all three)

| State | How to reach it in the mock |
|---|---|
| Flash-Next serving (steel global banner, remap markers) | default, or **Engine → Flash-Next serving** |
| Standard serving: the Flash-Next levels are **hidden (A)**, in a **collapsed card (B)** or **one toggle away (C)** | **Engine → Standard serving** |
| Every task on Default (today's behaviour), each showing what Default resolves to | **Levels → All default** |
| Task overrides: **Vision OCR → None** (default would be Medium), **Agent turn → High** (default Medium) | **Levels → Task overrides** |
| Tier override: **High reasoning → Med**, **Low reasoning → None**, with **Wiki grounding** still overriding its tier at High | **Levels → Tier override** |
| A tier with a cloud task (Medium: Intake materialize on Grok) and a mixed tier (Vision, Other) | open those tiers |

## The three variants

### A — `a-inline-rows.html` — a second control inline, only while Flash-Next serves
Each tier card keeps its Provider select and remap marker. Its **Reasoning** field is
relabelled *when Standard serves*. Under it is a new **Reasoning on Flash-Next** segmented
row (Default · None · Low · Med · High) and a one-line reading: *Default keeps each task's
own level: High (from the Standard picks)*, or *Every task runs at Med on Flash-Next · 1
task set individually · Reset to tier*. In Per-task overrides, each row gets a second
compact line: *Flash-Next* followed by a level chip with its source (*Standard pick*,
*from tier*, *set here*) and **D N L M H**. While Standard serves, all of this is
**hidden**. One quiet line says how many levels are kept.
*Trade-off:* the levels sit right beside the pick they shadow, so the cause and effect
is plainest, and it is the smallest structural change. Every tier card grows by about
90 px, and every task row by a line, which is the densest of the three. On Standard you
can't see or pre-set the levels at all.

### B — `b-flash-card.html` — its own "Flash-Next reasoning" card
A new card sits **above the tiers**, with a steel rail, **Flash-Next reasoning** and a badge
(*In use* / *Next time it serves*). It states that the model is fixed and only reasoning
is set here. Inside, there is **one row per tier** with a **select**
(*Default · High* / None / Low / Medium / High). Tapping a tier opens its tasks, each with its
own select (*Default · Medium*, …) and a source line. Cloud tasks are listed as staying
put. The tier cards below are **exactly as today**. Their remap marker gains a
*reasoning ↑* link that jumps to that tier in the card. The card is **always present**. On
Standard it starts collapsed, with a notice that the levels apply next time.
*Trade-off:* Standard and Flash-Next are cleanly separate, so the existing cards don't
change, and the levels can be pre-set at any time. Selects show the resolved default
in the closed control. But there are now two places to look for "how hard does this task
think", and the tier list appears twice on one screen.

### C — `c-mode-toggle.html` — a Standard | Flash-Next toggle swaps what the tiers edit
A two-segment **Routing for** control (each segment marked *serving* / *not serving*) sits
at the top of the tiers. It follows the serving engine. On **Flash-Next**, the same tier
cards take a steel wash. The Provider select becomes a locked
**Model · Qwen3.8 Flash-Next · set by the engine** row (*4 of 5 tasks* when a cloud task
stays put), and the Reasoning segments gain **Default**. Per-task rows show only **D N L M H**
with *Default · Medium · task default* under the name. Flip to **Standard** while Flash-Next
serves, and a notice says those picks are on hold until you switch back. Flip to
Flash-Next while Standard serves, and a notice says the levels apply next time.
*Trade-off:* there is one set of cards and one mental model ("which engine am I
editing?"), the most compact at any moment, and both sets can always be viewed and
edited. The cost is a mode: the screen looks different depending on a toggle, and an edit
made in the wrong mode is easy to miss. Seeing a task's Standard level and its Flash-Next
level side by side takes a flip.
