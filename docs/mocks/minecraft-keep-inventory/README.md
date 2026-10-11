# GUI gate — Minecraft keep inventory: Off · On · Power Pack (M10)

> **Status:** Living · **Last verified:** 2026-10-11
>
> Open — waiting for the owner's pick. Three variants are below. The chosen one becomes the
> binding spec for the PWA side of M10's keep-inventory charm (the toggle and the "managed"
> row the plan lists as *not built*), and its reasoning lands in `docs/reference/DESIGN.md`
> ("Minecraft server screen") in the implementing PR.
> Behaviour source: `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M10, "Keep your inventory, once per
> Power Pack" and "Shipped (wave mc-items, 2026-10-11)".

These are three interactive mocks of one control: a world's **keep inventory** becomes a
three-way choice, **Off · On · Power Pack**, in place of the rules editor's plain switch. They
are built on the binding Worlds mock (`../minecraft-worlds/b-worlds-subscreen.html`, built as
`frontend/src/screens/MinecraftWorlds.tsx`), and show its **Game rules** page and a trimmed
world page. Backups, Manage, the main Minecraft screen and the other sheets work as they do
there and are left out here.

Open each `.html` in a browser. Each file:

- is self-contained and works offline. The one picture is the Power Pack's icon,
  `../minecraft-items/a/power_pack.png` (the owner's pick, the ember), embedded at 16 px and
  drawn pixelated at 18–20 px;
- is phone-framed: 390 px, 560 px on desktop, full-bleed under 430 px, and it holds at 320 px;
- copies the Worlds mock's tokens and component styles verbatim, in dark and light, with tokens
  only, and follows the system theme by default. Its segments are the shipped `Seg`, its radio
  rows the Worlds mock's `.opt`, its tags the rules editor's *pending* and *changed* tags;
- renders the editor's real 39 rules from `frontend/src/minecraftWorlds.ts`;
- keeps a 44 px tap floor on every control and honours `prefers-reduced-motion`.

A dashed **Mock controls** strip above the phone is reviewer chrome, not design. It sets which
world you're looking at, where keep inventory starts, what the next save does, the screen and
the theme. Every state can also be opened by URL hash, for example
`a-segmented-row.html#where=other&start=packOff&net=hold&scr=rules&theme=light`.

## What the three choices mean (identical in all three)

| Choice | Writes | In game |
|---|---|---|
| **Off** | the plain rule, `keepInventory: false` | Vanilla — everyone drops their things where they die. |
| **On** | the plain rule, `keepInventory: true` | Vanilla keepInventory — everyone keeps everything. |
| **Power Pack** | the world's switch, `PUT /api/minecraft/worlds/{slot}/power-pack {"on": true}` (slot field `power_pack_keep`) | You keep everything only if you carry a **Power Pack** and **your own Tricorder**; one pack burns. Otherwise you drop, as with Off. |

The help line every variant carries: *Power Pack — 8 Ender Pearls around a Blaze Powder. It
only works with the player's own Tricorder.*

## Shared fixture and states

The fixture is the Worlds mock's: **Castle Hill** (slot 2) is loaded with 2 players on, and
**Skyblock run** (slot 4) is not loaded and not generated yet.

| Mock control | What it shows |
|---|---|
| World · *Loaded · running* | Castle Hill. A change applies live, at once; the toast says *Applied live — …*. |
| World · *Loaded · server stopped* | Castle Hill with the server stopped. A change is saved and applied when the server starts; the row carries *pending*. |
| World · *Not loaded (Skyblock run)* | A change is saved and applied when Skyblock run next loads; the row carries *pending*, and the world page's Game rules line says *1 pending*. |
| Start at · *Off* / *On* | The plain rule, the switch off. *On* carries the editor's usual *changed · default off* tag. |
| Start at · *Power Pack · saved Off* / *saved On* | The switch on. The rules view reports `managed: {"keepInventory": true}`, shown as *held on by the Power Pack*; the owner's plain choice underneath is Off or On. |
| Next save · *Works* | Each write takes ~0.9 s, showing the applying state, then lands. |
| Next save · *Holds (applying)* | The write stays in flight, so the applying state can be studied. Flip back to *Works* to let it land. |
| Next save · *Refused (409)* | The first write is refused (*the server is backing up Castle Hill*). The control goes back and a rose line says **nothing changed**. |
| Next save · *Refused after Power Pack off* | Leaving Power Pack for a choice that needs two writes: the switch-off lands, the rule write is refused. The rose line says exactly where it stands: *Power Pack is off, but keep inventory couldn't be set Off — … It's on your saved choice, On, for now.* |

