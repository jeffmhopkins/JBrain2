#!/usr/bin/env bash
# Provisions the Android SDK for building the JBrain360 app (android/) locally.
# Heavy (~1 GB of downloads) and never needed by a web/CI container, so it is NOT
# part of scripts/dev-setup.sh's auto-bootstrap — it is invoked on demand and
# mirrors the toolchain the CI `android` job sets up. Idempotent.
set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
CMDLINE_VER="11076708"
log() { printf '[android-setup] %s\n' "$*"; }

if [ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
    log "installing command-line tools into $ANDROID_HOME"
    tmp="$(mktemp -d)"
    curl -sSL -o "$tmp/cmdline-tools.zip" \
        "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_VER}_latest.zip"
    mkdir -p "$ANDROID_HOME/cmdline-tools"
    unzip -q "$tmp/cmdline-tools.zip" -d "$ANDROID_HOME/cmdline-tools"
    mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm -rf "$tmp"
fi

export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
log "accepting licenses + installing platform-35 / build-tools / platform-tools"
yes | sdkmanager --licenses >/dev/null
sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0" >/dev/null

log "done. Build with:"
log "  cd android && ANDROID_HOME=$ANDROID_HOME ./gradlew assembleDebug testDebugUnitTest"
