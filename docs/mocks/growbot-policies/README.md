# GrowBot → new walks and spins from the official repo

Kind: Living (mock set) · Status: **picked — A, a card on the Body tab (owner, 2026-11-12)**; binding spec in `docs/reference/DESIGN.md` "GrowBot — the desk robot's screen" · Last verified: 2026-11-12

The owner's ask (2026-11-12): *"I think occasionally the grow bot official repo will post up
new policies. Can you build in a way for the jbrain pwa to check if there are new policies for
the robot body?"* The box checks `britcruise9/GrowBot` twice a day against the commit its copy
was built from (`jbrain.growbot.upstream`, [`../../plans/GROWBOT_PLAN.md`](../../plans/GROWBOT_PLAN.md)
"Upstream policy watch") and notifies once per new upstream version. These three mocks are where
the PWA shows the result.

Open the HTML files directly in a browser. Each is standalone and makes no network request:
`shared.css` is the G2 mock set's tokens (lifted from `frontend/src/styles/tokens.css`) plus a
few rows, and `shared.js` holds the simulated check and the parts all three share. The dashed bar
above the phone is mock controls: what the last check found (up to date, new policies, a failed
check) and the theme. *Check now* pretends to ask GitHub.

## The three

- **A — a card on the Body tab** (`a-body-card.html`). **Picked.** Always there under the
  robots: quiet when current, the list of new and changed files when not, how they come in,
  *Check now* and *See on GitHub*.
- **B — a banner and a sheet** (`b-banner-sheet.html`). Nothing but one dim line until upstream
  moves, then an amber banner above every GrowBot tab opens a sheet with the same contents.
- **C — a row on Ops** (`c-ops-row.html`). Beside the box's own Update, since bringing policies
  in is an update; the GrowBot screen is unchanged.

## What every variant fixes

- Bringing policies in is a sync PR (`growbot/sync.sh` rebuilds the patched app around them),
  not something the phone can do, so no variant has a "Pull in" button that cannot work. Each
  says who does it: ask Claude to sync, then Ops → Update.
- Only policy files are listed (JSON weights under `app/policies/`, `app/policy*.json` and
  `policy/`); training checkpoints, notebooks and videos are counted with the other files.
