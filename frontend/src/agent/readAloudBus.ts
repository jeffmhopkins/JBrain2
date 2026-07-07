// A tiny in-app bus for live read-aloud setting changes.
//
// The chat read-aloud hook (useReadAloud) lives inside HomeScreen, which App.tsx keeps mounted
// for the whole session (so the stream's scroll position survives sub-screens). Because it never
// unmounts, its mount-time settings fetch can't refresh when the Settings OVERLAY changes the
// read-aloud voice / engine / on-off — the hook would keep speaking every agent turn in the
// app-load voice. This bus carries a saved change straight from the Settings writer to the
// mounted hook. Module-scoped EventTarget (not window) so it's isolated and easy to test.

export interface ReadAloudPatch {
  brain_read_aloud?: boolean;
  brain_answer_voice?: string;
  brain_read_aloud_engine?: "piper" | "native";
}

const bus = new EventTarget();
const EVENT = "change";

/** Announce a read-aloud setting the owner just saved, so the mounted chat hook applies it now. */
export function emitReadAloudSettings(patch: ReadAloudPatch): void {
  bus.dispatchEvent(new CustomEvent<ReadAloudPatch>(EVENT, { detail: patch }));
}

/** Subscribe to read-aloud setting changes; returns an unsubscribe. */
export function onReadAloudSettings(handler: (patch: ReadAloudPatch) => void): () => void {
  const listener = (e: Event) => handler((e as CustomEvent<ReadAloudPatch>).detail);
  bus.addEventListener(EVENT, listener);
  return () => bus.removeEventListener(EVENT, listener);
}
