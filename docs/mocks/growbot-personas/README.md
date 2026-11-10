# GrowBot → bodies, personas, launching, files and backups

Kind: Living (mock set) · Status: **picked — B, three tabs (owner, 2026-11-10, after first picking A)**; binding spec in `docs/reference/DESIGN.md` "GrowBot — the desk robot's screen" · Last verified: 2026-11-10

The GUI gate for wave **G4** of [`../../plans/GROWBOT_PLAN.md`](../../plans/GROWBOT_PLAN.md)
(§2c "The creature lives on the box, as profiles", §4 G4). It builds on the G2 pick,
[`../growbot-flash/`](../growbot-flash/README.md) **B** (the GrowBot screen), and replaces that
screen's per-robot *Open GrowBot* and *Open GrowBot without a robot* with personas.

The owner's ask (2026-11-10): *"I'm also a little bit confused on how we handle personas, and
phones, from the screen. We should have a way to connect bodies. And another way to manage
personas, And to launch personas from a link, and to launch persona's attached to the body.
There's that make sense? We also wanted a way to edit personas files and make backups"*, then
*"send all that through a UI expert … make sure it all works. Fluidly."*

Three directions for the same four jobs:

1. **The body.** One robot, *Fish* (`gb-e3b6d8`): on or off, its key (*New key*, *Revoke key*
   behind the 3-second arming confirm), who is wearing it now and on which phone, *Add a robot*
   (the G2 flash sheet, not mocked again).
2. **Personas.** A list; *New persona* from a seed (a fresh creature, or the personality of any
   checkpoint); rename, duplicate, export and import a soul file, reset to a fresh creature, and
   delete (armed, since it erases memories).
3. **Launch.** Per persona: *Open on this phone* (alone, no legs); *Connect to body* (this phone,
   wearing Fish, taking the body from whichever phone held it, whose persona carries on alone);
   *Open on another phone* (G2's two codes, where code 2 now names a persona and, optionally,
   the body). A persona runs on one phone at a time, so opening it elsewhere hands it over.
4. **Files and backups.** Per persona: the soul (identity, bond line, scratch, diary, wants, and
   the config regions persona · expression · meta · dream · harness · body), **editing** the
   config regions, visual memories with delete, and checkpoints (save named, list dated,
   restore behind a confirm, start a new persona from one).

Open `a-persona-cards.html`, `b-three-tabs.html` and `c-launch-board.html` directly in a
browser (each links to the others from its mock bar). Each is standalone and makes no network
request: `shared.css` holds the tokens copied from `frontend/src/styles/tokens.css` (both
themes), and `shared.js` holds the simulated state and every part the three share (the
hand-over rules, the sheets, the soul viewer and editor, visual memories, checkpoints, the
codes), so the variants differ only in structure. The dashed bar above the phone is mock
controls, not design: theme, whether Fish is on, whether this phone trusts the box, what the
next imported soul file is (fine, too big, not a soul), which phone holds which persona and who
wears Fish, and **Simulate a hand-over** (another phone opens a persona, optionally wearing
Fish, and this phone shows what moved). Dashed buttons inside the phone (*Mock: it scans code
2*, the file picker) are mock controls too. On a phone-width screen the bar folds to one line
so the mock fills the screen. `a.png`, `b.png` and `c.png` are 390 × 844 captures at 2×.

Every flow in each variant was clicked through in headless Chromium at 390 × 844, in both
themes. The run also checked that nothing overflows the phone sideways, that no request leaves
the page, that the console shows no errors, that a sheet's entrance plays once and not again
on re-render, and that nothing animates under `prefers-reduced-motion`.

## What every variant fixes or shares

These land whichever direction is picked.

- **Persona and body are two separate things on screen.** The body is shown once, as the body,
  with its own state; a persona never carries the robot's key or pairing code. Where a persona
  is reads in words beside its dot: *on this phone · wearing Fish*, *alone on Pixel 6a*, *not
  open · last on Tamsin's tablet 2 days ago*. Violet means the body throughout (the ring on
  the avatar, the *Connect to body* button, the hand-over banner), so it is never confused with
  the steel primary.
- **Every move says what it costs before or as it happens.** Three rules drive the
  wording, and each is in `G.place`:
  - A phone holds one persona at a time, because the creature keeps one soul per browser
    (`pb2_soul`). So opening a second one on this phone says the first one *closes here, kept
    on the box*.
  - A persona is open on one phone at a time. So opening it elsewhere says *it moved from Pixel
    6a, which now shows that it has moved*.
  - The body has one rider. So connecting says *Sprout let go of Fish and carries on alone on
    Pixel 6a*.

  A change to the body or a hand-over is a **banner**, which stays until *OK* (honest status,
  never a modal). A plain outcome is a toast.
- **Hand-over from elsewhere is visible here.** When another phone opens the persona this
  phone held, or takes Fish, this phone's GrowBot screen shows the banner. The creature on
  the old phone shows its own *it has moved* page (plan §2c, not mocked here).
