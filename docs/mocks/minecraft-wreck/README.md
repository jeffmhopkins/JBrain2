# GUI gate — Minecraft crash site at spawn (wave SW3)

> **Status:** Plan · **Last verified:** 2026-10-11
>
> Decision: **pending (owner)**. Three variants are below. The chosen one becomes the binding spec
> for SW3's frontend, and its reasoning lands in `docs/reference/DESIGN.md` ("Minecraft server
> screen") in the implementing PR.
> Behaviour source: `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §"SW1–SW3 — Spawn wreck", and the
> sidecar behaviour SW1 and SW2 shipped ("Verified facts" below).

These are three interactive mocks of how the owner controls the **crash site**: an escape pod
crashed at spawn with its parachute out behind it, and the damaged mothership hanging overhead.
Each one is built on the binding Worlds mock (`../minecraft-worlds/b-worlds-subscreen.html`,
built as `frontend/src/screens/MinecraftWorlds.tsx` and `MinecraftWorldSheets.tsx`), which lives
on the binding Minecraft screen (`../minecraft-ops/b-dedicated-screen.html`). They cover:

- whether a new world starts at a crash site;
- whether a reset with a seed starts at one;
- the server-wide default (`spawn_wreck`);
- what each world shows: on its way, building (with its phase), built, failed and trying again,
  or none.

Open each `.html` in a browser. Each file:

- is self-contained and works offline. The one picture, in B, is the spike's
  `pod_and_ship.jpg` render, downscaled and embedded (about 8 KB);
- is phone-framed: 390 px, 560 px on desktop, full-bleed under 430 px, and it holds at 320 px;
- copies the Worlds mock's tokens and component styles verbatim, in dark and light, with tokens
  only. Its switch row (`.sw-row` / `.switch`) is the shipped `mc-sw-row` / `mc-switch`, and its
  segments are the shipped `Seg`;
- uses Lucide-style outline icons;
- keeps a 44 px tap floor on every control;
- honours `prefers-reduced-motion`.

Everything else in the Worlds mock (load, import, reset, restore, backups, rules, server
settings, allowlist) works as it does there, so each variant is judged in place.

A dashed **Mock controls** strip above the phone is reviewer chrome, not design. It flips between
every state, picks the import file, flips the `spawn_wreck` setting (B has no switch for it on
screen), switches the theme, and jumps between screen levels.

## Shared fixture (identical in all three)

It is the Worlds mock's fixture, plus each slot's `wreck` state.

| Slot | World | Origin | `wreck` | Shows as |
|---|---|---|---|---|
| 1 | **World** | made on first boot | `null` | none — made before crash sites |
| 2 | **Castle Hill** (loaded) | imported .mcworld | `null` | none — imported worlds are never touched |
| 3 | Creative test | new world | `"done"` | built — the pod at spawn, the mothership overhead |
| 4 | Skyblock run | new world, not generated yet | `"armed"` | on its way — built the first time it starts |
| 5 | — | empty | — | |

`spawn_wreck` is on, the default.

| State (mock controls) | What it shows |
|---|---|
| The Worlds mock's 14 states | Unchanged, plus each world's crash-site state. |
| Crash site · building | Skyblock run was just loaded for the first time, with nobody on. The build is reported phase by phase: *reading the ground at spawn* → *building the pod* → *placing the mothership overhead* → built. It animates, and a toast says when it's done. |
| Crash site · failed | The same world, with the pod stage failed. It reads *didn't finish — trying again in 30 s (try 1 of 3)*, counts down, then builds. |
| Setting: `spawn_wreck` off | Every sheet starts at a plain spawn instead. |

Loading Skyblock run from its page runs the same build after the load's own phases.

## Behaviour common to all three

- **Only a world the box makes is armed.** That means a new world, or a reset with a seed (*same
  seed* or *new seed*). **Empty** clears the slot. Import and restore are never armed, and the
  import sheet says *No crash site is added*.
- **It's built on the world's first start.** The state follows what the build reports: `loaded`,
  `pod`, `ship`, then `done`. A `failed` build is tried again 30 s later, up to three tries.
- **There is no "add it now".** A world that's been played and has none says why: *A crash site
  can't be added to a world that's been played — it would land on what's built there.* Then it
  says how to get one, which is a reset with a seed. Each variant words that its own way.
- **A stop or a world switch mid-build** leaves the world armed. The build carries on at the next
  start, as the pack resumes from its own record.
- **A world's page** (Worlds → the world) always has a **Crash site** line in its facts.
- **Turning `spawn_wreck` off doesn't disarm a world already waiting for one.** The toast says so
  when one is: *Skyblock run still gets its crash site — set when made.*
- **The setting saves at once, with no restart.** It sits in its own block under the server
  settings' **Save settings**, because that Save applies at the next restart and this doesn't.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-switch-in-sheet.html` | *A setting like Cheats.* A switch row in the New-world and Reset sheets, the default in Server settings, and a status line on each world row. |
| **B** | `b-start-choice.html` | *How the world starts.* A two-option **Start** choice with a picture, and a compact badge on each world row. |
| **C** | `c-server-default.html` | *One server rule.* A single row in Server settings, with no choice per world. A world's state shows only on its own page. |

### A — a switch in the sheet

- **New world** gets a **Crash site at spawn** switch row under Cheats, built the same way. It
  starts at the server default. Its line reads *on — an escape pod crashed at spawn, its
  mothership overhead* or *off — the world's own spawn, nothing built*. It adds *· not the
  server default* when the two differ.
