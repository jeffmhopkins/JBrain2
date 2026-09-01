// The radio's audio element, owned by the LEASE rather than by the tuner sheet.
//
// It used to live inside SdrTunerSheet, which meant closing the sheet tore the
// element down and silenced the radio: sound only existed while the owner was
// looking at the controls. That is backwards. The session holds the tuner whether or
// not the sheet is open — the composer icon says so — so the audio belongs to the
// same lifetime, and the sheet is only somewhere to SHOW the transport.
//
// The trick that makes this cheap: a media element removed from the document keeps
// playing as long as a reference survives. So one element lives here for the life of
// the lease, and the sheet borrows it into its own slot while it is mounted (see
// `attach`). Moving it in the DOM does not restart it, so opening and closing the
// sheet is silent to the listener — and the native transport controls still work,
// because it really is the same element.
//
// A module store rather than React context, matching sdrSession.ts: this codebase
// has no context anywhere and a stream that survives unmounting cannot live in the
// tree that unmounts it.

// The proxied live stream. Same-origin and owner-session-authed; the sidecar itself
// sits on an internal network the browser has no route to.
export const SDR_AUDIO_SRC = "/api/sdr/audio";

let element: HTMLAudioElement | null = null;

/** The element, made on first use. Null when the DOM is not available (SSR, tests). */
function ensure(): HTMLAudioElement | null {
  if (element) return element;
  if (typeof document === "undefined") return null;
  element = document.createElement("audio");
  element.controls = true;
  element.className = "sdr-audio";
  element.preload = "none";
  return element;
}

/** Point the element at the live stream and start it. Idempotent per session. */
export function playSdrAudio(): void {
  const el = ensure();
  if (!el) return;
  // Re-pointing at the same src would restart the stream mid-listen, so only set it
  // when it is not already ours. The browser resolves src to an absolute URL, hence
  // the suffix test rather than equality.
  if (!el.src.endsWith(SDR_AUDIO_SRC)) el.src = SDR_AUDIO_SRC;
  if (!el.paused) return;
  // play() returns a Promise in modern browsers but `undefined` in older ones (and
  // in jsdom), so it cannot be chained blind — and it can throw outright rather than
  // reject. Both are contained here because the caller is the session store's
  // publish(): an exception escaping this would stop the poll notifying its
  // listeners, and the composer icon would freeze on a stale reading. Autoplay
  // refusal is normal besides, and not worth surfacing — the transport is right
  // there in the sheet.
  try {
    const started: unknown = el.play();
    if (started instanceof Promise) started.catch(() => {});
  } catch {
    // no sound this time; the owner can start it from the transport
  }
}

/** Release the stream. Called when the lease ends, never when the sheet closes. */
export function stopSdrAudio(): void {
  if (!element) return;
  // Contained for the same reason as playSdrAudio: this runs inside the session
  // store's publish(), and a throw here would strand every listener.
  try {
    element.pause();
    element.removeAttribute("src");
    element.load(); // drop the connection rather than leave it streaming unheard
  } catch {
    // best effort; the element is going out of the document either way
  }
  element.remove();
}

/**
 * Lend the element to a container for as long as that container is mounted.
 * Returns the undo, which puts it back in no document at all — where it keeps
 * playing, because that is the whole point.
 */
export function attachSdrAudio(host: HTMLElement | null): () => void {
  const el = ensure();
  if (!el || !host) return () => {};
  host.appendChild(el);
  return () => {
    el.remove();
  };
}

/** Test seam: drop the element so each test starts from nothing. */
export function resetSdrAudio(): void {
  element?.remove();
  element = null;
}
