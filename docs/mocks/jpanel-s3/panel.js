// The twins' panel firmware, compiled to WebAssembly (`firmware/mock/`), and the loop that runs it.
//
// The C side never returns: the engine's loop advances the scripted box's clock 40 ms at a time
// and calls `js_frame` once per render frame. Asyncify turns that call into a suspension point, so
// `runTo(t)` runs the panel until its clock reaches `t` and then hands the thread back. The page
// calls it from requestAnimationFrame with the wall clock; the smoke test calls it directly.

export const VOICE = { WAKE: 1, STOP: 2, SEND_SISTER: 3, SEND_DAD: 4 };
export const KNOB = {
  OFFLINE: 0, LATENCY: 1, UPLOAD_BPS: 2, TRANSCRIBE_MS: 3, NEXT_SEND_STATUS: 4,
  SPEECH_OK: 5, TALK_OUTCOME: 6, TALK_MS: 7, NEW_BOX: 8,
};

export async function createPanel(bytes, hooks = {}) {
  let ex = null;
  let rewinding = false;
  let started = false;
  let stopAt = 0;
  let simMs = 0;

  const mem = () => ex.memory.buffer;
  const cstr = (p) => {
    const b = new Uint8Array(mem(), p);
    let n = 0;
    while (b[n] !== 0) n++;
    return new TextDecoder().decode(b.subarray(0, n));
  };

  const imports = {
    env: {
      js_frame(step) {
        if (rewinding) {
          ex.asyncify_stop_rewind();
          rewinding = false;
          return;
        }
        simMs += step;
        if (hooks.onFrame) hooks.onFrame(simMs);
        if (simMs >= stopAt) {
          const at = ex.sim_async();
          const v = new Int32Array(mem());
          v[at >> 2] = at + 8;
          v[(at + 4) >> 2] = at + ex.sim_async_size();
          ex.asyncify_start_unwind(at);
        }
      },
      js_audio(ptr, n) {
        if (hooks.onAudio) hooks.onAudio(new Int16Array(mem(), ptr, n).slice());
      },
      js_event(ptr) {
        if (hooks.onEvent) hooks.onEvent(cstr(ptr));
      },
    },
    // printf's plumbing, reached only by a stray diagnostic: swallow it.
    wasi_snapshot_preview1: {
      fd_write: (fd, iov, n, written) => {
        const v = new Uint32Array(mem());
        let total = 0;
        for (let i = 0; i < n; i++) total += v[(iov >> 2) + i * 2 + 1];
        v[written >> 2] = total;
        return 0;
      },
      fd_close: () => 0,
      fd_seek: () => 0,
    },
  };

  const { instance } = await WebAssembly.instantiate(bytes, imports);
  ex = instance.exports;
  ex._initialize();
  ex.sim_init();

  const W = ex.sim_w();
  const H = ex.sim_h();

  function runTo(t) {
    stopAt = t;
    if (!started) {
      started = true;
    } else {
      ex.asyncify_start_rewind(ex.sim_async());
      rewinding = true;
    }
    ex.sim_run();
    ex.asyncify_stop_unwind();
  }

  // RGB565, byte-swapped for the panel's QSPI order, into RGBA.
  function paint(rgba) {
    const fb = new Uint16Array(mem(), ex.sim_fb(), W * H);
    for (let i = 0, o = 0; i < fb.length; i++, o += 4) {
      const raw = fb[i];
      const c = ((raw >> 8) | (raw << 8)) & 0xffff;
      rgba[o] = ((c >> 11) & 31) * 255 / 31;
      rgba[o + 1] = ((c >> 5) & 63) * 255 / 63;
      rgba[o + 2] = (c & 31) * 255 / 31;
      rgba[o + 3] = 255;
    }
  }

  return {
    W,
    H,
    get now() { return simMs; },
    runTo,
    advance: (ms) => runTo(simMs + ms),
    paint,
    state: () => JSON.parse(cstr(ex.sim_state())),
    touch: (kind, x, y) => ex.sim_touch(kind, x | 0, y | 0),
    voice: (v) => ex.sim_voice(v),
    hearing: (on) => ex.sim_hearing(on ? 1 : 0),
    button: (hold) => ex.sim_button(hold ? 1 : 0),
    message: (fromDad, ms) => ex.sim_message(fromDad ? 1 : 0, ms | 0),
    set: (knob, value) => ex.sim_set(knob, value | 0),
    target: (x, y) => ex.sim_target(x | 0, y | 0),
    targetName: (t) => cstr(ex.sim_target_name(t)),
  };
}
