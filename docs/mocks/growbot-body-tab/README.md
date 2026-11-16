# GrowBot → the Body tab, reimagined

Kind: Living (mock set) · Status: **picked — A, remote plus rows (owner, 2026-11-16)**; binding spec in `docs/reference/DESIGN.md` "GrowBot — the desk robot's screen" · Last verified: 2026-11-16

The owner's ask (2026-11-16): *"This page needs reimagined and sent through UI agent redesign"*,
then *"identify the problems and tell me them first. Give me options for the paths forward."*

## The problems it fixes

1. One scroll of about five phone screens: the robot card, *Who wears*, the whole *Controls*
   card (walks, pad, durations, Drive, Follow, Test, Tricks), *Add a robot*, the brain log and
   *Walks and spins*.
2. Two ways to drive one walk: the pad (a fixed move for N seconds) and a separate *Drive*
   section (live heading, speed and camera height), which asked for the walk a second time and
   had its own start/stop apart from the pad's.
3. *Walk with* put every trained walk on a full-width chip with its whole library name, seven
   rows or more above the pad.
4. Clipping: fixed-circle trick chips cut *Stand tall* and the escape spins, the long walk chip
   under *Drive* ran off the card, *straight on* wrapped.
5. Wordy, shifting labels (*Its own walks*, *Walks and spins run for*), and the durations sat
   between the pad and *Drive* although only the pad uses them.
6. Everyday controls mixed with developer tools: the body test history and log links, *Measure
   1 m* and *Flip frames* sat between driving and tricks.

## The three

Open the HTML files directly in a browser; each links to the others from its mock bar and makes
no network request. `shared.js` holds the simulated robot and every shared part; tokens and
chrome are `../growbot-policies/shared.css`, the new parts `body.css`. PNGs are 390-wide
captures at 2×.

- **A — remote plus rows** (`a-remote-rows.html`, `a.png`, `a-steer.png`, `a-walks.png`).
  **Picked.** The robot's card keeps its state and who wears it, with *Who wears <name>, and its
  key* as one row opening its sheet (pairing code, *New key*, *Revoke key*, *Who wears*).
  *Controls* is the remote: *Walking with <short name>* as one row opening a sheet (short name,
  the rest of the library name as its gist, the kind), a *Tap a move | Steer live* switch that
  makes the pad and the old Drive one thing, durations as one segmented row under the pad,
  *Tricks* on one line that scrolls sideways. *Follow me* (with the camera setup), *Body test*
  (with its past runs and logs) and *Legs* are rows that open sheets. The brain log and *Walks
  and spins* fold into one closed *For GrowBot's developer* section at the foot.
- **B — tabs inside Controls** (`b-tabs.html`, `b.png`). *Drive · Tricks · Follow · Test · Legs*,
  each pane about a screen; live steering under a *Steer live* disclosure in Drive.
- **C — cockpit** (`c-cockpit.html`, `c.png`, `c-remote.png`). A short overview with *Drive
  Fish* opening a full-screen one-handed remote: a joystick for live heading and speed, camera
  height on a side slider, *Stop* in the thumb zone, *Buttons* mode for the timed pad.

A fourth path, *tidy in place* (fix the clipping and names, merge the drives, move the
developer tools down), was offered in words and not mocked: the page stays long.
