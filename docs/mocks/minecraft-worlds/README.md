# GUI gate — Minecraft worlds and backups (waves M2 + M3)

> **Status:** Plan · **Last verified:** 2026-10-10
>
> Decision: **B — the Worlds sub-screen** (owner, 2026-10-10). Three variants are below (revision 2, after an independent
> review). The chosen one becomes the binding spec for M2 and M3's frontend, and its reasoning
> lands in `docs/reference/DESIGN.md` ("Minecraft server screen") in the implementing PR.
> Behaviour source: `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M2 and §M3, the owner's game-rules
> and seed additions (2026-10-10), and the backend behaviour fixed after the review
> ("Verified facts" below).

These are three interactive mocks of the **Worlds** and **Backups** management. They are added
to the binding Minecraft screen (`../minecraft-ops/b-dedicated-screen.html`, built as
`frontend/src/screens/MinecraftScreen.tsx`). They cover:

- the world slots: load, new world, import, rename, reset, per-world settings and **every game
  rule**;
- server-wide settings and the allowlist;
- each world's backups: back up now, pin, delete, restore (into the same or another slot),
  download, retention, and the last off-box copy.

Open each `.html` in a browser. Each file:

- is self-contained and works offline;
- is phone-framed: 390 px, 560 px on desktop, full-bleed under 430 px, and it holds at 320 px;
- copies screen B's tokens and component styles verbatim, in dark and light, with tokens only;
- uses the accent text tokens (`--ok-text`, `--warn-text`, `--danger-text`, `--steel-text`) for
  accent-coloured words;
- uses Lucide-style outline icons;
- keeps a 44 px tap floor on every control, text links included;
- honours `prefers-reduced-motion`.

A dashed **Mock controls** strip above the phone is reviewer chrome, not design. It flips
between every state, picks what the mock's *Choose file* returns, and switches the theme. B
also jumps between screen levels, and C between tabs.

## Shared fixture (identical in all three)

| Slot | World | Origin · seed | Settings | State |
|---|---|---|---|---|
| 1 | **World** | made on first boot · `-2794311108712645813` | Survival · Normal | 3.1 MB, last played Oct 4 |
| 2 | **Castle Hill** (loaded) | imported .mcworld · `-6104328617705162911` (from its level.dat) | Survival · Normal, rules at the defaults | 7.8 MB, playing now |
| 3 | Creative test | new world · `8675309` | Creative · Peaceful · cheats; day cycle, weather and mob spawning off | 5.2 MB |
| 4 | Skyblock run | new world · `5127438807419962211` | Survival · Hard (pending); keepInventory on (pending) | not generated yet |
| 5 | — | empty | | |

Backups: Castle Hill has 4. They are *before the castle roof* (yours, pinned), two automatic ones
(before loading Creative test, and before the update to 1.26.52.3) and *first night* (yours,
downloaded Oct 4, 8:20 pm, the world's last download). World and Creative test have one
automatic backup each. Each backup carries its world's seed and settings, as its level.dat
would. The server runs 1.26.52.3 with BlockyFox and Mira_P on. The allowlist is on, with the
four known players.

The import files the mock offers are:

| File | Result |
|---|---|
| `Hilltop Village.mcworld`, 9.4 MB | Imports normally. |
| `Hilltop Village.zip` | No `level.dat`, so it isn't a Bedrock world. |
| `Big build — Hilltop City.mcworld`, 300 MB | Warned: *Over 100 MB only uploads at home on the Wi-Fi.* |
| `Map pack — Skyblock XL.mcworld`, 1.3 GB | Refused: the limit is 1 GB. |

The good files bring their own settings: Survival · Easy, no cheats, seed
`3141592653589793238`, keepInventory and showCoordinates on.

| State (mock controls) | What it shows |
|---|---|
| Running · 2 on | The default. |
| Server stopped | **Load** says the world becomes the loaded one and the server stays stopped (no backup clause, since none is taken). Settings and rules on the loaded world are *pending — applied when the server starts*. Allowlist names apply when it starts. |
| Fresh box · empty slots | The day M2 lands: only the first-boot World, four empty slots, no backups, *No copy has left the box yet*. |
| Importing | `Hilltop Village.mcworld` over Creative test. First **Uploading** with this device's real bytes, then the job's phases (*backing up*, *writing*) with elapsed time. It animates. |
| Import · not Bedrock | The `.zip` has no `level.dat`. Nothing is written, the upload is discarded, and **Choose another file** is offered. |
| Import · 300 MB | The sheet warns *Over 100 MB only uploads at home on the Wi-Fi* (it fails over the Cloudflare tunnel), but still allows it. |
| Import · 1.3 GB | Refused in the sheet before anything uploads (*the limit is 1 GB*). |
| Seed unreadable | Castle Hill's level.dat couldn't be read. Its seed reads *unknown — not recorded*, and Reset withholds **Same seed**. |
| Loading a world | Switching to Creative test with players on: *warning players* (10 s) → *stopping* → *backing up* → *writing* → *starting*, one phase at a time with elapsed time. The status block reads *switching worlds*. |
| Reset in progress | Creative test reset to a new seed (not loaded, so the server isn't touched). |
| Restore in progress | *first night* restored into the loaded Castle Hill, with a 10 s warning and a restart. |
| Update running | Every world action is disabled, with the reason: *Busy updating Minecraft — loading, importing, resetting, restoring and backing up come back when it's done.* Rename, settings, rules, downloads, pins and the allowlist stay live. |
| Refused · update started | The API refused a Load because auto-update began first. A rose line says nothing changed and Castle Hill is still loaded. |
| At 20 backups | Castle Hill at 20 of 20. The retention line and the Back-up-now sheet both name the backup the next one removes. |

## Behaviour common to all three

- **One world is loaded at a time.** It is marked *loaded* with a green disc or chip dot, and a
  full-width **world loaded** tile heads the status block above the online and version tiles. The
  tile is its own row, so a long name wraps instead of truncating at 320 px.
- **Live means loaded and running.** Each world's settings say so in a line:
  - **Difficulty** changes live on the loaded, running world.
  - **Game mode** is the default for new players and new characters, applied at the next
    restart. Anyone who has played keeps their own mode.
  - **Cheats** apply at the next restart.
  - On the loaded world with the server stopped, every change is *pending — applied when the
    server starts*.
  - On a world that isn't loaded, every change is *pending — applied when it next loads*.

  An amber **pending** tag marks each waiting setting and rule, and a world's row counts them.
  Saved settings and rules are re-applied on **every** start, not only on a load.
- **The game-rules editor** covers all 39 rules the owner's server reports, at their defaults:
  - The rules are in seven collapsible groups: World, Time & weather, Players, Mobs & drops,
    Crafting, Commands, Display.
  - Each rule has a plain-English label, the rule id in small mono text, and a *changed · default
    X* tag when it differs.
  - Booleans are switches. Numbers are steppers with a typeable value, **debounced**: the value
    moves at once, and one save goes out when tapping or typing settles (700 ms). Four taps make
    one save and one toast.
  - `playerWaypoints` offers the current value plus *everyone* only. On this server both are
    *everyone*, so it reads as a fixed value, *the only value this server reports*.
  - A search box filters in place.
  - There is **Reset {group} to defaults** per group and **Reset all**.
  - The live/pending note at the top is **sticky** while you scroll the rules (in C, it sits just
    under the sticky world strip).
  - Reset all has room beneath it, so a toast never covers it. This is measured in all three
    variants at 390 and 320 px.
- **Load** confirms in the centre Dialog, in one sentence:
  - With players on, it is the destructive variant: *"BlockyFox and Mira_P get a 10-second
    warning in chat and are disconnected; Castle Hill is backed up first, then Creative test
    starts."*
  - With nobody on, it is a plain primary button.
  - With the server stopped: *"Creative test becomes the loaded world — the server stays stopped
    until you start it."*
- **The 10-second chat warning** is named wherever an action stops the loaded world with players
  online: Load; import, reset or restore of the loaded world; Stop; Restart.
- **New world** is a Sheet with these fields:
  - **Name**, up to 40 characters.
  - **Seed**: **Random** shows the 64-bit number it rolled, with **Re-roll**. **Enter a seed**
    takes any text up to 64 characters, with a counter, matching Bedrock's own seed box.
  - **Game mode**, **Difficulty** and a **Cheats** switch.
  - **World rules**, a collapsed step that holds the full editor at the defaults.

  The world is generated the first time it loads.
- **Import** is a Sheet with these parts:
  - Windows export help (*Play → pencil → Export World*).
  - The chosen file and its size. A file over 100 MB gets the home-Wi-Fi warning, and one over
    1 GB is refused before uploading.
  - A slot picker, when opened from outside a slot.
  - **"Settings come from the world file"**: its game mode, difficulty, cheats, seed and rules.
    The overwritten slot's settings aren't kept.
  - *"Overwriting takes a backup first."*
  - A hint that the file is checked for a `level.dat` and the 1 GB limit before anything is
    written.

  Importing over an occupied slot then confirms in the **Dialog**: *"Creative test is backed up
  first, then replaced by Hilltop Village with the file's own settings."* It is warn-toned, or
  danger-toned with the 10-second warning when it is the loaded world with players on.
- **Reset** is a Sheet with three options, then the Dialog:
  - **Same seed**: offered only when the seed is known.
  - **New seed**: with the same Random/Enter seed choice as New world.
  - **Empty**: not offered for the loaded world.

  The Dialog asks the owner to type the world's name, and its button stays disabled until the
  name matches. An emptied slot keeps its backups.
- **Rename** is a Sheet with one field, capped at 40 characters, with a counter.
- **Backups belong to a world.** Each list shows:
  - the label, or the automatic reason;
  - *yours* or *automatic*;
  - the size, the date and *pinned*.

  **Back up now** takes an optional label.
- **Restore** goes Sheet → Dialog. The Sheet picks the target, and says what comes with it:
  - **Its own world**: rolled back, backed up first.
  - **Another occupied world**: replaced. The backup's seed and settings come too, and that world
    is backed up first.
  - **An empty slot**: it becomes *"Castle Hill (Oct 4)"*, with the backup's seed and settings
    and the source world's rules.

  The Dialog's one sentence repeats the backup-first, plus the restart and 10-second warning when
  the target is loaded.
- **Pin** keeps a backup for good, outside the 20. **Delete** is **disabled on a pinned backup**,
  with *Unpin it first — a pinned backup can't be deleted.* An unpinned delete confirms in the
  Dialog.
- **Download .mcworld** records per-backup *downloaded {when}*, shown in the backup sheet. It
  also records the world's last download: **Last copy off the box: Oct 4, 8:20 pm — "first
  night"**, or the amber **No copy has left the box yet**, which says Minecraft backups aren't in
  the box backup.
- **Retention is explained where it acts**: *3 of 20 kept · 1 pinned*, a meter, and *Each world
  keeps its newest 20. Automatic ones go first; pinned ones are kept for good, outside the 20.*
  At the limit, it names the backup the next one removes.
- **Server-wide settings** are:
  - server name;
  - max players (1–30);
  - view distance (5–32);
  - **5 world slots**, fixed and read-only;
  - *Xbox sign-in required*, always on.

  They apply at the next restart, or when the server starts if it is stopped. With every slot in
  use: *All 5 slots in use — reset or empty one first.*
- **Allowlist**:
  - Turning the list **on or off applies at the next restart**. It shows *pending* until then,
    and turning it off confirms in the Dialog.
  - **Adding or removing a gamertag** is live while the server runs, otherwise *applied when the
    server starts*.
  - **Required for internet play** (*coming later*, since R1 isn't built).
- **Jobs show what `/status` reports, nothing more.** The job card shows:
  - the job's *what* (e.g. *Importing a world*);
  - its current **phase** (*warning players*, *backing up*, *stopping*, *writing*, *starting*),
    with a one-line explanation;
  - the elapsed time since `started_at`, ticking;
  - *Started 5:53 pm. It carries on if you leave — any device opening this screen sees it.*

  The only extra on the device that started an import is the upload's real bytes and meter.
  There are no per-step timestamps, no finished-step history, and no per-world "busy" marker,
  since the job doesn't name its world. The card sits where each variant shows world state: the
  Worlds section in A, the Worlds and world pages plus the main-screen row in B, and above the
  tabs in C. One job runs at a time: while it does, the other world actions are disabled and
  say *Wait — the server is {what}.*

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-sections-in-place.html` | *More of the same screen.* **Worlds** and **Backups** sections in screen B's scroll. |
| **B** | `b-worlds-subscreen.html` | *A place for worlds.* One row on screen B pushes **Worlds**, and each world pushes its own page with its backups. |
| **C** | `c-world-switcher.html` | *Look through one world.* A chip switcher at the top scopes the screen, with **World · Rules · Backups · Server** tabs. |

