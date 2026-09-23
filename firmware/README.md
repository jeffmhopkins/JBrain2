# Room endpoint firmware — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-09-22

The firmware for the two Waveshare panels, one per twin. Plan:
`../docs/plans/ROOM_ENDPOINT_PLAN.md` (§10 is the bring-up design this implements).
The printable deep back cover that makes room for a battery is
`../hardware/amoled18-back-plate/`.

## What this first image is for

**Making every firmware after it arrive over Wi-Fi.** Nothing else.

Both units go to the girls' rooms, so there is no permanent bench unit and no cable where the
device lives. That makes recovery the product rather than a convenience, and it decides what is
in this image and what is deliberately left out.

Kept out of the first image on purpose: display, touch and audio. A misconfiguration in any of
them is the class of fault that ends in a boot loop, and a boot loop ends with a screwdriver.
They arrive over the air, onto a unit that has already proved it can take an update — and they
have: PSRAM in 0.2.3, the display in 0.2.4, the face and touch in 0.2.6, the speaker in 0.2.7, a moving idle in 0.2.9, the version on the glass in 0.2.11.
Every one of those carried a byte-identical `bootloader.bin`, so each was a pure app OTA whose
rollback lands on the same bootloader. **Check that before shipping a release**, not after.

**PSRAM arrived that way in 0.2.3**, which is the first time that sentence was cashed rather
than written. Two things made it safe to try: only the app image changes (PSRAM is brought up
by the app, not the bootloader, so an OTA can carry it and a rollback lands on the same
bootloader), and `CONFIG_SPIRAM_IGNORE_NOTFOUND` degrades a wrong mode to "came up without
PSRAM" instead of panicking in early boot. That second one is also why the boot log reports
the size explicitly: the rollback gate cannot catch a panel that boots, reaches the box and
marks itself good while being 8 MB short of what the display needs.

## Two faults, not one: a panic (fixed) and a black screen (open)

**A dark panel beeps when you tap it.** On 0.2.27, with no panic recorded, the render task was
alive the whole time — polling touch, playing a tone, blitting a frame every 40 ms, re-asserting
display-on and brightness every thirty seconds — into a screen that stayed off. So the panic and
the black screen were never the same bug, and the section below (kept for the panic, which is
real and fixed) is wrong where it says otherwise. See ROOM_ENDPOINT_PLAN.md §10.4am.

**Do not trust `pmu_history: []`.** Until 0.2.28 it could not be anything else: the RTC ring's
validity magic was written only by `pmu_history_clear()`, which `main.c` calls only when a
report already carried samples — which needs the magic. A power cycle randomised it and the
ring became permanently unreadable while still being written. `pmu_report_history()` now copies
the survivors and arms the ring unconditionally at every boot, and samples the **TCA9554 at
0x20** alongside the AXP2101, because the expander is the one chip on this bus nobody has ever
read and the BSP brings the panel's reset and enable lines out on it.

## The panic (fixed in 0.2.26 and 0.2.27)

The panel's own telemetry carried `reset_reason: "panic"`. It crashes, and a panic is a **soft
reset** — which leaves the screen dark where a power cycle does not. Crash, reboot, black until
someone pulls the plug. Every symptom chased from 0.2.4 onward follows from that.

**Both first suspects are dead, by measurement.** `stack_free: 5532` of 8192 means the render
task's deepest use was ~2.7 KB — never close, even at the original 4096. And eight PMU samples
spanning a blackout were byte-identical with every rail up, so the AXP2101 is not cutting
anything either.

0.2.25 writes **breadcrumbs**: the render loop stores its stage in `RTC_NOINIT` memory, which
survives the reset a panic performs, and the next boot reports the last stage reached as
`crash_phase`. Not a line number, but it localises the crash — the backtrace is unreachable
because it prints to a console this panel does not have and which resets it on open, and there
is no coredump partition to add without a USB reflash. The stage list is next to `PHASE()` in
`display.c` and in ROOM_ENDPOINT_PLAN.md §10.4aj; keep the two together. **Read it as the
render task's position, not as the crash site** — a fault in the main task reports whichever
stage the render loop was parked in, and it parks in stage 10, where the capture blocks for a
full 40 ms of every 40 ms frame (§10.4al).

