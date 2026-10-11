# Independent review: the keep inventory Off · On · Power Pack mocks (M10)

> **Status:** Living · **Last verified:** 2026-10-11
>
> An adversarial review of `a-segmented-row.html`, `b-value-sheet.html`, `c-world-setting.html`
> and `README.md` in `docs/mocks/minecraft-keep-inventory/` (branch `mocks/mc-keep-inventory`,
> commit `cf3eb672`). It was written by a reviewer who did not build them (PROCESS.md, "GUI gate"
> and "Delegation"). It is measured against `docs/reference/DESIGN.md` ("Minecraft server
> screen", "Worlds and backups"), the binding Worlds mock, `docs/plans/MINECRAFT_BEDROCK_PLAN.md`
> §M10 ("Shipped (wave mc-items)"), and the shipped code: `deploy/minecraft/server.py`,
> `backend/src/jbrain/api/minecraft.py`, `frontend/src/minecraftWorlds.ts`,
> `frontend/src/screens/MinecraftWorlds.tsx` and `MinecraftWorldSheets.tsx`. Nothing in the mocks
> was changed.

## How it was checked

- **Rendering.** Headless Chromium (playwright-core 1.56, `/opt/pw-browsers/chromium`) at
  **390×844 and 320×568**, dark and light, with the system theme driving the default. That
  covered:
  - every hash state: 3 worlds × 4 starts × {rules, world} screens, plus B's sheet open in each;
  - these flows, driven by real clicks:
    - Off → Power Pack, held in flight and landed;
    - Power Pack (saved On) → Off at step 1, step 2 and landed;
    - *Refused (409)*;
    - *Refused after Power Pack off*;
    - the rules footer with Power Pack on;
    - C's **Change** jump.

  That makes 520 screenshots, plus element crops of the Players summary and the icon at 3× DPR.
  I looked at contact sheets of every flow in both themes at both sizes.
- **DOM audit on every shot.** It measured:
  - text contrast, composited token-to-surface;
  - hit areas, with `::after` extensions counted;
  - page and phone overflow;
  - clipped and ellipsised text;
  - console errors and warnings.

  A self-test first caught an injected 1.79:1 label, a 20 px button and a clipped line.
- **Fit at real type sizes.** The shipped `Seg` is `--fs-secondary` (14 px at Text size 100%),
  equal-flex, with no `nowrap`. The A segments were re-measured at 12, 13 and 14 px, with and
  without the mock's `flex: 1.3`.
- **Reduced motion.** With `reducedMotion: 'reduce'`, every spinner, the radio spinner, the sheet
  and C's flash compute to `animation: none` and `0s`.
- **Screenshots** are in the reviewer's scratchpad (`…/scratchpad/keepinv-review/shots/`, named
  `{variant}-{w}x{h}-{theme}-{state}.png`). They are not committed.

