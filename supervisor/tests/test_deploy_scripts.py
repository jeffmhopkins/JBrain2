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
DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES = ["install.sh", "update-inner.sh", "jbrain"]


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
JCODE_TURNKEY_SCRIPTS = ["update-inner.sh", "jbrain"]


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
    # bring it back after the stack is up. Gated on hosting so a stock stack is untouched.
    lines = (DEPLOY / "update-inner.sh").read_text().splitlines()
    text = "\n".join(lines)
    stop = next((i for i, ln in enumerate(lines) if "stop local-llm" in ln), None)
    build = next((i for i, ln in enumerate(lines) if "compose $JCODE_PROFILE build" in ln), None)
    up = next((i for i, ln in enumerate(lines) if "compose $JCODE_PROFILE up -d" in ln), None)
    restart = next((i for i, ln in enumerate(lines) if "up -d local-llm" in ln), None)
    assert "LOCAL_LLM_ENABLED=true" in text, (
        "the gateway stop/restart must be gated on LOCAL_LLM_ENABLED so a stock "
        "cloud stack (no local-llm) is never touched"
    )
    assert stop is not None, "update must stop the local-llm gateway to free memory"
    assert build is not None and up is not None
    assert stop < build, "the gateway must be stopped before the rebuild/recreate"
    assert restart is not None, "update must bring the gateway back after the stack is up"
    assert restart > up, "the gateway restart must follow the stack `up -d`"


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