**One task owns each chip, and that rule is load-bearing.** `esp_lcd_panel_io_spi` is not
thread-safe: `tx_param` drains the queue `tx_color` fills and reuses the same descriptor slot,
so two tasks on one io handle can wait forever on each other's transfers or `memset` a
descriptor under DMA. Until 0.2.26 there was exactly one such caller —
`display_set_brightness()` wrote `0x51` from the **main** task, on every boot and every
fifteen-minute settings fetch, against a handle the face task drives at ~25 fps. It now records
the value and the render loop applies it (phase 14). Nothing outside `face_task` may touch
`s_panel` or `s_io`; see ROOM_ENDPOINT_PLAN.md §10.4ak, including what that fix does **not**
yet claim.

The codec is the same story and 0.2.27 is the same fix. `esp_codec_dev.c` has **no lock at
all** — read, write, `set_out_vol` and `set_in_gain` go straight at the device struct and the
chip's I2C registers; the component's only mutex is in `audio_codec_data_i2s.c` and guards the
data path, not a control write. `audio_set_levels()` was the second main-task caller in the
same `apply_settings()` window that the 12:16 panic was bounded to, so it now records and
`audio_apply_levels()` runs on the render task as phase 15 (§10.4al). Nothing outside
`face_task` may touch `s_codec` either.

Superseded suspect: the render task's stack, created at 4096 bytes when it drew a static pattern and
now running an I2S capture, an accelerometer read, font rendering, PMU sampling, float tweening
and two LCD blits per frame. 0.2.24 raises it to 8192 and reports
`uxTaskGetStackHighWaterMark` as `stack_free`, so a near-overflow is visible **before** it is a
panic (ROOM_ENDPOINT_PLAN.md §10.4ai).

Do not debug this from the display side. Check `reset_reason` and `stack_free` in telemetry
first — the console cannot help, because opening it resets the panel.

## The robot is a rig, and a poke picks from a pool

Three modules ported from `frontend/src/pet/`, which was written to be ported — `face.ts` says
so in its own header. They keep the reference's numbers:

| web | panel | what it carries |
| --- | --- | --- |
| `face.ts` | `emotion.c` | six emotions plus `bewildered`, as lid geometry |
| `rig.ts` | `rig.c` | seventeen actions, limb poses, the figure transform, the gag skeleton |
| `variants.ts` | `variants.c` | weighted pools, per-variant cooldowns, the repetition penalty |

A tap picks a reaction rather than doing one thing, and hammering it softens the magnitude
toward a 0.35 floor — never to zero, because a motionless response is indistinguishable from a
broken one. Emotion is carried by lid geometry, whole-face motion and timing, **never colour**
(`docs/reference/DESIGN.md`); the colour cycle stays orthogonal, as the one thing a child steers.

Two deliberate omissions: whole-figure **rotation** (a per-pixel resample 25 times a second;
`ang` becomes a head tilt instead) and therefore `spin`, which is left out of the pools rather
than faked badly. See ROOM_ENDPOINT_PLAN.md §10.4an.

## Two bodies: the ostrich is the default

`face_state_t.form` selects which body is drawn. **The rig, the emotions and the tweening are
shared** — a form decides the shapes, never the behaviour, which is what stops a second body
from becoming a second animation system. The eyes in particular are byte-for-byte the same
call in both, so the six emotions come across for free.

The ostrich is what a panel shows out of the box, because that is what the twins asked for.
`docs/mocks/room-endpoint/ostrich-mock.py` is its spec at true geometry, drawn with these same
primitives, so `draw_ostrich()` is a transcription of it.

**Four taps then hold swaps the body**, and `"change into merc"` now does it too — that phrase
was the twin's original ask and it is the first entry in `vocab.c`. Three taps reboots, five
calibrates; four was a dead count and is now the form toggle.

A bird tucks its head under a wing, so **peekaboo rides the wing** rather than the robot's
hands-over-eyes. The arm pose drives the tail flap, because a bird has no arms and the tail is
the one thing on it that answers to that channel.

`face_zone()` is per form. An ostrich's head is high and small and its legs are most of its
height, so the robot's hitboxes would put "head" over empty space — a form whose zones were
not updated would answer every poke from the wrong pool.

## Where you poke him changes what he does

`face_zone()` maps a panel coordinate onto the rest silhouette — head (with the antenna), body,
arms, legs, or background — and each has its own pool. The head gets the warm ones, the belly
gets the gags, the sides get tickling, the feet get everything that leaves the ground. A tap
that misses him still answers, because "nothing happened" reads as broken.

