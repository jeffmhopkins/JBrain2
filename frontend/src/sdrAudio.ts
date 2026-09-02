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

/** Test seam: drop the element so each test starts from nothing. */
export function resetSdrAudio(): void {
  element?.remove();
  element = null;
  listeners.clear();
}
