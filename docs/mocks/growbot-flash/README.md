# GrowBot → flashing, pairing and opening a robot

Kind: Living (mock set) · Status: **B chosen** (owner, 2026-11-10) · Last verified: 2026-11-10

The GUI gate for wave **G2** of [`../../plans/GROWBOT_PLAN.md`](../../plans/GROWBOT_PLAN.md)
(§3 "Flashing", §4 G2). Three directions for the same four jobs:

1. **Flash a GrowBot** (a Raspberry Pi Pico 2 W on the box's USB): pick the board, pick the
   Wi-Fi networks it should join, run the flash (MicroPython with `picotool`, then the robot's
   files and generated `secrets.py` with `mpremote`), with a log, a failure and a success.
2. **The pairing code** (`gb-` + 6 hex) and the robot's **device key**: made at flash time,
   replaced, or revoked behind a 3-second arming confirm, as on the panels.
3. **Open GrowBot**: one per robot, it makes a one-time link and opens
   `https://jbrain.local:8444` on this phone, with a first-time note and a "download the box's
   certificate" path for a phone that does not trust the box yet.
4. **The Ops listing**: the `growbot` service row (up or down, robots connected now, last seen).

Open `a-endpoints-board.html`, `b-growbot-screen.html` and `c-inline-cards.html` directly in a
browser. Each is standalone and makes no network request: `shared.css` holds the tokens lifted
verbatim from `frontend/src/styles/tokens.css` (both themes), and `shared.js` holds the
simulated state and the parts all three share (the flash run, the Wi-Fi checklist, the key
actions, the Open GrowBot sheet, the Ops list), so the variants differ only in structure. The
dashed bar above the phone is mock controls, not design: switch screens and theme, change what
is on the USB (a new Pico, one already running MicroPython, nothing), make the next flash fail
loading MicroPython or fail joining Wi-Fi, take the `growbot` service down, and say whether
this phone trusts the box. "Open GrowBot" shows a pretend tab instead of navigating.

## What every variant fixes

These are faults in today's flasher (`EndpointsScreen.tsx`) that a GrowBot board would hit,
not preferences. They land whichever direction is chosen.

- **The board choice ignores the port.** The screen picks no board on purpose, because the S3
  and C6 panels are the same Espressif USB device and a guess is the wrong chip. A Pico is a
  different USB device (`2e8a`), so for it the port can say what it is: GrowBot is chosen and
  the panel boards are greyed with *"Not what's plugged in — that port is a Pico."* The port's
  only badge today is `panel` (`is_espressif`), so a Pico would be listed with none; it reads
  **Pico 2 W** now.
- **One typed network.** The screen takes a single SSID and password, but the plan writes the
  box's Wi-Fi *list* into `secrets.py`. Every variant shows the networks remembered on the box
  as a checklist (tried in order, more than one allowed), with *Add another network* for a new
  one.
- **One log for two tools, and a failure with no way out.** *"Flash failed — the log below says
  where"* leaves the owner reading `picotool` output. The flash is now three named rows (Load
  MicroPython · Copy its firmware and Wi-Fi · Join Wi-Fi and reach the box), and each failure
  states the situation and the recovery (`DESIGN.md` Voice & microcopy): hold BOOTSEL and
  replug when MicroPython would not load, check 2.4 GHz and range when Wi-Fi did not answer,
  and that the second try skips MicroPython.
- **Panel words on a robot.** "Plug a panel", "hold BOOT", "checks for firmware every 15
  minutes" are all wrong for a Pico. The copy is the board's own.
- **Every flash is a new unit.** On the panels, a reflash adds another row, which is how the
  owner ended up with one row per flash and a revoke that was hard to find. Every variant asks
  *Which robot is this*: a new robot, or *Reflash Quorra*, which keeps the pairing code and
  replaces the key, so the old key stops working (plan §2 "Reflashing revokes the old key").
- **The key actions say what they cost.** *Revoke key* is rose-tinted at rest and needs a
  second tap within 3 seconds (it disarms on its own). *New key* says the robot has to be on
  the box's USB, because the key lives in `secrets.py` on the chip; it is a short `mpremote`
  run with its own log, not a button that pretends.
