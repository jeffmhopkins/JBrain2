// Local read-aloud for the chat surface: when the owner has turned on the
// brain_read_aloud setting (the same switch that gates the wall display's piper
// voices), each assistant turn gets a play control next to its copy button, spoken
// on THIS device via the browser's Web Speech engine — no server round-trip, so it
// works wherever the PWA is open. The control has three states:
//   • play  — tap to speak this turn; long-press to arm auto-play
//   • pause — this turn is speaking; tap to stop
//   • auto  — auto-play is armed (long-press again to disarm)
// With auto-play armed, each new turn speaks itself as it streams in — fed sentence
// by sentence (`feed`) so it starts talking without waiting for the whole answer.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

const AUTOPLAY_KEY = "readAloudAutoPlay";

// Strip the markdown the assistant writes down to speakable prose (mirrors the wall
// display's mdToPlain): drop code, turn links into their text, and remove heading /
// quote / list markers, emphasis, and footnote chips so none are read out literally.
export function speakableText(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ") // fenced code blocks
    .replace(/`([^`]+)`/g, "$1") // inline code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ") // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") // links -> their text
    .replace(/\[\^[^\]]+\]/g, "") // footnote chips
    .replace(/^\s{0,3}(?:[>#]+|[-*+]\s|\d+\.\s)\s*/gm, "") // heading / quote / list markers
    .replace(/[*_~]/g, "") // emphasis
    .replace(/\s+/g, " ")
    .trim();
}

// Split complete sentences off the FRONT of `text` for incremental speech. A boundary
// is a . ! or ? followed by whitespace, or a newline; a trailing partial sentence is
// left behind (spoken on a later feed, or now if `flush`). Returns the chunks plus how
// many chars were consumed so a streaming caller can advance its cursor. A terminator
// with no following whitespace (e.g. "3.14") is NOT a boundary, so decimals stay whole.
export function chunkSentences(
  text: string,
  flush: boolean,
): { chunks: string[]; consumed: number } {
  const chunks: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    const term = c === "." || c === "!" || c === "?";
    const nextIsSpace = i + 1 < text.length && /\s/.test(text[i + 1] as string);
    if (c === "\n" || (term && nextIsSpace)) {
      let end = i + 1;
      while (end < text.length && /\s/.test(text[end] as string)) end++;
      const chunk = text.slice(start, end).trim();
      if (chunk) chunks.push(chunk);
      start = end;
      i = end - 1;
    }
  }
  let consumed = start;
  if (flush && start < text.length) {
    const tail = text.slice(start).trim();
    if (tail) chunks.push(tail);
    consumed = text.length;
  }
  return { chunks, consumed };
}

export interface ReadAloud {
  /** The brain_read_aloud setting is on AND this browser can speak — gates whether the
   * bubbles show a play control at all. */
  available: boolean;
  /** Key of the turn currently being spoken aloud, or null when silent — a bubble is
   * "playing" (shows pause) only when its key matches. */
  playing: string | null;
  /** Auto-play mode: new turns speak themselves as they stream in. Toggled by a
   * long-press on any play control; persisted across sessions (device-local). */
  autoPlay: boolean;
  /** Tap a turn's control: play it (markdown in; stripped to prose), or pause it if
   * it's the turn already playing. Starting a turn stops any other in flight. */
  toggle: (key: string, markdown: string) => void;
  /** Long-press a control: flip auto-play mode. */
  toggleAutoPlay: () => void;
  /** Feed the live text of a streaming turn (auto-play path): speaks any newly-complete
   * sentences and marks the turn playing. Call as text grows, then once with
   * done=true on settle to flush the tail. A no-op unless auto-play armed (the caller
   * gates on that). Never auto-starts an already-settled turn. When a NEW turn starts
   * streaming it cuts off whatever's speaking (a prior turn or a manual playback) and
   * takes over — the next turn always wins. */
  feed: (key: string, textSoFar: string, done: boolean) => void;
  /** Stop any in-flight speech at once. */
  stop: () => void;
}

const canSpeak = (): boolean => typeof window !== "undefined" && "speechSynthesis" in window;

export function useReadAloud(): ReadAloud {
  const [settingOn, setSettingOn] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  const [autoPlay, setAutoPlay] = useState<boolean>(() => {
    try {
      return localStorage.getItem(AUTOPLAY_KEY) === "1";
    } catch {
      return false;
    }
  });

  const playingRef = useRef<string | null>(null);
  // The turn currently being streamed to the engine, and how far (in stripped chars)
  // it has been dispatched — the cursor a `feed` advances so it only speaks new text.
  const feedKeyRef = useRef<string | null>(null);
  const spokenLenRef = useRef(0);
  // The utterance queue for the active turn: chunks enqueued but not yet finished, and
  // whether the turn is finalized (its end drains the queue and clears `playing`).
  const queueRef = useRef<{ key: string; pending: number; finalized: boolean }>({
    key: "",
    pending: 0,
    finalized: false,
  });
  // Turns the owner has paused — auto-play won't resume them mid-stream.
  const suppressedRef = useRef<Set<string>>(new Set());
  // A manual play is in flight — auto-play's `feed` defers so the two don't overlap.
  const manualRef = useRef(false);

  // Whether the owner enabled read-aloud at all — the same setting the wall reads.
  useEffect(() => {
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (!stale) setSettingOn(s.brain_read_aloud);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  const setPlay = useCallback((key: string | null) => {
    playingRef.current = key;
    setPlaying(key);
  }, []);

  const stop = useCallback(() => {
    manualRef.current = false;
    feedKeyRef.current = null;
    spokenLenRef.current = 0;
    queueRef.current = { key: "", pending: 0, finalized: false };
    setPlay(null);
    if (canSpeak()) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* speech engine unavailable — nothing to cancel */
      }
    }
  }, [setPlay]);

  // Enqueue one chunk for `key`, wiring its end to drain the queue — when the last
  // chunk of a finalized turn finishes, playback clears (but only while it's still the
  // active turn; a later turn replaces the queue).
  const speakChunk = useCallback(
    (key: string, text: string) => {
      const utt = new SpeechSynthesisUtterance(text);
      const settle = () => {
        const q = queueRef.current;
        if (q.key !== key) return;
        q.pending = Math.max(0, q.pending - 1);
        if (q.pending === 0 && q.finalized && playingRef.current === key) {
          manualRef.current = false;
          feedKeyRef.current = null;
          setPlay(null);
        }
      };
      utt.onend = settle;
      utt.onerror = settle;
      queueRef.current.pending += 1;
      window.speechSynthesis.speak(utt);
    },
    [setPlay],
  );

  // Start a fresh utterance queue for `key`: cancel whatever's playing (cutting off any
  // prior turn — manual or auto), reset the cursor, and mark the turn playing.
  const beginQueue = useCallback(
    (key: string) => {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* nothing to cancel */
      }
      manualRef.current = false; // this turn takes over from any manual playback
      queueRef.current = { key, pending: 0, finalized: false };
      spokenLenRef.current = 0;
      feedKeyRef.current = key;
      setPlay(key);
    },
    [setPlay],
  );

  const toggle = useCallback(
    (key: string, markdown: string) => {
      if (!canSpeak()) return;
      // Tapping the turn already playing pauses it — and suppresses it so auto-play
      // won't pick it back up if it's still streaming.
      if (playingRef.current === key) {
        suppressedRef.current.add(key);
        stop();
        return;
      }
      const text = speakableText(markdown);
      if (!text) return;
      suppressedRef.current.delete(key);
      try {
        beginQueue(key); // clears manualRef, so set it after
        manualRef.current = true;
        queueRef.current.finalized = true; // one settled utterance — done as soon as it ends
        speakChunk(key, text);
      } catch {
        stop();
      }
    },
    [beginQueue, speakChunk, stop],
  );

  const feed = useCallback(
    (key: string, textSoFar: string, done: boolean) => {
      if (!canSpeak()) return;
      if (suppressedRef.current.has(key)) return;
      if (feedKeyRef.current !== key) {
        if (done) return; // never auto-start an already-settled turn
        // A new agent turn is streaming — cut off whatever's speaking (a prior auto
        // turn OR a manual playback) and take it over. beginQueue cancels + clears
        // manualRef, so the next turn always wins over the old stream.
        beginQueue(key);
      } else if (manualRef.current) {
        return; // this turn is under manual playback — don't double-feed it
      }
      // speakableText trims the trailing space that marks a sentence boundary; restore
      // it while streaming so a delta ending in ". " speaks its sentence right away
      // rather than stalling until the next delta (or settle) arrives.
      let plain = speakableText(textSoFar);
      if (!done && /\s$/.test(textSoFar)) plain += " ";
      const pending = plain.slice(spokenLenRef.current);
      const { chunks, consumed } = chunkSentences(pending, done);
      try {
        for (const c of chunks) speakChunk(key, c);
      } catch {
        stop();
        return;
      }
      spokenLenRef.current += consumed;
      if (done) {
        queueRef.current.finalized = true;
        // Nothing left in the queue (e.g. a done with no new text) — clear now.
        if (queueRef.current.pending === 0 && playingRef.current === key) {
          feedKeyRef.current = null;
          setPlay(null);
        }
      }
    },
    [beginQueue, speakChunk, stop, setPlay],
  );

  const toggleAutoPlay = useCallback(() => {
    setAutoPlay((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(AUTOPLAY_KEY, next ? "1" : "0");
      } catch {
        /* private mode — the mode just won't persist */
      }
      return next;
    });
  }, []);

  const available = settingOn && canSpeak();
  // Disabling the whole feature stops any in-flight speech immediately.
  useEffect(() => {
    if (!available) stop();
  }, [available, stop]);
  // Leaving the surface (unmount) stops speech too — "off mid-stream stops it".
  useEffect(() => stop, [stop]);

  return { available, playing, autoPlay, toggle, toggleAutoPlay, feed, stop };
}
