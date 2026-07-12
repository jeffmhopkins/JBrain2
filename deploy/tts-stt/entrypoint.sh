#!/bin/sh
# Launch both halves of the tts-stt speech service in one container. Piper (TTS) is the
# always-on half read-aloud depends on, so it runs in the FOREGROUND. Whisper (STT, via
# llama-swap) runs in the BACKGROUND and ONLY when its config has been provisioned — so a
# fresh box without the whisper model still serves read-aloud instead of crash-looping (a
# missing config would make llama-swap exit and take the whole container, and piper, down).
#   • piper TTS server on :8801 (the wall forwards /tts* here; the api reaches TTS here)
#   • whisper.cpp STT via llama-swap on :8080 (the api reaches STT here) — when provisioned
set -e

if [ -f /models/llama-swap.yaml ]; then
  /app/llama-swap --config /models/llama-swap.yaml --listen :8080 --watch-config &
else
  echo "[tts-stt] /models/llama-swap.yaml absent — STT not provisioned (run" \
       "'jbrain enable-whisper'); serving read-aloud (TTS) only" >&2
fi

# Prefer the Python 3.12 TTS venv when it's present AND functional — it carries misaki's spaCy
# G2P (natural Kokoro pronunciation) that this image's system Python 3.14 can't install. Fall back
# to system python3 (piper-only, Kokoro-on-espeak) when the venv was skipped or failed to build, so
# read-aloud always runs. The `import piper` probe rejects a half-built venv.
TTS_PY=python3
if [ -x /opt/tts-venv/bin/python ] && /opt/tts-venv/bin/python -c "import piper" 2>/dev/null; then
  TTS_PY=/opt/tts-venv/bin/python
fi
exec "$TTS_PY" /tts/piper_server.py
