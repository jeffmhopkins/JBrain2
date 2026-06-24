#!/bin/sh
# DESTRUCTIVE — deletes downloaded LLM weights. Review the four guards before editing.
#
# The companion of deploy/local-models-sync.sh: after the sync drops an uninstalled
# model from LOCAL_MODELS / the gateway (so it has already STOPPED being served — the
# safety requirement is met before this runs), this reclaims its disk by deleting
# <models_dir>/<id>. Invoked from the sync with the FINAL keep set in $KEEP and the
# remove ids on argv.
#
# Contract:
#   KEEP   — env, whitespace-separated FINAL kept ids (post-subtraction). A removal is
#            NEVER deleted if it is still in KEEP (belt-and-suspenders against a bug in
#            the sync's set difference).
#   $1     — the models dir (absolute; the same path the sync passes download-weights).
#   $2..   — the remove ids (or via REMOVE env, whitespace-separated).
#
# For each remove id, `rm -rf` runs ONLY when ALL FOUR guards pass (fail-closed —
# any miss `continue`s without deleting):
#   1. charset: id matches ^[a-z0-9._-]+$  (no slashes, no "..", no spaces) — blocks traversal;
#   2. keep-set: id is NOT in $KEEP                                          — never delete a served model;
#   3. containment: realpath(<dir>/<id>) starts with realpath(<dir>)/        — stays under the models dir;
#   4. type: the resolved target is an existing directory.
# The single `rm -rf` names only a $target that has passed all four.
#
# POSIX sh (runs in the bash-less docker:cli one-shot). Best-effort: a failed delete
# only leaves weights on disk; the model is already unserved.
set -u

say() { printf '\n[prune-llm] %s\n' "$*"; }

MODELS_DIR="${1:?usage: prune-local-weights.sh <models_dir> [remove-ids...]}"
shift || true
# Remove ids: argv if given, else the REMOVE env (so both call shapes work).
REMOVE="${*:-${REMOVE:-}}"
KEEP="${KEEP:-}"

[ -n "$REMOVE" ] || { say "nothing to prune"; exit 0; }

# Absolute, symlink-resolved models dir — the prefix every delete must live under.
dir_abs="$(realpath "$MODELS_DIR" 2>/dev/null || true)"
[ -n "$dir_abs" ] && [ -d "$dir_abs" ] || { say "models dir missing — skipping prune"; exit 0; }

for id in $REMOVE; do
  # Guard 1 — charset: reject anything but lowercase catalog-id chars (blocks "..",
  # "/", whitespace, leading dashes resolving to flags, etc).
  case "$id" in
    *[!a-z0-9._-]*) say "skip (bad charset): $id"; continue ;;
    "")             continue ;;
  esac

  # Guard 2 — keep set: never delete a model still in the final served roster.
  skip=''
  for k in $KEEP; do
    [ "$k" = "$id" ] && { skip=1; break; }
  done
  [ -n "$skip" ] && { say "skip (still kept): $id"; continue; }

  target="$dir_abs/$id"

  # Guard 3 — containment: the resolved target must sit directly under the models
  # dir (its parent equals dir_abs, and its realpath is dir_abs + "/" + a single
  # component). Rejects a symlinked-out target or any path that escapes the dir.
  target_abs="$(realpath "$target" 2>/dev/null || true)"
  case "$target_abs" in
    "$dir_abs"/*) : ;;
    *)            say "skip (outside models dir): $id"; continue ;;
  esac
  [ "$(dirname "$target_abs")" = "$dir_abs" ] || { say "skip (nested path): $id"; continue; }

  # Guard 4 — type: only an existing directory is a weights dir to delete.
  [ -d "$target_abs" ] || { say "skip (not a directory): $id"; continue; }

  say "pruning weights: $id"
  rm -rf -- "$target_abs"
done