### A — sections in place

Screen B keeps its order. **Worlds** follows Online now:

- Every slot is a list row. A row shows the name, *loaded*, any pending count, mode,
  difficulty, size, last played and last backup.
- A row **expands in place** (DESIGN "row-level detail → inline expansion"). Open, it shows its
  facts with the seed and Copy, settings, a **Game rules** row (which opens a Sheet), Load,
  Rename, Import over and Reset, and *See its N backups*.
- Empty slots carry **New world** and **Import** directly.

**Backups** follows, one world at a time, picked by a chip row. Its card holds **Back up {world}
now**, the off-box line and retention, then rows that expand in place to Restore…, Download, Pin
and Delete. Server settings, the allowlist and Join close the scroll.

- **For:** the cheapest to build and the most native. Everything is one scroll, with no new
  routes. The slot list compares worlds side by side.
- **Against:** the screen gets long: about 4,350 px with one world and one backup open, against
  about 2,400 px for B's main screen. A world's backups sit away from the world itself, and the
  game rules go into a tall Sheet.

### B — Worlds sub-screen

Screen B gains one row under the status block, **Worlds & backups**. It shows *Castle Hill loaded
· 4 of 5 slots used* and *last copy off the box Oct 4* (amber when nothing has ever left), or the
running job's *what — phase*. Server settings, the allowlist and Join stay on screen B.

