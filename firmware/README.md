# Room endpoint firmware — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-09-19

The firmware for the two Waveshare panels, one per twin. Plan:
`../docs/plans/ROOM_ENDPOINT_PLAN.md` (§10 is the bring-up design this implements).

## What this first image is for

**Making every firmware after it arrive over Wi-Fi.** Nothing else.

Both units go to the girls' rooms, so there is no permanent bench unit and no cable where the
device lives. That makes recovery the product rather than a convenience, and it decides what is
in this image and what is deliberately left out.

Not here, on purpose: display, touch, audio, and **PSRAM**. A PSRAM misconfiguration is exactly
the class of fault that ends in a boot loop, and a boot loop ends with a screwdriver. Those
arrive over the air, onto a unit that has already proved it can take an update.

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
| `ca` | PEM of the box's Caddy internal-CA root |
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
(**Ops → Room endpoints**), so a panel authenticates as a device rather than as an owner and
a re-flash revokes the identity the panel had before.

## Building

CI is the authority (`.github/workflows/firmware.yml`, ESP-IDF **v5.5.5** — the version
Waveshare's own examples for this board target). Locally:

```sh
scripts/firmware-setup.sh                      # one-time, ~3.5 GB
. ~/esp-idf/export.sh && (cd firmware && idf.py build)
```

There is no hardware in CI, so what a green run proves is that the image compiles and fits the
partition table. Everything else is a bench question.
