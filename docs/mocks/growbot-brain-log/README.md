# GrowBot → the brain log for GrowBot's developer

Kind: Living (mock set) · Status: **picked — A, share sheet (owner, 2026-11-12)**; binding spec in `docs/reference/DESIGN.md` "GrowBot — the brain log" · Last verified: 2026-11-12

The GUI gate for the brain log of [`../../plans/GROWBOT_PLAN.md`](../../plans/GROWBOT_PLAN.md)
§2 "Brain log". The owner's ask (2026-11-12): *"we need a way to be able to generate a brain log
to share with the main growbot developer. Ideally would be able to like select a 1 hour 3-hour
6-hour time … maybe a zip file that just includes everything that it would need for
debugging?"*

Open `a-share-sheet.html`, `b-quick-buttons.html` and `c-preview-first.html` in a browser (each
links to the others from its mock bar). They share `brainlog.js` and the GrowBot mocks' tokens
in `../growbot-personas/shared.css`, and make no network request. `a.png`, `b.png` and `c.png`
are 390 × 844 captures at 2×.

What every variant shares: the window (1, 3 or 6 hours), *Hash what people said* (default) or
*Keep what people said*, and *Names to hide*, remembered on the phone and replaced with
`[name]` in either mode.

- **A — share sheet (picked).** *Share a brain log…* under *For GrowBot's developer* on the Body
  tab opens one sheet with every choice and *Download brain log (.zip)*.
- **B — quick buttons.** A card on the Body tab with *Hour · 3 hours · 6 hours*, each one tap to
  download; the privacy choices fold under a summary line. Fastest, but the choices are out of
  sight when the file leaves.
- **C — preview first.** A's sheet, but *See what's in it* first shows the window's counts and
  how a transcription will read, then *Download*. Needs a count endpoint on the box.
