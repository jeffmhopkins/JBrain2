# GUI gate — Minecraft "Power Pack teleport" switch (M10, transport on request)

> **Status:** Living · **Last verified:** 2026-10-12
>
> **Decision (binding, owner 2026-10-12): B — a switch in Settings**, as Revision 2 renders
> it (`b-settings-switch.html`, `png/b-*`): the applying line *Turning Power Pack teleport
> off…*, *their own* saved places on the New world row with *Wormhole gates aren't affected
> by this switch.* under it, the Worlds-list glance, and the help box recorded in DESIGN.md
> ("Power Pack teleport"). The off line keeps *Saved places still work with the Tricorder*,
> and the switch is offered at creation. Built as `TransportRow` in
> `frontend/src/screens/MinecraftWorlds.tsx`. The independent review is `REVIEW.md` in this
> folder. Behaviour source:
> `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M10, "Teleport to a known place, one Power Pack per
> trip" and its "Shipped (wave mc-transport, 2026-10-11)" note, which ends *Next: the PWA
> toggle, through its own GUI gate.*

Three mocks of one control: the per-world **Power Pack teleport** switch. It sits beside the
binding keep-inventory row (`../minecraft-keep-inventory/`, pick A, shipped as `KeepRow` in
`frontend/src/screens/MinecraftWorlds.tsx`). Each mock is built on the binding Worlds mock
(`../minecraft-worlds/b-worlds-subscreen.html`). Each shows the world page, the Game rules
page and the New world form.

Open each `.html` in a browser. Each one is self-contained and works offline. It is
phone-framed (390 px; full-bleed under 430 px), tokens only, dark and light, follows the
system theme, keeps a 44 px tap floor and honours `prefers-reduced-motion`. A dashed **Mock
controls** strip is reviewer chrome. Every state opens by hash, for example
`b-settings-switch.html#scr=world&tp=off&where=other&fb=toast&theme=light`.

## What exists today (verified in code)

- **The slot field.** It is `power_pack_transport` and is **on by default**. An older record
  reads as on, and so does a new, imported or reset world. A restore from another world's
  backup carries the source world's value (`deploy/minecraft/server.py`,
  `deploy/minecraft/worlds.py`).
- **How it reaches the pack.** The value rides in the same `scriptevent jb:config {…}` line
  as `power_pack_keep` and `wreck` (`pack_config`). That line is sent at every start, and at
  once to the loaded, running world.
- **The routes.** The owner's route is `PUT /api/minecraft/worlds/{slot}/power-pack-transport
  {"on": bool}` and the debug route is `PUT /api/debug/minecraft/worlds/{slot}/...`. Both
  answer `applies: "now" | "next load"`. When this gate ran there was no PWA control; pick B
  is now that control.
- **What off does.** With the switch off, `transport.js` refuses every trip Dave offers.
  Dave's guide entry `power_pack_transport` is shown only where the switch is on. **It does
  not touch wormhole gates** (`wormhole.js` never reads it) and changes no game rule.
- **Create.** When this gate ran the New world route didn't take the field. It does now:
  `power_pack_transport` in the create body, on when left out.

## Shared words and behaviour (identical in all three)

| | Copy |
|---|---|
| Label | **Power Pack teleport** |
| On | *On — players can ask Dave to take them to their own saved places; each trip burns a Power Pack.* |
| Off | *Off — Dave won't teleport anyone in this world. Saved places still work with the Tricorder.* |
| Help (fixed, explains before anyone flips it, since a flip can apply live) | *Power Pack teleport: a player asks Dave for one of their own saved places, then says Yes on a form. One Power Pack burns only if they arrive, and they need their own Tricorder. Wormhole gates aren't affected by this switch.* |
| Applying spinner | the switch's knob becomes a spinner |
| Toast | *Applied live — teleport off* / *Saved — teleport off when the server starts* / *Saved — teleport off from Skyblock run's next load* (from the route's `applies`) |
| Applying | *Turning Power Pack teleport off…* (all three) |
| Refusal (400/409) | rose line: *Couldn't turn Power Pack teleport off — {sidecar reason}. Nothing changed; it's still on.* After a 503 or a dropped connection it never says *nothing changed*. It re-reads `/worlds` and words what the slot reports, the keep row's rule. |
| Pending | the amber *pending* tag while it waits for a start or the world's next load, as on Cheats |

It is a plain on/off switch, so it gets the Ops switch, not a segmented control. Only one
write is in flight at a time, and it doesn't interact with keep inventory: the two Power
Pack uses are independent on the sidecar. Summaries mention it only **when it's off** (the
default is on), so a normal world's glance lines don't grow.

## Revision 2 — answering `REVIEW.md`

- **Every variant:**
  - The light PNGs are re-taken. The first set lost the 13 % tints (the rose refusal, the
    steel switch track) when they were reduced to 256 colours. They are now palette PNGs
    that keep every frequent exact colour.
  - The toast is shorter, and *Power&nbsp;Pack* never breaks, so the On line has no lone
    *Pack.*
  - A new **Worlds list** state (`scr=list`) shows the glance with teleport off: *… ·
    teleport off* on that world's row.
- **B:** the applying line now reads *Turning Power Pack teleport off…*. The New world row
  keeps *their own* saved places, with a one-line *Wormhole gates aren't affected by this
  switch.*
- **A1:** the row no longer wears the game rule's *changed · default on* tag. Its one tag
  says what it is and how resets treat it: *world switch · off · Reset all leaves it alone*.
  The rules footer (`a-7-rules-footer-off-*`) states that Reset all and the group resets
  leave both Power Pack settings alone. The switch is never counted in *N rules differ*.
- **C1–C3, C5–C7:**
  - The card has its own status line (*Live — a change reaches the game at once* / *Not
    loaded — … next loads*).
  - The recipe moved from the header into the card's note (*8 Ender Pearls **around** a
    Blaze Powder*), so the header is one plain line at 320.
  - The switch is **Power Pack teleport**, with the shared On and Off lines and the fixed
    help.
  - The keep-inventory row uses the switch label's size.
  - The re-aim claim is gone.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-under-keep-row.html` | *Both Power Pack uses side by side.* A row directly under **Keep inventory on death** in the rules editor's Players group. |