- **Worlds** groups the slots as *Loaded*, *Other worlds* and *Empty slots*, with the job card on
  top.
- **A world's page** has a status block (*loaded* / *not loaded · Castle Hill is loaded*) with
  facts and **Load**. Below it are **Settings** with a **Game rules** row, **Backups** for this
  world, and **Manage** (Rename, Import over, Reset).
- **Game rules** is its own pushed page, with the sticky live/pending note at the top.
- A backup row opens a **Sheet** with its facts, Restore…, Download, Pin and Delete.

- **For:** it matches the plan's own wording (*Ops → Minecraft → Worlds*) and the Data screen's
  precedent. Each world's backups live with the world (*backups belong to a slot*). Screen B
  stays short and about the server. The 39 rules get a full page.
- **Against:** depth. A restore from the main screen is Worlds → world → backup Sheet → restore
  Sheet → Dialog. Comparing worlds' backups means going back and forth. It adds two routes.

### C — world switcher

A sticky **chip strip** under the top bar lists every slot. The loaded world has a green dot
(amber while a job is bouncing the server), and empty slots a dashed outline. Picking a chip
scopes the screen:

- **The status block becomes that world's.** For the loaded world it is the built hero. For
  another world it reads *not loaded · Castle Hill is loaded · 2 players on*, with mode, size and
  backup tiles and **Load {world}**. For an empty slot it offers **New world** and **Import**.
