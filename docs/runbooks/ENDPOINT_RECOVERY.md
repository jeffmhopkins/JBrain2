# Recovering a room-endpoint panel

> **Status:** Living · **Last verified:** 2026-09-19

What to do when a panel stops working, in the order to try it. Plan:
`../plans/ROOM_ENDPOINT_PLAN.md`; firmware: `../../firmware/README.md`.

## The short version: it cannot be bricked

The ESP32-S3's **ROM bootloader is mask ROM — physically unwritable**. Holding **BOOT**
while resetting pulls GPIO0 low and drops the chip into ROM download mode over the same
USB-C port, *whatever* is in flash. No application firmware can prevent that, and nothing
in this project's flashing path burns eFuses (`esptool write_flash` does not), so the one
thing that could disable it — `DIS_DOWNLOAD_MODE` — is never touched.

**The worst realistic outcome is "this panel needs the cable again", never "this panel is
dead."** That is worth knowing before flashing anything, because it changes how much
caution the first attempt deserves: the answer is "some, but not paralysis."

## The ladder

Try these in order. The first two need nobody to touch the panel, which is the point —
both units end up in bedrooms.

### 1. OTA rollback — automatic

A freshly OTA'd image boots in `PENDING_VERIFY`. It stays that way until it has joined
Wi-Fi **and** fetched `GET /api/endpoint/firmware`; only then does it mark itself good. So:

- **New image crashes.** The reset that follows is enough — the bootloader sees an
  unconfirmed image and reverts to the previous slot.
- **New image runs but cannot reach the box.** It gives up its own probation after five
  tries over ~100 s and reboots into the previous slot deliberately.

The bar is "can I still be updated", not "do I look healthy", because the only
unrecoverable state is one an OTA cannot reach.

### 2. The factory app — automatic

If both OTA slots are unbootable, invalid otadata falls through to the `factory`
partition, which **OTA never writes**. Whatever was last flashed over USB is still there.

> **Caveat worth knowing (2026-09-19, still true 2026-09-21).** `factory` holds the *same*
> image as the application. `deploy/endpoint` writes the app to the factory partition on a USB
> flash, so rung 2 is a second copy of whatever is broken rather than a safety net. Closing it
> means the flasher writing a **recovery** image to `factory` and the application to `ota_0` as
> two separate artifacts. Rung 3 is unaffected and always available.
>
> The day the caveat anticipated — "a grown image (display, audio, wake word) flashed over
> USB" — arrived in 0.2.37, when linking ESP-SR took the app from 1.16 MB to 3.05 MB. That did
> not erode rung 2 further, but it did not fit `factory` at all, so the app partitions were
> resized to 3.5 MB each (`firmware/partitions.csv`).
>
> **A resize means every panel needs one more USB flash, and `otadata` is now part of it.**
> Without writing `otadata` a panel keeps booting whichever OTA slot it was last updated into
> — a slot that is at a *different offset* under the new table and holds the middle of an old
> image. `ota_data_initial.bin` is in `firmware/dist/` and the flasher writes it at `0xf000`,
> so a flash from the PWA resets a panel to boot `factory`. Nothing here needs a terminal.

### 3. ROM download mode — manual, and always available

When the panel will not boot at all, or rung 2 has been eroded per the caveat above:

1. Unplug the panel.
2. **Hold BOOT.** Keep holding it.
3. Plug the USB-C cable into the box.
4. Release BOOT after a second or two.

The chip is now in ROM download mode and is not running any of our code. Then:
**Endpoints → Rescan USB** — it enumerates the same way — pick it, and flash with
**Erase first (recovery)** ticked. That erase clears NVS too, so the unit comes back
unprovisioned and is re-provisioned by the same flash.

Normally none of this is needed: the board has an auto-download circuit and the flasher
drives it with `--before default_reset`, so an ordinary reflash does not require the
button. BOOT is for when the running firmware is wedged badly enough that the auto-reset
does not take.

## When the panel does not appear in Rescan USB

Two different faults, and the card distinguishes them:

- **"Nothing on USB"** — the flasher container is running and sees no serial device. Either
  nothing is plugged in, the cable is charge-only, or the panel is not enumerating.
- **"No panel flasher on this box"** — `JBRAIN_ENDPOINT_URL` is blank. A configuration
  answer, not a hardware one.

If a panel is plugged in with a known-good data cable and the first message persists, the
suspect is the container's view of the device rather than the panel: `/dev/ttyACM*` is a
kernel CDC-ACM tty admitted by `/dev` plus `device_cgroup_rules` in `docker-compose.yml`,
which is a different mechanism from the `/dev/bus/usb` mapping the `sdr` sidecar uses.

## What is not recoverable

**NVS erasure loses the unit's provisioning** — Wi-Fi credentials, device token, the box's
CA root — so the panel needs a USB flash to get them back. This is the one failure mode
that genuinely requires the cable, and it is why `nvs_flash_init` treats an erase as
something to log loudly rather than do quietly.

A re-flash also **mints a new device key and revokes the old one**. That is deliberate:
re-flashing is how a unit is handed over or recovered, and the credential it used to hold
should stop working at that moment.
