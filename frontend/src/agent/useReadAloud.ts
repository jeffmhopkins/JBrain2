// In-chat read-aloud for the chat surface: when the owner has turned on the
// brain_read_aloud setting (the same switch that gates the wall display's piper
// voices), each assistant turn gets a play control next to its copy button. The control
// has three states:
//   • play  — tap to speak this turn; long-press to arm auto-play
//   • pause — this turn is speaking; tap to stop
//   • auto  — auto-play is armed (long-press again to disarm)
// With auto-play armed, each new turn speaks itself as it streams in — fed sentence by
// sentence (`feed`) so it starts talking without waiting for the whole answer.
//
// Two engines, chosen by the brain_read_aloud_engine setting:
//   • "piper" (default): the box renders each sentence in the chosen voice
//     (brain_answer_voice) and the audio streams back over the owner's authenticated api
//     session (GET /api/brain/tts) — the same voice the wall uses — played back-to-back.
//     If the box can't render, it falls back to the device's native voice.
//   • "native": the browser's own Web Speech voice — no box needed.
// The engine is swapped under the same queue, so the three-state control, auto-play, and
// streaming behave identically either way.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type ReadAloudEngine = "piper" | "native";

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
  /** Read-aloud is on AND an engine can speak (the device's native voice, or piper
   * voices on the box) — gates whether the bubbles show a play control at all. */
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

const canPlayPiper = (): boolean => typeof window !== "undefined" && "Audio" in window;
const canSpeakNative = (): boolean => typeof window !== "undefined" && "speechSynthesis" in window;

interface PiperClip {
  key: string;
  text: string;
  first: boolean;
}

