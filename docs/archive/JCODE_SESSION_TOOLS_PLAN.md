# jcode per-session tools — independent tool versions per session (no broker, no nesting)

> **Status:** Shipped 2026-07 · `jcode/jcode-path.sh` per-session PATH shadowing

**Status: PROPOSED. The cheap, red-team-endorsed alternative to
`JCODE_CONTAINER_PER_SESSION_PLAN.md` (which three independent reviewers judged not viable
as scoped). Delivers the originating need — upgrade `grok` per session without an image
rebuild — by giving each session its own tool directory on `PATH`, inside today's single
jcode container. No privileged broker, no nested Docker, no host runtime dependency, and no
reversal of the parked netns decision.**

## Goal

Let each session carry its **own versions of the agent CLIs** (`grok` first; the mechanism
generalises to `claude`/`node`) without rebuilding the shared image and without one session's
upgrade affecting another. Concretely: `jcode-grok upgrade` inside a session installs a grok
binary into that session's private dir, which shadows the image's pinned copy for that
session only.

## Why this, not container-per-session

The motivation chain that started this (grok-rebuild pain → per-session tool versions) is
**fully met by `PATH` ordering + a per-session writable dir** — no isolation machinery. The
heavier "own services / nested Docker" want was the only thing that justified container-per-
session, and it was found speculative (no named project) and to carry a large security/ops
bill. See `JCODE_CONTAINER_PER_SESSION_PLAN.md` § "Red-team verdict" for the full reasoning;
this plan is the recommended cheaper variant (its "Variant 1").

## Design

- **Per-session tool dir.** A per-session `<tools_root>/<sid>/bin`, created on session
  `create` **outside** the git checkout (so it never shows as untracked / interferes with a
  build that scans the tree), removed on `delete`. Prepended to the shell's `PATH` and
  exported as `$JCODE_TOOLS_BIN`. A binary here shadows `/usr/local/bin` → the session's
  `grok` is its own copy. That `PATH` precedence is the entire isolation mechanism.
- **`jcode-grok` helper** (shipped in the image, on the shared `PATH`):
  - `jcode-grok upgrade [version]` — runs x.ai's installer with `GROK_BIN_DIR=$JCODE_TOOLS_BIN`
    (default = the `x.ai/cli/stable` pointer; an explicit arg pins a version). Same install
    path as `jcode/Dockerfile:26`, retargeted to the per-session dir.
  - `jcode-grok version` — shows the session's installed version vs the image default.
  - Fails **gracefully with a clear message** when egress is locked down
    (`JCODE_EGRESS_PROXY` set, x.ai not allowlisted). The failure is contained to the command,
    NOT the session-create path — which is exactly why this belongs per-session-on-demand and
    not in `create`.
- **Provenance preserved.** The image keeps shipping the pinned `GROK_BUILD_VERSION` as the
  default/floor; the per-session upgrade is explicit and opt-in, so no remote binary is pulled
  unless the owner asks. (`grok-config.sh` is unaffected — it still renders `~/.grok/config.toml`
  from the per-session `GROK_MODEL`; only the binary that reads it changes.)

## Non-negotiables / invariants

- All LLM calls still go through the shim/adapter; no data surface touched. This is purely a
  per-session filesystem/`PATH` change inside the existing sandbox.
- No new privilege, no Docker socket, no host dependency. The session shell already runs as the
  sandbox user; writing to its own dir needs nothing new.