**What passed everywhere:**
- no console errors or warnings;
- no page or phone overflow, and no clipped or ellipsised text (at the mocks' own type sizes);
- every control ≥ 44 px, including the segments (76 / 76 / 98 px wide at 320) and B's value
  button;
- all text ≥ 4.5:1 in both themes, reviewer chrome included;
- reduced motion honoured.

The README's render-check claims hold. The findings below are mostly what an audit can't see:
whether the words are true against the sidecar, and whether a parent can tell what the choices do.

## Facts that supersede the README

Each fact was read from the shipped code, and each one changes copy or behaviour in **all three
variants**.

| # | The mocks / README say | What is true | Where it shows | Fix |
|---|---|---|---|---|
| F1 | *Refused (409)*: *wait, the server is backing up Castle Hill*. | A backup does **not** take the lifecycle lock. `snapshot()` / `cold_snapshot()` run outside `_locked()`, so a rules or Power Pack write during a backup succeeds. The real 409s are `busy: loading a world`, `importing a world`, `resetting a world`, `restoring a backup`, `receiving a world upload`, `updating`, `restart`, `installing`, and a console `not running`. | Both refusal states in A, B and C. | Use a real reason, e.g. *the server is restoring a backup* or *restarting*. The shipped pattern quotes the detail: *Couldn't save the rule — busy: restoring a backup.* |
| F2 | A refused write means *Nothing changed*. | That is only true for a refusal that comes **before the save**: the `busy:` 409, the managed-rule 400 and a validation 400. `set_power_pack` saves the slot (`power_pack_keep`, `rules`) **first**, then sends the console lines. A `ConsoleError` there (the server stopping mid-write) returns 409 or 400 with the switch **already saved**. `_set_rules` behaves the same way (saved, then *the server refused keepInventory*). | *Refused (409)* on a Power Pack write; the second step of leaving Power Pack. | After any failure, re-read the slot and the rules (the shipped `RulesLayer.save` already re-reads the rules) and word the line from what was read back. Say *nothing changed* only when the re-read confirms it. Per DESIGN, a 503, a dropped connection or a timeout never says *nothing changed*. None of these states is mocked. |
| F3 | Turning Power Pack off hands keepInventory back to *your saved On / Off*. | The saved plain choice is `slot.rules.keepInventory` from `GET /worlds` (`worlds_view` spreads the slot record). The rules view never shows it. When the key is absent, `set_power_pack(off)` writes **Off** (`rules.setdefault("keepInventory", False)`). New worlds made without that rule, imports and resets start with `rules: {}` (`server.py:865`). So **Castle Hill, an import whose file had keepInventory on**, shows *On*, goes to Power Pack, and comes back *Off*. The step line *goes back to your saved On…* would then be false. | A's and C's applying line; B's *Your plain choice underneath is On*; both refusal-after-off lines. | Sidecar (preferred, one line): when the switch turns **on**, capture the current value, `rules.setdefault("keepInventory", known["keepInventory"])`. Then *saved* is always what the owner last saw. Failing that, the client derives saved as `slot.rules.keepInventory ?? false` and the copy says *back to Off* for a world with no saved choice. |
| F4 | The row carries *pending* after any change to a world that isn't live. | The API's `pending` compares effective keepInventory with `rules_known`. Turning Power Pack on where the world already keeps (saved On, last loaded On) reports **no** pending, though the pack's switch waits for the next load. Skyblock run (never loaded) with Power Pack on reports keepInventory **pending from the start**, but the mock's start state shows none. The mock's pending is `S.pend = true` after any write and never clears (Off → On → Off still says pending). | A's and B's row tag, C's *On death* tag, the world page's *1 pending*. | Either the sidecar reports the switch (e.g. a `power_pack_pending` on the rules view, from the pack's last `jb:config`), or the README says Power Pack pending is device-remembered, as DESIGN already allows for a not-loaded world's settings. The keepInventory tag itself keeps following the API. |
| F5 | Loaded · stopped: *applied when the server starts*. | Correct in effect: `_apply_rules_when_up` runs on every `_start()`. But `set_power_pack` returns `applies: "next load"` for the loaded, stopped world. | Toast and sheet foot. | Word it from `s.active`, as `rulesNote()` does. Never print the API's `applies` verbatim. |
| F6 | *Reset all* leaves Power Pack alone. | The shipped editor **breaks** once the switch is on, and the debug API can turn it on today. In the rules view `keepInventory` is `true` ≠ default, so: <br>• `changedRules` counts it; <br>• the switch shows On, and a tap is refused (400, *keepInventory is managed by the Power Pack charm while it is on*); <br>• **Reset all** and **Reset players to defaults** include it, and `_set_rules` rejects the **whole batch**. <br><br>The mocks fix Reset all, but they don't render the per-group reset link at all. | Rules footer; the Players group. | The build excludes `view.managed` ids from the changed counts, the group reset and Reset all. Mock the group link with Power Pack on. |
| F7 | — | `MinecraftRules` has no `managed`, `MinecraftSlot` has no `power_pack_keep` or `rules`, and there is no client call for `PUT …/power-pack`. | Build. | Additive. Name them in the README's build notes. |
| F8 | Only C needs New world / Import support (*not mocked*). | The New world sheet renders the **same `RulesEditor`** (`MinecraftWorldSheets.tsx`, `pending={[]}`). `MinecraftNewWorld` has no Power Pack field. In A and B, the three-way row would appear in the create form, where Power Pack can't be saved. | New world sheet, A and B. | Decide one of two: the draft editor shows plain Off / On only, or create sends a follow-up `PUT …/power-pack`. Say which in the README. |

