// Browser voice for JPet (docs/plans/JPET_PLAN.md W6) — Web Speech, isolated behind
// this module so the screens stay testable (jsdom has neither API; tests vi.mock this,
// like petScene/leafletMap). Everything is guarded: on a browser without the API the
// functions no-op / report unavailable, never throw.

// Minimal shapes for the non-standard SpeechRecognition API (not in lib.dom).
interface SpeechRecognitionResultLike {
  0: { transcript: string };
}
interface SpeechRecognitionEventLike {
  results: { 0: SpeechRecognitionResultLike };
}
interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}
type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function sttAvailable(): boolean {
  return recognitionCtor() !== null;
}

export interface Listening {
  stop: () => void;
}

/** Listen for one spoken phrase; `onText` fires with the transcript. Returns a handle
 *  to stop early, or null if the browser has no speech recognition. */
export function listenOnce(onText: (text: string) => void, onDone?: () => void): Listening | null {
  const Ctor = recognitionCtor();
  if (!Ctor) return null;
  const rec = new Ctor();
  rec.lang = "en-US";
  rec.interimResults = false;
  rec.onresult = (e) => {
    const transcript = e.results[0][0].transcript;
    if (transcript) onText(transcript);
  };
  rec.onend = () => onDone?.();
  rec.start();
  return { stop: () => rec.stop() };
}

/** Speak `text` in a bright, pet-ish voice. No-op without speech synthesis. */
export function speak(text: string): void {
  const synth = window.speechSynthesis;
  if (!synth || !text) return;
  const u = new SpeechSynthesisUtterance(text);
  u.pitch = 1.6; // high + quick → cute/toy-like
  u.rate = 1.1;
  synth.cancel();
  synth.speak(u);
}

export function ttsAvailable(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}
