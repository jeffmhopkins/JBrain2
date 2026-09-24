# jpanel → Panels tab

Kind: Living (mock set) · Status: **A shipped** · Last verified: 2026-09-24

Three directions for rebuilding the **Panels** tab of `JpanelScreen.tsx`, which shipped
ahead of its design pass and read as noticeably less finished than the Messages and
Flash tabs beside it.

**The owner chose A.** It is shipped; B and C are kept because the reasoning in the table
below is the record of what was weighed, and B's status spine is the obvious move if the
fleet ever outgrows a screenful.

Open `a-fleet-card.html`, `b-status-spine.html`, `c-expand-to-manage.html` directly in a
browser. Each is standalone: `_shared.css` holds the tokens lifted verbatim from
`frontend/src/styles/tokens.css`, and `_knobs.html` is the shared-settings card the three
variants include so it only exists once.

## What every variant fixes

These are faults, not preferences, and they land whichever direction is chosen.

- **`--text-dim` does not exist.** It was referenced 17× — in `jpanel.css` and again in the
  `.ops-panel-*` rules in `styles.css` — and defined nowhere. Every line meant to recede
  rendered at full `--text`, which is most of why the tab read as flat.

  Writing the gate for it (`frontend/src/cssTokens.test.ts`) turned up **five more of the
  same bug** elsewhere in `styles.css`, none of them noticed: `--fs-sm` on three plot labels
  (which silently inherited their size), `--mono` on monospace that was not, `--border-2`,
  `--surface-1` on a waveform label whose `color-mix` background was invalid and dropped
  whole, and a dead `--teal-tint` fallback. All six are fixed; the gate now fails the build
  on a seventh.
- **`.jp-panel`, `.jp-panel-head` and `.jp-panel-name` were each declared twice** in
  `jpanel.css`, and `MessagesTab` uses `.jp-panel` as well — so the Panels block had been
  silently restyling the Messages cards: thread cards came out on `--surface-2` at 12px
  radius under a 600-weight heading that had lost `--fs-title`. Two components shared one
  set of names, so the fix is two sets of names — the Panels tab is `.jp-unit*` now, and
  the gate is that no top-level selector in the sheet is declared twice.
- **No gutter.** The list ran to the bezel while the segmented control above it was
  inset, so nothing lined up. 16px, matching `.jp-threads`.
- **Tap targets under 44px**, and **Revoke styled like its neighbours** — the one
  irreversible action on the screen was the least distinguishable. `--danger-tint`, and
  rose from the resting state rather than only once armed.
- **Hardcoded type** — every font-size in the Panels half was a bare rem (0.72 … 0.88), all
  of them below every token in the scale, so the one tab the owner reads standing in front
  of a panel ignored Settings → Text size entirely.
- **Status by fade only.** A panel that had not reported in ten hours looked like one that
  reported a minute ago, only greyer. The words are on the meta line now
  (`panelStateWords`), and the timestamp takes amber or rose beside them.

Four behavioural bugs travelled with the rebuild, independent of direction, and are fixed:
revoke armed and never auto-disarmed (3s now), "Saved." was permanent (4s), the relative
timestamp was computed once and froze — so a panel that went silent an hour ago still read
as twelve minutes, the exact failure this screen exists to catch — and `PanelAudio`
returned `null` while loading, so the knobs card popped in a beat late and shoved every row
down the screen under a thumb already moving.

## The three

| | Idea | Costs |
|---|---|---|
| **A** `a-fleet-card` | Converge on the Ops fleet card, which already presents this data correctly. Name, right-aligned status, one meta line, three buttons. | Least invention and most consistency, but a long roster is still a wall of equal-weight cards. |
| **B** `b-status-spine` | A 3px coloured left edge per card carries health so six panels stay scannable without reading. Colour is **never alone** — the state is written beside it, per `DESIGN.md`. | A new device in the system's vocabulary; earns itself only if the fleet grows past three. |
| **C** `c-expand-to-manage` | Collapsed it is a roster; open one and the pet-name field and actions appear inline, instead of behind a second tap into a sub-editor. | One more tap to reach Revoke. Actions stay **visible** once open — deliberately not behind a swipe, since this tab exists because revoke was unfindable behind the Location screen's swipe rail.

## Not chosen here

The tab keeps three flat sections (shared knobs, then one card per panel). Grouping
panels by child, or folding the Panels tab back into Location, were both considered and
dropped: the first invents hierarchy for a two-item list, and the second is the arrangement
this tab was split out of.
