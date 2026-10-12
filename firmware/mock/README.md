# firmware/mock — the panel's UI, built for a browser

`build.sh` compiles the twins' panel firmware's pure-C UI to WebAssembly, as
`docs/mocks/jpanel-s3/sim.wasm`. That page's README says what the bench is for. This file covers
how it is built.

- **`sim.c`** is the harness. It includes `../host/jpanel_tests.c` whole, as `e2e_tests.c` does,
  for the scripted box, its fake link and its clock. It then wires the real machine, engine and
  arbiter together the way `display.c` does, draws the pet and `overlay_draw` into a 368×448
  framebuffer, and exports the page's entry points (`sim_touch`, `sim_message`, `sim_set`, …).
- **Time** is the scripted box's clock. Every wait in the engine and the box goes through
  `advance()`, which ticks the arbiter and runs one render frame every 40 ms of fake time. Each
  frame ends in `js_frame`. Asyncify (binaryen's `wasm-opt --asyncify`) turns that into a
  suspension, so `panel.js` can run the never-returning C loop up to a time and take the thread
  back.
- **Sound** is the arbiter's codec output (`g_codec` in `jpanel_tests.c`, NULL in the suites),
  so cue timing, ducking and the slot rules are the panel's own. The box's message audio is a
  synthetic voice (`JP_MOCK_VOICE`) instead of the suites' ramp.

The toolchain is the system clang, which needs the wasm32 target. CI pins `clang-18`. Everything
else is fetched on the first run into `~/.cache/jbrain-wasm`, sha256-checked:

- wasi-sdk 24's sysroot and compiler-rt builtins;
- binaryen 123 from npm.

The build is reproducible: no paths or timestamps get into the module. That is why CI can
byte-compare the committed `sim.wasm` against a fresh build.

```sh
firmware/mock/build.sh [out.wasm]   # default: docs/mocks/jpanel-s3/sim.wasm
node firmware/mock/smoke.mjs [out.wasm]
```

`smoke.mjs` drives the built module headless through a message from Dad (notice, play, heard,
AGAIN/REPLY), a reply recorded and sent, and a hold that opens the who-menu. It fails on any
invariant the machine reports broken.
