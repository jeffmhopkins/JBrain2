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

exec python3 /tts/piper_server.py
