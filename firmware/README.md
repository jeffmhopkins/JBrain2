# Room endpoint firmware — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-10-14

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

## Shaken, he gets dizzy; told he is loved, he coos (0.4.7)

Two actions that answer one thing each, so neither is in a poke pool:

- **Dizzy, from a shake.** `screen_shaken` (pure, host-tested) watches the same per-sample
  accelerometer difference the sleep timer uses and calls it a shake at four jerks of about a
  gravity inside 1.5 s, merging samples closer than 90 ms and resting 6 s after. He sits down
  hard (`figure_pose_t.sit`: the legs splay and `face.c` lowers the body by what that takes off
  this form's legs, so the feet stay put), shakes his head with the shake dying away, and three
  stars go round his head. `CUE_DIZZY` is a slow warble sliding down an octave, sounded only
  when nothing else owns the speaker.
- **Love, from `i love you` / `love you`.** Five hearts rise beside his head in turn, he sways,
  and `CUE_LOVE` is the coo twice, the second a third higher.

The bench has a Shake button and an "I love you" voice button.

**0.4.10: tickle tickle, and both of these twice as long.** `tickle tickle` / `tickle me` makes
him laugh helplessly for 4.8 s: he squirms side to side, bounces on every "ha", tosses his head,
kicks his legs out in turn, and tickle marks flicker either side of his body. `CUE_TICKLE` is a
laugh (`SH_LAUGH`: seven breathy "ha"s tumbling down), and because a cue is at most 0.8 s the
action says it again (`rig_recue`), three laughs in all, each only into a free speaker. The
dizzy spell is now 6.4 s and the love 5.6 s, twice what they were, with their motions'
cycle counts doubled so the head shakes and the hearts rise at the speed they did: twelve
hearts now, not five. The bench has a "Tickle tickle" button.

**And eating is a meal.** `ACT_EAT` was three pecks in 1.6 s and a 300 ms double bite. It is
now 4.8 s and five bites of a cookie (three since 0.4.11) (`EXTRA_FOOD`): the robot carries it from its hand to the
corner of its mouth, the bird pecks it off its wing, a piece goes out of it on each bite with
crumbs falling, and the fifth finishes it. The mouth opens wide before each bite, snaps shut
on it and chews after (`figure_pose_t.chew`, which the renderer takes alongside `talk`).
`CUE_EAT` is now a crunch and two munches, said on every bite through `rig_recue`, the first
included: the meal is the one action that does not sound as it starts
(`rig_cue_on_start`), since its hand is still on the way up. The bench has a "Have a snack"
button.

**0.4.11: three bites, and a laugh that does not freeze him.** The meal was too long at five
bites (owner), so it is three, 0.96 s apiece as before, each bite a bigger piece of the cookie.
And the tickle froze for a moment as it started and at each laugh after (owner). Cues are
rendered on the render task, and the laugh summed a band-limited triangle and a pulse per
sample over 740 ms: 7.3 ms on a desktop, the costliest cue in the table, and a multiple of that
on the panel. Its voice is now a triangle and a sawtooth straight off the phase, 0.46 ms for
the same laugh, cheaper than the giggle.

**"I'm going to tickle you" (0.4.11).** Owner: *"let's change tickle, to 'I'm going to tickle
you' and then when that happens, pokes make him be tickled ... after five tickles."* The phrase
(`im going to tickle you`, `gonna tickle you`; `tickle tickle` / `tickle me` are gone) is a
warning: `ACT_BRACE` hunches him with a nervous titter (`CUE_BRACE`, the laugh's shape run
upwards). `tickle.c` (pure, host-tested) then holds for 10 s, renewed by each tickle: a poke that
reaches him is a tickle rather than the zone's pool, the first four a giggle and the fifth the
laughing fit (`ACT_TICKLE`), after which pokes are pokes again — except during the fit
itself, which a poke only flinches (`rig_holds_pokes`): the child doing the tickling keeps
poking, and the sixth poke used to cut the fit off after its first laugh.

**0.4.13: tickled out.** Owner: *"on tickle mode, if tickled 5 times, he should fart and go
dazed."* The fifth tickle now starts a run, `rig_run_tickled_out`: the laughing fit, then a
fart, then `ACT_DIZZY`, sitting down hard with the stars going round. `display.c` keeps where
in the run he is and, as each step ends, starts the next with its own sound (`rig_run_next`).
Anything else taking over — a nod, a shake, a poke after the fit — ends the run, and a fart
asked for by voice never knocks him dizzy: what ends has to be the run's own step, the same
action started at the same moment (`rig_run_t.since`), for the run to go on — a voice "fart"
over the run's own fart is the same action, but a takeover all the same.

**0.4.12: a tickle that looks like a tickle.** Owner: *"when he's in tickle mode if I touch him
he still acts like I'm poking him versus tickling."* He did: the four tickles were `ACT_GIGGLE`,
one of the poke pools' own reactions, with the poke's flinch and colour change. Now each is
`ACT_TICKLED` — the fit's squirm, short, with the tickle marks and `CUE_TICKLED`, a held-in
"hehehe" — at a magnitude that rises tickle by tickle (`tickle_mag`), and a tickle neither
flinches him nor changes his colour. And while he is warned and doing nothing else he holds the
brace, hunched and shivering (`tickle_wait_p`), so the game is visibly on.

**One fart in ten is the long one (0.4.12).** Owner: *"about a 1 in 10 chance that when he
farts on command, that the fart lasts a good 7 seconds, and make sure the fart sounds funny."*
A voice `fart` is `rig_fart_pick(esp_random())`: one time in ten it is `ACT_MEGAFART`, 7 s of
him straining and wobbling, wide-eyed, as a cloud of gas piles up behind him (`EXTRA_GAS`). A
cue cannot run past 0.8 s, so the noise is `CUE_MEGAFART` re-cued whenever the speaker is free
(`rig_recue_continuous`) until the release at 0.93, each piece the next of eight characters —
a rumble, a climb, a sputter, a squeaker, a wet one, a balloon, a putt-putt and a trumpet — so
seven seconds of it never sounds like one tone looped (and each long one picks up where the
last left off, so no two start alike). A poke cannot cut it off.

**A different snack every time (0.4.11).** Owner: *"Kids are also requesting a random item to
eat, maybe one out of eight possibilities."* `food_t`: a cookie, an apple, a banana, a slice of
pizza, a donut, a carrot, a slice of watermelon or a strawberry, each a per-pixel shape in its
own colours (`food_px` in `face.c`), bitten and crumbed exactly as the cookie was. `display.c`
picks one per meal with `food_pick`, which never brings the last one again.

**What goes in must come out (0.4.11).** Owner: *"If he eats three times and within 1 minute,
he should have to poop afterwards. The poop should stay on the screen for say 20 seconds. And
should slide around with the angle of the device."* `poop.c` (pure, host-tested) counts meals
and owes one at the third inside 60 s; `display.c` starts `ACT_POOP` at the first moment he is
free on the pet screen. He squats (`figure_pose_t.sit`, as dizzy does), strains, and at
`POOP_AT` it lands behind him with `CUE_POOP`, a falling plop. On the floor it is the swirl
with a face and stink lines, behind the figure, and it SLIDES: the unsmoothed tilt pushes it,
friction slows it, the floor's ends bounce it back, and below a small tilt it does not start
at all, so it stays put on a table. It plops in, and shrinks away after 20 s.

**Eight poops (0.4.12).** Owner: *"make eight variations of poops also. Maybe we can vary
graphic, that poops expression, and the amount of stink, and maybe it has flies or not."*
`poop_drop` picks a kind, never the last one, from `POOP_LOOKS` (`poop.c`): each row a shape
(the swirl, a four-tier tower, a puddle, a pile of pellets, a log), a face (smiling, cool in
sunglasses, asleep, shocked, tongue out, grumpy, queasy with a sweat drop, winking), a stink
of 0–3 (more lines, rising higher and faster) and 0–3 flies on figure-of-eight paths. The
classic brown swirl, a golden one in shades, a sleepy puddle with flies, surprised bunny
pellets, a tower blowing a raspberry, a grumpy log in a swarm, a queasy green one and a pink
unicorn one that does not smell at all. No two are near-twins: every pair differs in at least
two of the four, which the host test holds the table to.

**A poke over the pet's own sound reacts again, and is heard.** A poke over any sound used to
only flinch, which with the meal crunching through two-thirds of its length and the tickle
laughing through half of its own made most pokes during them do nothing. Two halves to it: the
arbiter now lets a press cue cut a sounding PRESS-slot cue (the pet's own noise; never a reply
or another slot's result or chime, which a press is still dropped behind), and
`stream_facts_t.pet_only` tells `display.c` when that is all that is sounding, so it reacts
there and flinches everywhere else. `stream_tests` drives it through the real arbiter.

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

**There are several suites (see `host/README.md`), and the second one drives the interface.** `main/panel_sm.c` is the
panel's one state machine — which mode it is in, what a finger at (x, y) means, what sounds, and
what is drawn — a module that takes a frame's facts and returns ordered effects and a view.
`run_sm_tests` steps it one frame at a time against a fake engine, speaker and microphone, so a
notification, a press, a stop, a reply and a deep-sleep request can all be asserted without a
panel, and `test_storm` runs it through 300k randomised frames checking its invariants on every
one. That matters because *every* fault the owner reported on the evening of 2026-09-28 was an
arbitration fault. `firmware/host/README.md` says how to add a scenario.

## 0.4.0: one state machine, one engine, one speaker owner

**0.4.0 is the voice-post rewrite** (`../docs/plans/JPANEL_MESSAGING_FIXES_PLAN.md` §15): every
finding of the 2026-10 adversarial review fixed in one release, with no wrapper layer.

- **`panel_sm.c` is the interface.** Which mode the panel is in, what a finger means, what sounds
  and what is drawn are one machine, stepped once per frame; `display.c` only samples, runs the
  effects in order and draws `sm_view_t` (`ui.c`/`ui.h` are gone). `check_no_logic_in_display`
  keeps it that way.
- **`overlay.c` draws `sm_view_t`** (0.4.1): the notice, the run, the pair, the dot, the grid
  and the send's outcome, with no ESP dependency. That is what lets `firmware/mock/` build the
  same pixels for the browser bench, `docs/mocks/jpanel-s3/`: the machine, the engine, the
  arbiter, the cues, the pet and the overlays compiled to WebAssembly and pressed with a mouse.
- **`jpanel_core.c` is the engine**: one serial loop that owns every request and publishes one
  seqlocked snapshot; render posts commands and copies the snapshot, nothing else.
- **`stream.c` owns the speaker**: cues, the pet's reply, silence, the stop word and the message
  ring are decided there, host-tested tick by tick; `audio.c` is codec I/O.
- **Leases.** A SEND holds lease 1 from before its post to its end; a pet turn holds lease 2 for
  its upload. The microphone cannot open over a buffer being uploaded. Talk plays its reply from
  its own buffer through the reply slot and waits for it to finish before reading the next.
- **The persistent socket is off** (below) and **`SPEECH_MIN_INTERNAL` is 96 KB**, provisional
  until measured: a panel short of RAM boots deaf and says so (`speech_ok: false`) instead of
  looping.
- **Two watchdogs** (engine: heartbeat stalled and past its published deadline; arbiter: no tick
  for 10 s, only when the audio task exists), both off during an OTA, capped at three reboots by
  RTC words that a version change re-arms. The report's `restart_why` says `wd-engine` or
  `wd-arbiter`.
- **The report gains** `speech_ok` and a flat-integer `jp` object (voice post's counters) after
  `pmu_history`.
- **Placeholders until the F4 mocks are approved:** a blue `SENDING` dot while a message goes,
  then `SENT` / `NOBODY` / `NOT SENT` / `DROPPED` for 2.5 s; every refusal sounds `CUE_BUSY`.

Static internal RAM (`.data` + `.bss`) is gated in `firmware.yml` against 0.3.39's 97,144 B.

## The robot stays upright, and the meter keeps up

The accelerometer (not the gyroscope — gravity says which way is down, rotation rate does not)
picks one of four ways up (`orient.c`). Upside down is a 180° flip: a row-major buffer's
reversal, one pass. On its side the figure is drawn scaled into a 368×368 square, which a
quarter turn maps onto itself, and `blit_frame_rotated` permutes it onto the glass.

**0.4.6: he slides to the edge of the glass, and his head rights itself.** On its side the glass
is 448 long and the square 368, and the lean was clamped inside the square, so at full lean the
ostrich's feathers were cropped 40 px short of the glass (owner: *"cutting off before we even get
into the left extent of the screen"*). The square now slides into those bands with the lean
(`orient_window`), up to its edge being the glass's. The blit reads its source and a touch is
mapped back through the same `orient_panel_to_frame`, so the two cannot disagree. And while the
body slides downhill, the head turns back toward true up by 0.6 of the panel's angle, eased
(`orient_upright`, `face_state_t.upright`): the ostrich's head swings about its neck and its
crest, eyes and beak turn about the head's centre; the robot, with no neck, nods its head over a
little. The browser bench has a tilt slider for it.

**0.4.8: the righted head's eyes and beak turn with it.** 0.4.6 turned where each feature sat
but drew every one square to the glass, so on a panel held on its side the happy lower lid sat
beside each eye instead of under it and the beak's four segments stacked into level bars (owner,
with a photo). Each eye is now drawn turned through `upright` — outline, pupil, glint and both
lids — and the beak, its line and the cheeks turn about the head's centre with the jaw opening
along the head's own down. Upright the drawing is unchanged, pixel for pixel; the host test
turns the upright face and requires the turned one to match it.

**0.4.9: the play button rings with how much has been heard.** While a message plays, a ring
round the play/pause button fills clockwise from twelve o'clock (owner: *"a little circle border
that progresses to show how far it is until it's all the way complete"*). The whole track shows
from the first moment in grey, so how much is left is visible, and a paused message keeps its
ring where it got to. The panel streams a message and never holds all of it, so the length comes
from the box: `X-Jpanel-Duration-Ms` on `/next` and on a replay's `/message/{id}/pcm`, which the
engine publishes with the stream (`jp_cur_t.duration_ms`) and the machine turns into
`sm_view_t.progress` from the arbiter's audible ms. A box that sends no length draws no ring.
The same header switches on the engine's existing "80% of a short one" heard rule, which was
written for it and had been waiting on the box to send it.

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

Waking out of dark with a message still waiting restores the *big* pop-up. The badge it shrank
to is right for a child who was interrupted once and chose not to come; it is wrong for one who
walked into the room the next morning.

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

## One connection to the box, not one per request (0.3.39)

**0.4.4 makes each frame cheaper.** Lydian's panel on 0.4.3 reported 68 ms of work per frame (about
14 fps) and 13.5 frames a second with nobody touching it.
- **The idle redraw.** The render loop asked for a frame whenever the state machine emitted any
  effect, and since 0.4.0 it emits `MUTE_COMMANDS` on every frame, so every pass was a redraw.
  That effect no longer counts, so an idle pet is drawn at the 200 ms floor again and speech gets
  the processor and PSRAM back.
- **Limbs as one run per row.** `face_draw_limb` used to stamp a full disc at every pixel of a limb
  (about 6 writes per lit pixel, and the limbs are most of the ostrich). It now gathers each row's
  extent and writes it once, then draws the end ball as its own disc. Discs everywhere are drawn
  as row runs too. `host/tests.c` keeps the old per-pixel loops and checks 1,500 random limbs and a
  disc sweep against them, pixel for pixel. The limb routine borrows about 2 KB of the render
  task's stack while it runs.
- **The report's `note`** now splits the frame: `frames=N draw=avg/max blit=avg/max gap=avg/max`
  (ms), so the next step can aim at whichever is bigger.

**0.4.3 makes the animation run at the speed it was tuned for.** A cold review of the render
path found four things, all confirmed against the code:
- The loop slept a fixed 40 ms *after* each frame, so while animating a frame took draw + blit + 40 ms
  (about 12 fps against the 25 every per-frame tween assumes). It now tops the pass up to 40 ms,
  with at least one tick so the idle tasks still run.
- A reply's talking mouth was drawn at the 200 ms floor (5 fps), because the flag asking for a
  frame was set after the gate that reads it. A body change from the PWA had the same bug.
- The project builds at ESP-IDF's default -Og, and nothing set the CPU clock, so it ran at 160 MHz.
  The drawing files (`face.c`, `rig.c`, `emotion.c`, `font.c`, `overlay.c`, `confirm.c`,
  `caption.c`, `display.c`) now compile at -O2. They are flash-resident, so the internal RAM
  budget is unchanged. The CPU runs at 240 MHz, as esp-sr recommends for the S3.
- The report's `note` carries `frames=N work=avg/max gap=avg/max` (ms), so the real frame rate
  is readable through the debug API. A `gap` near 40 is 25 fps.

**0.4.2 keeps one HTTPS connection between requests** (`link_keep_alive`). 0.4.0's
one-session-per-request meant a full TLS handshake every 3 s for the settings poll alone, and the
first 0.4.0 panel's animation visibly lost its smoothness to them. The connection is still one at
a time under the same lock. It is only turned on after the recogniser has started, so boot keeps
the internal RAM speech needs. A response must be read to its last byte to be kept, a kept
connection idle for over 60 s is dropped, and a request whose kept connection the box had closed
is sent again on a fresh one. Telemetry reports `ws: "https-keep"`, with `ws_connects` counting
handshakes since boot and `ws_drops` counting the stale connections.

**0.4.0 turns the socket off** (`LINK_PERSISTENT_SOCKET 0` in `main/link.h`; plan
`JPANEL_MESSAGING_FIXES_PLAN.md` §6a). Its ~48 KB of internal RAM is what made 0.3.39 abort at
boot once speech started, so `link_start` now comes up in the HTTPS fallback described below — one
session per request, one at a time under one lock — creates no socket task, and push reaches the
panel through the nudge and the poll. The socket code is compiled out, not deleted. Every request
also runs to an absolute `deadline_ms` (plan §7.5a): each wait is cut into 250 ms steps clamped to
what is left, a request can be cancelled between steps, the head is fetched in steps rather than
one long read, and a body is bounded by 20 s per network gap. The arithmetic is `wsproto.c`'s and
is host-tested in `run_link_tests`. The rest of this section is 0.3.39's design, kept for the day
the socket returns as a budgeted wave of its own.

On 0.3.38 every request — the three-second settings poll, telemetry, the jpanel poll, a talk
turn, a send — opened its own TLS session (~40 KB of internal RAM, 1-2 s of handshake), and two
of those in flight at once did not fit: after a couple of talk turns everything failed with
`connect`. Now **`main/link.c` holds one TLS WebSocket to `/api/endpoint/ws` and every request
rides it**; the box replays each one through the same route its HTTPS twin reaches, so nothing on
the box has two implementations.

- **Callers did not change shape.** `talk.c`, `jpanel.c` and `ota.c` call `link_request()` and
  block on their own task exactly where they used to block in `esp_http_client`. A streamed
  answer (a voice-post message into the speaker ring) is a sink under a 16 KB credit window, so a
  slow speaker slows the box rather than the socket everyone shares.
- **Never two TLS sessions at once** is the invariant. While the socket is reconnecting a request
  waits for it rather than opening its own session. After three failed connects (or a 404 — a box
  without the route) the panel falls back to one HTTPS session per request, one at a time, and
  retries the socket every ten minutes. The OTA download keeps its own `esp_https_ota` session
  but closes the socket first (`link_suspend`) and reopens it if the install fails.
- **`main/wsproto.c` is the pure half** — frame ids, the control frames, reassembling a frame the
  client hands over in pieces, the credit window, the fallback policy, the error string — and it
  is on the host suite.
- **Telemetry says what "connect" never did:** `ws`, `ws_err` (esp-tls code, errno, upgrade
  status), `ws_connects`, `ws_drops`, and `int_min` / `int_big`, the internal-heap low-water mark
  and largest 8-bit block. `GET /api/debug/endpoint/reach` shows them beside the box's own view
  of each socket.

The component is espressif's `esp_websocket_client`, pinned in `dependencies.lock` like the
others and fetched by the IDF component manager at build time — nothing to install by hand.
`CONFIG_ESP_WS_CLIENT_SEPARATE_TX_LOCK` is on so sends from three tasks do not queue behind the
receive loop. The design and what is still unmeasured are in ROOM_ENDPOINT_PLAN.md, "One socket
for everything".

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
# 4. rebuild and commit the browser bench, 5. republish it (below)
firmware/mock/build.sh && node firmware/mock/smoke.mjs
```

**Steps 4 and 5 keep the bench true.** See "The bench follows the firmware" below.

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

## The bench follows the firmware

`docs/mocks/jpanel-s3/` is this firmware's UI running in a browser. It is not a drawing.
`firmware/mock/build.sh` compiles the files in `main/` that decide and draw what the glass
shows into `sim.wasm`:
- `panel_sm.c`, `jpanel_core.c` and `stream.c` (the machine, the engine and the speaker);
- `cue.c`, the pet (`face.c`, `rig.c`, `emotion.c`) and `overlay.c`, `confirm.c` and `font.c`.

A change to any of them changes the bench the next time it is built. What keeps it honest:

- **CI.** `firmware.yml` rebuilds `sim.wasm` and **fails the PR if the committed one is not
  byte-for-byte what the source builds**, then runs `smoke.mjs` against it. On `main` the bench's
  source copy can never be behind the firmware.
- **The published copy is a snapshot.** https://claude.ai/artifact/TTyvginDY5RDYGQ5Xz4We8 is
  republished by hand, and nothing in CI can do it. **After any change under `main/` that
  reaches the glass, republish it** from `docs/mocks/jpanel-s3/` (index.html, panel.js,
  sim.wasm), or it shows the last version that was published.
- **One part is copied, not compiled.** `firmware/mock/sim.c` stands in for `display.c`, so it
  copies `display.c`'s input sampling, effect executor, pet animation and screen stages. Those are
  the bench's only way to drift, and CI does not catch it. **A change to those parts of
  `display.c` (or to `talk.c`'s turn) must be made in `sim.c` too.** New pixels drawn over the pet
  belong in `overlay.c`, not `display.c`, so the bench gets them for free.

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
