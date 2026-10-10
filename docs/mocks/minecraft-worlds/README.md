# GUI gate — Minecraft worlds and backups (waves M2 + M3)

> **Status:** Plan · **Last verified:** 2026-10-10
>
> Decision: **pending owner choice**. Three variants are below. The chosen one becomes the
> binding spec for M2 and M3's frontend, and its reasoning lands in `docs/reference/DESIGN.md`
> ("Minecraft server screen") in the implementing PR. Behaviour source:
> `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M2 and §M3, plus the owner's game-rules and seed
> additions (2026-10-10).

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
| 1 | `world` | made on first boot · `-2794311108712645813` | Survival · Normal | 3.1 MB, last played Oct 4 |
| 2 | **Castle Hill** (loaded) | imported .mcworld · seed **unknown** | Survival · Normal, rules at the defaults | 7.8 MB, playing now |
| 3 | Creative test | new world · `8675309` | Creative · Peaceful · cheats; day cycle, weather and mob spawning off | 5.2 MB |
| 4 | Skyblock run | new world · `5127438807419962211` | Survival · Hard (pending); keepInventory on (pending) | not generated yet |
| 5 | — | empty | | |

Backups: Castle Hill has 4. They are *before the castle roof* (yours, pinned), two automatic ones
(before loading Creative test, and before the update to 1.26.52.3) and *first night* (yours). That
last one is the only one ever downloaded, on Oct 4. `world` and Creative test have one automatic
backup each. The server runs 1.26.52.3 with BlockyFox and Mira_P on. The allowlist is on, with
the four known players.

| State (mock controls) | What it shows |
|---|---|
| Running · 2 on | The default. |
| Server stopped | **Load** says the server stays stopped. **Back up now** is a straight copy. Allowlist edits apply when the server starts. |
| Fresh box · empty slots | The day M2 lands: only the first-boot world, four empty slots, no backups, *No copy has left the box yet*. |
| Importing | `Hilltop Village.mcworld` (9.4 MB) is imported over Creative test. The steps are upload with real bytes, check, **Back up Creative test first**, then write. It animates. |
| Import · not Bedrock | A `.zip` without `level.dat`/`db/` fails at the check step: nothing written, upload discarded, **Choose another file**. |
| Import · too big | An 812 MB file is refused in the sheet before anything uploads (limit 500 MB). |
| Loading a world | Switching to Creative test with players on: 30 s chat warning → stop → back up Castle Hill → switch → start. It animates. The status block reads *switching worlds*. |
| Reset in progress | Creative test reset to a new seed (not loaded, so the server isn't touched). |
| Restore in progress | *first night* restored into the loaded Castle Hill: back up → stop → restore → start. |
| Update running | Every world action is disabled, with the reason said: *Busy updating Minecraft — loading, importing, resetting, restoring and backing up come back when it's done.* Rename, settings, rules, downloads, pins and the allowlist stay live. |
| Refused · update started | The API refused a Load because auto-update began first. A rose line says nothing changed and Castle Hill is still loaded. |
| At 20 backups | Castle Hill at 20 of 20. The retention line and the Back-up-now sheet both name the backup the next one removes. |

## Behaviour common to all three

- **One world is loaded at a time**, and it is marked *loaded* everywhere: a green disc or chip
  dot, and a **world loaded** tile in the status block. The tile is new to the built hero.
- **Load confirms in the centre Dialog**, in one sentence. With players on it is the destructive
  variant: *"BlockyFox and Mira_P get a 30-second warning in chat and are disconnected; Castle
  Hill is backed up first, then Creative test starts."* With nobody on, or the server stopped
  (*"…the server stays stopped until you start it"*), it is a plain primary button.
- **New world** is a Sheet with these fields:
  - **Name**.
  - **Seed**: **Random** shows the 64-bit number it rolled, with **Re-roll**. **Enter a seed**
    takes any text up to 64 characters, with a live counter, the same as Bedrock's own seed box.
  - **Game mode** and **Difficulty**.
  - **World rules**, a collapsed step that holds the full editor at the defaults.

  The world is generated the first time it loads, and every view says *not generated yet*.
- **Import** is a Sheet with these parts:
  - Windows export help (*Play → pencil → Export World*).
  - The chosen file and its size.
  - A slot picker, when opened from outside a slot. Each slot in it says *empty*, or *replaces X
    — backed up first*.
  - An info notice when overwriting: **"Overwriting takes a backup first."**
  - When the target is the loaded world, the notice also names who gets disconnected.

  *Too big* is caught in the sheet. *Not a Bedrock world* is caught at the check step, and a bad
  file never touches a slot.
- **Reset** is a Sheet with three options, then the Dialog:
  - **Same seed**: offered only when the seed is known, otherwise *Not offered — an imported
    world's seed is unknown*.
  - **New seed**.
  - **Empty**: not offered for the loaded world (*Load another first*).

  The Dialog asks the owner to **type the slot's name**. Its button stays disabled until the
  name matches. An emptied slot keeps its backups, and says so.
- **Rename** is a Sheet with one field. Only the name changes.
- **Per-world settings** are game mode, difficulty, cheats and every game rule. Each says how it
  applies:
  - **On the loaded world, a change applies live, at once** (green line). Cheats is the
    exception: it is a `server.properties` key, so it applies at the next restart.
  - **On any other world, it is saved and applied when that world next loads**. Every changed
    setting and rule carries an amber **pending** tag until then, and the world's row counts
    them.
- **The game-rules editor** (owner addition) covers all 39 rules the live server reports, at
  their defaults:
  - The rules are in seven collapsible groups: World, Time & weather, Players, Mobs & drops,
    Crafting, Commands, Display.
  - Each rule has a plain-English label, the rule id in small mono text, and a *changed ·
    default X* tag when it differs.
  - Booleans are switches. Numbers are steppers with a typeable value (step 1, 5 for the sleep
    percentage, 1,000 for the command limits). `playerWaypoints` is a select.
  - A search box filters in place.
  - There is **Reset {group} to defaults** per group and **Reset all**.
- **Backups belong to a world.** Each list shows:
  - the label, or the automatic reason (*Before loading Creative test*);
  - *yours* or *automatic* (also as a person or clock glyph);
  - the size, the date and *pinned*.

  **Back up now** opens a Sheet with an optional label. It says *copied live — players stay
  on*, or *a straight copy*. A backing-up row shows in the list while it runs.
- **Restore** goes Sheet → Dialog. The Sheet picks the target slot. That can be its own world,
  another world (*backed up first*), or an empty slot (*becomes "Castle Hill (Oct 4)",
  nothing else changes*). The Dialog's one sentence says the target is backed up first. When the
  target is the loaded world, it adds that the server restarts and who is disconnected.
- **Pin** keeps a backup for good, outside the 20. **Delete** confirms in the Dialog, and the
  Dialog says so when the backup is pinned or downloaded.
- **Download .mcworld** records the time. Each world shows **Last copy off the box: {when} —
  {which}**, or the amber **No copy has left the box yet**. Both say that Minecraft backups
  aren't in the box backup, so a download is the only copy kept elsewhere.
- **Retention is explained where it acts**: *3 of 20 kept · 1 pinned*, with a meter, and *Each
  world keeps its newest 20. Automatic ones go first; pinned ones are kept for good, outside the
  20.* At the limit, it names the backup the next one removes.
- **Server-wide settings** are:
  - server name;
  - max players;
  - view distance and tick distance;
  - **world slots** (5 by default, raisable; it can only drop down to the highest slot in use);
  - *Xbox sign-in required*, read-only and always on.

  Changes save with an explicit button. Slots apply at once, and the rest at the next restart.
- **Allowlist**:
  - An on/off switch. Turning it off confirms in the Dialog.
  - **Required for internet play** (*coming later*, since R1 isn't built).
  - The gamertags, each with a 44 px remove button and *on now / has played here / hasn't
    joined yet*. Removing someone who is on says *they stay on until they leave*.
  - An Add field. Adding is live with no restart, or *applied when the server starts* while
    the server is stopped.
- **One job at a time.** Load, import, reset, restore and back-up share the server's lifecycle
  lock with updates (M1). While one runs, the others are disabled and say what they are waiting
  for. A job that touches the loaded world drives the status block (*switching worlds*,
  *restoring*, *resetting*, *importing*), and Start/Stop/Restart read *Busy with the restore —
  the server is handled for you.*
- **Honest data.** Sizes, seeds, last played and last backup show only where they exist. A
  never-loaded world reads *not generated yet* with no size. An imported world's seed reads
  *unknown*. Upload progress shows real bytes, and the other steps are phase text with
  timestamps, never a fake bar.

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

**Backups** follows, one world at a time, picked by a chip row (the loaded world first, with a
count each). Its card holds **Back up {world} now**, the off-box line and retention, then the
rows. A row also expands in place to Restore…, Download, Pin and Delete. Server settings, the
allowlist and Join close the scroll.

- **For:** the cheapest to build and the most native. Everything is one scroll, with no new
  routes. The slot list compares worlds side by side.
- **Against:** the screen gets long: about 4,300 px with one world and one backup open, against
  about 2,450 px for B's main screen. The backups for a world sit away from the world itself, so the chip and *See its
  backups* stitch them together. Game rules have to go into a tall Sheet.

### B — Worlds sub-screen

Screen B gains one row under the status block, **Worlds & backups**. It shows *Castle Hill
loaded · 4 of 5 slots used* and *last copy off the box Oct 4* (amber when nothing has ever left).
While a job runs, the row shows that job and its step. Server settings, the allowlist and Join
stay on screen B, because they are the same for every world.

- **Worlds** groups the slots as *Loaded*, *Other worlds* and *Empty slots*. Rows show the
  download state too, and the job card sits on top.
- **A world's page** has a status block (*loaded* / *not loaded · Castle Hill is loaded*) with
  facts and **Load**. Below it are **Settings** with a **Game rules** row, **Backups** for this
  world, and **Manage** (Rename, Import over, Reset).
- **Game rules** is its own pushed page, not a Sheet, so it has the whole height.
- A backup row opens a **Sheet** with its facts (kind, size, kept, off the box), Restore…,
  Download, Pin and Delete.

- **For:** it matches the plan's own wording (*Ops → Minecraft → Worlds*) and the Data screen's
  precedent. Each world's backups live with the world, as the plan says (*backups belong to a
  slot*). Screen B stays short and about the server. It is the roomiest for the 39 rules, and
  later per-world features (Dave on or off per world, M4 index, M8 maps) have an obvious home.
- **Against:** depth. A restore from the main screen is Worlds → world → backup Sheet → restore
  Sheet → Dialog. Comparing worlds' backups means going back and forth. It adds two routes.

### C — world switcher

A sticky **chip strip** under the top bar lists every slot. The loaded world has a green dot, a
world mid-job an amber dot, and empty slots a dashed outline. Picking a chip scopes the screen:

- **The status block becomes that world's.** For the loaded world it is the built hero. For
  another world it reads *not loaded · Castle Hill is loaded · 2 players on*, with mode, size and
  backup tiles and **Load {world}**. For an empty slot it offers **New world** and **Import**
  there and then.
- **Tabs (World · Rules · Backups · Server)** with tab semantics and arrow keys:
  - **World**: facts with the seed, Settings, Online now (loaded world only), Manage.
  - **Rules**: the editor inline.
  - **Backups**: this world's list, with the count on the tab.
  - **Server**: introduced by *Same for every world*. It holds Update, Online, Players, server
    settings, the allowlist and Join.
- A job on another world shows as a one-line *Restoring into … — show* link.

- **For:** the most direct for "work on this one world": one tap to switch, nothing pushed. Rules
  get an inline tab. Load sits where Start/Stop sits for the loaded world, which teaches the model
  (one world runs).
- **Against:** the built screen's server sections move behind a **Server** tab, so Update, Online
  and Players are a tap further than today. The chip strip scrolls sideways past about three
  worlds at 390 px. Tabs plus a switcher are two levels of selection on one screen. The scope is
  invisible for things that can't be scoped (players, updates), which is why the Server tab has
  to say so.

## Render check (2026-10-10)

The check ran in headless Chromium (Playwright) for each variant at **390×844 and 320×844**,
in both themes. It covered all 12 states, plus each variant's deep views (expanded rows, the
world page, the rules editor, every tab) and every modal (new world with a typed seed and rules
open, import, reset sheet and typed-name dialog, load dialog, restore sheet into another slot and
its dialog, back up now, the backup sheet). The audit script was first proven to catch an
injected 1.9:1 text and a 20 px button.

- **Contrast:** every text in the phone clears **4.5:1** in both themes. Two exclusions are by
  the earlier round's rules: disabled button labels, and DESIGN's `.sect` section headers
  (`--text-3`).
- **Tap targets:** every control is ≥ 44 px, including text links (overlays), switches and
  the remove buttons. The only exceptions are the copied top bar's vitals readout and the mock
  controls.
- **Overflow:** nothing runs past the phone at 390 or 320 px, and nothing scrolls sideways
  except the intended chip strips.
- **Console:** no errors.

Screenshots of the key states were taken and inspected. Fixes made from them: automatic backup
reasons now wrap instead of truncating, duplicate New world/Import buttons were removed, *1 rule
differs*, a failed import no longer reads *Writing null*, and the refusal's time matches the
update's.

## Recommendation

**B.** The plan already names this surface *Ops → Minecraft → Worlds*. Backups are per slot by
the owner's decision, and B is the only variant where a world's backups, settings, rules and
destructive actions sit together on one page about that world. That is also where the owner is
most careful. The 39-rule editor needs room, and in B it gets a full page rather than a tall
Sheet (A) or a tab under two selectors (C). Screen B, which was chosen *because* later waves would
land on it, stays a short server page with one glanceable row that also carries the
off-box-copy warning. Every later per-world feature (Dave per world, the M4 index, M8 maps) has
an obvious home on the world page.

If the owner prefers fewer taps, choose **C** over A. C keeps worlds one tap apart, at the cost
of moving the server sections behind a tab. A is the cheapest, but it makes screen B about 1.75×
as long as B's main screen and splits a world from its backups.

## Assumptions made where the spec was open

- **Seeds.** The owner said imported worlds read *unknown*. The first-boot world shows a seed on
  the assumption that M2's wrapper records the seed it generated with (or reads it once on
  migration). If it can't, that world reads *unknown* too, and Same seed is withheld for it.
- **Import over the loaded world** is allowed and runs stop → back up → write → start, like
  reset and restore. The plan's wrapper refuses a live import, so the backend orchestrates it,
  as it does for Load.
- **Import size limit** is shown as 500 MB (the plan says "a few hundred MB").
- **Pinned backups sit outside the 20.** Otherwise 20 pins would block every new backup. The
  plan says pins are kept forever but doesn't say whether they count.
- **Live versus next-load.** Game rules, difficulty and game mode on the loaded world are applied
  over the console (`/gamerule`, `/difficulty`, `/defaultgamemode`), which works with cheats off.
  `allow-cheats` is a `server.properties` key, so it applies at the next restart. The other
  server-wide settings apply at the next restart too.
- **`playerWaypoints` choices** are shown as *everyone* and *off*. The real option list should
  be read from the server, which only reported the current value.
- **Rule steps and bounds** (e.g. `randomTickSpeed` 0–4,096, `spawnRadius` 0–128) are sensible
  UI limits, not Bedrock's own.
- **One job at a time** follows M1's single lifecycle lock. Downloads, pins, rename, settings,
  rules and the allowlist stay live during a job or an update.
- **The new *world loaded* tile** joins the built hero's two tiles. It is a three-tile row that
  holds at 320 px.
- **Not covered:** Dave's per-slot switch (M5/M6), and the M4 world-index note on reset. Both
  belong to waves that aren't built.

Decision: pending owner choice
