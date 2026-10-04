#!/bin/sh
# Nightly (and pre-update) backup: schema+data dump plus blob volume archive.
# Restore with restore.sh (jbrain restore <stamp>) — drilled end-to-end; keep
# both sides in step when adding volumes or moving data outside Postgres.
#
# POSIX sh (not bash): the import/reset one-shots call this from inside the
# bash-less docker:cli (Alpine) container, so a `#!/usr/bin/env bash` shebang
# fails there with "env: can't execute 'bash'". Busybox date/find/stat must do,
# which is why the date maths goes through `date -d @EPOCH` only.
#
# Retention is by stamp, not mtime: every update runs this, so a day of updates
# used to keep a full ~1 GB blob tarball per run for 14 days (118 GB on the box,
# 2026-10-04). Now: every backup from the last 48 h, then the newest per calendar
# day back KEEP_DAYS days. And a blob tarball is only written when the volume
# changed since the newest one, so each dump records the tarball it pairs with in
# `jbrain-<stamp>.blobs` (its own, or the reused one). restore.sh reads that; only a
# dump from before the sidecar existed falls back to "newest tarball at or before",
# and pruning never drops a tarball a kept dump pairs with.
#
# Stamps are UTC (`date -u`): update/import/reset run this in the UTC docker:cli
# container and the nightly cron on the host, and local-time stamps from the two
# did not order by real time.
#
# A backup is the dump AND its blobs or nothing: both are written to dot-prefixed
# .tmp names and renamed only once every step succeeded, so a failed pg_dump or tar
# never leaves a final-named file that would read as the day's newest backup and
# get the good one pruned.
set -eu

KEEP_DAYS="${KEEP_DAYS:-14}"
RECENT_S=172800

# Epoch seconds to a 14-digit stamp number (YYYYmmddHHMMSS), in UTC like the file
# names, so the two compare directly.
_stamp_at() { date -u -d "@$1" +%Y%m%d%H%M%S; }

# A file's stamp (YYYYmmdd-HHMMSS) as one 14-digit string.
_stamp_num() { printf '%s%s' "${1%%-*}" "${1#*-}"; }

# A >= B for 14-digit stamps, compared as day then time: each half fits 32 bits,
# so no shell's `test` integer width matters.
_ge() {
  [ "${1%??????}" -gt "${2%??????}" ] && return 0
  [ "${1%??????}" -eq "${2%??????}" ] && [ "${1#????????}" -ge "${2#????????}" ]
}

# Our own names only, newest first. Names never carry spaces (we wrote them).
_stamps() { # DIR PREFIX SUFFIX
  ls -1 "$1" 2>/dev/null \
    | grep -E "^$2[0-9]{8}-[0-9]{6}$3\$" \
    | sed "s/^$2//; s/$3\$//" \
    | sort -r || true
}

# Retention verdict for one stamp, walked newest first. Uses _recent, _floor and
# _seen from the caller: a day is "seen" once its newest backup is kept, so an
# older one from the same day goes.
_keep_stamp() {
  _n="$(_stamp_num "$1")"
  _d="${1%%-*}"
  if _ge "$_n" "$_recent"; then
    _seen="$_seen $_d"
    return 0
  fi
  if [ "$_d" -lt "$_floor" ]; then
    return 1
  fi
  case " $_seen " in *" $_d "*) return 1 ;; esac
  _seen="$_seen $_d"
  return 0
}

# The blob tarball a dump pairs with: the stamp its sidecar records, else (a dump
# from before sidecars) the newest tarball at or before it — restore.sh's rule.
# Prints nothing and fails when there is none, or the sidecar says "none".
blob_for_stamp() { # DIR STAMP
  if [ -f "$1/jbrain-$2.blobs" ]; then
    _p="$(cat "$1/jbrain-$2.blobs")"
    case "$_p" in
      [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9])
        printf '%s\n' "$_p"
        return 0
        ;;
    esac
    return 1
  fi
  _want="$(_stamp_num "$2")"
  for _b in $(_stamps "$1" 'blobs-' '\.tar\.gz'); do
    if _ge "$_want" "$(_stamp_num "$_b")"; then
      printf '%s\n' "$_b"
      return 0
    fi
  done
  return 1
}

