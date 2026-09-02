// The radio's audio element, owned by the LEASE and parked in the document for its
// whole life.
//
// Two earlier shapes of this were wrong, in the same direction. First the element
// lived inside SdrTunerSheet and was destroyed on unmount, so sound existed only
// while the owner had the controls open. Then it lived here but was LENT to the
// sheet — appended while mounted, removed on unmount — on the assumption that a
// media element with a live JS reference goes on playing once detached.
//
// It does not. The HTML spec is explicit: when a media element is removed from a
// document, the user agent runs the internal pause steps. Measured in Chromium,
// `paused` flips false -> true on the very next tick after `.remove()`. jsdom does
// not implement that step, which is why a passing unit test said otherwise.
//
// So the element is appended to <body> once and never moved. The sheet does not
// borrow it; it renders its own transport and drives it through `toggleSdrAudio`.
// Nothing about playback depends on any component being mounted.
//
// A module store rather than React context, matching sdrSession.ts: this codebase
// has no context anywhere, and audio that must outlive the tree cannot live in it.

// The proxied live stream. Same-origin and owner-session-authed; the sidecar itself
// sits on an internal network the browser has no route to.
export const SDR_AUDIO_SRC = "/api/sdr/audio";

type Listener = (playing: boolean) => void;

let element: HTMLAudioElement | null = null;
const listeners = new Set<Listener>();

// The Web Audio tap the tuner's tape display reads. Kept here rather than in the
// component because `createMediaElementSource` may be called only ONCE per element,
// and this module owns the only element there is.
let audioCtx: AudioContext | null = null;
let analyserNode: AnalyserNode | null = null;
let tapped = false;
let armed = false;

// The tape's history, kept HERE rather than in the component that draws it, because
// the owner wants to open the tuner and see what already happened — not start a
// recording by looking at it. Sampling therefore runs for as long as the radio is
// playing, and the sheet only ever reads this buffer.
//
// A timer rather than an animation frame: rAF is throttled hard in a background tab
// and stops entirely when nothing is being painted, which is exactly the case this
// buffer exists to cover. 20 Hz over 12 s is 240 columns — more than a phone-width
// canvas can show — and costs one array pass per 50ms.
const SAMPLE_HZ = 20;
export const TAPE_WINDOW_S = 12;
const TAPE_LEN = SAMPLE_HZ * TAPE_WINDOW_S;
// Demodulated speech sits low in the range; this lifts a normal signal to most of the
// height without flattening a loud one against the top.
const TAPE_GAIN = 2.6;

const levels = new Float32Array(TAPE_LEN);
let levelAt = 0;
let sampler: ReturnType<typeof setInterval> | null = null;
let samples: Uint8Array<ArrayBuffer> | null = null;

/** The rolling level history: oldest-to-newest is `at` forward, wrapping. */
export function sdrLevels(): { levels: Float32Array; at: number; length: number } {
  return { levels, at: levelAt, length: TAPE_LEN };
}

function sample(): void {
  const node = analyserNode;
  if (!node || !isSdrPlaying()) return;
  if (!samples || samples.length !== node.fftSize) {
    samples = new Uint8Array(new ArrayBuffer(node.fftSize));
  }
  node.getByteTimeDomainData(samples);
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    const centred = ((samples[i] ?? 128) - 128) / 128;
    sum += centred * centred;
  }
  levels[levelAt] = Math.min(1, Math.sqrt(sum / samples.length) * TAPE_GAIN);
  levelAt = (levelAt + 1) % TAPE_LEN;
}

function startSampling(): void {
  if (sampler !== null || typeof setInterval === "undefined") return;
  sampler = setInterval(sample, 1000 / SAMPLE_HZ);
}

function stopSampling(): void {
  // Clearing is NOT conditional on a timer having run. Returning early when there was
  // no sampler left the previous station's audio in the buffer, so the next session
  // opened its tuner showing history that belonged to a different frequency.
  if (sampler !== null) {
    clearInterval(sampler);
    sampler = null;
  }
  levels.fill(0);
  levelAt = 0;
}

function announce(): void {
  const playing = isSdrPlaying();
  for (const listener of listeners) listener(playing);
}

/** The element, made and parked in <body> on first use. Null when there is no DOM. */
function ensure(): HTMLAudioElement | null {
  if (element) return element;
  if (typeof document?.body === "undefined" || !document.body) return null;
  const el = document.createElement("audio");
  // No `controls`: a live stream has no timeline, so the native transport offers a
  // scrubber over nothing and reads 0:00 / 0:00 forever. The sheet draws play/pause.
  el.className = "sdr-audio";
  el.preload = "none";
  // Present but not seen. Removing it from the document would pause it (see above),
  // so it is hidden rather than detached.
  el.setAttribute("aria-hidden", "true");
  el.style.display = "none";
  el.addEventListener("play", announce);
  el.addEventListener("pause", announce);
  document.body.appendChild(el);
  element = el;
  return el;
}

function attempt(el: HTMLAudioElement): void {
  // play() returns a Promise in modern browsers but `undefined` in older ones (and in
  // jsdom), so it cannot be chained blind — and it can throw outright rather than
  // reject. Both are contained because callers include the session store's publish():
  // an exception escaping would stop the poll notifying its listeners and freeze the
  // composer icon on a stale reading. Autoplay refusal is normal besides; the sheet's
  // play button is how the owner recovers from it.
  try {
    const started: unknown = el.play();
    if (started instanceof Promise) started.catch(announce);
  } catch {
    announce();
  }
}