## Cross-variant findings

### Blocking

1. **Refusal copy is not truthful against the API (F1, F2).**
   - Every refusal names a cause the sidecar never refuses for (a backup).
   - The first-write refusal says *Nothing changed* without knowing it. A Power Pack write that
     fails at the console has already saved the switch.
   - **Fix:**
     - real `busy:` reasons;
     - re-read and render from the re-read;
     - a mocked 503 / dropped-connection state that says the server may still be working;
     - a mocked stale-device 400 (*managed by the Power Pack charm*) that re-reads and shows
       Power Pack.
2. **"Back to your saved choice" can be false (F3).** It shows in A's and C's step line, B's
   sheet, and every refusal-after-off line. Fix it in the sidecar, or change the copy to match
   `?? false`.
3. **The managed rule must be excluded from every reset and count (F6), and the spec must say so.**
   - The README covers Reset all only.
   - Add the group reset to the mock and the rule to the README. Today's shipped editor is broken
     the moment the switch is on.

### Non-blocking

- **Whose Tricorder.** The help line says *the player's own Tricorder*, but A's meaning line, B's
  radio and C's card say *your own Tricorder*. The reader is the owner, a parent, who has no
  Tricorder in mind. Use *their own Tricorder* everywhere. Consider *(Dave gives every player
  one)* so a parent doesn't wonder where it comes from.
- **Jargon.** *Vanilla keepInventory* means nothing to a non-player parent. Use *Minecraft's
  normal keep-inventory rule — everyone keeps everything.* C's *Drop everything / Keep
  everything* labels are the clearest words in the round. They are worth borrowing into the
  meaning lines whichever variant wins.
- **The two-write transition is truthful but avoidable.**
  - The mocks order it correctly (switch off, then rule), name both steps, and the
    refusal-after-off line states the real intermediate state, which I verified against
    `set_power_pack`.
  - A one-field API change, `PUT …/power-pack {"on": false, "keep": true|false}`, set under the
    one lock, would remove the partial state and *Refused after Power Pack off* entirely. Owner
    pick: *Leaving Power Pack* is easier with it.
- **Long toasts.** *Saved — keep inventory: Power Pack, applied when Skyblock run next loads*
  wraps to five lines in the 999 px-radius pill at 320 and sits on the control (A, B and C). Shorten
  it, e.g. *Saved — Power Pack from Skyblock run's next load.*
- **The Power Pack icon.**
  - It is game-item pixel art in UI chrome. DESIGN's Iconography is Lucide-only with no filled
    icons, so record a carve-out for item art with the pick.
  - It is drawn at 18–20 px from a 16 px source, a non-integer scale. Uneven pixels are visible
    at 3× DPR. Draw it at 16 px, or ship a 32 px source.
- **Keyboard.** The three-way control is a `radiogroup` of `role="radio"` buttons, all
  tab-stops, with no arrow keys. That matches the shipped `Seg` and `.mc-opt` lists. Fix it once
  in `Seg` (roving tabindex and arrows) rather than here.
  - The mock re-renders by `innerHTML`, so its `role="status"` applying line is a new node each
    step and may not be announced. In the build, keep one persistent live region.
- **Fixture.** Skyblock run starting on Power Pack should show *pending* (F4). The *Refused after
  Power Pack off* control does nothing when the pick equals the saved choice (one write), and the
  README should say so.