### How a change is written

- **Off ↔ On** is one write of the plain rule (`PUT …/rules`), as today.
- **Off or On → Power Pack** is one write: the switch on. The sidecar holds `keepInventory` on
  and keeps the owner's plain choice underneath.
- **Power Pack → Off or On** turns the switch off first, which hands keepInventory back to the
  saved plain choice. If the pick differs from that choice, a second write sets the rule. The
  applying line names each step: *Turning Power Pack off — keep inventory goes back to your
  saved On…*, then *Setting keep inventory Off…*. With the switch on, the sidecar refuses a
  keepInventory write, so the order is fixed: switch off, then rule.
- **While a write is in flight** the picked option shows a spinner in place of its icon or
  radio, and the other two don't take taps. Only one write runs at a time.
- **Reset all** in the rules editor resets an *On* back to Off. It leaves a *Power Pack* world
  alone, and the footer says so (*keep inventory is on Power Pack, which Reset all leaves
  alone*). Without that, Reset all would send a keepInventory write the sidecar refuses.
- **Where it applies** follows the rest of the screen: live on the loaded, running world; when
  the server starts on the loaded, stopped world; when it next loads on any other world. The
  sidecar tells the pack the switch after the rules at every start, and at once on the loaded
  world, so this matches the rules editor's own note.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-segmented-row.html` | *A rule with three values.* The row's switch becomes a segmented **Off · On · Power Pack**, with the pick's meaning and the help line under it. |
| **B** | `b-value-sheet.html` | *A rule with an explained choice.* The row keeps its one-line shape with a value button; the value opens a Sheet with three radio rows and their explanations. |
| **C** | `c-world-setting.html` | *Death is a world setting.* **On death** joins Game mode, Difficulty and Cheats on the world's page; the rules row turns read-only and points there. |

### A — segmented in the row

- **The row** becomes a full-width rule like the editor's number rows: the label, `keepInventory`
  in mono, its tags, then the **Off · On · Power Pack** segments (the shipped `Seg`, each a real
  44 px, the Power Pack segment with its icon).
- **Under it**, one line says what the pick means (*Vanilla — everyone drops their things where
  they die.* / *… keeps everything.* / *Kept only with a Power Pack and your own Tricorder — one
  pack burns. Otherwise you drop, as with Off.*), then the Power Pack help line on a quiet
  surface.
- **Applying** puts the spinner on the picked segment and the step in the line under it. A
  refusal shows inline under the segments, so it can't scroll out of sight at the page's top.
- **The Players group** summary adds *· Power Pack* while it's on, and the world page's Game
  rules line ends *· keep inventory Power Pack*.

- **For:** the fewest taps, and all three choices are visible where the owner already looks
  for the rule. It reuses the segmented control the world page already has. The meaning line
  updates with the pick, so a wrong tap explains itself.
- **Against:** it's the tallest rule in the editor, and the only one that doesn't look like a
  rule. Search finds it, but the group gets heavier. *Power Pack* in a third of a 320 px row is
  tight; it fits, with the icon, at 13 px.

### B — the value opens a sheet

- **The row** keeps a rule's one-line shape. Its sub-line says the pick in a few words
  (*everyone drops their things*), and its control is a 44 px value button: *Off ›*, *On ›* or
  *[icon] Power Pack ›*.