Zones follow the 180° flip, so his head is his head whichever way up the panel is held.
Transient action offsets are deliberately *not* applied: a hitbox that leaps during a jump is
one nobody can learn.

**The CST820's orientation is unmeasured.** Nothing here had ever read a coordinate from it. So
a marker ring is drawn where the firmware thinks the finger was, and `tap: [x, y, zone]` goes
out in telemetry — if the dot isn't under the finger, the mapping is wrong and the numbers say
how. One tap settles it (ROOM_ENDPOINT_PLAN.md §10.4aq).

## Host tests: `make -C firmware/host test`

`face.c`, `font.c`, `emotion.c`, `rig.c` and `variants.c` have **no ESP dependencies** — a
standing claim that nothing checked until now. The host build is the check, and it runs in CI
before the toolchain pull because it takes two seconds. It is also the only place this firmware
has assertions at all, since there is no hardware in CI.

It is `-std=gnu11`, not `-std=c11`, deliberately: ESP-IDF builds this code as `-std=gnu17`, and
a host build in a *stricter* dialect tests a language the device never compiles.

Run it before every firmware push. It found three real defects the ESP build had compiled
cleanly — two missing includes that IDF supplied transitively, and a zero-init bug that made the
variant pool repeat itself on the first pokes after boot (§10.4ao).

## The robot stays upright, and the meter keeps up

The accelerometer (not the gyroscope — gravity says which way is down, rotation rate does not)
flips the frame 180° when the panel is inverted. A 180° rotation of a row-major buffer is
exactly its reversal, so it is one pass and it takes the version label and the meter with it.
Ninety degrees does not fit a 368×448 panel.

## One task owns the codec, one owns the panel

`audio.c` runs its own task and performs **every** codec operation. `audio_beep()` and
`audio_set_levels()` are requests, not actions.

That is not tidiness. `esp_codec_dev.c` contains no lock of any kind — read, write,
`set_out_vol` and `set_in_gain` all walk straight into the device struct and the chip's I2C
registers, and the component's only mutex guards the I2S *data* path. Two tasks on one handle
is the race that cost a panic (§10.4al). The same rule already applies to the panel in
`display.c`, arrived at the same way, and ESP-SR would have reintroduced the codec half of it:
a feed task reading while the render task beeped.

It also takes the blocking 40 ms capture **out of the render loop**, which is where that loop
spent most of its wall clock — and why `crash_phase` reported stage 10 whatever actually
failed. **Stage 10 changed meaning in 0.2.33**; breadcrumb readings do not compare across that
line.

The rate is **16 kHz**, not the vendor BSP's 22050, because ESP-SR's front end takes 16-bit
16 kHz and nothing else. A beep and a level meter are no worse for it.

## The microphone is always on, and the meter is down the left edge

A green bar at x 4..16 tracks the loudest sample in each frame. Always running, no gesture: a
microphone has no symptom, and a meter answers "is it working" at a glance. The left edge is
free by construction — the head spans x 76..292 and the arms reach x 104.

The capture **paces the render loop**: one chunk per frame, sized to the frame period, so it
drains as fast as the I2S DMA fills. Read any slower and the meter falls further behind the
room every second, which looks like bad calibration and is a backlog.

The peak also rides out in telemetry as `mic_peak`, so the microphone can be confirmed from
the box once the panel is on a charger in another room (ROOM_ENDPOINT_PLAN.md §10.4aa).

## Volume, microphone gain and brightness are settings, not constants

`GET /api/endpoint/settings` (panel key) and `PUT` (owner). The panel applies them at boot and
on every cycle, and the five-second hold reboots — so it is also how a change takes effect at
once. Tuning is seconds rather than a build, CI run, deploy and OTA.

They live in `app.endpoint_settings`, **not** `app.settings`: that table is gated on
`app.is_owner()` and holds the Gmail client secret, the Moltbook key and the global kill, and a
panel is a `device_key` precisely so a stolen one cannot reach them. The panel policy is
`FOR SELECT`, so read-only is structural; an RLS isolation test pins both halves
(ROOM_ENDPOINT_PLAN.md §10.4ab).

Ceilings are clamped at the API and say so in the log: volume 85 (above the confirmed-good 70,
below the vendor's 90, so a slipped digit cannot reach a child's ear), mic gain 42 (the
ES8311's PGA truncates above it), brightness floor 10 (zero looks exactly like the blanking
fault).

## The screen sleeps, and says so rather than looking broken

