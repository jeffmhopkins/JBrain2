# GUI gate — the Minecraft Ops card (wave M1)

> **Status:** Plan · **Last verified:** 2026-10-10
>
> Decision: **B**, with C's player table (owner, 2026-10-10). Three variants below
> (revision 2, after an independent review). The chosen one becomes the binding spec for
> M1's frontend, and its reasoning lands in `docs/reference/DESIGN.md` in the implementing
> PR. Behaviour source:
> `docs/plans/MINECRAFT_BEDROCK_PLAN.md` §M1.

These are three interactive mocks of a **Minecraft** surface on the Ops screen. They cover:

- the Bedrock server's lifecycle;
- its version and updates;
- who plays on it.

Open each `.html` in a browser. Each file:

- is self-contained and works offline;
- is phone-framed: 390 px, 560 px on desktop, full-bleed under 430 px;
- follows `DESIGN.md` "Theming" in dark and light, with tokens only;
- uses Lucide-style outline icons;
- keeps a 44 px tap floor on every control, text links included;
- honours `prefers-reduced-motion`.

A dashed **Mock controls** strip above the phone is reviewer chrome, not design. It flips
between every state, previews the add-on stats and switches the theme. In B it also switches
the entry point.

## Shared fixture (identical in all three)

| | |
|---|---|
| Server | `world` · Survival · Normal · allowlist off · LAN name **JBrain** · `192.168.1.40` port `19132` |
| Version | running **1.26.52.3**; newer **1.26.60.4** in the update scenarios |
| Players | BlockyFox (14h 05m, 19 sessions), Mira_P (9h 40m, 12), Steve42 (3h 15m, 5), Pebble_J (48 min, 2) |
| Online | BlockyFox · 42 min, Mira_P · 7 min. Session clocks tick in real minutes |

| State (mock controls) | What it shows |
|---|---|
| Running · 2 on | The default: uptime, two players online, up to date. A rests **collapsed** here. |
| Running · empty | Nobody on, so Stop and Restart act without a confirm. Uptime reads `41 d 4 h`. |
| Stopped | Start only. The online list says the server is stopped. |
| Installing | First-boot download progress, then *starting — generating the world*, then running (animates). Version, world, join and players all read **—** or *not installed*, and the roster is empty. |
| Install failed | The download error verbatim and **Retry install**. Nothing else is claimed. |
| Update available | *Update available: 1.26.60.4*, the "you're behind" notice, the matched article's title, four quoted lines and **Release notes**. |
| Update · no notes yet | *Release notes not published yet*, a link to `aka.ms/MinecraftUpdate`. The update still works. |
| Updating | Backing up → Downloading → Restarting → Done, with timestamps (animates). Players stay on until Restarting, and the copy says so. |
| Update failed (download) | *Still on 1.26.52.3*, nothing installed, backup kept, newer clients still locked out, **Try again**. |
| Update rolled back | *1.26.60.4 wouldn't start — rolled back to 1.26.52.3*. The server runs again from `pre-update-1.26.52.3`, which is kept, and newer clients are still locked out. |
| Add-on: Preview | Lifetime stats with sample numbers, tagged **Preview · sample numbers** (neutral, not a domain colour). |

## Behaviour common to all three

- **Before the first install, nothing is claimed.** Only the state block, progress or error,
  and Retry are shown. The version reads *not installed — will install 1.26.52.3*. World and
  Join read **—**. Players reads *no one has played yet*, because a fresh server has no
  history. Check-for-updates and auto-update are hidden.
- **Stop and Restart confirm only when someone is on.** The wording is *"2 players — BlockyFox
  and Mira_P — will be disconnected; the world is saved first."*, one sentence. With nobody on
  they act at once.
- **Restart reads as restarting**, not stopping: *restarting — saving the world, then starting
  again*.
- **Update always confirms**, and the confirm states the consequence: a backup
  (`pre-update-<version>`) is taken first, and players stay on while it downloads. Then the
  server restarts and players are dropped for about a minute. The button is **Update
  Minecraft**, so it can't be confused with the stack's own **Update server** on the System
  card.
- **Updating a stopped server leaves it stopped.** The confirm says so, the step list reads
  *Installing — server stays stopped*, and the server is not started.
- **Auto-update on restart never acts silently.** With the switch on and an update waiting,
  Restart always confirms, even with nobody on. Its title becomes *Restart and update to
  1.26.60.4?* and the text names the backup. The switch's own line says each restart backs up
  first.
