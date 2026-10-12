# Independent review: the Power Pack teleport switch mocks (M10, transport on request)

> **Status:** Living · **Last verified:** 2026-10-12
>
> An independent review of `a-under-keep-row.html`, `b-settings-switch.html` and
> `c-power-pack-card.html` in `docs/mocks/minecraft-transport-switch/` (branch
> `feat/mc-mocks-transport-map`, commit `08ce0603`). The reviewer did not build them and had not
> seen the designer's reasoning (PROCESS.md, "GUI gate"). The README was read only for the brief
> and the states. Every judgement below is of the rendered PNGs, measured against
> `docs/reference/DESIGN.md` ("Principles", "Color tokens", "Typography", "Spacing & shape",
> "Accessibility", "Voice & microcopy", "Worlds and backups", "Crash site at spawn") and the
> keep-inventory precedent (`../minecraft-keep-inventory/REVIEW.md`). Nothing in the mocks was
> changed.

## How it was checked

- **Every PNG was viewed**: `{a,b,c}-{1..4}-*-{dark,light}.png` (390 × 844 at 2×) and
  `{a,b,c}-5-applying-320-dark.png` (320 × 568), 27 in all.
- **Contrast was measured** on the light PNGs (sampled token-to-surface): the help text, the steel
  tags (*a world switch · not a game rule*, *changed · default on*), the amber *pending* tag, the
  steel *Power Pack* in the Game rules line, the green live note and the primary *Create world*
  label. All are between **5.3:1 and 6.1:1**, so they pass. The dark theme passes by eye
  everywhere. The only sub-4.5 text is the dimmed world page under the New world sheet's scrim
  (≈1.9:1), which is correct for an inert background.
- **A PNG artefact, not a design fault.** In the light PNGs the 13 % **rose** tint behind the
  refusal line is absent (`*-3-refused-light.png`: the block is the card's own white). The CSS
  is right (`.errline{background:var(--rose-tint)}`). A spot render of the sister map mock in
  headless Chromium shows the non-steel tints painting in light, so this is the capture. Re-take
  the light PNGs before the owner judges them, or the refusal looks like plain text.
- **Not mockable from PNGs:** keyboard, reduced motion and hit areas. The README's audit claims
  (no overflow, every control ≥ 44 px) are taken as given. The switch rows are visibly ≥ 44 px
  tall in every shot.

## Cross-variant findings

### What holds in all three

- **Tokens and type.** Accent text uses `--steel-text` / `--warn-text` / `--ok-text`, and tints
  sit under them. The switch is the shipped Ops switch, and the segmented controls are the
  shipped `Seg`. There's no raw colour in the chrome.
- **The refusal is honest and follows DESIGN.** A real `busy:` reason (*the server is restoring a
  backup*) and *Nothing changed; it's still on* for a 409, which matches "Worlds and backups"
  (refusal says *nothing changed*; a 503 never does).
- **Applying.** The knob becomes an amber spinner and the meaning line names the step
  (`*-5-applying-320-dark.png`). Amber is DESIGN's in-progress colour.
- **Pending** is the shipped amber tag, on a world that isn't loaded (`*-2-off-pending-*`).

### Blocking

- None that apply to every variant.

### Non-blocking

- **The toast sits on content** at 390 (`a-2`: over *Fire damage*; `b-2` and `c-2`: over the
  Backups row). *Saved — Power Pack teleport off from Skyblock run's next load* wraps to three
  lines in the pill. It's the same as the keep-inventory note. Shorten it to *Saved — teleport
  off from Skyblock run's next load*, or lift the toast above the last card.
- **The applying line drifts from the spec.** The README's shared table says *Turning Power Pack
  teleport off…*. A renders that, but B and C render *Turning it off…*
  (`b-5-applying-320-dark.png`, `c-5-applying-320-dark.png`). Pick one. *Turning it off…* is
  better in a row whose label already names the thing.
- **"Their own saved places".** The On line says *their own saved places* on the world page and
  in the rules editor, but the New world rows (`b-4`, `c-4`) say *their saved places*. Keep
  *own*. It's the fact that stops a parent worrying about players teleporting to each other.
- **Glance lines when off.** The README promises that summaries mention the switch only when
  it's off. Only A renders one (the Players summary). None of the three shows the Worlds-list row
  or the world page's facts for a world with teleport off. Mock that row for the pick.
- **Light refusal tint.** Re-capture, as above.

## Variant A — under the keep-inventory row

**Verdict: pass with changes.**

**Blocking**
- **A1. The rules editor's counts and resets aren't shown with teleport off.**
  - `a-2-off-pending-*` gives the row *changed · default on*, the same tag a game rule wears.
    That tag tells the owner **Reset all** and the Players group reset will flip it back.
  - The README says they must not. That's the exception the keep row already needed (DESIGN:
    *the rule is left out of every changed count, the group reset and **Reset all***), and none
    of it is rendered.
  - **Fix:**
    - mock the Players group foot and the **Reset all** footer with teleport off;
    - either drop *changed · default on* from this row or make the resets include it, and say
      which in the README.

