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
