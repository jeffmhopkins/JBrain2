# jcode per-session tools — independent tool versions per session (no broker, no nesting)

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

## Waves

- **Wave T1 — per-session tool dir + PATH.** `SessionManager` gains a `tools_root`; `create`
  makes `<tools_root>/<sid>/bin`, `delete` removes it; `Session` carries `tools_dir`. New
  `tools_env(tools_dir)` in `terminal.py` prepends the bin to `PATH` and exports
  `$JCODE_TOOLS_BIN`; `serve_terminal` merges it alongside `model_env`/`preview_env`.
  *Tests:* bin is on `PATH` ahead of `/usr/local/bin`; two sessions stay independent; the dir
  is gone after `delete`. Per-task adversarial review.
- **Wave T2 — `jcode-grok` helper + image/setup.** Ship `jcode-grok` (install/upgrade/version)
  in the image on the shared `PATH`; wire `dev-setup.sh` to install it; keep `GROK_BUILD_VERSION`
  as the default. *Tests:* helper resolves target dir + version, errors cleanly without egress
  (installer faked — no network in tests). Per-task adversarial review.
- **Wave T3 — (optional) launcher surface.** A per-session "Tools" control in the launcher
  showing the grok version with an Upgrade action, via a small control-server endpoint that
  runs `jcode-grok` in the session. Gated on owner wanting UI now vs CLI-only.
- **Wave T4 — (optional) generalise to a per-session HOME.** Give each session its own `$HOME`
  (own `~/.grok`, `~/.claude`, npm prefix, shell history) so per-session versioning extends to
  `claude`/`node` and per-session CLI config, not just `grok`. Larger; only if wanted.

Each wave follows `docs/PROCESS.md`: parallel tasks off a `wave-N` branch, per-task + per-wave
independent adversarial review (reviewer ≠ author), one PR per wave, CI green before merge.

## Open question for the owner

How far in the first pass: **T1+T2 only** (per-session `grok` upgrade via the shell — smallest,
ships the originating need), or also **T3** (launcher button) and/or **T4** (per-session HOME so
it covers `claude`/`node` too)?