**Non-blocking**
- **A2. It's a non-rule in the "game rules" page, and it needs a tag to explain itself.**
  - *a world switch · not a game rule* is accurate, but the owner (a parent) has to read jargon
    to learn why one row behaves differently.
  - On the New world sheet (`a-4-new-world-*`) the switch sits under **World rules — Set before
    the world is generated**, which is untrue of a slot setting, and the summary becomes *13
    rules + teleport*.
- **A3. The tag row wraps to two lines** with three pills (*pending · a world switch · not a game
  rule · changed · default on*, `a-2-*`). It's the busiest row in the editor, on top of the keep
  row's ≈ 230 px.
- **A4. It's buried.** Worlds → world → Game rules → Players. For a switch whose only route
  today is the debug API, that's the deepest place the owner could look.
- **Strength:** both Power Pack uses are visible together (`a-1-on-live-*`). The live sticky note
  (*Live — a change applies in the game at once*) is true for it, as the pack is told at once.

## Variant B — a switch in Settings

**Verdict: pass with changes.**

**Blocking**
- None.

**Non-blocking**
- **B1. The On line has a widow.** *on — players can ask Dave to take them to their own saved
  places; each trip burns a Power / Pack.* wraps to three lines with *Pack.* alone at 390
  (`b-1-on-live-*`, `b-3-refused-*`). Use a non-breaking space in *Power Pack*.
- **B2. The help box is new in this card.** Cheats and Crash site have none. It's justified,
  because a flip applies live and the help explains the mode before it. Record it in DESIGN with
  the pick so the next switch row doesn't copy it by default.
- **B3. New world has no help** (`b-4-new-world-*`). That's acceptable, because nothing applies
  live there, but it is the only place the owner first meets the switch on a new world. A
  one-line *Wormhole gates aren't affected.* would cover the one thing a parent might assume.
- **B4. The Power Pack connection is in words only.** The Game rules row's *keep inventory Power
  Pack* (steel, 5.9:1 in light) sits two rows below the switch, so the two uses are near each
  other without being merged.
- **Strength:**
  - It's the shipped Cheats / Crash-site row exactly.
  - The card's status line was updated truthfully (*difficulty and Power Pack teleport change
    live; game mode and cheats apply at the next restart*, `b-1`).
  - *pending* sits by the label on a world that isn't loaded (`b-2`).
  - The refusal line sits between the switch and its help (`b-3`).
  - It fits 320 cleanly (`b-5`).

## Variant C — a Power Pack card

**Verdict: fail as rendered** (fixable, but three blockers).

**Blocking**
- **C1. Nothing on the card says where the switch applies.**
  - On the loaded, running world, the Settings line above still reads *Loaded and running —
    difficulty changes live; game mode and cheats apply at the next restart* (`c-1-on-live-*`).
  - The Power Pack card under it has no line of its own, so a parent infers *restart*, and the
    flip applies live. It's the keep-inventory round's C1 again.
  - **Fix:** give the card its own status line, worded from the slot (*Live — a change reaches
    the game at once* / *…when Skyblock run next loads*).
