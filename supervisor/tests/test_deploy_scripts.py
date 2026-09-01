"""Deploy scripts reachable from a one-shot must be POSIX sh.

The export/import/reset/update one-shots run in the bash-less docker:cli
(Alpine) container (gateway.UPDATER_IMAGE). A script with a
`#!/usr/bin/env bash` shebang dies there with "env: can't execute 'bash'" —
exactly how the reset safety-backup failed when backup.sh was still bash. So
every script the one-shots invoke (the *-inner.sh entrypoints and backup.sh,
which import/reset call by path) must declare `#!/bin/sh` and parse under a
POSIX shell. Host-only scripts (restore.sh, install.sh) may stay bash.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

# Reachable from a one-shot container: the entrypoints sh-exec'd by the
# gateway, plus backup.sh which import-inner/reset-inner call as ./backup.sh.
ONESHOT_SCRIPTS = [
    "export-inner.sh",
    "import-inner.sh",
    "reset-inner.sh",
    "update-inner.sh",
    "backup.sh",
    # Reached from update-inner.sh: the local-model provisioning sync and the
    # weight downloader it calls both run in the bash-less updater, so both must
    # parse under POSIX sh.
    "local-models-sync.sh",
    "download-local-weights.sh",
    # The (destructive) weight pruner the sync calls for uninstalled models — also
    # runs in the bash-less updater, so it gets the POSIX-shebang + sh -n checks.
    "prune-local-weights.sh",
]


@pytest.mark.parametrize("name", ONESHOT_SCRIPTS)
def test_oneshot_script_uses_posix_sh_shebang(name: str) -> None:
    first_line = (DEPLOY / name).read_text().splitlines()[0]
    assert first_line == "#!/bin/sh", (
        f"{name} is run inside the bash-less docker:cli one-shot; a bash "
        f"shebang fails with 'env: can't execute bash'. Got: {first_line!r}"
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh available")
@pytest.mark.parametrize("name", ONESHOT_SCRIPTS)
def test_oneshot_script_parses_under_posix_sh(name: str) -> None:
    result = subprocess.run(
        ["sh", "-n", str(DEPLOY / name)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{name} is not POSIX sh:\n{result.stderr}"


# The deploy scripts that lay down host files for the stack. Each must copy the
# SearXNG settings host file the compose bind mount points at — without it the
# bind source is missing, Docker mounts an empty dir over /etc/searxng/settings.yml,
# SearXNG drops to its HTML-only defaults, and /search?format=json answers 403, so
# jerv reports web search as unavailable.
# `jbrain` is deliberately NOT here: its update case delegates to update-inner.sh
# rather than laying these down itself. Listing it would re-assert the duplication
# that let the two update paths drift apart — see
# test_the_host_cli_delegates_to_the_one_update_script.
DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES = ["install.sh", "update-inner.sh"]


@pytest.mark.parametrize("name", DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES)
def test_script_deploys_searxng_settings(name: str) -> None:
    text = (DEPLOY / name).read_text()
    assert "searxng/settings.yml" in text, (
        f"{name} must copy the SearXNG settings to the host bind path; a missing "
        f"file makes Docker mount an empty dir and SearXNG refuse the JSON API"
    )


@pytest.mark.parametrize("name", DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES)
def test_script_clears_stale_searxng_settings_path(name: str) -> None:
    # On a box already broken by the missing-file bug, the bind path is an empty
    # directory Docker created. `cp file dir/` would drop the file *inside* that
    # directory instead of replacing it, so the dir mount survives and SearXNG
    # still crash-loops. The path must be removed before the copy so the box heals.
    text = (DEPLOY / name).read_text()
    assert "rm -rf searxng/settings.yml" in text, (
        f"{name} must remove any stale searxng/settings.yml (a Docker-made "
        f"directory on an already-broken box) before copying the file in"
    )


@pytest.mark.parametrize("name", DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES)
def test_script_ensures_searxng_secret(name: str) -> None:
    # SearXNG refuses to start without a secret, so a stack that predates the
    # web-search service (no SEARXNG_SECRET in .env) must have one backfilled or
    # web search stays down. install.sh writes it for fresh .env files and
    # backfills existing ones; the update paths backfill when absent.
    text = (DEPLOY / name).read_text()
    assert "SEARXNG_SECRET" in text, f"{name} must ensure SEARXNG_SECRET is set"


# Both update paths must keep the opt-in code-mode sandbox (jcode) turnkey: once the
# operator has enabled it (a one-time scripts/jcode-setup.sh), the PWA update and the
# host `jbrain update` keep it built/current with no CLI.
# `jbrain` delegates its update to update-inner.sh, so the shared script is the only
# place this can be asserted — listing both would re-pin the duplication that let
# them drift.
JCODE_TURNKEY_SCRIPTS = ["update-inner.sh"]


@pytest.mark.parametrize("name", JCODE_TURNKEY_SCRIPTS)
def test_update_keeps_jcode_turnkey_when_enabled(name: str) -> None:
    # Gated on JCODE_ENABLED=true so a stock stack never builds or starts the
    # arbitrary-code sandbox; when on, activate the `jcode` profile (so the rebuild +
    # recreate include it) and self-heal the api<->jcode bearer (so enabling it never
    # requires re-running the setup script).
    text = (DEPLOY / name).read_text()
    assert "JCODE_ENABLED=true" in text, f"{name} must gate jcode on JCODE_ENABLED=true"
    assert "--profile jcode" in text, (
        f"{name} must activate the jcode profile when enabled so update rebuilds it"
    )
    assert "JCODE_TOKEN" in text, (
        f"{name} must backfill the api<->jcode token so enable stays CLI-free"
    )


# The job launcher (jlaunch) is DEFAULT-ON (single-user box): a base-stack service, not
# profile-gated. Every path that provisions the stack must backfill its api<->jlaunch
# bearer (the fail-closed control server won't start without it), and NONE may hide it
# behind a JLAUNCH_ENABLED gate or a `--profile jlaunch`.
# Same: the host CLI delegates, so update-inner.sh carries this for both callers.
JLAUNCH_PROVISION_SCRIPTS = ["install.sh", "update-inner.sh"]


@pytest.mark.parametrize("name", JLAUNCH_PROVISION_SCRIPTS)
def test_provisions_jlaunch_token(name: str) -> None:
    text = (DEPLOY / name).read_text()
    assert "JLAUNCH_TOKEN" in text, (
        f"{name} must provision the api<->jlaunch bearer (jlaunch is default-on)"
    )


@pytest.mark.parametrize("name", JLAUNCH_PROVISION_SCRIPTS)
def test_jlaunch_is_not_profile_gated(name: str) -> None:
    text = (DEPLOY / name).read_text()
    assert "--profile jlaunch" not in text, (
        f"{name} must not profile-gate jlaunch — it's a default-on base-stack service"
    )
    assert "JLAUNCH_ENABLED=true" not in text, (
        f"{name} must not gate jlaunch on JLAUNCH_ENABLED — it's default-on"
    )


def test_compose_runs_jlaunch_by_default() -> None:
    # The service is in the base stack (no `profiles:` key), and the api default-wires
    # the in-network control server so /jlaunch + the Math tile are live out of the box.
    compose = (DEPLOY / "docker-compose.yml").read_text()
    assert "profiles: [jlaunch]" not in compose, (
        "the jlaunch service must not be profile-gated — it's default-on"
    )
    assert "JBRAIN_JLAUNCH_URL: ${JLAUNCH_URL:-http://jlaunch:9101}" in compose, (
        "the api must default-wire the jlaunch control server URL"
    )


def test_update_marks_worktree_safe_before_pull() -> None:
    # The pull runs as root inside the updater container against a bind-mounted
    # worktree owned by the host operator's UID; without a safe.directory entry
    # git aborts with "dubious ownership" and the PWA update fails. The guard
    # must precede the pull (a host-side config never reaches the container).
    lines = (DEPLOY / "update-inner.sh").read_text().splitlines()
    safe = next((i for i, ln in enumerate(lines) if "safe.directory" in ln), None)
    pull = next((i for i, ln in enumerate(lines) if "pull --ff-only" in ln), None)
    assert safe is not None, "update-inner.sh must mark the worktree safe.directory"
    assert pull is not None, "update-inner.sh must run the pull"
    assert safe < pull, "safe.directory must be set before the pull"


def test_update_frees_llm_gateway_memory_before_recreate() -> None:
    # The LLM gateway pins its resident model set (~91 GB) in unified memory and is
    # profile-gated, so the update's plain `up -d` never recreates it — it would sit
    # at full memory through the rebuild/migrate/recreate. On the Strix Halo box that
    # combined pressure drove a kernel reclaim livelock that hard-locked the host
    # (even keyboard/mouse), so the update must STOP the gateway before the churn and
    # restart it after the stack is up. Gated on hosting so a stock stack stays put.
    lines = (DEPLOY / "update-inner.sh").read_text().splitlines()
    text = "\n".join(lines)

    def idx(needle: str) -> int | None:
        return next((i for i, ln in enumerate(lines) if needle in ln), None)

    stop = idx("rm -sf local-llm")
    build = idx("compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE build")
    up = idx("compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE up -d")
    assert "LOCAL_LLM_ENABLED=true" in text, (
        "the gateway stop/restart must be gated on LOCAL_LLM_ENABLED so a stock "
        "cloud stack (no local-llm) is never touched"
    )
    assert stop is not None, "update must stop the local-llm gateway to free memory"
    assert build is not None and up is not None
    assert stop < build, "the gateway must be stopped before the rebuild/recreate"
    # The gateway comes back INSIDE the quiesced window now (the smoke test needs
    # it), so "restart after `up -d`" is no longer the invariant. What must still
    # hold is that an enabled gateway is running when the update finishes — including
    # when auto-update is off and nothing in the quiesced window ever rebuilt it.
    restarts = [i for i, ln in enumerate(lines) if "up -d local-llm" in ln]
    assert restarts, "update must restart the gateway"
    assert restarts[-1] > up, (
        "the LAST gateway start must follow the stack `up -d`, so an auto-update-off "
        "box still ends with its gateway running"
    )


def _rollback_rebuild_idx(lines: list[str]) -> int | None:
    """Index of the ROLLBACK rebuild — the plain `build local-llm` that recreates
    the gateway on the pinned base after a failed smoke test.

    Anchored on the rollback verdict line, not on "the first plain build in the
    file". That heuristic silently stopped pointing at the rollback when the
    floating build gained a patch-only `else` branch, which is also a plain
    `build local-llm`: both tests using it then resolved to a line ABOVE the
    smoke test, so one asserted an impossible ordering and the other searched an
    empty range. Neither was checking its stated invariant any more — and the
    script they guard is the one the owner runs from the PWA, with no terminal
    to fall back on.
    """
    verdict = next(
        (
            i
            for i, ln in enumerate(lines)
            if "rolled back to its pinned base" in ln
            and not ln.lstrip().startswith("#")
        ),
        None,
    )
    if verdict is None:
        return None
    return next(
        (
            i
            for i in range(verdict, len(lines))
            if "build local-llm" in lines[i]
            and "--pull" not in lines[i]
            and not lines[i].lstrip().startswith("#")
        ),
        None,
    )


def test_update_gateway_auto_update_is_default_on_smoke_tested_and_rolls_back() -> None:
    # DEFAULT-ON (the owner has no terminal — CLAUDE.md #10): every update floats the
    # gateway onto the newest llama.cpp (--pull on the FLOATING base tag) so a
    # freshly-released model's architecture is supported with no manual step, then
    # SMOKE-TESTS the build. A failed smoke rolls BACK to the pinned, known-good base,
    # so tracking master never leaves the box unable to serve. It must be OPT-OUT
    # (LOCAL_LLM_AUTO_UPDATE=false), NOT opt-in, and gated on LOCAL_LLM_RUNNING (set
    # only under LOCAL_LLM_ENABLED) so a stock cloud stack with no gateway is untouched.
    lines = (DEPLOY / "update-inner.sh").read_text().splitlines()
    text = "\n".join(lines)

    # Match COMMANDS, not the block's descriptive comments (which name the same steps).
    def idx(needle: str) -> int | None:
        return next(
            (
                i
                for i, ln in enumerate(lines)
                if needle in ln and not ln.lstrip().startswith("#")
            ),
            None,
        )

    # The gate is an OPT-OUT: run unless the operator explicitly set the flag to false.
    gate = next(
        (
            ln
            for ln in lines
            if "LOCAL_LLM_AUTO_UPDATE=false" in ln and not ln.lstrip().startswith("#")
        ),
        None,
    )
    assert gate is not None, (
        "auto-update must be default-on, opt-out via LOCAL_LLM_AUTO_UPDATE=false"
    )
    assert "grep -q '^LOCAL_LLM_AUTO_UPDATE=true'" not in text, (
        "auto-update must not be opt-in (=true) — the owner has no terminal to set .env"
    )
    # Default ON: the only things that turn it off are an explicit .env=false or the
    # owner's stored setting. Asserted on the resolved variable rather than the shape of
    # one grep, so the gate can be restructured without breaking the guarantee.
    assert "AUTO_UPDATE_ON=1" in text, (
        "the flag must start ON and be cleared by an opt-out"
    )
    assert '[ -n "$AUTO_UPDATE_ON" ]' in text, (
        "the block must run off the resolved flag"
    )
    assert "jbrain.cli local-llm-auto-update" in text, (
        "the owner's PWA toggle must be consulted — .env is unreachable for them"
    )
    assert "LOCAL_LLM_RUNNING" in text, (
        "auto-update must be gated on the gateway being enabled"
    )

    floating_build = idx("build --pull local-llm")
    smoke = idx("jbrain.cli local-llm-smoketest")
    # The rollback rebuild is a PLAIN build — no --pull and no LOCAL_LLM_BASE
    # override — so it recreates the gateway on the reproducible pinned base
    # from cached layers. It is not the only plain build in the script (the
    # patch-only branch is one too), so it is located by its position after the
    # rollback verdict — see `_rollback_rebuild_idx`.
    rollback = _rollback_rebuild_idx(lines)
    assert floating_build is not None, (
        "must rebuild the gateway with --pull on the floating base"
    )
    assert 'LOCAL_LLM_BASE="$FLOATING"' in text, (
        "the floating rebuild must override the base tag"
    )
    assert smoke is not None, "must smoke-test the floated build before keeping it"
    assert rollback is not None, "must have a pinned-base rollback rebuild"
    assert floating_build < smoke < rollback, (
        "order must be: floating --pull build → smoke → (on failure) pinned rollback"
    )


def test_downloader_python_heredoc_delimiter_is_quoted() -> None:
    # download-local-weights.sh embeds a Python program as a heredoc inside a
    # single-quoted `bash -c '...'`. The heredoc delimiter MUST be quoted (<<'PY')
    # so the body is fed to Python verbatim. With an unquoted <<PY, the container's
    # bash command-substitutes any backtick in the body and expands $-expressions —
    # a backticked `hf download` in a comment actually ran the command and injected
    # its help text ("Download files from the Hub.") into the source, so Python died
    # with an IndentationError and the download silently never started.
    text = (DEPLOY / "download-local-weights.sh").read_text()
    # The quoted delimiter, escaped to survive the outer single-quoted bash -c string.
    quoted = "<<'\"'\"'PY'\"'\"'"
    assert quoted in text, "the Python heredoc delimiter must be quoted (<<'PY')"
    assert "<<PY" not in text, "a bare <<PY lets bash expand backticks/$ in the body"


def test_downloader_kills_a_stalled_transfer() -> None:
    # A silently hung connection (never errors, never returns) made the old blocking
    # `subprocess.check_call(args)` wait forever — the updater sat "running" with zero
    # bytes moving and a 120B pull never finished, needing a manual container kill. The
    # downloader must instead run hf under a stall watchdog that kills a transfer making
    # no progress and lets the existing progress-aware loop resume from the partials, so
    # a hung download self-heals on the next update with no operator action.
    text = (DEPLOY / "download-local-weights.sh").read_text()
    assert "subprocess.check_call(args)" not in text, (
        "the bare blocking check_call hangs forever on a stalled connection; the "
        "download must run under the stall watchdog instead"
    )
    assert "STALL_SECONDS" in text and "_run_until_stalled" in text, (
        "the downloader must bound a no-progress transfer with a stall watchdog"
    )
    # It must actually kill the hung process group (not just wait on it) to resume.
    assert "killpg" in text and "start_new_session=True" in text, (
        "the watchdog must kill the hf process group so a hung transfer can resume"
    )


def _logical_lines(text: str) -> list[str]:
    # Join backslash-continued shell lines into one logical command.
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
        else:
            out.append(buf + line)
            buf = ""
    if buf:
        out.append(buf)
    return out


def test_config_write_runs_as_root() -> None:
    # The weights dir is bind-mounted root-owned (the sudo setup + the root download
    # container), but the api image runs as non-root appuser. The llama-swap.yaml
    # write must run as root (--user 0) or it fails with PermissionError, which the
    # best-effort sync swallows — so the model downloads but never gets a gateway
    # config and never enables. Guard both the sync and the first-enable script.
    scripts = (
        DEPLOY / "local-models-sync.sh",
        DEPLOY.parent / "scripts" / "local-llm-setup.sh",
    )
    # Match the invocation, not a comment that merely mentions the module — and not the
    # `--check` PREFLIGHT, which only resolves globs to decide what still needs
    # downloading.
    # It writes nothing, so it correctly runs as the ordinary api user. This used to
    # take the
    # FIRST match, which silently became the preflight when that was added ahead of the
    # write
    # (PR #1141) — so the assertion moved off the line it exists to guard and started
    # failing
    # on a script that was, and is, correct.
    needle = "python -m jbrain.llm.llama_swap_config"
    for script in scripts:
        writes = [
            ln
            for ln in _logical_lines(script.read_text())
            if needle in ln and "--check" not in ln
        ]
        assert writes, f"{script.name}: no llama-swap.yaml write invocation found"
        for cmd in writes:
            assert "--user 0" in cmd, (
                f"{script.name}: the llama-swap.yaml write must run as root"
            )


def test_prune_script_guards_each_delete() -> None:
    # The ONE destructive `rm -rf` must sit behind all four hard guards (charset,
    # keep-set exclusion, realpath containment, directory type) and only ever name
    # the guarded $target_abs — never a raw, unvalidated catalog id.
    text = (DEPLOY / "prune-local-weights.sh").read_text()
    # Guard 1 — charset regex rejecting traversal/slashes/spaces.
    assert "[!a-z0-9._-]" in text, "prune must reject non-catalog-id charsets"
    # Guard 2 — keep-set exclusion (never delete a still-served model).
    assert "$KEEP" in text, "prune must exclude ids still in the final keep set"
    # Guard 3 — realpath containment against the models dir prefix.
    assert "realpath" in text and '"$dir_abs"/*' in text, (
        "prune must verify the resolved target stays under the models dir"
    )
    # The single rm -rf must reference the guarded variable, never a raw id, and
    # appear exactly once.
    rm_lines = [
        ln
        for ln in text.splitlines()
        if "rm -rf" in ln and not ln.lstrip().startswith("#")
    ]
    assert rm_lines == ['  rm -rf -- "$target_abs"'], (
        f"the destructive delete must be a single guarded rm; got: {rm_lines}"
    )


def test_sync_subtracts_the_remove_queue() -> None:
    # The sync must read the uninstall queue, subtract it from the union BEFORE the
    # manifest is built (so the removed model drops out of everything downstream),
    # and clear the queue at the end.
    lines = (DEPLOY / "local-models-sync.sh").read_text().splitlines()
    text = "\n".join(lines)
    assert "local-remove-ids" in text, "sync must read the uninstall queue"
    assert "local-remove-clear" in text, "sync must clear the uninstall queue"
    assert "prune-local-weights.sh" in text, "sync must invoke the guarded pruner"
    # The subtraction (grep -vxF on the remove ids) must precede the manifest build.
    subtract = next((i for i, ln in enumerate(lines) if "grep -vxF" in ln), None)
    manifest = next(
        (i for i, ln in enumerate(lines) if "jbrain.llm.local_catalog $ids" in ln), None
    )
    assert subtract is not None, "sync must subtract the remove set with grep -vxF"
    assert manifest is not None, "sync must build the manifest from $ids"
    assert subtract < manifest, "the remove subtraction must precede the manifest build"


def test_sync_applies_removals_when_the_roster_empties() -> None:
    # Regression: "uninstall every served model" leaves an empty post-subtraction
    # $ids, which is now a VALID terminal state — the removal must still be APPLIED
    # (LOCAL_MODELS=[], restart, prune, clear). A bare `[ -n "$ids" ] || exit 0`
    # would bail before any of that, wedging the uninstall forever. The early exit
    # must therefore also require an EMPTY remove queue, and the download/swap steps
    # (which `_manifest([])` would otherwise turn into a full-catalog pull) must be
    # gated on a non-empty $ids.
    text = (DEPLOY / "local-models-sync.sh").read_text()
    assert '[ -n "$ids" ] || { say "no models to sync"; exit 0; }' not in text, (
        "the unconditional empty-$ids exit drops queued removals — it must also "
        "require an empty remove queue"
    )
    assert '[ ! -s "$remove_file" ]' in text, (
        "the no-op early exit must also require the remove queue to be empty"
    )
    assert 'if [ -n "$ids" ]; then' in text, (
        "the download/llama-swap steps must be gated on a non-empty $ids so an "
        "empty roster does not re-pull the whole catalog via _manifest([])"
    )


def test_sync_sweeps_stale_cache_partials() -> None:
    # A killed/interrupted download leaves tens of GB of huggingface .cache/*.incomplete
    # staging in an otherwise-complete model dir, which nothing else reclaims. The sync
    # must sweep `<models>/*/.cache` from every model dir, guarded by a realpath
    # containment check so a symlinked-out cache is never followed.
    text = (DEPLOY / "local-models-sync.sh").read_text()
    assert "/.cache" in text and "clearing partials" in text, (
        "the sync must sweep stale huggingface .cache partials from each model dir"
    )
    assert "realpath" in text, "the .cache sweep must realpath-check containment"


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh available")
def test_prune_deletes_only_guarded_targets(tmp_path: Path) -> None:
    # Behavioral coverage for the destructive deleter: run the real script against a
    # scratch models dir and assert each guard holds in practice — a removed id is
    # deleted, a kept id and a traversal/charset-violating id survive, and a symlink
    # escaping the models dir is never followed into a delete.
    models = tmp_path / "models"
    models.mkdir()
    (models / "removed").mkdir()
    (models / "kept").mkdir()
    (models / "also-kept").mkdir()

    # An escape target outside the models dir, reachable via a symlink inside it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary").write_text("do not delete me")
    (models / "escape").symlink_to(outside)

    script = DEPLOY / "prune-local-weights.sh"
    # removed: deletable; kept: in KEEP; ../escape & "bad id": charset; escape: a
    # symlink out of the dir (containment guard).
    argv = ["removed", "kept", "../escape", "bad id", "escape"]
    result = subprocess.run(
        ["sh", str(script), str(models), *argv],
        capture_output=True,
        text=True,
        env={"KEEP": "kept also-kept", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr

    assert not (models / "removed").exists(), "a plain removed id must be deleted"
    assert (models / "kept").exists(), "an id still in KEEP must never be deleted"
    assert (models / "also-kept").exists(), "KEEP entries not on argv stay untouched"
    assert outside.exists() and (outside / "canary").exists(), (
        "a symlink escaping the models dir must not be followed into a delete"
    )


def test_update_skips_the_smoke_test_when_the_gateway_image_did_not_change() -> None:
    """The smoke test exists to vet a new UPSTREAM build. When the rebuild produces the
    identical image there is nothing new to vet — and it is not free: it loads a model
    into
    the iGPU, tens of GB of disk read and minutes of the box's attention, on every
    routine
    update that changed nothing."""
    text = (DEPLOY / "update-inner.sh").read_text()

    assert "BEFORE_IMG=" in text and "AFTER_IMG=" in text, (
        "the image id must be compared across the rebuild"
    )
    assert '[ "$BEFORE_IMG" = "$AFTER_IMG" ]' in text
    assert "skipping the smoke test" in text
    # An unknown BEFORE (first build on a fresh box) must NOT read as "unchanged" — that
    # would skip the vet on the one build nothing has ever verified.
    assert '[ -n "$BEFORE_IMG" ]' in text


def test_update_persists_the_build_stamp_into_env() -> None:
    """Exporting alone scopes the revision stamp to one script's process, so any later
    compose build outside it bakes an image that reports its revision as "unknown" — the
    exact question /api/debug/version exists to answer."""
    text = (DEPLOY / "update-inner.sh").read_text()

    assert "JBRAIN_GIT_SHA=$JBRAIN_GIT_SHA" in text
    for key in ("JBRAIN_GIT_SHA", "JBRAIN_GIT_DESCRIBE", "JBRAIN_BUILD_TIME"):
        assert 'sed -i "s|^${_key}=.*|${_stamp}|" .env' in text or key in text
    # Rewrite-in-place, never append-a-duplicate: two lines for one key would leave the
    # winner decided by file ordering.
    assert 'grep -q "^${_key}=" .env' in text


# --- one update, two callers -------------------------------------------------


def test_the_host_cli_delegates_to_the_one_update_script() -> None:
    """`jbrain update` must CALL update-inner.sh, not reimplement it.

    They were two copies and they drifted: the host one never rebuilt the gateway
    on the floating llama.cpp tag or smoke-tested it, the containerized one never
    re-applied the OOM hardening. So the PWA path did the memory-heavy work without
    the protection against exactly that, and hard-locked the box repeatedly. This
    test is what stops them diverging again."""
    text = (DEPLOY / "jbrain").read_text()
    case = text[text.index("  update)") : text.index("  enable-lan)")]

    assert "update-inner.sh" in case, "the host CLI must delegate to the shared script"
    assert "JBRAIN_HOST_UPDATE=1" in case, "host-only steps are gated on this flag"
    # No reimplementation: the moment this case grows its own build/migrate/recreate,
    # the
    # two paths have started drifting again.
    for step in ("docker compose build", "docker compose run", "docker compose up"):
        assert step not in case, f"host update must not reimplement `{step}`"


def test_host_only_steps_are_gated_not_duplicated() -> None:
    """The callers differ in CAPABILITY, not intent — mDNS, on-box image models
    and OOM hardening need systemd/sysctls/apt, which the updater container has no
    route to. They live in the shared script behind a flag, not a second copy."""
    text = (DEPLOY / "update-inner.sh").read_text()

    assert 'HOST_UPDATE="${JBRAIN_HOST_UPDATE:-}"' in text
    for host_only in ("lan-setup.sh", "comfyui-setup.sh", "oom-hardening.sh"):
        assert host_only in text, f"{host_only} must live in the shared script"
    assert text.count('[ -n "$HOST_UPDATE" ]') >= 3, "each host-only step must be gated"


def test_update_releases_models_and_removes_the_gateway_before_building() -> None:
    """Stopping the container alone leaves the kernel to reclaim tens of gigabytes
    exactly as the build starts allocating — the race that hard-locked this box,
    keyboard included. The models are released first, the container is REMOVED (not
    just stopped) so nothing can bring it back mid-update, and the script waits for
    it to actually be gone."""
    text = (DEPLOY / "update-inner.sh").read_text()
    lines = text.splitlines()

    def idx(needle: str) -> int:
        return next(
            i
            for i, ln in enumerate(lines)
            if needle in ln and not ln.lstrip().startswith("#")
        )

    unload = idx("jbrain.cli local-llm-unload")
    remove = idx("rm -sf local-llm")
    build = idx("docker compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE build")

    assert unload < remove < build, (
        "release models, then remove the gateway, then build"
    )
    # `stop` is not enough: a stopped container is one stray `up -d` away from
    # reloading.
    assert "rm -sf local-llm" in text
    assert "ps -q local-llm" in text, "must wait for the gateway to actually be gone"


def test_nothing_restarts_the_gateway_mid_update() -> None:
    """local-models-sync's own `up -d` landed squarely in the build/recreate
    window — the one window this whole dance exists to keep clear."""
    update = (DEPLOY / "update-inner.sh").read_text()
    sync = (DEPLOY / "local-models-sync.sh").read_text()

    assert "JBRAIN_SKIP_GATEWAY_START=1" in update
    assert "export JBRAIN_SKIP_GATEWAY_START" in update
    assert 'if [ -n "${JBRAIN_SKIP_GATEWAY_START:-}" ]; then' in sync, (
        "the sync must honour the flag, not start the gateway during an update"
    )


# --- the one-off compose runs are bounded ------------------------------------


def test_the_smoke_test_and_toggle_read_are_bounded() -> None:
    """These are the calls that have actually wedged this box.

    An update stalled twice at `jbrain-api-run-... Created` — a container compose
    created
    and never started — and `set -e` plus an unbounded wait means the update stops there
    forever with the stack half recreated. The gateway client's own httpx timeouts
    cannot
    help when the process never starts, so the bound has to be outside it.
    """
    text = (DEPLOY / "update-inner.sh").read_text()

    assert "run_bounded()" in text

    # Join shell line-continuations first: both calls are wrapped, so the CLI name and
    # the
    # runner that bounds it sit on different physical lines.
    joined = text.replace("\\\n", " ")
    for call in ("local-llm-smoketest", "local-llm-auto-update"):
        line = next(
            ln
            for ln in joined.splitlines()
            if call in ln and not ln.lstrip().startswith("#")
        )
        assert "run_bounded" in line, f"{call} must run under the ceiling"
    assert "SMOKE_TIMEOUT_S=" in text and "TOGGLE_TIMEOUT_S=" in text


def test_a_timeout_reads_as_failure_not_success() -> None:
    """A hung smoke test must take the SAME path as a failed one — roll the gateway
    back to
    the pinned base — rather than stopping the update dead or, worse, being mistaken
    for a
    pass and keeping an unverified build."""
    text = (DEPLOY / "update-inner.sh").read_text()

    # Captured in the `else`: after a completed `if ...; fi`, `$?` is the status of the
    # IF
    # STATEMENT, which is 0 when the condition merely failed. Reading it below the `fi`
    # turns every failure into a success — it did exactly that when first written.
    assert "else\n" in text
    body = text[text.index("run_bounded()") : text.index("run_bounded()") + 1200]
    assert body.index("else") < body.index("_rc=$?"), (
        "the status must be captured inside the else, not after the fi"
    )
    # busybox has historically exited 143 rather than GNU's 124.
    assert '"$_rc" -eq 124' in text and '"$_rc" -eq 143' in text


def test_a_killed_run_cleans_up_its_orphaned_container() -> None:
    """`timeout` kills `docker compose run`, but the container is the DAEMON's child
    — so
    --rm never fires and it is left behind, one per attempt."""
    text = (DEPLOY / "update-inner.sh").read_text()

    assert 'docker ps -aq --filter "name=jbrain-api-run-"' in text
    assert "docker rm -f" in text


# --- the update does not compete with itself for memory ----------------------
#
# This box hard-locked, repeatedly, part-way through a PWA update. The kernel trace was
# unambiguous: llama-server failing an order:0 (single 4 KB page) allocation inside
# amdgpu_ttm_tt_populate, with __GFP_RETRY_MAYFAIL — the kernel reclaims hard and then
# FAILS rather than OOM-killing, so nothing died, nothing was logged, and the host
# livelocked in reclaim down to the USB keyboard. earlyoom and the reclaim sysctls were
# already applied at the time, so hardening alone does not cover this. The fix is
# structural: the update stops doing several memory-heavy things at once.


def _update_lines() -> list[str]:
    return (DEPLOY / "update-inner.sh").read_text().splitlines()


def _cmd_idx(lines: list[str], needle: str) -> int | None:
    """Index of the first COMMAND (not comment) containing `needle`."""
    return next(
        (
            i
            for i, ln in enumerate(lines)
            if needle in ln and not ln.lstrip().startswith("#")
        ),
        None,
    )


def _call_idx(lines: list[str], name: str) -> int | None:
    """Index of the TOP-LEVEL call of a shell function — a bare, unindented `name`.

    Unindented matters: the same call also appears indented inside the signal handler,
    and matching that instead would compare the handler's position to the phase order.
    """
    return next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == name), None)


def test_update_quiesces_the_stack_around_the_build_and_the_model_load() -> None:
    lines = _update_lines()

    quiesce = _call_idx(lines, "quiesce_stack")
    build = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE build")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    up = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE up -d")
    unquiesce = _call_idx(lines, "unquiesce_stack")

    assert quiesce is not None, "the update must quiesce the stack for the heavy phase"
    assert build is not None and smoke is not None and up is not None
    assert quiesce < build < smoke < up, (
        "order must be: quiesce -> build -> model load -> recreate. The model load is "
        "what froze the box; it belongs at the emptiest moment, not on top of a "
        "freshly-recreated stack."
    )
    assert unquiesce is not None and unquiesce > up, (
        "profile-gated services stopped by the quiesce must be put back after "
        "`up -d` — it does not name them, so they would stay down for good"
    )


def test_the_quiesce_keeps_the_control_plane_so_the_pwa_can_watch() -> None:
    """Going fully dark would free about a gigabyte and cost the owner any way to
    tell a slow build from a wedged one — on the operation that has repeatedly
    frozen the box."""
    text = (DEPLOY / "update-inner.sh").read_text()

    line = next(ln for ln in text.splitlines() if ln.startswith("QUIESCE_KEEP="))
    kept = line.split("=", 1)[1].strip().strip('"').split()
    for service in ("db", "api", "supervisor", "proxy"):
        assert service in kept, f"{service} must stay up through a quiesce"


def test_a_dead_updater_does_not_leave_the_box_half_stopped() -> None:
    """Without the trap, an updater killed mid-quiesce leaves the box silently missing
    its worker, embedder and speech services until somebody happens to notice."""
    text = (DEPLOY / "update-inner.sh").read_text()

    assert "trap unquiesce_stack EXIT" in text, (
        "the un-quiesce must run on abnormal exit, not only on the happy path"
    )


def _use_idx(lines: list[str], name: str) -> int | None:
    """Index of the first USE of a shell function — never its `name() {` definition.

    Matching the definition instead is worse than useless: the definition necessarily
    precedes everything, so an ordering assertion against it can never fail, and the
    call site could be deleted outright with the test still green. This one did exactly
    that before a reviewer caught it.
    """
    return next(
        (
            i
            for i, ln in enumerate(lines)
            if name in ln
            and not ln.lstrip().startswith("#")
            and not ln.strip().startswith(f"{name}()")
        ),
        None,
    )


def test_the_page_cache_is_dropped_before_the_model_load() -> None:
    """MemAvailable counts reclaimable page cache as free, and reclaiming it under
    pressure is exactly what livelocks this hardware. After a build that just wrote tens
    of GB of layers, the number reads fine and cannot be realised in time — so the gate
    would be measuring optimism. Dropping first makes the reading honest."""
    lines = _update_lines()

    drop = _use_idx(lines, "drop_page_cache")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    assert drop is not None, "the update must drop the page cache before a load"
    assert smoke is not None and drop <= smoke, "drop the cache BEFORE the load"
    assert "drop_caches" in "\n".join(lines)


def test_a_failed_cache_drop_is_reported_not_assumed() -> None:
    """Both drop paths are best-effort and silenced, so an unconditional "page cache
    dropped" would claim something that did not happen — and the headroom gate below
    would then be counting cache it cannot reclaim. The before/after numbers are the
    only evidence either way."""
    text = (DEPLOY / "update-inner.sh").read_text()

    for probe in ('_before="$(mem_available_gb)"', '_after="$(mem_available_gb)"'):
        assert probe in text, "sample memory on both sides of the drop"
    assert '[ "$_after" = "$_before" ]' in text, "compare the two, don't assume"


def test_the_api_is_paused_for_the_model_load() -> None:
    """The api is kept up through the build so the PWA can watch — but it runs the
    keep-warm prime, which retries every 5s and loads the chat model the moment the
    gateway answers. That is a second ~60 GB allocation racing the smoke test's, and it
    would defeat the headroom gate outright: the gate samples free memory once, then
    allocates on top of whatever the prime took."""
    lines = _update_lines()

    pause = _use_idx(lines, "pause_api")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    build = _cmd_idx(lines, "build --pull local-llm")

    assert pause is not None and smoke is not None and build is not None
    assert pause < build < smoke, "pause the api before anything can load a model"
    assert 'QUIESCED="$QUIESCED api"' in "\n".join(lines), (
        "the paused api must join the restore set, so the trap covers it too"
    )


def test_the_gateway_is_emptied_before_the_rollback_rebuild() -> None:
    """The rollback is the one branch nobody exercises, and it was the worst one. A
    smoke
    test fails at the TOOL PROBE — with both models loaded — and a timeout leaves a load
    still running, so the gateway can be holding ~90 GB. `up -d` on a changed image
    force-recreates it, and with no stop_grace_period that is SIGKILL in 10s: the kernel
    reclaims all of it at once while the rebuild allocates. That is verbatim the
    collision
    the pre-build unload exists to prevent."""
    lines = _update_lines()

    rollback_msg = _cmd_idx(lines, "rolled back to its pinned base")
    assert rollback_msg is not None
    rebuild = _rollback_rebuild_idx(lines)
    assert rebuild is not None, "the rollback branch must rebuild on the pinned base"
    release = next(
        (
            i
            for i in range(rollback_msg, rebuild)
            if lines[i].strip() == "release_models"
        ),
        None,
    )
    assert release is not None, (
        "empty the gateway before recreating it on the pinned base"
    )


def test_a_killed_quiesce_is_recoverable_without_a_terminal() -> None:
    """The EXIT trap cannot cover the kills that actually happen here: the supervisor
    reaps a hung one-shot with remove(force=True) (SIGKILL) and a power cut runs
    nothing.
    Every service is `restart: unless-stopped`, which by definition does NOT restart
    something explicitly stopped — so without a durable record the box comes back
    missing
    its worker, embedder and speech services, and `comfyui`/`mqtt` (which no `up -d`
    names) would stay down forever. The owner has no terminal to fix that with."""
    lines = _update_lines()
    text = "\n".join(lines)

    assert "QUIESCE_STATE=" in text, "the quiesced set must be recorded on disk"
    write = _cmd_idx(lines, '> "$QUIESCE_STATE"')
    stop = _cmd_idx(lines, 'stop -t 30 "$_svc"')
    assert write is not None and stop is not None and write < stop, (
        "record the set BEFORE stopping, so a kill between the two still leaves a trail"
    )
    heal = _use_idx(lines, "restore_stale_quiesce")
    quiesce = _call_idx(lines, "quiesce_stack")
    assert heal is not None and quiesce is not None and heal < quiesce, (
        "heal a previous update's quiesce before taking this update's census, or the "
        "still-stopped services are invisible to it"
    )


def test_a_signal_trap_exits_instead_of_resuming() -> None:
    """A POSIX signal trap RESUMES at the next statement. A handler that only restores
    the stack would therefore un-quiesce and then carry straight on into the build and
    the ~60 GB load with everything resident — the pre-fix condition, and with QUIESCED
    now empty it could never re-quiesce. It would also exit 0, so the PWA would report a
    signalled update as a success."""
    text = (DEPLOY / "update-inner.sh").read_text()

    assert "trap on_signal INT TERM" in text, "signals need a handler that exits"
    start = text.index("on_signal() {")
    handler = text[start : text.index("trap unquiesce_stack EXIT")]
    assert "exit" in handler, "the signal handler must exit, not fall through"


def test_the_quiesce_stops_services_one_at_a_time() -> None:
    """`docker compose stop a b c` validates every name against the CURRENT compose file
    and refuses the whole command if one is unknown. The set here comes from Docker's
    labels, so a container left by a renamed service (server-brain, whisper) puts an
    unknown name in the list — the batch fails, `|| true` swallows it, and the log still
    says "quiesced" over a stack that never stopped. That silently restores the exact
    pre-fix behaviour, on the class of update most likely to be big."""
    text = (DEPLOY / "update-inner.sh").read_text()

    quiesce = text[text.index("quiesce_stack() {") : text.index("unquiesce_stack() {")]
    assert 'stop -t 30 "$_svc"' in quiesce, "stop per service, not one batched command"
    assert "stop -t 30 $QUIESCED" not in quiesce, (
        "a batched stop can be vetoed by one name"
    )


def test_a_service_that_does_not_come_back_is_reported() -> None:
    """This function is the only thing between a failed update and a box silently
    missing
    its worker, embedder and speech services — and the owner reads this log because they
    have no terminal. Swallowing the failure defeats the purpose."""
    text = (DEPLOY / "update-inner.sh").read_text()

    restore = text[text.index("unquiesce_stack() {") :]
    restore = restore[: restore.index("\n}")]
    assert "WARNING" in restore, "a restore that fails must say so in the update log"


def test_migrations_run_next_to_the_recreate_not_before_the_gateway_work() -> None:
    """The api is deliberately kept up, so migrating early leaves the OLD image serving
    the owner's PWA against a NEW schema for the whole gateway rebuild + smoke test.
    That
    was defensible at seconds; the window now contains an unconditional multi-GB base
    image pull on a rolling tag."""
    lines = _update_lines()

    migrate = _cmd_idx(lines, "run --rm migrate")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    up = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE up -d")

    assert migrate is not None and smoke is not None and up is not None
    assert smoke < migrate < up, (
        "migrate after the gateway work, immediately before `up -d`"
    )


def test_the_floating_gateway_pull_is_bounded() -> None:
    """It is an unconditional multi-GB registry pull on a rolling tag, and it runs with
    the stack quiesced — so a stall here is a stall of the whole box, not one
    service."""
    text = (DEPLOY / "update-inner.sh").read_text()
    joined = text.replace("\\\n", " ")

    line = next(
        ln
        for ln in joined.splitlines()
        if "build --pull local-llm" in ln and not ln.lstrip().startswith("#")
    )
    assert "run_bounded" in line, "the floating pull must run under a ceiling"
    assert "PULL_TIMEOUT_S=" in text


def test_the_gateway_is_emptied_again_before_the_stack_is_recreated() -> None:
    """The tool probe loads gpt-oss (~60 GB of pinned unified memory). Recreating a
    dozen containers on top of that is the same collision the pre-build unload
    prevents, just arriving from the other end."""
    lines = _update_lines()

    # `release_models` is the shared helper; the raw CLI call lives only in its body.
    unloads = [i for i, ln in enumerate(lines) if ln.strip() == "release_models"]
    pre_build = _cmd_idx(lines, "jbrain.cli local-llm-unload")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    up = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE up -d")

    assert pre_build is not None, "there must be an unload before the build"
    assert smoke is not None and up is not None
    assert any(smoke < i < up for i in unloads), (
        "the models the smoke test loaded must be released before the recreate"
    )


def test_reclaim_hardening_runs_before_the_memory_heavy_phase_on_both_paths() -> None:
    """Hardening applied after the thing it protects against has already run is
    decoration — it sat below the build and the model load, which is where the
    freeze happened. And the containerized (PWA) caller is the one that rebuilds the
    gateway and loads a model, so it must not be the path with no protection: the
    vm.* knobs are not namespaced, so a privileged one-shot applies the same values
    the host would."""
    lines = _update_lines()
    text = "\n".join(lines)

    harden = _cmd_idx(lines, "oom-hardening.sh")
    knobs = _cmd_idx(lines, "host_kernel_write vm/min_free_kbytes")
    build = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE build")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")

    assert harden is not None and knobs is not None and build is not None
    assert harden < build and knobs < build, "harden before the build, not after it"
    assert smoke is not None and knobs < smoke
    assert "--privileged" in text, "the container path needs a privileged one-shot"


def test_container_applied_sysctls_match_the_host_hardening_script() -> None:
    """Two places write the same kernel knobs — the host script (persistent, in
    /etc/sysctl.d) and the update's per-boot fallback for the containerized caller.
    If they drift, a PWA-updated box is tuned differently from a terminal-updated
    one — the exact class of bug that made these two scripts one in the first
    place."""
    import re

    hardening = (DEPLOY / "oom-hardening.sh").read_text()
    update = (DEPLOY / "update-inner.sh").read_text()

    for knob in ("min_free_kbytes", "watermark_scale_factor", "swappiness"):
        host = re.search(rf"^vm\.{knob}\s*=\s*(\d+)", hardening, re.M)
        container = re.search(rf"host_kernel_write vm/{knob} (\d+)", update)
        assert host is not None, f"oom-hardening.sh must set vm.{knob}"
        assert container is not None, f"the update must apply vm/{knob} in-container"
        assert host.group(1) == container.group(1), (
            f"vm.{knob} differs: host {host.group(1)} vs container {container.group(1)}"
        )


# --- SDR: the DVB blacklist (docs/plans/SDR_RADIO_PLAN.md, S0b) ---------------
#
# The owner runs this box from the PWA with no terminal (CLAUDE.md #10), so both
# halves of freeing an RTL-SDR — the modprobe drop-in and evicting the module
# already bound to it — have to survive the containerized update path.

UPDATE = DEPLOY / "update-inner.sh"


def _sdr_claimed(tmp_path: Path, entries: list[str] | None) -> int:
    """Run update-inner.sh's sdr_dvb_claimed against a fake driver directory.

    `entries` None = the driver isn't loaded at all (no directory). A list builds
    the directory with those names; sysfs names a BOUND interface `1-1:1.0` and
    the driver's own control files `bind`/`unbind`/`uevent`.
    """
    body = UPDATE.read_text()
    start = body.index("sdr_dvb_claimed() {")
    end = body.index("\n}", start) + 2
    fn = body[start:end]

    drv = tmp_path / "dvb_usb_rtl28xxu"
    if entries is not None:
        drv.mkdir()
        for name in entries:
            (drv / name).write_text("")

    script = f"{fn}\nSDR_DRIVER_DIR={drv} sdr_dvb_claimed\n"
    return subprocess.run(["sh", "-c", script], check=False).returncode


def test_sdr_claim_detected_when_an_interface_is_bound(tmp_path: Path) -> None:
    # A bound interface is a symlink named bus-port:config.interface.
    assert _sdr_claimed(tmp_path, ["bind", "unbind", "uevent", "1-1:1.0"]) == 0


def test_sdr_claim_not_detected_when_only_control_files_exist(tmp_path: Path) -> None:
    # The module is loaded but holds nothing — blacklisting would be a no-op.
    assert _sdr_claimed(tmp_path, ["bind", "unbind", "uevent", "module"]) != 0


def test_sdr_claim_not_detected_when_the_driver_is_absent(tmp_path: Path) -> None:
    # The common case: no dongle, or already blacklisted. Must leave the box alone.
    assert _sdr_claimed(tmp_path, None) != 0


def test_sdr_claim_not_detected_on_an_empty_driver_dir(tmp_path: Path) -> None:
    # The unexpanded-glob case: `for x in dir/*` yields the literal pattern.
    assert _sdr_claimed(tmp_path, []) != 0


def test_blacklist_names_every_driver_that_claims_an_rtl_sdr() -> None:
    body = UPDATE.read_text()
    start = body.index('RTLSDR_BLACKLIST="')
    blob = body[start : body.index('"', start + len('RTLSDR_BLACKLIST="'))]
    for module in ("dvb_usb_rtl28xxu", "rtl2832", "rtl2830"):
        assert f"blacklist {module}" in blob


def test_blacklist_is_written_through_the_host_helper_not_a_bare_redirect() -> None:
    # A plain `>` inside the updater container writes the CONTAINER's /etc and the
    # dongle stays claimed — the exact way earlyoom's thresholds sat unapplied.
    body = UPDATE.read_text()
    assert 'host_file_write "$RTLSDR_BLACKLIST_PATH" "$RTLSDR_BLACKLIST"' in body
    assert "> /etc/modprobe.d" not in body


def test_blacklist_is_conditional_on_a_live_claim() -> None:
    # Never blacklist a driver on a box with no SDR in it.
    body = UPDATE.read_text()
    assert "if sdr_dvb_claimed; then" in body


def test_module_unload_reaches_the_host_from_the_container_path() -> None:
    # modprobe -r inside the updater container is a no-op on the host's modules;
    # it has to go through PID 1's namespaces, like the systemctl restart above it.
    body = UPDATE.read_text()
    start = body.index("host_module_unload() {")
    fn = body[start : body.index("\n}", start)]
    assert "nsenter -t 1" in fn
    assert "--privileged" in fn and "--pid=host" in fn
    assert 'modprobe -r "$1"' in fn


def _unbind(tmp_path: Path, entries: list[str]) -> str:
    """Run host_driver_unbind's host branch against a fake driver directory.

    Returns what landed in the driver's `unbind` file — the kernel's documented
    detach is writing an interface name into it.
    """
    body = UPDATE.read_text()
    start = body.index("host_driver_unbind() {")
    fn = body[start : body.index("\n}", start) + 2]

    drv = tmp_path / "dvb_usb_rtl28xxu"
    drv.mkdir()
    for name in entries:
        (drv / name).write_text("")

    call = f"HOST_UPDATE=1 SDR_DRIVER_DIR={drv} host_driver_unbind dvb_usb_rtl28xxu"
    subprocess.run(["sh", "-c", f"{fn}\n{call}\n"], check=True)
    return (drv / "unbind").read_text()


def test_unbind_walks_past_the_first_interface(tmp_path: Path) -> None:
    # Each write to sysfs `unbind` is its own detach ACTION, so `>` per write is
    # correct; a plain file in a fake tree therefore only retains the last one.
    # Seeing the SECOND interface there is what proves the loop did not stop at
    # the first — which is the property that matters for a multi-interface device.
    written = _unbind(tmp_path, ["bind", "unbind", "uevent", "1-1:1.0", "1-1:1.1"])

    assert written.strip() == "1-1:1.1"


def test_unbind_ignores_the_drivers_own_control_files(tmp_path: Path) -> None:
    # Writing "bind" or "uevent" into unbind would be nonsense; only names that
    # look like an interface (bus-port:config.interface) are devices.
    written = _unbind(tmp_path, ["bind", "unbind", "uevent", "module", "1-1:1.0"])

    assert written.strip() == "1-1:1.0"


def test_unbind_is_a_no_op_when_the_driver_is_absent(tmp_path: Path) -> None:
    body = UPDATE.read_text()
    start = body.index("host_driver_unbind() {")
    fn = body[start : body.index("\n}", start) + 2]
    missing = tmp_path / "not-loaded"
    script = f"{fn}\nHOST_UPDATE=1 SDR_DRIVER_DIR={missing} host_driver_unbind x\n"

    assert subprocess.run(["sh", "-c", script], check=False).returncode == 0


def test_unbind_runs_before_the_module_unload() -> None:
    # modprobe -r REFUSES an in-use module and a bound device is use — which is
    # exactly why the first on-box run left the dongle claimed after the drop-in
    # had already been written. Order is the fix, so pin it.
    body = UPDATE.read_text()
    unbind_at = body.index("host_driver_unbind dvb_usb_rtl28xxu")
    unload_at = body.index("host_module_unload dvb_usb_rtl28xxu")
    assert unbind_at < unload_at


def test_unbind_uses_a_privileged_container_for_the_sysfs_write() -> None:
    # An ordinary container gets sysfs read-only; the write needs --privileged,
    # the same property host_kernel_write relies on for /proc/sys.
    body = UPDATE.read_text()
    start = body.index("host_driver_unbind() {")
    fn = body[start : body.index("\n}", start)]
    assert "--privileged" in fn


def test_the_stuck_message_says_reboot_not_replug() -> None:
    # A blacklist stops a module being LOADED, not one already resident, so
    # re-inserting the device just lets the loaded driver re-bind it. Telling a
    # remote owner to re-plug is advice that both fails and needs hands on the box.
    body = UPDATE.read_text()
    stuck = [ln for ln in body.splitlines() if "still holding the dongle" in ln]
    assert stuck, "the still-claimed branch should report what to do"
    assert "reboot" in stuck[0].lower()
    assert "re-plug" not in stuck[0].lower()


# --- SDR: the sidecar's compose wiring (SDR_RADIO_PLAN.md, S0b-ii) -------------

COMPOSE = DEPLOY / "docker-compose.yml"


def test_sdr_service_is_profile_gated() -> None:
    # A stock deploy has no radio; the service must never start on one.
    assert "profiles: [sdr]" in COMPOSE.read_text()


def test_sdr_passes_through_the_whole_usb_tree_not_one_node() -> None:
    # devnum increments on every re-plug, so pinning /dev/bus/usb/001/005 breaks the
    # first time the dongle moves. The radio is selected by serial instead.
    # Comments legitimately NAME a pinned node to explain why it is wrong, so look at
    # the mapping lines only.
    mappings = [
        ln
        for ln in COMPOSE.read_text().splitlines()
        if "/dev/bus/usb" in ln and "#" not in ln
    ]
    assert mappings == ["      - /dev/bus/usb:/dev/bus/usb"], mappings


def test_sdr_network_is_egress_free_and_declared() -> None:
    # A receiver has no business reaching the internet; topology, not policy.
    compose = COMPOSE.read_text()
    radio = compose.index("\n  radio:\n")
    assert "internal: true" in compose[radio : radio + 200]


def test_only_the_api_and_the_sdr_join_the_radio_network() -> None:
    compose = COMPOSE.read_text()
    joiners = [ln for ln in compose.splitlines() if "networks:" in ln and "radio" in ln]
    assert len(joiners) == 2, f"unexpected members of the radio network: {joiners}"
    assert any("[radio]" in ln for ln in joiners)
    assert any("edge" in ln for ln in joiners)


def test_the_sdr_image_installs_the_radio_tools() -> None:
    # A sidecar that starts without rtl_fm fails three layers from the cause, so the
    # Dockerfile proves the tool exists at BUILD time.
    dockerfile = (DEPLOY / "Dockerfile.sdr").read_text()
    assert "rtl-sdr" in dockerfile
    assert "command -v rtl_fm" in dockerfile


def _sdr_present(tmp_path: Path, devices: list[tuple[str, str]]) -> int:
    """Run update-inner.sh's sdr_present against a fake /sys/bus/usb/devices tree."""
    body = UPDATE.read_text()
    ids_line = next(ln for ln in body.splitlines() if ln.startswith("RTLSDR_USB_IDS="))
    start = body.index("sdr_present() {")
    fn = body[start : body.index("\n}", start) + 2]

    root = tmp_path / "devices"
    for i, (vendor, product) in enumerate(devices):
        entry = root / f"1-{i}"
        entry.mkdir(parents=True)
        (entry / "idVendor").write_text(vendor + "\n")
        (entry / "idProduct").write_text(product + "\n")

    script = f"{ids_line}\n{fn}\nSDR_USB_ROOT={root} sdr_present\n"
    return subprocess.run(["sh", "-c", script], check=False).returncode


