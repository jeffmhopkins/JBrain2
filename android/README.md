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

## The server URL — there isn't one to configure

The app is **universal**: no server URL is baked in. It learns its server from the
**pairing payload** at pairing time (`PairingPayload` — the owner's pairing code is
`base64url({v, u: serverURL, c: code})`), then stores it and uses it for everything
(session mint, the `/dash` WebView, location publishing). One APK works for any
deploy; there is nothing to set per build.

## Sideloading the published APK

On every push to `main` that touches `android/**`, CI builds the debug APK and
publishes it as the rolling **`android-latest`** pre-release, so the latest build is
always at a stable URL:

```
https://github.com/jeffmhopkins/JBrain2/releases/download/android-latest/jbrain360-debug.apk
```

Download it on the phone and install (Android 8.0+; enable "install unknown apps"),
then pair: paste the owner's pairing code (which carries the server) into the app.

The APK is **debug-signed** (fine for personal sideloading, not the Play Store) and
uses standard system-CA TLS, so the server needs a **real cert** (e.g. Let's
Encrypt), not self-signed.
