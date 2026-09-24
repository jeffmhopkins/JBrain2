# jpanel → Panels tab

Kind: Plan (mock set) · Status: proposed · Last verified: 2026-09-24

Three directions for rebuilding the **Panels** tab of `JpanelScreen.tsx`, which shipped
ahead of its design pass and reads as noticeably less finished than the Messages and
Flash tabs beside it.

Open `a-fleet-card.html`, `b-status-spine.html`, `c-expand-to-manage.html` directly in a
browser. Each is standalone: `_shared.css` holds the tokens lifted verbatim from
`frontend/src/styles/tokens.css`, and `_knobs.html` is the shared-settings card the three
variants include so it only exists once.

## What every variant fixes

These are faults, not preferences, and they land whichever direction is chosen.

- **`--text-dim` does not exist.** It is referenced 17× — in `jpanel.css` and again in the
  `.ops-panel-*` rules in `styles.css` — and is defined nowhere. Every line meant to
  recede renders at full `--text`, which is most of why the tab reads as flat. Replace
  with `--text-2` / `--text-3`.
- **`.jp-panel`, `.jp-panel-head` and `.jp-panel-name` are each declared twice** in
  `jpanel.css`, and `MessagesTab` uses `.jp-panel` as well — so the Panels block has been
  silently restyling the Messages cards. De-duplicate *before* touching anything else in
  that file.
- **No gutter.** The list runs to the bezel while the segmented control above it is
  inset, so nothing lines up. 16px, matching Flash.
- **Tap targets under 44px**, and **Revoke styled like its neighbours** — the one
  irreversible action on the screen is the least distinguishable. `--danger-tint`.
- **Hardcoded type**, so the tab ignores Settings → Text size. Use `--fs-*`.
- **Status by fade only.** A panel that has not reported in ten hours looks like one that
  reported a minute ago, only greyer. Say it in words.

Four behavioural bugs travel with the rebuild, independent of direction: revoke arms and
never auto-disarms, "Saved." is permanent, the relative timestamp is computed once and
freezes, and `PanelAudio` returns `null` while loading so the knobs card vanishes rather
than showing a skeleton.

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
