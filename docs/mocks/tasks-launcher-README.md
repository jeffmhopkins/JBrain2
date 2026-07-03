# Tasks — launcher surface — three directions

Three interactive mockups for a new **Tasks** launcher tile: saved **prompts** that spawn an
agent session, run **on a schedule (recurring / one-off) or on demand**, and link back to the
**historical sessions** each run produced. A task is, in essence, a saved chat opener with a clock
on it — so the surface reuses paradigms the design system already settled: the Workflow
automation card (enable toggle, status dot, next/last-run meta, Run now), the Runs log (a run
row → its session), and the Chats agent/scope pickers (Curator / Jerv / Teacher, with Curator's
domain-scope dial).

All three are single-file, dark-first with a working theme toggle, phone-framed (390×800),
tokens-only (no raw hex outside the token sheet), outline icons, no emoji. Each is genuinely
interactive: cards expand, toggles flip, Run-now simulates a launch, the editor's agent picker
reveals the scope dial for Curator, and the schedule controls re-render their plain-language
summary live.

## Owner decisions baked into all three (from the kickoff)

- **Agent is per-task.** The editor lets you pick **Jerv** (web), **Curator** (reads your notes,
  with a domain-scope dial), or **Teacher** (prompt-only). Jerv/Teacher hide the scope dial and
  start with empty scopes — the firewall, not just a label, exactly as the Chats picker does.
- **Standalone tile**, separate from the existing note-event **Workflow** engine. Tasks is the
  personal, prompt-driven surface; Workflow stays the pipeline machinery.
- **Scheduling:** on demand · once at a set time · recurring (daily / weekdays / weekly).
- **Result delivery:** **push notification** + a **home-feed card** + **saved to history** (history
  is always-on; the other two are per-task toggles).

## A — list-first + full-screen editor · `tasks-launcher-a-list-editor.html`

The closest reuse of the Workflow card paradigm. The screen is a scannable list of task cards
grouped **Scheduled / On demand**, with a green "N running" live strip up top. Each card reads
**`<agent chip> · <schedule>`** with an enable toggle, a status dot, and a next/last-run meta line.
Tapping a card expands it inline to show the **prompt**, **recent runs** (each row → opens that
run's session, with a failed run's error in a rose mono strip), and Edit / Run-now. **＋ New task**
rises a **full-screen editor** (matching the card-launcher slide-up): name, a big prompt textarea,
the agent picker (Curator reveals the scope presets), the schedule segment (On demand / Once /
Repeats, each revealing its controls + a live summary line), and the delivery toggles.

- **Strengths:** lowest-friction scanning; the most faithful reuse of the settled Workflow +
  Runs + Chats patterns, so it feels native immediately; one card carries the whole lifecycle.
- **Tradeoffs:** the "what runs next" view is implicit in the meta lines rather than a first-class
  timeline; the full-screen editor is one long scroll.

## B — agenda-first + history tab · `tasks-launcher-b-agenda-timeline.html`

Leads with **what runs next** on a time spine (Now / Tomorrow / Friday / Sunday), each entry a
node-on-a-rail card; the running task carries a live progress meter (per DESIGN.md "honest status,
always visible"). Three tabs — **Agenda / Library / History** — separate the three jobs: the
**Library** is the compact on/off task list, and **History** is the chronological **session feed**
(Today / Earlier), each row opening its session. **＋ New task** opens a **stepped editor**
(Prompt → Agent → Schedule → Delivery) with a progress rail, so each decision gets its own calm
screen.

- **Strengths:** strongest "when does this run / what has run" feel; the stepped editor is the
  gentlest first-task authoring; History as its own tab scales best as sessions pile up.
- **Tradeoffs:** most chrome of the three (three tabs + a wizard); the agenda and library show
  overlapping data; an extra tap to reach the full task list.

## C — compose-first / prompt-centric · `tasks-launcher-c-compose-first.html`

Treats a task as a **saved prompt**, so the screen *opens with the editor itself*, omnibox-style: a
hero compose box with **agent**, **schedule**, and **delivery** as tappable **chips** beneath it
(each opens a small sheet — the agent sheet reveals Curator's scope dial in place). **Run once**
fires it now; **Save task** keeps it. Below sit the saved tasks as compact one-tap rows (with an
inline Run-now) and the recent sessions as a short feed. Lowest friction from intent → running
task: you author a task the way you'd start a chat.

- **Strengths:** fastest create path; the prompt is the star, which matches "feed a jerv session"
  literally; chips keep the advanced controls one tap away without a wall of form.
- **Tradeoffs:** managing many existing tasks is secondary (the list is compact/abridged); the
  always-present composer spends the top third of the screen even when you came to check a run.

## Chosen: A (implemented)

The owner picked **Direction A**. It is now the binding spec and is implemented: a `tasks`
launcher tile → `TasksScreen` (list + full-screen editor), backed by owner-only RLS tables
(`tasks` / `task_runs`, migration 0091), a schedule engine + minute-cadence web-process loop,
a headless runner that spawns an agent session per run, and the `/api/tasks` surface. The
historical sessions a run produces are browsable in Full Brain → Chats. Deferred follow-ups:
sourcing FCM tokens for the push poke, the home-feed card surface, and a deep-link that opens a
specific run's session directly from the run row.

## Recommendation

**Direction A (list-first + full-screen editor)** as the primary — it reuses the most already-settled
paradigms (Workflow card, Runs row → session, Chats agent/scope picker), so it lands native with the
least new vocabulary, and a single card carries author → schedule → run → session end to end. Borrow
**B's live "N running" strip** (already included at the top of A) and keep **B's History view** in
mind as a promotion if scheduled tasks proliferate and the per-card recent-runs list stops being
enough. **C's compose-first** is the most delightful for *creating* tasks; if first-run authoring
friction proves to be the thing that matters most, fold C's hero composer in as the "＋ New task"
entry rather than A's blank full-screen form.

## After a pick

The chosen mock becomes the binding spec for the React build (per `docs/reference/DESIGN.md` + the
`docs/reference/PROCESS.md` GUI mock gate). Implementation then adds: the **`tasks`** launcher tile +
`LauncherTarget`/`Card` wiring in `Launcher.tsx` / `App.tsx`; a `TasksScreen` (+ editor) following
the chosen layout; backend task records (prompt + agent + scope + schedule + delivery) on an
RLS-scoped table with an isolation test; scheduler entries that, on fire, open an `AgentSession`
with the task's persona/scope and replay the prompt; and the push / home-feed-card delivery hooks.
No code ships until the mock is chosen.