export function useReadAloud(): ReadAloud {
  const [settingOn, setSettingOn] = useState(false);
  const [hasVoices, setHasVoices] = useState(false);
  const [engine, setEngine] = useState<ReadAloudEngine>("piper");
  const [playing, setPlaying] = useState<string | null>(null);
  const [autoPlay, setAutoPlay] = useState<boolean>(() => {
    try {
      return localStorage.getItem(AUTOPLAY_KEY) === "1";
    } catch {
      return false;
    }
  });

  // Live copies read by the callbacks (which close over these without re-creating).
  const engineRef = useRef<ReadAloudEngine>("piper");
  const hasVoicesRef = useRef(false);
  const answerVoiceRef = useRef("en_US-amy-medium");

  const playingRef = useRef<string | null>(null);
  // The turn currently being streamed to the engine, and how far (in stripped chars)
  // it has been dispatched — the cursor a `feed` advances so it only speaks new text.
  const feedKeyRef = useRef<string | null>(null);
  const spokenLenRef = useRef(0);
  // The queue for the active turn: chunks enqueued but not yet finished, and whether the
  // turn is finalized (its last chunk finishing drains the queue and clears `playing`).
  const queueRef = useRef<{ key: string; pending: number; finalized: boolean }>({
    key: "",
    pending: 0,
    finalized: false,
  });
  // Turns the owner has paused — auto-play won't resume them mid-stream.
  const suppressedRef = useRef<Set<string>>(new Set());
  // A manual play is in flight — auto-play's `feed` defers so the two don't overlap.
  const manualRef = useRef(false);

  // --- piper engine state ---------------------------------------------------------
  // A generation token bumped by every stop()/beginQueue so an in-flight piper fetch or
  // clip from a superseded turn bails without touching state. The FIFO of clips for the
  // active turn, the <audio> playing now (so stop can pause it), a resolver that unsticks
  // the play await on stop, whether a pump owns the current gen, a per-turn clip counter
  // (only the first clip carries the silence lead), and whether the box failed (so the
  // turn degrades to the native voice).
  const genRef = useRef(0);
  const piperFifoRef = useRef<PiperClip[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const piperResolveRef = useRef<(() => void) | null>(null);
  const piperRunningGenRef = useRef<number | null>(null);
  const piperClipCountRef = useRef(0);
  const piperFailedRef = useRef(false);

  // Stable (ref-only reads) so the callbacks that depend on them don't churn each render.
  const usePiper = useCallback(
    (): boolean =>
      engineRef.current === "piper" &&
      hasVoicesRef.current &&
      canPlayPiper() &&
      !piperFailedRef.current,
    [],
  );
  const canVoice = useCallback((): boolean => usePiper() || canSpeakNative(), [usePiper]);

  // Whether read-aloud is on, which voice answers speak in, and which engine to use.
  useEffect(() => {
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (stale) return;
        setSettingOn(s.brain_read_aloud);
        if (s.brain_answer_voice) answerVoiceRef.current = s.brain_answer_voice;
        const eng: ReadAloudEngine = s.brain_read_aloud_engine === "native" ? "native" : "piper";
        engineRef.current = eng;
        setEngine(eng);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  // Which piper voices the box has — piper mode needs at least one; without any (box
  // unreachable / no models) piper mode falls back to the device's native voice.
  useEffect(() => {
    let stale = false;
    api
      .brainVoices()
      .then((voices) => {
        if (stale) return;
        hasVoicesRef.current = voices.length > 0;
        setHasVoices(voices.length > 0);
      })
      .catch(() => {
        if (!stale) {
          hasVoicesRef.current = false;
          setHasVoices(false);
        }
      });
    return () => {
      stale = true;
    };
  }, []);

  const setPlay = useCallback((key: string | null) => {
    playingRef.current = key;
    setPlaying(key);
  }, []);

  // One chunk of the active turn finished (a native utterance ended, or a piper clip
  // played): drop the pending count and, when the last chunk of a finalized turn is done,
  // clear playback — but only while it's still the active turn.
  const settle = useCallback(
    (key: string) => {
      const q = queueRef.current;
      if (q.key !== key) return;
      q.pending = Math.max(0, q.pending - 1);
      if (q.pending === 0 && q.finalized && playingRef.current === key) {
        manualRef.current = false;
        feedKeyRef.current = null;
        setPlay(null);
      }
    },
    [setPlay],
  );

  // Play one rendered piper clip, resolving when it ends / errors / is stopped.
  const playAudio = useCallback((blob: Blob): Promise<void> => {
    return new Promise<void>((resolve) => {
      let url = "";
      const finish = () => {
        if (url) {
          try {
            URL.revokeObjectURL(url);
          } catch {
            /* nothing to revoke */
          }
        }
        if (piperResolveRef.current === wrapped) piperResolveRef.current = null;
        resolve();
      };
      const wrapped = () => {
        if (audioRef.current) {
          try {
            audioRef.current.pause();
          } catch {
            /* already torn down */
          }
        }
        finish();
      };
      try {
        url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;
        piperResolveRef.current = wrapped;
        audio.onended = finish;
        audio.onerror = finish;
        void audio.play().catch(finish);
      } catch {
        finish();
      }
    });
  }, []);

  // The box failed mid-turn: re-voice everything still queued (and mark the turn degraded
  // so later chunks go native too), so a reachable device voice finishes the reply.
  const piperFallback = useCallback(() => {
    piperFailedRef.current = true;
    const items = piperFifoRef.current.splice(0);
    if (canSpeakNative()) {
      for (const it of items) {
        const utt = new SpeechSynthesisUtterance(it.text);
        utt.onend = () => settle(it.key);
        utt.onerror = () => settle(it.key);
        window.speechSynthesis.speak(utt);
      }
    } else {
      for (const it of items) settle(it.key);
    }
  }, [settle]);

  // Drain the piper FIFO one clip at a time (fetch → play → next), tied to the gen that
  // started it so a stop()/switch abandons it. Only one pump runs per gen.
  const pumpPiper = useCallback(() => {
    const myGen = genRef.current;
    if (piperRunningGenRef.current === myGen) return;
    piperRunningGenRef.current = myGen;
    const run = async () => {
      while (genRef.current === myGen && piperFifoRef.current.length) {
        const item = piperFifoRef.current[0] as PiperClip;
        let blob: Blob;
        try {
          blob = await api.brainTts(answerVoiceRef.current, item.text, item.first ? undefined : 0);
        } catch {
          if (genRef.current === myGen) piperFallback();
          break;
        }
        if (genRef.current !== myGen) break;
        await playAudio(blob);
        if (genRef.current !== myGen) break;
        piperFifoRef.current.shift();
        settle(item.key);
      }
      if (piperRunningGenRef.current === myGen) piperRunningGenRef.current = null;
    };
    void run();
  }, [playAudio, piperFallback, settle]);

  // Enqueue one chunk for `key` on the active engine: a piper clip (fetched + played in
  // order) or a native utterance (the browser serialises these). Either way its end
  // settles the queue.
  const speakChunk = useCallback(
    (key: string, text: string) => {
      queueRef.current.pending += 1;
      if (usePiper()) {
        piperFifoRef.current.push({ key, text, first: piperClipCountRef.current === 0 });
        piperClipCountRef.current += 1;
        pumpPiper();
      } else if (canSpeakNative()) {
        const utt = new SpeechSynthesisUtterance(text);
        utt.onend = () => settle(key);
        utt.onerror = () => settle(key);
        window.speechSynthesis.speak(utt);
      } else {
        settle(key); // nothing can voice — keep the accounting balanced
      }
    },
    [pumpPiper, settle, usePiper],
  );

  // Tear down whatever's playing (both engines) and reset the piper state.
  const teardown = useCallback(() => {
    genRef.current += 1; // supersede any in-flight piper fetch/clip
    piperResolveRef.current?.(); // unstick the current clip's await
    const audio = audioRef.current;
    audioRef.current = null;
    if (audio) {
      try {
        audio.pause();
      } catch {
        /* already torn down */
      }
    }
    piperFifoRef.current = [];
    piperClipCountRef.current = 0;
    piperFailedRef.current = false;
    manualRef.current = false;
    if (canSpeakNative()) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* speech engine unavailable — nothing to cancel */
      }
    }
  }, []);

  const stop = useCallback(() => {
    teardown();
    feedKeyRef.current = null;
    spokenLenRef.current = 0;
    queueRef.current = { key: "", pending: 0, finalized: false };
    setPlay(null);
  }, [teardown, setPlay]);

  // Start a fresh queue for `key`: cut off whatever's playing (any prior turn — manual or
  // auto), reset the cursor, and mark the turn playing.
  const beginQueue = useCallback(
    (key: string) => {
      teardown();
      queueRef.current = { key, pending: 0, finalized: false };
      spokenLenRef.current = 0;
      feedKeyRef.current = key;
      setPlay(key);
    },
    [teardown, setPlay],
  );

  const toggle = useCallback(
    (key: string, markdown: string) => {
      if (!canVoice()) return;
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
        queueRef.current.finalized = true; // the whole turn is known — done when it drains
        // Native speaks the whole turn as one utterance; piper renders sentence-sized
        // clips (bounded < the server's /tts cap) and plays them back-to-back.
        if (usePiper()) {
          for (const c of chunkSentences(text, true).chunks) speakChunk(key, c);
        } else {
          speakChunk(key, text);
        }
      } catch {
        stop();
      }
    },
    [beginQueue, speakChunk, stop, usePiper, canVoice],
  );

  const feed = useCallback(
    (key: string, textSoFar: string, done: boolean) => {
      if (!canVoice()) return;
      if (suppressedRef.current.has(key)) return;
      if (feedKeyRef.current !== key) {
        if (done) return; // never auto-start an already-settled turn
        // A new agent turn is streaming — cut off whatever's speaking (a prior auto turn
        // OR a manual playback) and take it over. beginQueue cancels + clears manualRef,
        // so the next turn always wins over the old stream.
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
    [beginQueue, speakChunk, stop, setPlay, canVoice],
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

  // Native covers any device that can speak; piper covers a device that can play audio
  // and a box that has voices. Either path being possible makes read-aloud available.
  const available =
    settingOn && (canSpeakNative() || (engine === "piper" && hasVoices && canPlayPiper()));
  // Disabling the whole feature stops any in-flight speech immediately.
  useEffect(() => {
    if (!available) stop();
  }, [available, stop]);
  // Leaving the surface (unmount) stops speech too — "off mid-stream stops it".
  useEffect(() => stop, [stop]);

  return { available, playing, autoPlay, toggle, toggleAutoPlay, feed, stop };
}
