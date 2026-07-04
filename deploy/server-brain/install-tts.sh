#!/usr/bin/env bash
# Install piper + the wall display's default voice models for server-side read-aloud
# (see README "Read aloud"). Idempotent: re-running only fetches what's missing.
#
#   bash deploy/server-brain/install-tts.sh                 # piper + voices (run-on-host)
#   bash deploy/server-brain/install-tts.sh --voices-only   # just the models (piper baked in the image)
#
# Voices land in ./voices (or $BRAIN_PIPER_VOICES_DIR). Joe reads prompts, Amy reads
# answers by default; the picker lists every model you drop here, so add more freely.
set -euo pipefail

VOICES_ONLY=0
[ "${1:-}" = "--voices-only" ] && VOICES_ONLY=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOICES_DIR="${BRAIN_PIPER_VOICES_DIR:-$HERE/voices}"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US"
MODELS=(en_US-amy-medium en_US-joe-medium)   # answer, prompt — add more names here to preinstall

log() { printf '[tts-setup] %s\n' "$*"; }

# --- piper (skipped with --voices-only: the compose image bakes it in) ------
if [ "$VOICES_ONLY" = 1 ]; then
  log "voices-only: skipping piper install (baked into the server-brain image)"
elif command -v piper >/dev/null 2>&1; then
  log "piper already on PATH"
elif command -v pipx >/dev/null 2>&1; then
  log "installing piper via pipx"; pipx install piper-tts
else
  log "installing piper via pip --user (consider pipx)"; python3 -m pip install --user piper-tts
fi

# --- voice models -----------------------------------------------------------
mkdir -p "$VOICES_DIR"
fetch() { # fetch <url> <dest>
  if [ -s "$2" ]; then log "have $(basename "$2")"; return; fi
  log "downloading $(basename "$2")"
  curl -fL --retry 3 --retry-delay 2 -o "$2.part" "$1"
  mv "$2.part" "$2"
}
for m in "${MODELS[@]}"; do
  voice="${m#en_US-}"; voice="${voice%-medium}"          # en_US-amy-medium -> amy
  fetch "$BASE/$voice/medium/$m.onnx"      "$VOICES_DIR/$m.onnx"
  fetch "$BASE/$voice/medium/$m.onnx.json" "$VOICES_DIR/$m.onnx.json"
done

log "done — voices in $VOICES_DIR:"
ls -1 "$VOICES_DIR"/*.onnx 2>/dev/null | sed 's#.*/##;s/\.onnx$//' | sed 's/^/  /'
if [ "$VOICES_ONLY" != 1 ]; then
  command -v piper >/dev/null 2>&1 && piper --version 2>/dev/null \
    || log "note: 'piper' not on PATH yet — open a new shell or add ~/.local/bin"
fi