prune_backups() { # DIR NOW_EPOCH KEEP_DAYS
  _recent="$(_stamp_at $(($2 - RECENT_S)))"
  _floor="$(_stamp_at $(($2 - $3 * 86400)))"
  _floor="${_floor%??????}"

  # A run that died mid-write leaves its .tmp; a day old, nothing is still writing it.
  find "$1" -maxdepth 1 -type f -name '.*.tmp' -mtime +0 -exec rm -f {} + || true

  _seen=""
  _kept=""
  for _s in $(_stamps "$1" 'jbrain-' '\.dump'); do
    if _keep_stamp "$_s"; then
      _kept="$_kept $_s"
    else
      rm -f "$1/jbrain-$_s.dump" "$1/jbrain-$_s.blobs"
      echo "pruned jbrain-$_s.dump"
    fi
  done
  # A sidecar is written just before its dump is renamed into place; one whose dump
  # never arrived is a dead run's, gone once it leaves the 48 h window.
  for _s in $(_stamps "$1" 'jbrain-' '\.blobs'); do
    if [ ! -f "$1/jbrain-$_s.dump" ] && ! _ge "$(_stamp_num "$_s")" "$_recent"; then
      rm -f "$1/jbrain-$_s.blobs"
    fi
  done

  # A tarball some kept dump restores with stays whatever its own day says —
  # skipped writes mean a dump's blobs can be days older than the dump.
  _needed=""
  for _s in $_kept; do
    _b="$(blob_for_stamp "$1" "$_s")" && _needed="$_needed $_b"
  done

  _seen=""
  for _s in $(_stamps "$1" 'blobs-' '\.tar\.gz'); do
    if _keep_stamp "$_s"; then
      continue
    fi
    case " $_needed " in *" $_s "*) continue ;; esac
    rm -f "$1/blobs-$_s.tar.gz" "$1/blobs-$_s.sha"
    echo "pruned blobs-$_s.tar.gz"
  done
}

# Prints the newest tarball's stamp when its recorded fingerprint equals FP — the
# volume is unchanged since, so another archive would be a byte-identical copy. A
# tarball with no .sha (written before this check existed) never matches.
blobs_unchanged_since() { # DIR FP
  [ -n "$2" ] || return 1
  _last="$(_stamps "$1" 'blobs-' '\.tar\.gz' | head -n 1)"
  [ -n "$_last" ] && [ -f "$1/blobs-$_last.sha" ] || return 1
  [ "$(cat "$1/blobs-$_last.sha")" = "$2" ] || return 1
  printf '%s\n' "$_last"
}

# Sourced by the tests for the functions above; nothing below runs.
if [ "${BACKUP_LIB_ONLY:-}" = 1 ]; then
  return 0 2>/dev/null || exit 0
fi

cd "${JBRAIN_DIR:-/opt/jbrain2}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DUMP_TMP="backups/.jbrain-$STAMP.dump.tmp"
BLOB_TMP="backups/.blobs-$STAMP.tar.gz.tmp"
# sha256 of no input: what a find that listed nothing (or failed mid-pipe) hashes to.
EMPTY_SHA=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

mkdir -p backups
trap 'rm -f "$DUMP_TMP" "$BLOB_TMP"' EXIT

# Before writing, too: on a full disk, this is what makes room for the dump.
prune_backups backups "$(date +%s)" "$KEEP_DAYS"

docker compose exec -T db pg_dump -U jbrain -Fc jbrain > "$DUMP_TMP"

# Blob volume lands in Phase 1; archive it once it exists. "none" records a dump
# with no blob volume, which restore leaves alone.
PAIRED=none
if docker volume inspect jbrain_blobs >/dev/null 2>&1; then
  # Path, size and mtime of every file: cheap next to a 1 GB tar, and any add,
  # delete or rewrite changes it. Taken BEFORE the tar, so a write racing the
  # archive leaves a stale fingerprint and the next run archives again, never the
  # reverse. A failed or empty fingerprint always archives.
  FP="$(docker run --rm -v jbrain_blobs:/blobs:ro alpine sh -c \
    "set -o pipefail; cd /blobs && find . -type f -exec stat -c '%n %s %Y' {} + | sort | sha256sum")" \
    || FP=""
  FP="${FP%% *}"
  [ "$FP" != "$EMPTY_SHA" ] || FP=""
  if SAME="$(blobs_unchanged_since backups "$FP")"; then
    echo "blobs unchanged since $SAME, skipped"
    PAIRED="$SAME"
  else
    docker run --rm -v jbrain_blobs:/blobs:ro -v "$PWD/backups:/out" alpine \
      tar czf "/out/.blobs-$STAMP.tar.gz.tmp" -C /blobs .
    mv "$BLOB_TMP" "backups/blobs-$STAMP.tar.gz"
    # Written only once the archive is whole, so a failed tar is never "unchanged".
    [ -z "$FP" ] || printf '%s\n' "$FP" > "backups/blobs-$STAMP.sha"
    PAIRED="$STAMP"
  fi
fi

# Sidecar first, then the dump: a dump never exists without its pairing.
printf '%s\n' "$PAIRED" > "backups/jbrain-$STAMP.blobs"
mv "$DUMP_TMP" "backups/jbrain-$STAMP.dump"

prune_backups backups "$(date +%s)" "$KEEP_DAYS"

echo "backup complete: $STAMP (blobs: $PAIRED)"