- **Tabs (World · Rules · Backups · Server)** have tab semantics and arrow keys. At 320 px a tab's
  count stacks under its label, so nothing truncates.
  - **World**: facts with the seed, Settings, Online now (loaded world only), Manage.
  - **Rules**: the editor inline, its note sticky under the strip.
  - **Backups**: this world's list.
  - **Server**: *Same for every world*. It holds Update, Online, Players, server settings, the
    allowlist and Join.

- **For:** the most direct for "work on this one world": one tap to switch, nothing pushed. Load
  sits where Start/Stop sits for the loaded world.
- **Against:** the built screen's server sections move behind a **Server** tab. The chip strip
  scrolls sideways past about three worlds. Tabs plus a switcher are two levels of selection on
  one screen.

## Render check (revision 2, 2026-10-10)

The check ran in headless Chromium (Playwright) for each variant at **390×844 and 320×844**, in
both themes. It covered:

- all 14 states;
- each variant's deep views: expanded rows, the world page, the rules page or tab, every tab;
- every modal: new world with a typed seed and rules open, the import sheet and the import-over
  Dialog, the reset sheet and typed-name Dialog, the load Dialog, the restore sheet into another
  slot and its Dialog, back up now, and the backup sheet.

The audit script was first proven to catch an injected 1.9:1 text and a 20 px button.