- **"You're behind" is explicit**, and it survives failure. Clients update themselves and can
  only join a server on exactly their version, so an available update is presented as
  *players on the newest version can't join*. Both failure states keep the line *Players
  who've updated still can't join*.
- **The changelog is quoted, not summarised.** It is labelled *From Mojang's changelog ·
  first 4 lines, verbatim*. The matched article's title (*"Minecraft Bedrock Edition 26.60 –
  Changelog" · posted Oct 8*) sits above the quote, so a wrong match is visible. The quote is
  followed by the **Release notes** link.
- **Disabled controls say why.** During an update: *Busy updating — Start, Stop and Restart
  come back when it's done.* During a transition, Update says it is available once the
  server has finished.
- **Lifetime stats are honest.** Without the add-on they are **—** or one line, *Arrives with
  the companion add-on*, with the reason. The preview is tagged as sample data.
- **Durations read like a person wrote them.** Minutes, then `14h 05m`, then whole hours for
  play totals (`812 h`). Uptime counts days (`41 d 4 h`).
- **Light theme uses deepened accent text.** Accent-coloured *words* (button labels, badges,
  links, state words) use `--ok-text`, `--warn-text`, `--danger-text` and `--steel-text`.
  Dark keeps the pastel; light mixes it 46–54% toward `--text`, the pattern `DESIGN.md`
  "Syntax tokens" already uses. Dots, tints and rules keep the pastel. Load-bearing muted text
  (the changelog label, uptime, *last checked*, tile captions) moved from `--text-3` to
  `--text-2`. If this card is built, the four text tokens belong in `tokens.css` and the
  DESIGN token table.

### Other ways to stop the same container (applies to every variant)

The Minecraft container will also appear in Ops' service groups (as *Other*, from
`groupContainers`), and that row has its own generic Stop / Restart. The header's **Restart
all** also restarts it. Neither may bounce players silently:

- **The service-group row's Stop and Restart route through this card's confirm.** They don't
  use the generic `window.confirm`, so the *"N players will be disconnected"* text is the same
  everywhere. Logs, Rebuild and Copy logs stay on the row.
- **Restart all** confirms with *"That includes Minecraft — 2 players (BlockyFox and Mira_P)
  will be disconnected for about a minute."* whenever anyone is on. This is wired in all three
  mocks: press **Restart all** in the Ops header.

## The three variants

| Variant | File | Thesis |
|---|---|---|
| **A** | `a-inline-card.html` | *Another Ops card.* One `OpsCard` in the stack, built from the System card's label rows. |
| **B** | `b-dedicated-screen.html` | *A place you go.* A card-launcher destination, with an Ops shortcut row into it. |
| **C** | `c-tabbed-card.html` | *One card, three jobs.* An `OpsCard` whose body is a Server · Players · Updates segmented control. |

### A — inline card

The card sits under Local engine.

**Collapsed by default.** Like every Ops card except System, it shows title, summary and
state dot. It opens itself when its verdict needs the owner: an update waiting, a failure,
an update running. If the owner collapses it, it stays collapsed until that verdict changes,
which is Host settings' re-key rule. The mock starts collapsed in the healthy states.

**Expanded, it is label rows, in this order:**

- **Server**: state badge, uptime, Restart / Stop.
- **Version**: stacked like the System card's Load row, with *Update Minecraft* on the
  `ops-update-bar`.
- **Online**.
- **World** and **Join**: moved above Players so the join steps aren't at the bottom.
- **Players**: each expands in place to its totals.

Without the add-on, lifetime stats are a single line under Players rather than rows of
dashes. With the preview on, an all-players Stats row appears and expanded players show
their tiles.

**Confirms.** Stop and Restart use Ops' **tap-again** idiom. It now disarms on blur, matching
`UpdateControl` (the old 5 s timer is gone). A real 44 px **Cancel** sits beside it, and the
consequence sentence sits beneath. Update gets an inline confirm panel.

- **For:** the most native. It reuses `OpsCard`, `.ops-vrow`, `.ops-update-bar` and the
  tap-again idiom almost verbatim. It is the cheapest to build, and it costs nothing on Ops
  while healthy.
- **Against:** it is still long when it opens itself for an update (Version with changelog,
  steps, auto-update). That is the moment the rest of Ops is pushed down. Tap-again is a
  lighter confirm than DESIGN's paradigm table gives destructive acts. Everything later waves
  add (slots, backups, remote play) would have to fit into it too.

