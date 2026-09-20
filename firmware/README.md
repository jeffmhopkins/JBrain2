# Room endpoint firmware — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-09-20

The firmware for the two Waveshare panels, one per twin. Plan:
`../docs/plans/ROOM_ENDPOINT_PLAN.md` (§10 is the bring-up design this implements).

## What this first image is for

**Making every firmware after it arrive over Wi-Fi.** Nothing else.

Both units go to the girls' rooms, so there is no permanent bench unit and no cable where the
device lives. That makes recovery the product rather than a convenience, and it decides what is
in this image and what is deliberately left out.

Kept out of the first image on purpose: display, touch and audio. A misconfiguration in any of
them is the class of fault that ends in a boot loop, and a boot loop ends with a screwdriver.
They arrive over the air, onto a unit that has already proved it can take an update — and they
have: PSRAM in 0.2.3, the display in 0.2.4, the face and touch in 0.2.6, the speaker in 0.2.7, a moving idle in 0.2.9.
Every one of those carried a byte-identical `bootloader.bin`, so each was a pure app OTA whose
rollback lands on the same bootloader. **Check that before shipping a release**, not after.

**PSRAM arrived that way in 0.2.3**, which is the first time that sentence was cashed rather
than written. Two things made it safe to try: only the app image changes (PSRAM is brought up
by the app, not the bootloader, so an OTA can carry it and a rollback lands on the same
bootloader), and `CONFIG_SPIRAM_IGNORE_NOTFOUND` degrades a wrong mode to "came up without
PSRAM" instead of panicking in early boot. That second one is also why the boot log reports
the size explicitly: the rollback gate cannot catch a panel that boots, reaches the box and
marks itself good while being 8 MB short of what the display needs.

## The panel does not hold a still image, and the reason is still open

Drawn once it goes dark within minutes, and **redrawing an identical frame every 500 ms does
not prevent it** (0.2.7). A tap — whose only distinction is that it changes the picture —
brings it straight back.

This was briefly blamed on the debug console, which really could strand a panel in the ROM
bootloader and really is fixed (ROOM_ENDPOINT_PLAN.md §10.4s). But the panel blanks with
nobody on its serial port, so that was a second bug sitting on top of this one, not this one
(§10.4t). Beware the reading trap: when a console read "brings the robot back", that is the
reset it performs, not a cure.

**Working rule, under test in 0.2.9: consecutive frames must differ.** The idle is a slow bob
for exactly that reason, and it is load-bearing rather than decoration. Whatever W4's rig
becomes, its idle path must keep changing the picture — a dimmed *still* face is a face that
vanishes, so quiet hours dims with a `0x51` write and never freezes the frame.

If 0.2.9 still blanks, the next instrument is the CO5300's power-mode register (`0x0A`) read
while the screen is dark: it says directly whether the controller still thinks the display is
on.

## The two things that make "cable once" true

Neither can be added later. The image that lacks them is precisely the one that strands a unit.

1. **Rollback, gated on reaching the box.** A freshly OTA'd image boots in `PENDING_VERIFY` and
   the bootloader reverts it on the next reset unless it marks itself good. The bar here is not
   "the app works" but **"the box is still reachable for the next update"** — because the only
   unrecoverable state is one an OTA cannot reach. An image that renders nothing but can still
   be updated is a bad afternoon; an image that looks perfect and cannot be updated is a trip
   with a screwdriver. The probation window is five tries over ~100 s, so the box restarting
   mid-round does not cost a good image.

2. **A frozen factory app.** `factory` is never OTA'd, and invalid OTA data falls back to it.

## The partition table is permanent

OTA rewrites an app slot and nothing else, so changing `partitions.csv` means a USB reflash.
It is therefore laid out for what the endpoint will eventually be — including a 3.5 MB `model`
partition reserved for ESP-SR wake-word models that nothing uses yet — and the space is
reserved now, while reserving it is free. Ends exactly at 16 MB.

## Generic image, personalised at flash time