## Variant A — segmented in the row

**Blocking**
- **A1. Power Pack isn't explained until it's picked, and picking applies live.**
  - With Off or On selected, the row shows that pick's meaning plus the help line. The help line
    describes the **item** (*8 Ender Pearls around a Blaze Powder. It only works with the
    player's own Tricorder*), not the **mode**.
  - Nothing visible says Power Pack keeps your things, or that a pack burns, until the parent taps
    it. On Castle Hill that tap applies live, with two players on.
  - Seen in `a-*-live-off-rules` and `a-*-live-on-rules`, both widths and themes.
  - **Fix:** the fixed help line states the mode: *Power Pack: players keep everything only while
    carrying a Power Pack and their own Tricorder — one burns per death. Made from 8 Ender Pearls
    around a Blaze Powder.* The meaning line stays as it is.

**Non-blocking**
- **A2. It only fits 320 with bespoke Seg CSS.**
  - The mock sets 13 px, then 12 px with `flex: 1.3` on the last segment at ≤ 360.
  - The shipped `Seg` is 14 px, equal flex, with no `nowrap`. Measured at 320:
    - 14 px equal: *Power Pack* needs 94 px in 83;
    - 14 px with `flex: 1.3`: 101 in 98;
    - 12 px equal: 88 in 83.
  - At the default Text size (75%) it fits. At 90–100% it wraps to two lines in a 44 px segment.
  - **Fix:** give `Seg` an icon slot and per-option weight (or a ≤ 360 rule) in the build, and
    re-measure at Text size 100%.
- **A3. The saved plain choice is invisible while Power Pack is on.** It surfaces only in the step
  line when leaving (*goes back to your saved On…*). Borrow B's line under the meaning: *Under it:
  On — comes back if you turn Power Pack off.*
- **A4. Players summary wraps mid-name at 320.** It renders *15 rules · 1 pending · Power / Pack*.
  Use a non-breaking space.
- **A5. `Seg` grows an API.** The build needs icons, a busy spinner that replaces the icon, and
  `aria-disabled` siblings, all as shared `Seg` props rather than a fork. With the spinner in
  place, the Power Pack segment loses its icon mid-write, which is fine since the label stays.
- **A6. It is the tallest row in the editor** (≈ 230 px at 320 with the help line). It is
  acceptable, because search finds it and it sits first in Players.

## Variant B — the value opens a sheet

**Blocking**
- None of its own. Cross-variant 1–3 apply.

**Non-blocking**
- **B1. At 320×568 the sheet's foot is below the fold.**
  - The foot holds where it applies, the fail-safe and the applying step.
  - With Power Pack on, the options fill the 88% sheet, so *Applies to Castle Hill live, at
    once* and *Turning Power Pack off…* need a scroll (`b-320x568-*-sheet`,
    `*-packOn2off-step1`). The parent picks without seeing *live*.
  - **Fix:** put the *applies* line under the sheet head. Pin the step line, or put it in the
    picked radio's sub-line.
- **B2. The row squeezes at 320.**
  - The ≈145 px *Power Pack ›* button leaves the label about 110 px. *Keep inventory / on death*
    wraps, and *kept with a Power Pack and your Tricorder* runs four lines.
  - **Fix:** shorten the Power Pack sub-line (*kept with a Power Pack*), or drop it under 360 px.
- **B3. The value button is new chrome.** The editor's precedent for a non-switch value is the
  `select` of choice rules. It is fine as a pattern, but record it in DESIGN with the pick.
- **B4. Depth.** It is a Sheet over Game rules over the world page. That is the same depth DESIGN
  already accepts for restore. In the mock, Escape and the close button return focus to the
  value button (swipe-down is not mocked).
- **Strength:** it is the only variant that shows all three meanings, the recipe, the saved
  choice and the fail-safe **before** a commit. A refusal keeps the sheet open with the radio put
  back.

## Variant C — On death on the world page

**Blocking**
- **C1. The world page's status line hides that On death applies live.**
  - The shared Settings line reads *Loaded and running — difficulty changes live; game mode and
    cheats apply at the next restart.* On death sits under it and isn't named, so the reader
    infers *restart*.
  - **Fix:** *…difficulty and On death change live; game mode and cheats apply at the next
    restart.*

**Non-blocking**
- **C2. Two places, one editable.**
  - The rules editor stops being *every game rule*.
  - New world and Import have no place for it (F8 is worse here: there is no draft row to point
    at).
- **C3. The flash ring is clipped at the card edge at 320.** It hugs the edge, with no padding
  inside the card (`c-320x568-dark-change-jump`). It is cosmetic.
- **C4. The toast covers the cards** on a not-loaded save at 320 (cross-variant toast note).
- **Strength:** the plainest labels in the round, *Drop everything / Keep everything / Keep with
  a Power Pack*. All three meanings are visible before a pick, and it matches the plan's first
  sketch (a switch on the world's page).

## Cross-variant comparison

| | A — segmented row | B — value sheet | C — world setting |
|---|---|---|---|
| Parent sees what Power Pack **does** before committing | **No** (A1: item, not mode) | Yes, in full | Yes, briefly |
| Pack + own Tricorder + one burns stated | After the pick only | Yes | Yes |
| Taps to change | 1 | 2 | 1 (from the world page) |
| A mis-tap on a live world | Applies at once | Applies at once (in a sheet) | Applies at once |
| Where it applies, visible at pick | Sticky note (yes) | Sheet foot, **off-screen at 320×568** | Status line, **wrong for On death** (C1) |
| Saved plain choice shown | Only when leaving | Yes | Only when leaving |
| Applying / two-step honesty | Good | Good | Good |
| Refusal copy | F1/F2 (all) | F1/F2 (all) | F1/F2 (all) |
| Fit with the shipped editor | Extends `Seg` (A2, A5) | New value button (B3) | Moves a rule out (C2) |
| 320 px | Fits only with bespoke type (A2) | Row squeezes (B2) | Fine |
| New world form (F8) | Needs a decision | Needs a decision | Uncovered |
| Contrast / 44 px / overflow / console | Pass | Pass | Pass |

## Recommendation

**A, as the README recommends, but only with A1 fixed.** Keep inventory is a rule, and the rules
editor is where the owner already looks. A changes the least, keeps one place to set it, and
reuses the segmented control the world page already has. Its one real weakness is that a parent
can't tell what Power Pack does until they pick it, live. A rewritten help line closes that at no
cost in taps.

Conditions before A is binding:
1. **A1:** the help line states the mode (keep only with a pack **and** their own Tricorder, one
   burns) as well as the recipe.
2. **Cross-variant 1 (F1, F2):**
   - real refusal reasons;
   - re-read after any failure;
   - *nothing changed* only when confirmed;
   - mocked 503 / dropped-connection and stale-device managed-400 states.
3. **Cross-variant 2 (F3):** the sidecar captures keepInventory when the switch turns on, or the
   copy matches `?? false`.
4. **Cross-variant 3 (F6):** managed ids are excluded from the changed counts, the group reset and
   Reset all, and the group reset is mocked with Power Pack on.
5. **F4, F8:** the README says where Power Pack *pending* comes from and what the New world form
   shows.
6. **A2:** the shipped `Seg` is extended with props (icon, weight, busy), not forked. The row is
   re-measured at 320 at Text size 90% and 100%.
7. **Wording:** *their own Tricorder*, and no *vanilla keepInventory*.

**Recommended, not required:** the one-call `PUT …/power-pack {"on": false, "keep": …}`. It
removes the partial-failure state, and *Leaving Power Pack* stops being an owner question.

**Choose B** if the owner wants the explanation, the saved choice and the fail-safe at the moment
of choosing over one fewer tap. Fix B1 (*applies* under the sheet head) first. **Choose C** only
with C1 fixed and an answer for the New world form.