- **C2. The section header breaks at 320.**
  - `c-5-applying-320-dark.png` wraps **POWER / PACK** into two lines beside a two-line *8 Ender
    Pearls round a Blaze / Powder*. It reads as a two-column table and not a section header.
  - DESIGN's section header is one 12 px uppercase `--text-3` line. The right-hand recipe is
    bold, at 14 px or more, and out-shouts the header at 390 too (`c-1`).
  - **Fix:** move the recipe into the card's note, or set the header's meta in `--text-3` and let
    it drop below the header under 360 px.
- **C3. One switch, three names, and the shared copy isn't used.**
  - The card says **Teleport to saved places**, but the toast (`c-2`) and the refusal (`c-3`) say
    *Power Pack teleport*, and the New world sheet (`c-4`) labels the same switch **Power Pack
    teleport**.
  - The README's shared Off line (*Saved places still work with the Tricorder*) and the fixed help
    line are missing from the card. The card's lines instead say *ask Dave, say Yes on the form,
    one pack burns on arrival* and *Dave won't teleport anyone here*, which drop *their own*
    saved places.
  - **Fix:** label it *Power Pack teleport* everywhere (the card's title already supplies *Power
    Pack*, so *Teleport* alone also works, if the toast and the refusal match), and use the shared
    On / Off lines.

**Non-blocking**
- **C4. Keep inventory appears twice on one page.** The Game rules row says *keep inventory Power
  Pack*, and the card's **Keeping things on death** row says *Keep inventory is on Power Pack — set
  in Game rules, under Players*. Only one of them changes it (the keep-inventory round's C2
  weakness).
- **C5. The card's two rows don't share a type size.** **Teleport to saved places** is the small
  bold switch label, but **Keeping things on death** is a visibly larger nav-row title (`c-1`).
  Two sibling rows in one card should share a label style.
- **C6. The wormhole note adds a claim** the brief doesn't make: *a gate's planter always pays one
  pack per re-aim*. *Re-aim* is player jargon. Check it against the pack, and say *each time it's
  pointed somewhere new* if it stays.
- **C7. The recipe is spelled two ways.** The card says *round a Blaze Powder*, but the keep row
  says *around*.
- **Strength:** it explains the currency once, and it will scale when powered armour (M14)
  arrives.

## Cross-variant comparison

| | A — under keep row | B — Settings switch | C — Power Pack card |
|---|---|---|---|
| Says where it applies (live / next load) | Sticky note, yes | Status line, **updated** | **No (C1)** |
| Fixed help before a flip | Yes | Yes | **No (C3)** |
| One name for the switch | Yes | Yes | **No (C3)** |
| Matches an existing pattern | Extends the rules editor with a non-rule | Cheats / Crash site exactly | A new card |
| Depth from the world page | 2 taps | 0 | 0 |
| Needs exceptions elsewhere | Counts and resets (**A1**) | None | None |
| New world form | Inside *World rules* (A2) | Under Crash site | Under Crash site, plus a note |
| 320 px | Fine | Fine | **Header breaks (C2)** |
| Contrast / tokens | Pass | Pass | Pass |

## Recommendation

1. **B.** It's the only variant with no blocking issue. It reuses the exact row the owner already
   reads for Cheats and Crash site, its status line was updated truthfully, and it puts a
   switch with a live effect where the owner already changes live settings, with no rules-editor
   exceptions. Before it's binding:
   - fix B1 (the widow);
   - settle the applying line;
   - keep *their own* on the New world row;
   - mock the Worlds-list glance with teleport off;
   - record the help-box-under-a-switch pattern in DESIGN.
2. **A**, only if the owner values seeing both Power Pack uses together over the rules editor
   meaning *game rules*. A1 (counts and resets) must be mocked and decided first, and the New
   world placement under *World rules* needs rewording.
3. **C** last. Its idea, one card for the currency, is good for later, when M14 adds a third use.
   Today it carries three blockers: it doesn't say live, it breaks at 320, and it names the switch
   three ways. It also repeats keep inventory on the page.