The artifact CI publishes carries **no credentials**: no Wi-Fi password, no device token, no
certificate. It is byte-identical for both units and safe to publish.

Per-unit configuration is an NVS blob the box writes at flash time, in the `jbrain` namespace:

| Key | Meaning |
|---|---|
| `ssid` / `pass` | the house network. **2.4 GHz only** — this radio has no 5 GHz. |
| `api` | base URL, e.g. `https://jbrain.local/api`, no trailing slash |
| `token` | the device credential, sent as `Authorization: Bearer …` |
| `ca` | PEM of the box's Caddy internal-CA root. **Optional** — present only when the panel is pointed at the box's LAN name, whose certificate that root signs. Absent means the box was reached at a public hostname, and the firmware validates against the compiled-in public-CA bundle instead. Never both. |
| `name` | which twin's endpoint this is (optional) |

An OTA rewrites only the app slot, so all of it survives every update. The one path that
destroys it is a corrupt NVS partition, which forces an erase — and that is the single
failure mode that still needs the cable.

## The server contract

One endpoint, served by the box at `backend/src/jbrain/api/endpoint.py`:

```
GET {api}/endpoint/firmware        Authorization: Bearer <device token>
→ 200 {"version": "0.1.0", "url": "https://jbrain.local/api/endpoint/firmware/bin"}
```

Reaching it is *also* the health signal the rollback gate turns on, which is why there is no
separate `/healthz` probe: the thing that proves a unit is recoverable is exactly the thing
that recovers it. `version` is compared verbatim against `firmware/version.txt` as built, so
**bumping that file is what makes a unit update.**

The bearer token is the unit's own `device_key`, minted and written into NVS by the flasher
(the PWA's **Endpoints** screen), so a panel authenticates as a device rather than as an owner and
a re-flash revokes the identity the panel had before.

## How it reaches the box

**The built images are committed, in `dist/`, and that is the whole distribution.** The box
already pulls this entire repository from `main` on every Ops → Update (`deploy/update-inner.sh`
does a `git fetch` + `reset --hard`) and builds its own images out of that tree, so the api
mounts `firmware/` read-only and flashes what it finds there. No release, no CDN, no
credential, no network at all at flash time, and nothing for the owner to press first.

This replaced a GitHub release. The release worked in principle and failed in practice: it
put api.github.com, github.com and a signed CDN host between a board plugged into the box's
own USB socket and the button next to it, and the first real flash died on a DNS lookup
inside that chain with nothing to show for it but `Request failed: 500`.

So changing the firmware is three commits' worth of one act:

```sh
# 1. edit main/, 2. bump version.txt, 3. rebuild and commit dist/
. ~/esp-idf/export.sh && (cd firmware && idf.py fullclean && idf.py build)
scripts/firmware-dist.sh
```

**`fullclean` is not belt-and-braces.** An incremental build can carry a component that was
added to `main/CMakeLists.txt`'s `REQUIRES` and then removed again — the link order keeps the
ghost, the binary differs from what the committed source produces, and the only thing that
says so is CI failing this check. That cost a cycle on 0.2.8.

`firmware.yml` rebuilds on CI and **fails the PR if `dist/` is not byte-for-byte what the
source produces** — `CONFIG_APP_REPRODUCIBLE_BUILD=y` is what makes that check possible at
all, since ESP-IDF otherwise stamps the build date and absolute paths into every image.

Bumping `version.txt` is still the single act that makes a flashed panel update itself, since
the running image compares that same string against the manifest.

## Building

CI is the authority (`.github/workflows/firmware.yml`, ESP-IDF **v5.5.5** — the version
Waveshare's own examples for this board target). Locally:

```sh
scripts/firmware-setup.sh                      # one-time, ~3.5 GB
. ~/esp-idf/export.sh && (cd firmware && idf.py build)
scripts/firmware-dist.sh                       # copy the built set into dist/ — it ships
```

There is no hardware in CI, so what a green run proves is that the image compiles and fits the
partition table. Everything else is a bench question.