- **Reset** shows the same row under *Same seed* and *New seed*, but not under *Empty*.
- **World rows** gain one line: *Crash site: on its way · building — the pod · didn't finish —
  trying again · built · off*.
- **Server settings** gains **Crash site by default**: *on — new worlds start at one; turn it off
  for a world in its sheet*.

- **For:** it is the smallest new idea. It is the Cheats pattern, which the sheet already has.
  It gives a choice per world and a default in the PWA, which closes the rule #10 gap SW3 names.
  The row line answers "will this one have it?" from the list.
- **Against:** it adds a line to every world row, including *off* on worlds where it can never
  change. The reset switch needs a `wreck` override on reset, which the sidecar doesn't take yet
  (see "Assumptions").

### B — a Start choice

- **New world** asks how it starts, with **Start**: two large segments, **Crash site** (with the
  render) and **Plain spawn** (a plain landscape glyph). It's preselected from the server default,
  and the label says *server default* or *not the server default*. The hint describes the pick:
  *An escape pod crashed at spawn, its parachute out behind it, under the damaged mothership.
  Built the first time it starts.*
- **Reset** shows the same choice under *Same seed* and *New seed*.
- **World rows** carry a compact badge after the name: *crash site · on its way* (steel),
  *crash site · building* (amber), *crash site · trying again* (rose), or *crash site* (neutral)
  once built. Worlds without one have no badge, so the badge row wraps under the name at 320 px
  rather than truncating it.
- **No switch for the default.** It only decides which option comes preselected. The mock
  controls flip it.

- **For:** the most legible. The picture says what a crash site is, which a switch label can't,
  and that matters the first time. A plain spawn is a first-class choice, not a switch turned
  off. The badge is quiet when there's nothing to say.
- **Against:** the sheet gets taller by the two pictures. It leaves `spawn_wreck` with no PWA switch, so
  the rule #10 gap stays open for that setting. Like A, the reset choice needs a `wreck` override
  on reset.

### C — the server default only

- **Server settings** gains one row, **New worlds start at a crash site**, with *on — an escape
  pod crashed at spawn, its mothership overhead*. Its copy says it covers new worlds and resets
  with a seed, and that it's built on the world's first start. It also says imported and
  restored worlds are never touched, and a played world can't get one later.
- **New world and Reset** have no control. A read-only line says what will happen: *Starts at a
  crash site — new worlds start at one while it's on in Server settings.*
- **World rows** show nothing. A world's page has the **Crash site** line in its facts, with the
  build's phase while it runs.

- **For:** no new per-world control, and it matches what the sidecar takes today (reset re-arms
  by the setting). It closes the rule #10 gap with one switch. The world list stays as it is.
- **Against:** a one-off plain world means flipping the server setting and remembering to flip
  it back. Turning it off doesn't stop a world already armed. Nothing in the list says which
  worlds have one.

## Render check (2026-10-11)

These checks ran in jsdom against all three files:

- Each loads with no script errors, in both themes.
- **New world:** each variant's control is present and toggles (C has its read-only line), and
  **Create** adds the world to the list armed. A's row line and B's badge appear on the new row.
- **Reset:** each variant's control or line appears under the seed options.
- **Server setting:** A's and C's switch toast names Skyblock run when turned off. B has no
  switch.
- **Build:** loading Skyblock run runs *reading the ground* → *building the pod* → *placing the
  mothership* → built. The *building* and *failed* states render, with the retry countdown.
- Each file is well-formed HTML with no external URLs.

They were **not** yet checked in a real browser at 390 and 320 px for contrast, tap targets and
truncation, as the Worlds round was. The variant pieces reuse that round's audited components,
with B's Start segments as the one new control.

## Recommendation

**A.** It reuses a pattern the owner already knows from Cheats. It gives both a per-world choice
and a PWA switch for the default, which is what SW3 has to close. Its row line is the cheapest
way to see which worlds have one.

If the owner wants the first-time experience to explain itself, choose **B**, and add A's default
switch to Server settings to close the gap. Choose **C** only if one-off plain worlds won't
happen.

## Verified facts (sidecar behaviour these mocks are built to)

1. **`spawn_wreck`** is a server setting, on by default. It decides whether a world the box
   creates or regenerates is armed.
2. **A create can override it** per world: `wreck: true | false`.
3. **Each slot's `wreck`** is `"armed"` (builds on its first start), `"done"` (built, or passed
   over), or `null` (off, imported or restored).
4. **Imported and restored worlds are never armed.**
5. **A reset with a seed re-arms** per the setting.
6. **A played world can't get one later**, since building would land on players' work. So there
   is no add-it-now action.
7. **The build reports phases** `loaded`, `pod`, `ship`, `done` and `failed`. A failure is
   retried 30 s later, and it gives up after three tries.

## Assumptions still open

- **A and B need a reset override.** Their reset sheets take a choice. Today the sidecar re-arms
  a reset only by the setting, so these need a `wreck` override on reset, as create has. C needs
  nothing new.
- **`done` shows as *built*.** It also covers an SW1-era world that was passed over. The slot
  doesn't tell the two apart.
- **The build's phase reaches the PWA** with the slot (or `/status`), so every device sees it.
- **Turning `spawn_wreck` off leaves an armed world armed**, since arming happens at create or
  reset.
- **Not covered:**
  - a build that gave up after three tries, which the plan doesn't say how to show;
  - the debug route;
  - the plan's SW3 line about a "re-arm action". The owner's facts rule out adding one to a
    played world, so a reset with a seed is the only way back to a crash site.