- **The Sheet** is headed by the rule and the world. Three radio rows (`.opt`) explain each
  choice in full; *Power Pack* adds the recipe and, while it's on, *Your plain choice
  underneath is On — it comes back if Power Pack is ever turned off.* Off and On say *Turns
  Power Pack off.* while it's on. The foot says where it applies (*Applies to Castle Hill live,
  at once.* or *Applies when Skyblock run next loads — marked pending until then.*) and the
  plan's fail-safe: *If the Power Pack ever faults, everyone keeps their things.*
- **A pick applies at once.** The picked radio becomes a spinner and the foot shows the step.
  On success the sheet closes, and the toast and row carry the result. A refusal keeps the
  sheet open, with the rose line at its top and the radio put back.

- **For:** the most room to explain, at the moment of choosing, and the rules list keeps its
  rhythm. It is the only variant that can show the saved plain choice and the fail-safe without
  crowding a row. It reads well for someone meeting Power Pack for the first time.
- **Against:** one more tap, and the choices are hidden until it's opened. A sheet over a
  pushed page over a pushed page is the deepest the Minecraft screen goes. There is no help
  line under the row itself; the explanation lives in the sheet.

### C — "On death" on the world's page

- **The world's page** gains **On death** under Cheats, inside **Settings — this world only**:
  three compact radio cards, *Drop everything* (Off — vanilla), *Keep everything* (On — vanilla
  keepInventory) and *[icon] Keep with a Power Pack* (only with a Power Pack and your own
  Tricorder — one pack burns), then the help line. It shares the card's own status line
  (*Loaded and running — …* / *Not loaded — a change is saved and applied when …*) and carries
  the card's *pending* tag.
- **The rules editor's row** is read-only: *Power Pack — set on the world's page, under On
  death*, with a 44 px **Change** button that goes back to the world's page, scrolls to the
  choice and focuses it.

- **For:** it matches the plan's first sketch (a switch on each world's page, with the rules
  row showing it's managed) and puts the decision with the other per-world choices, where it's
  found without opening the 39-rule editor. Plain-English names (*Drop everything*) need no
  rule vocabulary.
- **Against:** it moves a vanilla rule out of the rules editor, which is the one place that
  promised *every game rule*. Two places mention it, and only one changes it. The world page's
  Settings card gets taller by three cards. New world and Import would need their own copy of
  the choice to set it up front (not mocked).

## Render check (2026-10-11)

Every state above ran in headless Chromium at 390×844 and 320×568, in dark and light (the
system theme), against all three files, with these checks:

- no horizontal overflow of the page or the phone, and no truncated text;
- every visible text at least 4.5:1 against its composited background, reviewer chrome
  included;
- every control's hit area at least 44×44 px (the switch's and links' `::after` extensions
  counted);
- no console errors.

All passed. The screenshots were kept outside the repo.

## Recommendation

**A.** Keep inventory is a rule, and the rules editor is where the owner set it before; A
changes the least about where it lives and makes the third choice visible without a tap. The
meaning line and the help line carry Power Pack's two conditions (a pack *and* your own
Tricorder) at the moment of the pick, and the applying line names both writes when leaving
Power Pack.

Choose **B** if the owner would rather keep the rules list uniform and have the full
explanation (the saved plain choice, the fail-safe) at hand when choosing. Choose **C** if On
death should be a headline per-world choice beside Difficulty rather than one rule among 39.

## Owner picks

- **Which variant** — A, B or C.
- **The labels.** *Off · On · Power Pack* (A, B) or *Drop everything · Keep everything · Keep
  with a Power Pack* (C). Either set can go in either variant.
- **Leaving Power Pack.** The mocks always land on the pick, writing the rule a second time
  when it differs from the saved plain choice. The alternative is to drop the saved choice from
  the UI entirely; the sidecar keeps it either way.
- **Reset all** leaves a Power Pack world on Power Pack. The alternative is that it turns Power
  Pack off too.
