# JBrain360 Android app

The forked tracker's host app (Phase 7 M5). It holds the device key in the Android
Keystore, exchanges it for the session cookie natively (`/session/mint`), and hosts
the member dashboard SPA (served at `/dash`) in a locked-down WebView.

## Status

- **M5a** — the WebView shell: a locked-down `WebView` (`DashboardActivity`) that
  loads the configured server's `/dash`, plus a unit-tested URL helper
  (`DashboardConfig`). Builds to a debug APK; JVM unit tests run headless.
- **M5b** — native Keystore key-gen + `/session/mint` → inject the session cookie.
- **M5c** — the pairing screen (redeem a pairing code → provision the device).
- **M5d** — WebView lockdown hardening + the OwnTracks location-publishing engine.

## Build & test

Requires JDK 17+ and the Android SDK (platform-35, build-tools, platform-tools).
Provision the SDK once with `./setup-android-sdk.sh` (installs to `~/android-sdk`),
then:

```sh
ANDROID_HOME=~/android-sdk ./gradlew assembleDebug testDebugUnitTest
```

On-device instrumented tests need an emulator/device (KVM); the JVM unit suite and
the debug build run anywhere, which is what CI gates.

## The server URL

The dashboard's base URL is baked in at build time (the app loads `<base>/dash` and
requires `https`). Set it with a Gradle property — no source edit:

```sh
./gradlew assembleDebug -PdashboardBase=https://your-server
```

Unset, it falls back to a placeholder (so plain `assembleDebug`/tests still compile).

## Sideloading the published APK

On every push to `main` that touches `android/**`, CI builds a debug APK with the
deployment URL baked in and publishes it as the rolling **`android-latest`**
pre-release, so the latest build is always at a stable URL:

```
https://github.com/jeffmhopkins/JBrain2/releases/download/android-latest/jbrain360-debug.apk
```

Download it on the phone and install (Android 8.0+; enable "install unknown apps").
Two things make it usable:

- Set a repo **variable** `DASHBOARD_BASE` (Settings → Secrets and variables →
  Actions → Variables) to your `https://` server. Until it's set, the published APK
  points at the placeholder.
- The APK is **debug-signed** (fine for personal sideloading, not the Play Store)
  and uses standard system-CA TLS, so the server needs a **real cert** (e.g. Let's
  Encrypt), not self-signed.