### B — dedicated screen (card-launcher destination + Ops shortcut row)

Minecraft is a **card-launcher destination** under SYSTEM, beside Ops and Data. This follows
the precedent `DESIGN.md` records for Data, which was lifted off Ops into its own launcher
screen when it grew.

**Two ways in:**

- **The launcher tile** carries a state dot and word, and flags *update* or *failed*.
- **An Ops shortcut row** is a plain list row with a trailing chevron (DESIGN "Lists"), not
  an `OpsCard` caret, since it navigates rather than expanding. It shows the state, version
  and who's on, or the amber *update available · 1.26.60.4 — newer clients can't join*.

Both open the same screen, and its back chevron returns to where you came from. Use the
**Entry** mock control to see each.

**The screen**, top to bottom:

- An amber banner while an update waits, or a rose banner after a failure that keeps the
  lockout line.
- A status block: state, two tiles (online now, version), Start / Restart / Stop.
- **Update**: `1.26.52.3 → 1.26.60.4`, changelog, a full-width *Update Minecraft*.
- **Online now**.
- **Players**: rows; a tap opens a bottom **Sheet** with a close button.
- **Lifetime stats**: a "who leads" list, placeholder until the add-on.
- **Server**: world, mode, allowlist with what *off* means, how to join.

**Confirms** use the shared center **Dialog**: destructive variant for Stop and Restart, one
sentence of consequence. The hidden dialog is emptied and inert, and focus moves into it when
it opens.

The player Sheet is a deliberate deviation from DESIGN's "row-level detail → inline
expansion". The per-player view grows with the add-on's stats, and a Sheet holds that without
lengthening the list.

- **For:** the most room, and the right home for what is coming (M2 world slots, M3 backups,
  R1 remote play, the companion). It matches *"primary tasks get a full screen"* and DESIGN's
  Dialog-for-destructive rule. It holds up best at 320 px.
- **Against:** one tap further from a glance, and a new screen, route and launcher tile to
  build. The Ops row is a second entry point to keep in step with the tile.

### C — tabbed card

An `OpsCard` (same collapsed header as A) whose body is a **Server · Players · Updates**
segmented control. The tabs have proper tab semantics (`aria-controls`/`aria-labelledby`,
arrow keys, and badge text for screen readers).

- **Server**: state and controls, then a waiting, running or failed update **said on this tab
  too**, with a jump to Updates. Then a 2×2 facts grid and a *How to join* box. The card always
  opens on Server; it no longer jumps tabs for the owner.
- **Players**: *Online now* pills, then an *All players* list with a **Time / Stats** switch.
  - **Time**: player with *since* (first seen), total with session count, last seen with
    day and time.
  - **Stats**: deaths, mobs and blocks mined, with a row tap opening the full set. No sideways
    scroll at 390 or 320 px.
- **Updates**: from/to tiles, the behind notice, changelog, *Update Minecraft*, steps, check,
  auto-update.

Confirms are **inline strips** with short action labels (*Stop*, *Update*), the sentence
above them carrying the consequence. Switching tabs drops an armed confirm, because its strip
is no longer on screen.

- **For:** the open card stays about one screen tall. The Players list is the best side-by-side
  "who plays most" view.
- **Against:** tabs inside a collapsible card are a new nesting on Ops. Detail for Updates is
  still a tap away. Like A, everything later waves add has to fit into one card.

## Review (2026-10-10)

An independent reviewer rendered all three in headless Chromium. They covered 9 states × 2
themes × 3 variants, with scripted audits for contrast, tap size, overflow at 390 and 320 px
with stress data, focus and reduced motion. All Blocking and Should-fix findings were
addressed in revision 2.

**Cross-variant changes:**

- Install states claim nothing that doesn't exist yet.
- Light-theme accent text uses the new deepened text tokens.
- Load-bearing muted text moved to `--text-2`.
- The button is renamed **Update Minecraft**.
- The mid-update copy says players stay on until the restart.
- Both failures keep the lockout line, and a rollback failure was added.
- Updating a stopped server keeps it stopped.
- Restart has its own *restarting* state.
- Auto-update makes Restart confirm and name the backup.
- Disabled controls say why.
- Every text link has a 44 px overlay.
- Uptime counts days.
- The article title is shown above the quote.
- The preview tag is neutral, not violet.
- Restart all confirms with players on, and the generic service-row path is documented
  above.
