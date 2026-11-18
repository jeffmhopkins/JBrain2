# GrowBot vision — Dave's depth sense

> **Status:** In progress · **Last verified:** 2026-11-18 · **Waves:** V1✅ V2◻️ V3◻️ V4◻️ V5◻️

The owner's ask (2026-11-18): "Are there phone based vision models that could do a middle layer
of processing that would identify walls and objects etc in real time that could help?" and "how
to abstract that for the sim is important too". The exploring runs in the sim house showed why
it matters:
- Dave walks into walls without noticing.
- He stops about 80 cm short of the piano because it fills his low camera's view.
- The camera check for "am I stuck" fails on the sim's blank walls.

The phone is a Pixel 10. The creature is a web page in Chrome on it, and Qwen on the box is the
brain.

## What the research settled

- **ARCore is out.** Its depth, plane finding and motion tracking run on the world-facing (rear)
  camera only; the selfie camera gets face tracking alone. Dave sees through the front camera,
  and an `immersive-ar` WebXR session would also take over the screen his face is drawn on.
- **The Tensor G5's TPU is not reachable from a web page**, and nor is ML Kit or LiteRT's NPU
  path, which are native. The page can run models on the GPU through WebGPU (ONNX Runtime Web,
  LiteRT.js) or on the CPU through WebAssembly. The page already ships ONNX Runtime Web (wasm)
  and MediaPipe tasks-vision.
- **The Pixel 10's GPU is new to Android: Imagination's PowerVR DXT-48**, not the Mali of earlier
  Pixels. Early drivers left it clocked low. WebGPU on it is unproven, so the wasm path has to
  be good enough on its own. At his walking pace of about 4 cm a second, one frame a second is
  enough.
- **His camera never sees the floor near him.** In the sim the lens is about 10 cm up and looks
  30 to 47 degrees up, with a 75 degree tall view, so the bottom of the picture is at or above
  the horizon. The common "find where the floor ends" approach cannot work. What can work is
  each pixel's place in the room: from its depth, the camera's pitch and the lens height, how
  far it is across the floor, which way and how high. Anything under half a metre is something
  his legs would walk into.
- **Candidate models:**
  - Depth Anything V2 Small (25M parameters, ONNX for Transformers.js, a metric indoor variant
    trained on Hypersim) for depth.
  - SegFormer-B0 trained on ADE20K (3.7M parameters) for labels. Its 150 classes include wall,
    floor, door, sofa, lamp, plant, refrigerator, person and animal, but no piano.
  - COCO detectors (MediaPipe's EfficientDet-Lite) know no walls or doors, so they add little.
  - No published phone frame rates exist for these models, so wave V2 measures them.

## The design: one interface, two sources

```
 phone: camera ─► depth model + segmentation model ─┐
                                                     ├─► maps ─► reduce() ─► scene ─► uses
 sim:   MuJoCo depth + geom ids (+ phone-like noise) ┘
```

**Maps** are the seam. A source hands `gbVision.feed()` one camera frame's depth map (metres
along the camera's axis, per pixel) and label map (an index into the page's `VOCAB`, per pixel),
with the camera's pitch and lens height. Each source maps its own classes onto the vocabulary:
ADE20K's on the phone, geom names in the sim. Everything after the maps is one file,
`growbot/patches/growbot-vision.js`, in both. So a sim run tests the real logic, and the sim's
blank walls stop mattering.

**A scene** is what `reduce()` makes of one frame:
- Nine directions across his view, each with how far the nearest thing at his height is, what
  it is, or how far the picture can tell it is clear.
- The openings among them: a run of directions much further than what flanks it.
- The labelled things in view, with bearing and distance.

Bearings are degrees, positive to his left, as everywhere on the page.

**Uses**, each live only while a source has fed a fresh frame:
1. One line in each turn on legs: left, ahead and right, the open ways and the things in view.
2. Pushing: a forward walk with something under 0.7 m ahead that gets no closer in six seconds
   (or touching it) stops the walk, beside the grey-thumbnail check that the blank walls defeat.
3. A way (`explore:{go}`) or a round of the walls does not start a walk into something seen
   within 0.3 m. A round turns away from it as if it had bumped.