/** Point the element at the live stream and start it. Idempotent per session. */
export function playSdrAudio(): void {
  const el = ensure();
  if (!el) return;
  // Re-pointing at the same src would restart the stream mid-listen, so only set it
  // when it is not already ours. The browser resolves src to an absolute URL, hence
  // the suffix test rather than equality.
  if (!el.src.endsWith(SDR_AUDIO_SRC)) el.src = SDR_AUDIO_SRC;
  if (el.paused) attempt(el);
  armAnalyser();
  if (analyserNode) startSampling();
}

/** Release the stream. Called when the lease ends, never when the sheet closes. */
export function stopSdrAudio(): void {
  if (!element) return;
  try {
    element.pause();
    element.removeAttribute("src");
    element.load(); // drop the connection rather than leave it streaming unheard
  } catch {
    // best effort; the element is being discarded either way
  }
  element.remove();
  element = null;
  stopSampling();
  announce();
}

/** The sheet's play/pause. Resuming re-points at the stream to rejoin it live. */
export function toggleSdrAudio(): void {
  const el = ensure();
  if (!el) return;
  if (el.paused) {
    playSdrAudio();
    return;
  }
  el.pause();
  // Drop the connection while paused. This is live radio: resuming should rejoin the
  // broadcast as it is NOW, not play out however many minutes queued up while the
  // owner was not listening. `playSdrAudio` re-points at the src to reconnect.
  el.removeAttribute("src");
  announce();
}

/** Whether sound is actually coming out, for the transport to reflect. */
export function isSdrPlaying(): boolean {
  return element !== null && !element.paused && element.hasAttribute("src");
}

/** Subscribe to play/pause; returns an unsubscribe. */
export function subscribeSdrAudio(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The analyser tapping the live stream, or null if one cannot safely be made.
 *
 * Routing the element through an AudioContext is a ONE-WAY door: once
 * `createMediaElementSource` has taken it, its output only reaches the speakers
 * through the graph, and a context that is not running means silence. Autoplay
 * policy suspends a context created outside a user gesture, so tapping the element
 * before checking would trade a working radio for a picture of one — the same
 * failure this file has already been fixed for twice, in a new costume.
 *
 * So the order is load-bearing: make the context, get it running, and only THEN
 * take the element. Called from a real tap (opening the tuner, or the play button),
 * so by the time it matters the gesture has happened. Until it succeeds the radio
 * plays untouched and the caller simply has no data to draw.
 *
 * This also depends on the stream being SAME-ORIGIN, which `/api/sdr/audio` is
 * because it proxies rather than redirects. Cross-origin media taints the graph and
 * the analyser reads pure silence while the audio plays on perfectly — measured, and
 * a very quiet way to lose the display. If that proxy is ever replaced by a redirect
 * to the sidecar, this stops returning data and nothing else breaks to say so.
 */
export function sdrAnalyser(): AnalyserNode | null {
  if (analyserNode) return analyserNode;
  const el = element;
  if (!el || tapped) return analyserNode;
  const Ctor: typeof AudioContext | undefined =
    typeof AudioContext !== "undefined"
      ? AudioContext
      : (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    if (!audioCtx) audioCtx = new Ctor();
    if (audioCtx.state === "suspended") void audioCtx.resume();
    // Still not running — do NOT take the element. Try again on the next tap.
    if (audioCtx.state !== "running") return null;
    const source = audioCtx.createMediaElementSource(el);
    tapped = true; // whatever happens next, this element can never be tapped again
    const node = audioCtx.createAnalyser();
    node.fftSize = 2048;
    node.smoothingTimeConstant = 0.72;
    source.connect(node);
    // Without this the graph is a dead end and the radio goes silent.
    node.connect(audioCtx.destination);
    analyserNode = node;
    startSampling();
    return node;
  } catch {
    return null; // no visualiser; the radio is unaffected
  }
}

/**
 * Try to take the tap at the next touch anywhere in the app.
 *
 * Without this the analyser is only built when the tuner sheet opens, so the FIRST
 * open after a session starts always shows an empty tape — the one time the owner is
 * most likely to be looking for what they just missed. Any tap will do, and the
 * listener removes itself once the tap lands.
 */
function armAnalyser(): void {
  if (armed || tapped || typeof document === "undefined") return;
  armed = true;
  const attempt = () => {
    if (sdrAnalyser()) {
      document.removeEventListener("pointerdown", attempt, true);
      document.removeEventListener("keydown", attempt, true);
    }
  };
  document.addEventListener("pointerdown", attempt, true);
  document.addEventListener("keydown", attempt, true);
}

/** Test seam: drop the element so each test starts from nothing. */
export function resetSdrAudio(): void {
  element?.remove();
  element = null;
  listeners.clear();
  // Guarded: a reset that throws leaves the tap half-torn-down, and every later
  // caller inherits the wreckage.
  try {
    void audioCtx?.close()?.catch(() => {});
  } catch {
    // already closed, or never a real context
  }
  audioCtx = null;
  analyserNode = null;
  tapped = false;
  armed = false;
  stopSampling();
  samples = null;
}