- **Contrast:** every text in the phone clears **4.5:1** in both themes. The exclusions are as in
  the earlier round: disabled button labels, and DESIGN's `.sect` section headers (`--text-3`).
- **Tap targets:** every control is ≥ 44 px.
- **Overflow and truncation:** nothing runs past the phone, and nothing scrolls sideways except
  the chip strips. A new **ellipsis check** finds no truncated text at either width. That covers
  the hero's world tile, the difficulty segments (2×2 at ≤ 360 px) and C's tabs, plus the long
  file names and the seed box, which now wrap.
- **Toast vs. Reset all:** measured in all three variants at both widths, with no overlap.
- **Behaviour spot-checks:**
  - four stepper taps produce one save and one toast;
  - Load while stopped has no backup clause;
  - C's sticky rules note sits flush under the world strip (both at 124 px).
- **Console:** no errors.

Screenshots of the key states were taken and inspected in both themes.

## Recommendation

**B, still.** The review changed contracts, not the shape of the choice:

- B is the only variant that puts a world's backups, settings, rules and destructive actions
  together on one page about that world. That matches *backups belong to a slot*.
- It gives the 39-rule editor a full page, where the sticky live/pending note reads best.
- It keeps screen B a short server page, with one glanceable row that also carries the job's
  phase and the off-box-copy warning.

If the owner prefers fewer taps, choose **C** over A. A is the cheapest, but it makes the screen
about 1.8× as long as B's main screen and splits a world from its backups.

## Verified facts (backend behaviour these mocks are built to)

1. **Import**: the hard limit is 1 GB. Over 100 MB it only uploads at home on the Wi-Fi (the
   Cloudflare tunnel refuses it). The check requires only a `level.dat`.
2. **Settings**: difficulty applies live on the loaded, running world. Game mode is the default
   for new players and new characters, at the next restart, and existing players keep theirs.
   Cheats apply at the next restart.
3. **Seeds** are read from each world's own `level.dat` (imports, the first-boot world,
   restores). They read *unknown — not recorded*, with Same seed withheld, only when that file
   can't be read. The first-boot world is named **World**.
4. **An imported world** brings its own game mode, difficulty, cheats, seed and rules. The
   overwritten slot's settings are not inherited.
5. **`playerWaypoints`** offers the current value plus *everyone*.
6. **Live means loaded and running.** Otherwise a change waits, and saved rules are re-applied on
   every start.
7. **A pinned backup can't be deleted** until it is unpinned.
8. **Jobs**: `/status` gains `job: {what, phase, started_at} | null`. The phases are *warning
   players*, *backing up*, *stopping*, *writing* and *starting*. Every device sees it.
9. **The chat warning is 10 seconds**, before any stop of the loaded world with players on.
10. **Downloads**: a per-backup `downloaded_at` sits alongside the per-world `last_download`.
11. **Server settings**: view distance 5–32, max players 1–30, and **5 fixed slots**. There is
    no tick distance or slot count.
12. **Allowlist**: on/off applies at the next restart. Names are live while the server runs,
    otherwise applied when it starts.
13. **Import over an occupied slot** confirms in the Dialog, like restore.
14. **Load while stopped** takes no backup.
15. **Restore into an empty slot** is named *"<source> (<backup date>)"*. It takes its seed and
    settings from the backup's `level.dat`, and its rules from the source world. Into another
    occupied slot, the backup's seed and settings replace that slot's.
16. **Reset → New seed** takes an optional typed seed. New world has a Cheats switch. Names cap
    at 40 characters.

## Assumptions still open

- **Pinned backups sit outside the 20.** Otherwise 20 pins would block every new backup.
- **Rule steps and bounds** (e.g. `randomTickSpeed` 0–4,096, `spawnRadius` 0–128) are sensible
  UI limits, not Bedrock's own.
- **One job at a time** follows M1's single lifecycle lock. Downloads, pins, rename, settings,
  rules and the allowlist stay live during a job or an update.
- **Not covered:** Dave's per-slot switch (M5/M6) and the M4 world-index note on reset. Both
  belong to waves that aren't built.

Decision: **B — the Worlds sub-screen** (owner, 2026-10-10).