- `dev-setup.sh` + the Dockerfile updated in the SAME wave as the helper (CLAUDE.md #8).
- Tests land with the code: 80% backend gate; the delete/purge path (a teardown guarantee)
  covered.

## Scope (owner-confirmed)

**Build T1 + T2 + T4. Skip T3 (no launcher UI).** Because T4 (per-session `$HOME`) is in
scope, **T1 establishes the per-session HOME as the foundation** and the tool dir lives under
it (`$HOME/.local/bin`) — rather than building a standalone `tools_root` that T4 would rework.
T4 then shrinks to "npm prefix + a `claude` path." Same end state, no throwaway work.

## Waves

- **Wave T1 — per-session `$HOME` + tool dir + PATH (the foundation).** `SessionManager` gains a
  `home_root`; `create` provisions `<home_root>/<sid>` with `.local/bin` + `.npm-global`,
  `delete` removes it; a `home_for(sid)` accessor (HOME is derived, not added to the serialized
  session, so the api contract is untouched). New `home_env(home)` in `terminal.py` sets `HOME`,
  prepends `$HOME/.local/bin` + the npm-global bin to `PATH`, and exports `$JCODE_TOOLS_BIN` +
  `NPM_CONFIG_PREFIX`; `serve_terminal` merges it alongside `model_env`/`preview_env`. The
  per-session HOME also makes `~/.grok` (via `grok-config.sh`), `~/.claude`, and shell history
  per-session for free. *Tests:* HOME set + bin ahead of `/usr/local/bin`; create provisions /
  delete purges the home; `home_for` deterministic; real `prepare_home` makes the bin dirs.
- **Wave T2 — `jcode-grok` helper + image. [DONE]** `jcode-grok upgrade [version]` installs
  grok into `$JCODE_TOOLS_BIN` (installer's latest, or a pinned arg), shadowing the image's
  copy for that session; `jcode-grok version` shows session-vs-image. Shipped on the shared
  `PATH` via the Dockerfile; `GROK_BUILD_VERSION` stays the pinned default/floor. Fetches the
  installer as its own step so a locked-egress failure is a clear message, not a pipe error.
  `dev-setup.sh` unchanged — the helper is image-provided (built by `jcode-setup.sh`), not a
  dev dependency. *Tests:* fake `curl` (no network) covers install-into-session-bin, version
  pass-through, clean egress-failure, refuse-outside-session, and usage.
- **Wave T4 — generalise to `claude`/`node`. [DONE]** With the per-session npm prefix from T1
  (`NPM_CONFIG_PREFIX=$HOME/.npm-global`, its bin led by `jcode-path.sh`), `jcode-claude
  upgrade [version]` runs `npm i -g @anthropic-ai/claude-code@<v>` into the session prefix,
  shadowing the image's `claude` for that session only; `jcode-claude version` shows
  session-vs-image. Shipped on the shared `PATH` via the Dockerfile. Note the egress contrast:
  claude pulls from `registry.npmjs.org` (on the allowlist), so it upgrades even under strict
  egress — unlike `jcode-grok` (x.ai). Per-session CLI **config** (`~/.grok`, `~/.claude`,
  shell history) already follows from the per-session HOME (T1), no extra work.
  *Tests:* fake `npm` (no network) covers package+version pass-through, default `latest`,
  npm-failure, refuse-outside-session, usage.

Each wave follows `docs/PROCESS.md`: independent adversarial review (reviewer ≠ author) and
tests in the same change. (Wave branches/PRs per the process; this work is on the session's
designated branch and opens a PR only when the owner asks.)

## Provenance note

The image keeps a pinned `grok` (`GROK_BUILD_VERSION`) for provenance. A per-session
`jcode-grok upgrade` trusts x.ai's TLS endpoint with **no** checksum/signature pinning (the
same posture the image build already uses), and a bare `upgrade` (no version arg) pulls the
installer's *latest* — i.e. less pinned than the image default. Pass an explicit version to
pin. This is acceptable under the threat model (TLS + the egress allowlist excludes x.ai, so
strict-egress sessions can't reach it at all), but it is a deliberate trust choice, not a
verified supply chain. [Independent review, Waves T2+T4, finding 2.]

## Known residuals

- **Orphaned per-session dirs on container restart.** Sessions are in-memory, so a jcode
  container restart loses the session objects and `delete` never runs for them — leaving
  `/work/.home/<sid>` (and, identically, the existing `/work/<sid>` checkouts) on the
  volume. T1 inherits and doubles this pre-existing checkout-orphan posture rather than
  introducing a new class. No disk janitor exists today; if one is ever added for orphaned
  checkouts it must sweep `/work/.home/<sid>` too. Acceptable for a personal box (same
  volume, inside the egress/firewall boundary). [Independent review, Wave T1, finding 3.]

## Dropped

- **T3 — launcher "Tools" UI.** Not wanted now; the shell helpers (`jcode-grok` / `jcode-claude`)
  are the interface. Can be added later as a thin control-server endpoint over the same helpers.