def test_sdr_present_finds_the_nooelec_dongle(tmp_path: Path) -> None:
    # The real box: a root hub, a wireless receiver, and the NESDR SMArt v5.
    assert (
        _sdr_present(tmp_path, [("1d6b", "0002"), ("046d", "c52b"), ("0bda", "2838")])
        == 0
    )


def test_sdr_present_is_false_with_no_radio(tmp_path: Path) -> None:
    assert _sdr_present(tmp_path, [("1d6b", "0002"), ("046d", "c52b")]) != 0


def test_sdr_present_is_false_on_a_box_without_sysfs(tmp_path: Path) -> None:
    body = UPDATE.read_text()
    ids_line = next(ln for ln in body.splitlines() if ln.startswith("RTLSDR_USB_IDS="))
    start = body.index("sdr_present() {")
    fn = body[start : body.index("\n}", start) + 2]
    script = f"{ids_line}\n{fn}\nSDR_USB_ROOT={tmp_path / 'absent'} sdr_present\n"

    assert subprocess.run(["sh", "-c", script], check=False).returncode != 0


def test_the_shell_usb_id_list_matches_the_python_table() -> None:
    # Two copies of the same fact, in languages that cannot import each other. This is
    # the pin that makes drift fail CI instead of silently disabling the radio.
    shell = next(
        ln for ln in UPDATE.read_text().splitlines() if ln.startswith("RTLSDR_USB_IDS=")
    )
    shell_ids = set(shell.split("=", 1)[1].strip('"').split())

    usb_devices = (
        DEPLOY.parent / "supervisor/src/supervisor/usb_devices.py"
    ).read_text()
    python_ids = set(re.findall(r'"([0-9a-f]{4}:[0-9a-f]{4})":', usb_devices))

    assert shell_ids == python_ids, "the updater's id list drifted from KNOWN_SDR_IDS"


def test_the_sdr_profile_is_enabled_by_hardware_not_by_a_flag_the_owner_must_set() -> (
    None
):
    # The owner has no terminal, so "edit .env" is not an instruction they can follow.
    body = UPDATE.read_text()
    assert 'if sdr_present; then\n  SDR_PROFILE="--profile sdr"' in body


def test_the_sdr_profile_reaches_both_the_build_and_the_recreate() -> None:
    # A profile in `build` but not `up -d` builds an image nothing starts.
    body = UPDATE.read_text()
    # Order among profiles is meaningless to compose; what matters is that the radio
    # reaches BOTH commands. A profile in `build` but not `up -d` builds an image
    # nothing starts.
    assert "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE build" in body
    assert "compose $JCODE_PROFILE $TUNNEL_PROFILE $SDR_PROFILE up -d" in body


def test_the_host_cli_activates_the_same_profile() -> None:
    # Host/PWA parity: an SSH `jbrain restart` must not drop the radio.
    jbrain = (DEPLOY / "jbrain").read_text()
    assert "^SDR_ENABLED=true" in jbrain
    assert "--profile sdr" in jbrain