4. The view strip's text says how far each of the eight ways is clear, from the directions he
   looked along from that spot.
5. `map:{saw}` with no distance takes the depth sense's distance in that direction, instead of
   the 1.5 m default. That default is how the piano ended up 80 cm off.

The sim's choice of source is part of the interface. `--vision off` matches today's phone,
`truth` sends the maps as rendered, and `noisy` adds a phone model's errors: a whole-frame scale
that wanders, per-pixel jitter, holes and lost labels.

## Waves

### V1 ✅ — the layer, the sim's source, the uses (2026-11-18)

`growbot-vision.js` holds the vocabulary, `reduce()`, the openings, the pushing test, the line,
and the page half: `feed`, `fresh`, `take`, `aheadNow`, `distNow` and `isPushing`. The uses are
wired into `growbot-pano.js` and `growbot-map.js`. `growbot/sim/dave_sim/vision.py` renders the
maps at 48x64 about three times a second, and `--vision` picks the source.

Against rays cast in the sim house from eight poses, the per-direction distances came within a
few centimetres of the truth. The labels came out right too: sofa, shelf, counter, plant, person
and the cat. The A/B results on wall-following are in the PR that landed V1.

### V2 — measure the models on the Pixel 10

A bench page beside the camera test (`camtest.html`) runs each candidate on the front camera's
frames and records what it costs and how right it is:
- Models: Depth Anything V2 Small metric-indoor and SegFormer-B0 ADE20K.
- Precision: fp16 and int8.
- Input size: 128, 192 and 256 px.
- Backend: WebGPU and wasm.

It records milliseconds per frame, heat, and battery per minute. Distances to a wall and to the
couch are checked against a tape measure at 0.2, 0.5, 1 and 2 m, from Dave's own height and
pitch. That last check is the real question: these models learned from eye-level pictures, and
his view is from 10 cm off the floor looking up.

To pass, depth and labels each need one frame a second or better on wasm, and distance needs to
be within 15% from 0.2 to 2 m. If a model fails, the fallback is sending a small frame to the box
and running the model on its GPU, at the cost of a LAN round trip.

### V3 — the phone's source

A worker runs the models V2 picks on the creature's open camera, on legs only, and only while he
walks or explores, to save battery. It feeds `gbVision` maps with the pitch from the phone's
gravity reading and the lens height from his stance. ADE20K's classes map onto `VOCAB`, and the
classes that are not things go to "unknown". A per-phone switch turns it on, and the PWA's Body
tab shows the depth sense's view for a dry run.

### V4 — the models in the sim

An option runs V3's phone source on the sim's rendered pictures in headless Chromium, in place of
the true maps. That measures how far the models' errors move the scores. It needs textured walls
in `house.py`, or the blank walls the depth sense was meant to get past come back.

### V5 — lean on it

- A round of the walls keeps the wall at a set distance by the side directions, instead of
  probing into it and bumping.
- A doorway the depth sense sees goes on his map by itself.
- "Am I stuck" no longer needs the grey-thumbnail check.

## Risks

- WebGPU on the PowerVR GPU may be missing or slow. Wasm at small sizes is the floor, and the box
  is the fallback.
- His low, upturned view is unlike the models' training pictures. V2 measures it before anything
  leans on it.
- The models get mirrors, glass and dark rooms wrong. Pushing still needs a reading that stops
  changing, so a wrong distance does not stop him by itself.
- Battery and heat on a phone that already runs the face tracker. V3 runs it only while he moves.

## Sources

- [Pixel 10 GPU: PowerVR DXT-48 and its drivers (Android Authority)](https://www.androidauthority.com/google-pixel-10-tensor-g5-benchmarks-3590355/)
- [Google on Pixel 10 GPU updates (9to5Google)](https://9to5google.com/2025/10/23/google-says-pixel-10-will-get-gpu-updates-soon/)
- [Depth Anything V2 Small for Transformers.js (ONNX)](https://huggingface.co/onnx-community/depth-anything-v2-small)
- [WebXR in Chrome vs ARCore features](https://developers.google.com/ar/develop/webxr/arcore-comparison)
- [WebXR depth sensing in Chrome (blink-dev)](https://groups.google.com/a/chromium.org/g/blink-dev/c/6z48GgKC95I)