- **Editing is new, and every save is undoable.** The config regions are editable: persona
  (personality), expression, meta (name and description), dream, harness (thinking) and body.
  Each save makes an automatic checkpoint (*Before editing personality*) first. Its toast has
  *Undo*, which restores that checkpoint. JSON regions refuse a bad save with *That isn't valid —
  check the brackets and quotes. Nothing was saved.* An empty personality, or one over the soul's
  size cap, is refused the same way. A persona open on a phone picks the change up the next time
  it is idle, because `applySoul` reads config at load (the §2b idle reload).
- **The child-safety floor is shown, and locked.** Under the personality, and in its editor:
  *Child-safety rules are always added after the personality. They can't be edited here, in a
  soul file or in the creature.* (`prompts.js` appends `SAFETY_FLOOR` after any persona.)
- **What the creature writes itself is read-only, and says why.** Identity, the bond line
  (append-once, written in a dream), scratch, wants and the diary carry *Sprout writes these
  itself … To take one back, restore a checkpoint.* Restoring a checkpoint is the way to undo
  them.
- **Harness and body say who wins.** Plan §2b keeps models and leg calibration with the robot.
  So their editors say *While it wears Fish, the robot's own settings win over these.*
- **Rename is the list's name, not the creature's.** It also writes `meta.name`. The sheet
  says the creature *still calls itself Sprout until you tell it otherwise*, because upstream
  keeps `goals.selfName` person-confirmed inside the creature.
- **A seed is a personality, a duplicate is a creature.** *New persona* → *Start from* lists
  *A fresh creature* and every checkpoint (*Sprout — Before the beach trip: its personality
  from 29 Oct. Memories start fresh.*). Persona, expression and dream are copied, and memories
  start fresh. *Duplicate* copies the whole persona, memories and visual memories included, but
  not its checkpoints.
- **Every destructive act has its recovery.** Restore uses the centre confirm dialog, which
  names the date and says the current state is saved first. Reset is armed and saves *Before
  reset* first. Its toast has *Undo*. Delete is armed, and its second tap says what goes:
  *erases its memories and 3 checkpoints*. Deleting a visual memory is armed, and says a
  checkpoint made before still has it. Import is checked before anything changes. A file that
  is too big or isn't a soul says so, with *Nothing changed*. Importing over a persona saves a
  checkpoint first.
- **Export says what is not in the file.** Upstream's soul file holds no visual memories
  (they are IndexedDB), so the toast says they stay on the box. A checkpoint does carry them.
- **Open on another phone** keeps G2's two codes and both hints, and adds two choices: *It
  opens as* (a persona) and *With the body?* (alone, or wearing Fish, saying who lets go). If
  that persona is open somewhere, a warning says opening it hands it over. *Copy code 2 as a
  link instead* answers *"launch personas from a link"*. It is a one-time, two-minute link for
  sending to a trusted phone, never a standing link.
- **The Nearby devices hint and the certificate stay reachable** after the first-time note:
  inside *A phone won't open GrowBot?* (open by default on B's Phones tab), in the other-phone
  sheet, and in the first-time sheet.
- **Motion and focus.** Sheets slide up, the dialog scales in, pushed pages slide from the
  right (and back from the left), banners drop in, and toasts rise, all 160–200 ms ease-out.
  Each plays only when the element first appears, so a timer or an armed button re-rendering
  the screen never replays it. All of it is off under `prefers-reduced-motion`. Escape and a tap
  on the scrim close a sheet or the dialog. Text fields keep focus and caret across re-renders.