- **Hidden dependencies are written down.** The Wi-Fi step says the robot reaches the box at
  its LAN address and needs a USB reflash if that address changes (plan §3). Opening a robot
  that is offline or revoked says what happens (GrowBot opens, its legs don't move) instead of
  failing quietly.
- **The certificate is explained once, with a way out.** First open on a phone shows why a
  warning may appear (the box's own certificate on `:8444`, which camera and microphone need)
  and offers *Download the box's certificate*, the PWA path `../../runbooks/LOCAL_ACCESS.md`
  names as missing. The mock cannot detect trust, and neither can the real page before the
  link opens, so the note is shown once per phone rather than guessed. Serving the root (the
  public `lan-root.crt` the box already publishes) is a G2 build item whichever variant wins.
- **Ops says what a robot needs.** The `endpoint` row becomes *panel and robot flasher*. The new
  `growbot` row's second line is *1 of 2 robots connected now*, its open body lists each robot
  with its last-seen time, and down it says the consequence (*no robot can connect until it is
  back*) beside the rose dot.

## The three

| | Idea | Costs |
|---|---|---|
| **A** `a-endpoints-board` | **Another board.** GrowBot joins the Room endpoints stepper the owner already knows (steps 1–3 unchanged in shape), success adds a step 4 *Paired* with the code and *Open GrowBot*, and a **Robots** tab beside Flash holds the robots afterwards, the way jpanel's Panels tab holds the panels. Ops reads only and points to the Robots tab. | Least invention. But the everyday tap (*Open GrowBot*) lives two levels down on a screen named for panels, and the stepper carries both panel and robot steps. When G3 settings and G4 profiles arrive, they have no natural home here. |
| **B** `b-growbot-screen` | **Its own screen.** A GrowBot launcher tile (it says who is connected) opens a screen where the robots come first and *Open GrowBot* is the biggest button on each. *Add a robot* runs the flash as a guided sheet, one step and one button at a time (plug in → Wi-Fi → flashing → paired), and the log is behind *Show the log* until something fails. jpanel's Flash tab recognises a Pico and sends it here, so there is one door. | A new screen and launcher tile. A four-step sheet stretches `DESIGN.md`'s "longer flows are full screens"; it stays one primary action per step, and the sheet can be closed mid-flash and reopened from *Flashing — tap to watch*. If the rule is read strictly, the same four steps become a full screen with no other change. |
| **C** `c-inline-cards` | **Where you already look.** No new screen. The launcher gets one wide live GrowBot tile with an *Open* per robot, the one-tap everyday path. Everything else expands in place inside the `growbot` row on Ops → Services: each robot (code, key, Open, new key, revoke) and *Add a robot*, a compact inline flash. | Fewest screens and taps to open. But it puts a worked, several-minute form back into the scanned dashboard, which `EndpointsScreen.tsx`'s header records as the mistake that gave the flasher its own screen. A wide live tile is also a new device in the launcher grid, and Ops ends up holding things that are not service operations. |

## Chosen: B

The owner picked **B** on 2026-11-10. `b-growbot-screen.html` is the binding spec for G2; A and C
are kept as the record of what was weighed.

## Recommendation: B

The owner flashes a robot once and opens it every day, and B is the only direction built that
way round: *Open GrowBot* is the first thing on the screen, and flashing is a step-by-step
sheet behind one button. It also gives the next two waves a home. G3's settings and G4's
profiles are about a robot, and on B they are sections of the same screen rather than a card
in Settings plus a tab on the flasher plus a row on Ops. A is the safe fallback if the owner
would rather not add a screen, and C's one-tap launcher Open could be added to B's tile later
without taking on C's form-in-Ops cost.

## Not chosen here

- **The plan's G3 "GrowBot card in Settings, beside the panels"** is left to G3's own gate.
  If B is chosen, that gate should weigh putting the settings on the GrowBot screen instead.
- **Watching the robot's console** (the flasher's step 4 for panels) is not mocked. It carries
  over unchanged as *Watch the robot* in whichever direction is chosen.
- **Renaming a robot** is not shown. A reflash with the same pairing code keeps the name, and a
  rename belongs with G3's settings.