| **B** | `b-settings-switch.html` | *A world setting like Cheats.* A switch row in the world page's **Settings — this world only**, built like Crash site at spawn. |
| **C** | `c-power-pack-card.html` | *A Power Pack card.* A card on the world page listing what a pack can be spent on: the teleport switch and a link to keep inventory. |

### A — under the keep-inventory row

- **The row** shares the keep row's full-width shape: the label, a steel tag *world switch ·
  on · Reset all leaves it alone* (no mono rule id, because it has none), the switch on the
  right, the on/off line, then the help on a quiet surface. It never wears the game rule's
  *changed · default …* tag, and the rules footer says both Power Pack settings are left
  alone by the resets.
- **The glance lines.** The Players group summary reads *13 rules + teleport · Power Pack*
  and adds *· teleport off* when it's off. The world page's Game rules line does the same.
- **New world.** The draft rules editor's Players group shows Keep inventory **Off · On**
  (unchanged), then this switch, default on.
- **For:** both Power Pack uses are in one place. The owner who just set keep inventory to
  Power Pack sees the other use under it. No new surface is added.
- **Against:** a non-rule sits in the editor that promises game rules, and the tag has to
  explain that. *Reset all* and the changed counts must leave it out, as they leave out the
  held keep rule, and that is one more exception. It's buried three pages deep (Worlds → world
  → Game rules) and the Players group gets taller still.

### B — a switch in Settings (recommended)

- **The row** is a switch row under Cheats, built as Cheats and Crash site at spawn are: a
  bold label, the on/off line, the switch, *pending* when it waits, and the help under it.
  On the loaded, running world the card's status line becomes *difficulty and Power Pack
  teleport change live; game mode and cheats apply at the next restart*. The pack is told
  at once, so that line is true.
- **New world.** A switch row under **Crash site at spawn**, default on, with the same line.
- **For:** it is what the field is: a per-world slot setting, not a game rule, beside the
  other per-world settings. It reuses the Cheats and Crash-site pattern exactly (DESIGN
  "Crash site at spawn" picked the same shape), it is found without opening the 39-rule
  editor, and it needs no exceptions in the rules editor's counts and resets.
- **Against:** it isn't next to keep inventory. The two Power Pack uses live on different
  pages, so the owner reads the help line to connect them. The Settings card gets one row
  taller.

### C — a Power Pack card

- **The card** is headed **Power Pack**. Its note reads *8 Ender Pearls around a Blaze
  Powder. What a pack can be spent on in this world — players need their own Tricorder for
  either.* Then come its own live / next-load status line, the **Power Pack teleport**
  switch row with the shared lines, and the fixed help. Last is **Keep inventory on death**,
  a navigation row (*On Power Pack — set in Game rules, under Players*) that pushes Game
  rules focused on the row.
- **New world.** The switch row, as in B, plus *Keep inventory's Power Pack mode is set once
  the world exists, under Game rules.*
- **For:** it explains the currency once and groups its uses, and it will scale to the next
  uses (powered armor, M14). Keep inventory stays where pick A put it.
- **Against:** one more card on the world page for one switch today. Two places mention keep
  inventory and only one changes it (the same weakness keep-inventory C had). The label
  drifts from the plan's name, *Power Pack teleport*.

## Rendered (`png/`, 390 × 844 at 2×, dark and light; plus a 320 × 568 applying state)

For each variant `v` in `a`, `b`, `c`:

- `png/{v}-1-on-live-{dark,light}.png` — on, the loaded running world;
- `png/{v}-2-off-pending-{dark,light}.png` — just turned off on a world that isn't loaded:
  *pending* and the toast;
- `png/{v}-3-refused-{dark,light}.png` — a 409, with the rose line;
- `png/{v}-4-new-world-{dark,light}.png` — the New world sheet;
- `png/{v}-5-applying-320-dark.png` — mid-write at 320 × 568;
- `png/{v}-6-worlds-list-off-{dark,light}.png` — the Worlds list glance with teleport off;
- `png/a-7-rules-footer-off-{dark,light}.png` — A only: the rules footer, with Reset all
  leaving the switch alone.

The render script audited every shot: no horizontal overflow, every control's hit area at
least 44 px (the switch's `::after` counted), and no console errors. All passed.

## Recommendation

**B** — the independent review's pick too. The switch is a slot setting with its own route, not a vanilla rule. B puts it with
the slot settings in the shape the screen already uses for Cheats and the crash site, and
keeps the rules editor honest about holding game rules. The help line names keep inventory's
sister rule in words, and the plan's own name for it (*Power Pack teleport*) stays. Choose
**A** if seeing both Power Pack uses in one place matters more than the rules editor's
purity. Choose **C** if more Power Pack uses are coming soon enough to justify the card.

## Owner picks

- **Which variant:** A, B or C.
- **The off line.** *Saved places still work with the Tricorder* tells players they have an
  alternative. Drop it if that's more than the owner wants to promise.
- **New world.** The mocks offer the switch at creation (default on), which needs the
  create body to accept the field. The alternative is the keep switch's rule: saved worlds
  only.