Five minutes with nothing happening dims the panel to a quarter of its configured brightness;
fifteen takes it to zero **and stops blitting**. Touch, movement, a voice command or an
arriving message brings it straight back. The policy is `screen.c` — pure arithmetic, so the
host suite holds it to the properties its comments claim.

It does **not** call `esp_lcd_panel_disp_on_off(false)`. This controller has a documented habit
of not coming back from display state transitions, and a sleep whose wake path is the operation
most likely to fail is not a power saving, it is a toy that is dead every morning. Brightness 0
leaves the display initialised and the io handle untouched, so waking is one 0x51 and one
frame — both of which the render loop already does thousands of times an hour.

Note that this deliberately drives the brightness the settings API refuses to accept: the
clamp's floor of 10 exists because *a setting* of zero is indistinguishable from the blanking
fault. A sleep is not a setting — the configured brightness is untouched underneath it, and the
panel comes back to it — so the stage is reported instead, as `screen` in the telemetry body
and in the `render: N frames ok` beat. That matters because a sleeping screen stops blitting
on purpose, which makes `blit_ok` stop climbing: the exact signature of the stalled render task
that cost 0.2.44 a photograph from the owner to diagnose.

The accelerometer threshold (`SCREEN_MOVE_COUNTS`, 900 raw counts ≈ 0.11 g) is reasoned rather
than measured. Every wake it causes logs its magnitude next to the threshold, so a panel waking
itself on an empty table says what to raise it to.

## Maintenance gestures: N short taps, then hold

The tap count selects the action — **three reboots, five calibrates the touchscreen** — and the
rhythm is the guard. Each press must begin within 500 ms of the previous release. Against
20 000 simulated presses including 3-9 s leans, a bare hold fires 1937 times and this fires 12;
not zero on purpose, because a gesture that can never happen by accident cannot be performed on
purpose either.

One amber pip appears per counted tap and the bar grows during a hold that will actually do
something — a hold after four taps draws nothing, because a bar promises an action.

**The hold is decided on press *duration*, not on the press edge.** That is what lets five taps
exist at all: arming at the fourth press would clear the count before the fifth ever landed.

Letting go mid-hold abandons the whole sequence, so a half-finished gesture never leaves the
panel one press from acting. It lives in `gesture.c`, pure and host-tested, because **both**
failure directions cost something: a false positive acts on a toy in a child's hands, a false
negative strands an owner who has no terminal.

## Touch calibration: five taps, then hold

The middle of the panel reads true and the outer 20% is skewed — the ordinary edge behaviour of
these controllers, and exactly what a fixed scale cannot fix, because the error is zero in the
centre and grows outward.

Sixteen targets (4 knots per axis), **three or more taps each**, then a piecewise-linear
correction per axis.
**Four knots is a measurement**: against a simulated edge compression whose worst error is
29 px, three knots leave 12.3, four leave 7.1 and five leave 4.6 — and four holds that ratio
across distortion strengths, which matters because the real curve is unknown and over-fitting a
model we invented would be its own mistake.

**One tap is not a measurement.** Where a tap lands moves with how much fingertip goes down,
and on a 1.8" panel a millimetre of contact-patch drift is about eleven pixels — more noise
than the edge error the routine exists to remove. So each target takes taps until they
*agree* (three minimum, six cap) and contributes their **median**: one slip with the side of a
finger drags a mean and cannot move a median. The crosshair is amber while it wants more and
turns green when they agree, with a dot per tap recorded.

Modelled against occasional gross misses — one tap in six landing up to 40 px off, which is
what the owner actually described — the difference is in the tail rather than the mean:

| | mean worst | worst case | runs over 15 px |
| --- | --- | --- | --- |
| one tap per target | 12.5 px | 38 px | 80 / 400 |
| adaptive, requiring agreement | 7.5 px | 12 px | **0 / 400** |

Against *uniform* jitter the extra taps barely help at all, because the residual is dominated
by the model's own error. The tail is the whole story, and the test asserts the tail.

The outer 6% is extrapolated from the outer segment rather than clamped, because clamping would
flatten precisely the band that is wrong. A non-monotone result is **refused** and the previous
calibration kept: a folded map puts two places at one coordinate, and the only way out of it is
the touchscreen it just broke. Stored in its own NVS namespace, re-checked on load, and also
reported in telemetry so a re-flash costs a re-run rather than a fact nobody has any more.


