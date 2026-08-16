"""Deploy scripts reachable from a one-shot must be POSIX sh.

The export/import/reset/update one-shots run in the bash-less docker:cli
(Alpine) container (gateway.UPDATER_IMAGE). A script with a
`#!/usr/bin/env bash` shebang dies there with "env: can't execute 'bash'" —
exactly how the reset safety-backup failed when backup.sh was still bash. So
every script the one-shots invoke (the *-inner.sh entrypoints and backup.sh,
which import/reset call by path) must declare `#!/bin/sh` and parse under a
POSIX shell. Host-only scripts (restore.sh, install.sh) may stay bash.
"""

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
    build = idx("compose $JCODE_PROFILE $TUNNEL_PROFILE build")
    up = idx("compose $JCODE_PROFILE $TUNNEL_PROFILE up -d")
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
    # The rollback rebuild is a PLAIN build — no --pull and no LOCAL_LLM_BASE override —
    # so it recreates the gateway on the reproducible pinned base from cached layers.
    rollback = next(
        (
            i
            for i, ln in enumerate(lines)
            if "build local-llm" in ln
            and "--pull" not in ln
            and not ln.lstrip().startswith("#")
        ),
        None,
    )
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
    # Match the invocation, not a comment that merely mentions the module.
    needle = "python -m jbrain.llm.llama_swap_config"
    for script in scripts:
        cmd = next(ln for ln in _logical_lines(script.read_text()) if needle in ln)
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
    build = idx("docker compose $JCODE_PROFILE $TUNNEL_PROFILE build")

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
    """Index of the bare CALL of a shell function — not its definition, not the trap."""
    return next((i for i, ln in enumerate(lines) if ln.strip() == name), None)


def test_update_quiesces_the_stack_around_the_build_and_the_model_load() -> None:
    lines = _update_lines()

    quiesce = _call_idx(lines, "quiesce_stack")
    build = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE build")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    up = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE up -d")
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


def test_the_page_cache_is_dropped_before_the_model_load() -> None:
    """MemAvailable counts reclaimable page cache as free, and reclaiming it under
    pressure is exactly what livelocks this hardware. After a build that just wrote tens
    of GB of layers, the number reads fine and cannot be realised in time — so the gate
    would be measuring optimism. Dropping first makes the reading honest."""
    lines = _update_lines()

    drop = _cmd_idx(lines, "drop_page_cache")
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    assert drop is not None, "the update must drop the page cache before a load"
    assert smoke is not None and drop <= smoke, "drop the cache BEFORE the load"
    assert "drop_caches" in "\n".join(lines)


def test_the_gateway_is_emptied_again_before_the_stack_is_recreated() -> None:
    """The tool probe loads gpt-oss (~60 GB of pinned unified memory). Recreating a
    dozen containers on top of that is the same collision the pre-build unload
    prevents, just arriving from the other end."""
    lines = _update_lines()

    unloads = [i for i, ln in enumerate(lines) if "jbrain.cli local-llm-unload" in ln]
    smoke = _cmd_idx(lines, "jbrain.cli local-llm-smoketest")
    up = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE up -d")

    assert len(unloads) >= 2, "unload before the build AND after the smoke test"
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
    build = _cmd_idx(lines, "compose $JCODE_PROFILE $TUNNEL_PROFILE build")
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
