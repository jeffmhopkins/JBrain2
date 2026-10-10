# GUI gate — local engine switch (Flash-Next wave F3a)

> **Status:** Plan · **Last verified:** 2026-10-02
>
> **Retired 2026-10-10:** the owner settled on Flash-Next, so the segmented switch left Ops;
> the card became the read-only **Ops → Engine** page (`../ops-launcher/`). A's confirm,
> progress and notice registers live on there. Kept as the record of the round.
>
> **Decided 2026-10-02: A — segmented toggle inside an Ops "Local engine" card** is the
> binding spec (`a-segmented-toggle.html`); B and C are kept as the rivals. Chosen because it
> is native to the Ops card stack (everything collapsed except System; the card opens itself
> on a switch, fallback or rollback). The off-engine row control keeps the real label
> "Stage". Reasoning lands in `docs/reference/DESIGN.md` with the F3a frontend. Behaviour source: `docs/plans/FLASH_NEXT_ENGINE_PLAN.md`
> §4c, §4d, §5, F3.

Three interactive mocks of the owner's **Ops → Local engine** surface: switching the box's
on-box LLM engine between **Standard** (the `local-llm` gateway — gpt-oss-120b,
Qwen3.8-27B, …) and **Flash-Next** (one model, Qwen3.8-Flash-Next, its own container).
Only one engine runs at a time. Open each `.html` in a browser. Each has a dark/light
toggle, a caption, and a dashed **scenario panel** above the phone that drives every
state. Timings are compressed about 12×. The elapsed and estimate figures shown are
real-scale.

## Shared states (identical data in all three)

| State | How to reach it in the mock |
|---|---|
| Idle on Standard | default |
| Idle on Flash-Next (global banner: *Flash-Next active · since 00:24 · Switch back*) | **Start as → On Flash-Next**, or finish a switch |
| Confirm (states the consequence: pause length, drain, rollback) | tap the switch control |
| Switching: **drain** (bounded, cancellable) → **stop** → **settle memory** (live GTT falling) → **start** (load clock, ~50 s for Flash-Next) → **smoke** (text · image · tool call) | confirm |
| Success (desired = effective, persisted) | let it run |
| Failure → automatic rollback, reason surfaced | **Simulate → Fail next switch**, then switch |
| Desired ≠ effective (*Flash-Next selected · Standard serving (fallback: …)*), with Retry / Keep | **Start as → Deploy fallback** |
| Disabled: Flash-Next not installed (links to On-box models → Install) | **Simulate → Flash-Next installed** off |
| Disabled: a one-shot is running (update / perplexity) | **One-shot** buttons |
| Global banner for a debug/test job | **Simulate → Debug job**, or **Perplexity running** |
| LLM settings: an off-engine model's load control is disabled with *Runs on the Flash-Next engine — switch engines to load it* (and the mirror for Standard models while Flash-Next serves); per-task picks marked *→ Flash-Next (engine active)* | **Screen → LLM settings** |

Readouts in every variant: the engine (desired vs effective), GTT used against the pool,
host free GiB, decode tok/s (an em dash while no engine serves), the last smoke result with
per-check timings, and the engine log tail. The **global banner** sits under the top bar on
every screen (try **Screen → Home**). It is steel while Flash-Next serves, amber while a
switch or a test job runs, and rose after a rollback (dismissible).

## The three variants

### A — `a-segmented-toggle.html` — segmented toggle inside an Ops card
A new **Local engine** `OpsCard` in the collapsed stack. Like the Host settings card, it
**opens itself** when it needs attention (a switch, a fallback, a rollback). A
**Standard | Flash-Next** segmented control. Tapping the other segment expands an
**inline confirm panel** with the consequence and Cancel / Switch. Progress is
**phased text, a steps list and the log tail**, the same as Server update. The readouts are
the System card's label rows.
*Trade-off:* the most native to today's Ops screen, and the cheapest to build on `OpsCard`.
The engine state is behind a collapsed card when nothing is wrong, and a segmented control
reads as a cheap toggle for a multi-minute, disruptive act. The confirm panel has to
carry that weight.

### B — `b-engine-cards.html` — two engine cards side by side
The card body is a **2-up status-card grid**, one card per engine. Each shows what it costs
and delivers (GTT, tok/s; *needs ~74 GiB* when stopped). Tap the stopped card to **make it
active**, then confirm in a **center Dialog** (one sentence of consequence). During the
switch each card narrates its own side (*draining → stopped* / *starting 0:31 → smoke
test*) above a **five-segment stepper**. LLM settings groups its models **by engine** to
match.
*Trade-off:* the clearest picture of "two engines, exactly one on". It is the best at
showing what you'd be switching *to* before you commit. Two half-width cards are tight
at 390px, so model lists truncate. The Dialog is the heaviest confirm of the three. It
is also the most new layout.

### C — `c-stateful-block.html` — status-first block, one action + stage timeline
A **stateful block at the top of Ops**, outside the collapsed stack. A plain-language
headline (*Standard is serving*), three readings, a **vertical stage timeline** and
**one primary action** in the thumb zone. When idle, the timeline is a one-line
*Last switch* row that expands. During a switch, the live stage expands in place with
its own detail (drain count, falling GTT, load clock, smoke sub-checks). The confirm is
a **bottom Sheet** that **previews every step with its time estimate**. **Cancel** is
offered only while draining, because nothing has stopped yet.
*Trade-off:* the most honest at a glance, and the best narration of a long operation.
It breaks the "everything collapsed except System" Ops rule, because it is always
expanded at the top. It spends that space even when nothing is happening.