- Focus returns to the control, or to its replacement, after each action.

Nits fixed:

- *"Mojang hasn't posted…"* replaces the unverified *"usually within a day"*.
- Names are joined with a list formatter.
- There is one time format.
- `today`/`yesterday` are lowercase.
- The neighbour stack now matches Ops (System memory, Panels, History).

**Per-variant changes:**

- **A**: collapsed default; long gamertags ellipsise while session times stay visible; World
  and Join move above Players; the dash-tile Stats row is cut until the add-on; the label is
  *Version*, not *Running*; tap-again disarms on blur and Cancel is a real button.
- **B**: two hero tiles (no truncation at 320 px); a launcher tile plus an Ops shortcut row;
  *2 on now*; one-sentence dialog; a Sheet close button; lowercase state words; a spinner on
  Starting.
- **C**: stays on Server, with the update stated there; a stats table with no sideways
  scroll; *Seen* carries the day; *Time / Stats* chips; short strip labels; one eyebrow above
  the quote; tab ARIA and arrow keys.

**Re-run audit (revision 2, reviewer's scripts with selectors updated for the new
structure).** No JS errors.

- **Contrast:**
  - Every text on the card clears 4.5:1 in both themes, except:
    - disabled button labels, which are exempt and now explained;
    - DESIGN's `.sect` section headers (`--text-3` by DESIGN rule);
    - the neighbouring Ops cards' carets and service counts, mirrored from the live app.
  - Those last two belong to the muted-token contrast audit, not this card.
- **Overflow:** nothing runs off the phone at 390 or 320 px, and nothing scrolls sideways.
  Only stress gamertags ellipsise (with a `title`), plus the System neighbour's summary at
  320 px.
- **Tap targets:** every control on the card is ≥ 44 px. The only sub-44 controls are the
  live Ops header's own Runs, Refresh and Restart all buttons.

### Where the reviewer and I differ

The reviewer's assessment, recorded here as written in substance:

> **B, with C's per-player table borrowed into it.** The later waves are not hypothetical —
> world slots (M2), backups and restore (M3), remote play (R1) and the companion (M4–M8) all
> land on this surface, and DESIGN already records the Data screen being lifted off Ops when
> it grew. A, when it opens itself for an update, was ~1,450 px — 1.8 phone screens, every
> Mojang release. B is the only variant that uses the centre Dialog for the riskiest act
> (disconnecting family mid-game), and it held up best at 320 px.

## Recommendation (revised)

**B, with C's Time / Stats player list in its Players section.** I recommended A in round
one. I've changed my view, for three reasons.

1. **The later waves land here.** My "migrate to B later" for A was a second build and a
   second gate scheduled in advance. That is the Data screen's history repeated on purpose.
2. **A's main argument doesn't hold.** It rests on "collapsed most of the time", but the
   moment it matters is when it opens itself for an update. That is exactly when it is
   longest and pushes the rest of Ops away.
3. **Dialog weight matches the act.** Disconnecting family mid-game is the riskiest act here,
   and B is the variant where the confirm's weight matches it.

B's cost is a launcher tile and a route. The Ops shortcut row keeps the glance where the
owner already looks.

If the owner prefers to keep it on Ops, choose **A** (revision 2) over C. C's Server-tab
update line closes the hidden-update gap, but it adds a nesting no other Ops card has.

## Assumptions made where the spec was open

- **Placement**: A and C sit under the Local engine card, above System memory. B's Ops
  shortcut row sits in the same slot.
- **Changelog lines are placeholder text** in Mojang's style. The real card quotes the
  article's first bullets. The **Release notes** href is the owner-supplied example article
  (the 26.52 hotfix), standing in for the 26.60 article. The displayed title is what the
  matcher would show.
- **Starting / stopping / restarting** are transient states, with the 60 s save grace stated.
  Restart is stop-then-start, as the plan says.
- **Rollback is the wrapper's job.** The *rolled back* state assumes the wrapper restores
  `pre-update-<old>` and restarts the old build when the new one won't start. The card's
  "still on the old version" claim depends on that guarantee.
- **Session totals include the live session**, and an online player's *last seen* reads *on
  now*.
- **Lifetime stats** follow the plan's list (deaths and cause, mobs, blocks mined and placed,
  distance). The add-on decides the real fields.
- **Auto-update on restart** is included from the plan's M1 text even though it was not in the
  owner's list, because the Update button changes what a restart does.