The hold alone used to be the whole gesture, guarded by its length — five seconds, because
4-5 year olds were measured producing *ordinary* taps lasting up to 4.2 s. A 0.8 s margin
against a determined four-year-old is not much, and both units are going to the twins.

So the length is no longer doing the work; a **rhythm** is. Three short taps, each beginning
within 500 ms of the previous release, then a sustained press. Children mashing a panel produce
plenty of taps and plenty of leans; what they do not produce is that sequence. Against a
simulated 20 000 presses including leans of 3-9 s, the old gesture fires 1937 times and this
one fires 12 — and not zero on purpose, because a gesture that can never happen by accident
cannot be performed on purpose either.

One amber pip appears per counted tap, and the bar grows during the hold as before. Letting go
mid-hold abandons the whole sequence, so a half-finished gesture never leaves the panel one
press from rebooting.

It lives in `gesture.c`, pure and host-tested (`firmware/host`), because **both** failure
directions cost something: a false positive reboots a toy in a child's hands, and a false
negative strands an owner who has no terminal with no way to force a firmware re-check.

## It listens, and what it hears scrolls along the bottom

The owner asked for two things in one sentence: understand the microphone **on the panel**
rather than over Wi-Fi, and **put the text on the screen as it recognises it**. Both are built,
and the honest account of what that means is the important half.

**MultiNet resolves a list, it does not transcribe.** `vocab.c` holds every phrase the panel can
be told — 22 of them, and the panel logs that count at boot — and MultiNet7 answers with *which one* it heard, offline, in under half a
second. The smallest genuinely open-vocabulary model anyone runs is Whisper tiny int8 at ~75 MB
against 8 MB of PSRAM. That is two orders of magnitude, not a tuning problem (§10.4ar), so
dictation is not a thing this board can be persuaded into.

**So the ticker shows the phrase the model resolved, and shows nothing when it resolved
nothing.** A toy that prints a guess is worse than one that misses: a four-year-old can read the
miss and cannot read the invention, and a hallucinated command the robot then *acts on* reads as
the toy being broken.

**No wake word.** "Just try and listen" was the request, so WakeNet is disabled and MultiNet sees
every frame the front end calls speech. That decision is what shapes `vocab.c`: every phrase is
always live, so each is **two words minimum** — a one-word always-on vocabulary fires at the
television — and the host suite enforces that, plus lowercase-only (the grapheme-to-phoneme pass
silently refuses anything else, leaving the panel deaf to exactly one thing with nothing on
screen to say so) and no phrase being a prefix of another.

**No AEC, and not by choice:** one ES8311 and no ES7210 means no playback reference channel to
cancel against (§10.5 A). The front end is told the truth about its input — `"M"`, one
microphone — rather than handed a fake channel to cancel against silence.

**The one-owner rule holds.** esp-sr's usual arrangement is its own task reading I2S; here
`audio.c` already owns the codec, so it is the only caller of `speech_feed`, which accumulates to
the front end's chunk size (not the capture's 40 ms) and hands off. MultiNet runs on its own
task pinned to **core 1**, because core 0 carries Wi-Fi and a 200 ms recognition pass beside the
radio makes both stutter.

**The red dot is a compliance requirement, not decoration.** The ICO Children's Code requires a
recording indicator, so `caption.c` draws one whenever the microphone is open, brightens it while
someone is talking, and draws nothing at all when it is not — "muted is a promise", and this is
the only thing on the glass that keeps it. Three host tests assert it against the real state.

The ticker itself clears a black strip behind the text before drawing. The figure's feet reach
y=435 on a 448 px panel and the line runs at 432; measured, "PLAY PEEKABOO" ran straight through
the ostrich's toes. On an AMOLED an unlit pixel is off, so the strip costs nothing and reads as a
subtitle bar.

## The speech models ship before the code that uses them

`firmware/dist/srmodels.bin` (2.91 MB) holds WakeNet9 `hiesp` and MultiNet7 English, selected in
`sdkconfig.defaults`. They shipped in 0.2.31, three versions before the code above called
them — which is the point of this section.

**They ship first because OTA can never deliver them.** `esp_https_ota` writes app slots; the
models live in the `model` data partition (`0xaa0000`, 3.5 MB, reserved at the very first
bring-up for exactly this). So they reach a panel through the one USB flash each unit gets,
which is why `ARTIFACT_IMAGES` carries `srmodels.bin -> 0xaa0000` and why a missing model blob
refuses the whole flash rather than writing a panel that cannot listen.