- **No dead ends.** Every empty state says what to do next. Every disabled button says why
  (*Fish's key is revoked — reflash it first*). A revoked body turns its card's action into
  *Reflash Fish* and shows a rose banner.

## The three

| | Idea | Costs |
|---|---|---|
| **A** `a-persona-cards` | **Persona cards.** Today's screen turned round: a compact **Body** card (Fish, who is wearing it, *Let go*, keys, *Add a robot*), then one card per persona with its two everyday buttons on it, **Open on this phone** and **Connect to body**, plus *Open on another phone*. Both common paths are **one tap** from arriving. *Files ›* pushes one page per persona: jump chips (Soul · Visual memories · Checkpoints · Manage), editable regions with *Edit* (a sheet), what it remembers as expanding rows, visual memories, checkpoints and the manage actions, with the two launch buttons pinned in the foot. *New persona* and *Import* are the screen's foot. | The fastest launch and the smallest change from today's screen. But the card list grows two buttons per persona, so three or more personas push the body card and the help off the first screen. The persona page is one long scroll, which the jump chips only soften. |
| **B** `b-three-tabs` | **Three tabs, one per job the owner named:** **Personas** (rows with one **Open** button), **Body** (Fish, its keys, and **Who wears Fish** as a radio list, so the body's rider can be picked from the body's side, even for a persona already open on another phone), **Phones** (each phone and what it holds, *Set up another phone*, the hints open). *Open* raises one launch sheet with three big choices, each saying what happens to the body and to the phone it leaves. A persona's page has its own tabs: Soul · Memories · Checkpoints · Manage. | The clearest map of bodies vs personas vs phones, and the launch sheet explains every move before it happens. But the common paths cost **two taps** (Open → choice). Choosing a rider from the Body tab needs a new api ability: telling the box that a persona already open on another phone now rides Fish, without that phone doing anything (see below). Tabs inside a tabbed screen are one more level to learn. |
| **C** `c-launch-board` | **A launch board.** The screen opens on one composer: **Who** (persona chips, starting on what this phone holds) · **Where** (This phone / Another phone) · **Body** (Alone / Wearing Fish). It reads back what will happen in a sentence (*Opens Captain Bolt on this phone, wearing Fish. Sprout lets go of Fish and carries on alone on Pixel 6a.*), and one big foot button does it. Below it, **Who's where** shows Fish and each phone at once. A persona opens a **workshop** that shows the soul as files (*personality.md*, *expression.json* … you can edit; *who-i-am.txt*, *bond-line.txt*, *diary.log* … it writes; *visual-memories/*). Each file is a full page with *Edit* in the foot. Beside the files is a **History** timeline of its checkpoints, from *Now* down to *Made*. | Says the consequence before every launch, and matches the owner's own words, *"edit personas files and make backups"*. Reopening this phone's persona is one tap, and connecting it is two (*Wearing Fish*, then Open). But it is the most invented: the files are the mock's names for regions of one JSON document, not real files, and a composer is a new pattern in the app. Manage hides behind *⋯*. |

## Recommendation: A, with C's sentence (the owner picked B)

A is the only direction where both everyday paths, *open my persona here* and *put it in the
body*, are one tap from arriving. The owner does those daily and edits a soul rarely. A also
keeps G2's B screen recognisable: the body card is today's robot card, and the persona cards
take the place of *Open GrowBot*. C's read-back sentence is worth taking whichever wins: under
the two buttons on a persona card, one line such as *Sprout lets go and carries on alone on
Pixel 6a* would make *Connect to body* say what it costs before the tap. B is the better pick
if the owner wants the body managed from the body's side. It needs the remote-rider route below.

## What the owner's ask changes in the plan

- **Editing the soul.** §2c says the PWA shows the soul *read-only, since the creature writes
  them*. The owner now wants editing. All three mocks make the config regions editable (each
  save checkpointed first, then `sanitizeSoulCfg` and the size cap as an import) and keep the
  memory surfaces read-only. G4 needs a write route for config regions, and §2c's text changes.
  The sync's version check (§2c "Sync") must treat a PWA edit as a newer soul, so the open phone
  reloads it rather than overwriting it with its cache.
- **Harness and body regions vs §2b.** A persona's soul carries `harness` (models) and `body`
  (gait, trims), while §2b keeps models and leg calibration with the robot. The mocks say the
  robot wins while the persona wears Fish. The plan should say so, and say whether the soul's
  copy applies when the persona is alone.
- **"Launch from a link".** Read here as a one-time, two-minute link (code 2 as text) to send
  to a trusted phone. If the owner means a **standing** per-persona link (a bookmark or the
  installed app's icon), that is a credential. The installed app's `/start` would then reopen
  *the persona that phone last held* rather than "the creature on its robot", which is a G4
  change to the installable-app work in §4.
- **Remote rider (B only).** B's *Who wears Fish* lets the PWA hand Fish to a persona already
  open on another phone. The §2c rider route is set by the phone that connects. Making it
  settable from the PWA for another phone's session is new, and needs its own auth test.
- **Delete takes the checkpoints.** The mocks delete a persona's checkpoints with it and say so
  on the armed tap. If the owner would rather keep them as seeds, delete should leave them under
  a *Deleted personas* group instead.
- **Export does not carry visual memories.** Upstream's soul file is `pb2_soul` alone, so an
  exported file loses faces. That is the safer default for a file that leaves the box. A
  checkpoint (on the box) keeps them.
- **One persona per phone** follows from upstream's single `pb2_soul` key. The plan implies it
  but doesn't say it. The hand-over route should close the persona a phone held when another
  opens there.

## Not mocked here

- The G2 flash and rekey sheets (*Add a robot*, *New key*, *Reflash Fish*) are stubs that
  name the G2 sheet. They are unchanged.
- The creature's own *it has moved* and *the body has gone* pages on the old phone, and the
  hiding of its pairing box. Those are in `growbot/patches/`, not the PWA.
- G3's settings card. Its gate should weigh putting it on this screen (G2 README "Not chosen
  here").
- A second robot body. Each variant's *Add a robot* says a second robot would get its own card,
  and *Connect to body* would then ask which one.
