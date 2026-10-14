# The twins' panels (ESP32-S3-Touch-AMOLED-1.8): the bench

Kind: Living (mock set) · Status: **interactive; runs firmware 0.4.13's own UI code** · Last verified: 2026-10-14

An interactive mock of the twins' panel, with one difference from Jeff's C6 set
(`../jpanel-c6/`): nothing here is a drawing of the panel. `sim.wasm` is the firmware's own UI
code, compiled for the browser by `firmware/mock/build.sh`. That covers the state machine
(`panel_sm.c`), the voice-post engine (`jpanel_core.c`), the speaker arbiter (`stream.c`), the
cues (`cue.c`), the pet (`face.c`, `rig.c`, `emotion.c`) and every overlay (`overlay.c`,
`confirm.c`, `font.c`). They run against the scripted box the host suites use
(`firmware/host/jpanel_tests.c`). So a press, a hold, a swipe or a message arriving does on this
page what it does on the glass, and the sounds are the panel's codec samples.

- `index.html` is the page. It shows the panel in its case, with the box, voice, pet-turn and
  speed controls beside it. Below that are the machine's view flags, live, and a log of every
  cue, effect, mode change and request.
- `panel.js` is the loader. It drives the Asyncify loop, so the C program can stop every 40 ms
  frame and give the browser its turn.
- `sim.wasm` is built output, committed. `firmware.yml` rebuilds it and fails the PR when it is
  not byte-for-byte what the source builds.

Interactive copy: https://claude.ai/artifact/TTyvginDY5RDYGQ5Xz4We8. It is republished by hand
after a UI change, from these three files. Locally, the page fetches `sim.wasm`, so serve the
directory rather than opening the file:

```sh
firmware/mock/build.sh                       # after any change under firmware/main
(cd docs/mocks/jpanel-s3 && python3 -m http.server 8000)   # then open localhost:8000
node firmware/mock/smoke.mjs                 # the headless run CI does
```

## What it is good for

- **Messaging flows end to end.** Send a message, then tap the notice. Let it play, stop it
  early, or swipe the queue. Then AGAIN, REPLY, and the recording and its outcome. Turn the box
  off, or make the next send come back NOBODY (409, as the box answers it) or an error.
- **Touch targets.** "Show touch targets" paints the machine's own hit test (`sm_target`) over the
  glass at 8 px resolution, so a control drawn where its target is not (invariant I3) shows up
  at a glance.
- **Timing.** 10× runs the waits the owner set: the notice shrinking to its badge (15 s), the
  again/reply pair standing (10 s) and the who-menu timing out (10 s). It does this without
  sitting through them.
- **Tilt.** The bench's slider turns the drawn case right side down, and the pet leans and
  rights his head as the accelerometer would make him. Upright only: on its side the panel also
  slides the figure's square into the glass's side bands, which the page does not draw.
- **Shake, love, tickle and a snack.** "Shake the panel" shakes the drawn case and starts the
  dizzy action the panel's shake detector starts; the "I love you" voice button starts the
  hearts, "I'm going to tickle you" the warning (then poke him five times: he laughs, farts and sits dazed) and "Have a snack" a snack (three snacks inside a
  minute and he poops; tilt the panel to slide it); "Fart (the long one)" forces the
  one-in-ten seven-second fart. The bench does not run the shake detector itself.
- **F4.** The SENDING / SENT / NOBODY / NOT SENT / DROPPED visuals are 0.4.x placeholders until
  the owner approves the final ones (`docs/plans/JPANEL_MESSAGING_FIXES_PLAN.md` §15 row F).
  This page is where they get reviewed. Each new version is in `overlay.c`, so the mock and
  the panel cannot diverge.

## Where it can be wrong

`firmware/mock/sim.c` is the only part that is not firmware, and its header lists what it fakes:

- the render frame's input sampling and the executor (copies of `display.c`'s);
- the pet's talk turn (a timer standing in for `talk.c`'s request);
- the recogniser (the voice buttons stand in for the phrases). The voice detector hears her
  for a set time each time a microphone opens (2.5 s by default), and while "Hold while she
  talks" or Space is held. With "she says nothing", a recording is dropped after 4 s, as on
  the glass;
- the screen's dim and dark stages;
- the accelerometer, which is the tilt slider (the lean's arithmetic is copied from `display.c`;
  the axes and the head's righting are `orient.c`'s own).

It does not model the quarter turns, calibration, the maintenance gestures, voice actions or colours.
Names are placeholders (this panel is QUORRA and its twin is Tamsin), never a child's real name.