**That is affordable because the command list is not in the blob.** MultiNet phrases are
supplied at runtime as phoneme strings, so adding or retuning a command is an ordinary OTA.
This image changes only if the wake word or the model generation does.

It is command-word recognition — up to 200 phrases, under 500 ms, offline — **not dictation**.

**It is checked by content, not by bytes.** esp-sr's packer collects models with `os.walk` and
never sorts, so two correct builds of the identical models differ in nearly every byte while
being exactly the same size — CI wrote `mn7_en` first, this machine wrote `fst` first.
`scripts/srmodels-inventory.py` prints every file as `model/file sha256 length`, sorted, and CI
diffs that. Byte-for-byte equality modulo an ordering nobody chose, and it still catches a wrong
wake word, a missing model or a truncated file (§10.4as).

Cost: esp-sr is 308 MB and a clean build goes from ~50 s to ~2 min. That buys a `srmodels.bin`
CI verifies instead of a committed blob nobody checks.

## No Wi-Fi is not a reason to stop being a robot

Until 0.2.32 a failed join ended `app_main` outright:

```c
if (net_connect(&cfg, WIFI_TIMEOUT_MS) != ESP_OK) { ota_confirm_health(false); return; }
...
imu_start();   // never reached
```

Two consequences, both found the hard way on a panel flashed with wrong credentials. The
**accelerometer never started**, because `imu_start()` sat after that return — so the panel drew,
beeped and answered taps while refusing to lean or flip, which looks nothing like a network
fault. And **the OTA loop ended with it**, so a panel that booted while the router was down
stayed unreachable until someone power-cycled it, on a device whose whole premise is that
nobody has to touch it.

Now `imu_start()` runs before the network (it is on I2C and owes the radio nothing), and a
failed join falls through to the loop, retrying every 60 s via `net_retry()` rather than the
15-minute update cycle. `net_connect` cannot be called twice — its one-time init is wrapped in
`ESP_ERROR_CHECK` and returns `ESP_ERR_INVALID_STATE` the second time, so calling it again does
not fail, it *aborts* — hence the separate entry point.

Probation is unchanged: a pending image with no Wi-Fi still gives up and rolls back.

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

## The partition table changed once, and it cost a USB flash

OTA rewrites an app slot and nothing else, so changing `partitions.csv` means a USB reflash.
The original layout reserved a 3.5 MB `model` partition for models nothing used yet, because
reserving it was free — and that call paid for itself: the models went on in 0.2.31 and the
recogniser in 0.2.37 without ever touching the table for them.

What did move was the **app slots**. Linking ESP-SR took the image from 1.16 MB to 3.05 MB and
`factory` was 1.5 MB — and `factory` is precisely where a USB flash writes, so the image would
not have gone on at all. `idf.py build` says this as a *warning*, which is how a build that
cannot be flashed still exits zero.

So the three app slots are now **3.5 MB each**, equal by construction: the recovery app and the
two OTA slots all hold the same image, and a slot smaller than its siblings is one that cannot
take an update they can. The 3 MB came from the OTA slots (4.5 → 3.5 each), which means
**`model` and `storage` keep their exact offsets** — that was worth engineering for, because
every constant that moves is another place a panel can be bricked from and the box's flasher
hardcodes `MODEL_OFFSET`.

**A resize makes `otadata` mandatory on every USB flash**, and it was not being written. A
panel updated into `ota_0` has `otadata` naming that slot; a USB flash writes `factory`, so the
panel would ignore the image just written. That was survivable while the layout was fixed and
is not survivable across a resize, because the stale pointer now names a slot at a *new* offset
holding the middle of an old image. `ota_data_initial.bin` ships in `dist/` and the flasher
writes it at `0xf000`.

Four supervisor tests hold the layout: no gaps or overlaps and it ends exactly at 16 MB, the
three app slots are equal, **every app slot is bigger than the committed image** (the invariant
the old "≥ 4 MB" constant was standing in for, now checked against the bytes that ship), and
`otadata` is in the flash set.

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

**Every source change rebuilds the image, including a comment-only one.** The app descriptor
embeds `app_elf_sha256`, a hash of the ELF — and the ELF carries debug info, so touching a
comment changes the binary. A docs-and-comments commit still needs `dist/` regenerated, and
assuming otherwise is a red `firmware` job.

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
