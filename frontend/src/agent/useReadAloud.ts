// In-chat read-aloud for the chat surface: when the owner has turned on the
// brain_read_aloud setting (the same switch that gates the wall display's piper
// voices), each COMPLETED assistant turn gets a play/pause control next to its copy
// button. Tapping play speaks that one turn, and the control flips to pause; tapping
// pause (or playing another turn, or leaving the surface) stops it at once.
//
// Two engines, chosen by the brain_read_aloud_engine setting:
//  - "piper" (default): the box renders the turn in the chosen voice (brain_answer_voice)
//    and the audio streams back over the owner's authenticated api session
//    (GET /api/brain/tts), so the voice matches the wall. A long reply is split into
//    sentence-sized clips so audio starts fast and plays back-to-back. If the box can't be
//    reached, it falls back to the device's native voice so read-aloud still works.
//  - "native": always the browser's own Web Speech voice — no box needed.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type ReadAloudEngine = "piper" | "native";

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

// Split prose into clips no longer than `cap` chars (< the server's 1000-char /tts cap),
// preferring sentence boundaries so each clip renders quickly and the reply plays back
// gaplessly; a single sentence longer than the cap is hard-split so nothing is dropped.
export function chunkForTts(text: string, cap = 800): string[] {
  const sentences = text.match(/[^.!?]+[.!?]*\s*/g) ?? [text];
  const out: string[] = [];
  let cur = "";
  for (const s of sentences) {
    if (cur && cur.length + s.length > cap) {
      out.push(cur.trim());
      cur = "";
    }
    cur += s;
    while (cur.length > cap) {
      out.push(cur.slice(0, cap).trim());
      cur = cur.slice(cap);
    }
  }
  if (cur.trim()) out.push(cur.trim());
  return out.filter(Boolean);
}

export interface ReadAloud {
  /** Read-aloud is on AND an engine can actually speak (a native voice on this device,
   * or piper voices on the box) — gates whether the bubbles show a play control at all. */
  available: boolean;
  /** Key of the turn currently being spoken aloud, or null when silent — a bubble is
   * "playing" (shows pause) only when its key matches. */
  playing: string | null;
  /** Play one turn (markdown in; stripped to prose), or pause it if it's the turn
   * already playing. Starting a new turn stops any other in flight so they never
   * overlap. */
  toggle: (key: string, markdown: string) => void;
  /** Stop any in-flight audio at once. */
  stop: () => void;
}

const canPlayPiper = (): boolean => typeof window !== "undefined" && "Audio" in window;
const canSpeakNative = (): boolean => typeof window !== "undefined" && "speechSynthesis" in window;

export function useReadAloud(): ReadAloud {
  const [settingOn, setSettingOn] = useState(false);
  const [hasVoices, setHasVoices] = useState(false);
  const [engine, setEngine] = useState<ReadAloudEngine>("piper");
  const [playing, setPlaying] = useState<string | null>(null);
  const answerVoiceRef = useRef("en_US-amy-medium");
  // Live copies for the toggle callback (which closes over these without re-creating).
  const engineRef = useRef<ReadAloudEngine>("piper");
  const hasVoicesRef = useRef(false);
  const playingRef = useRef<string | null>(null);
  // The <audio> element in flight, so stop()/a switch can pause it at once.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // A generation token: every stop()/switch bumps it, so a still-in-flight async
  // fetch/play from a superseded turn sees the mismatch and bails without touching state.
  const genRef = useRef(0);

  // Whether read-aloud is on, which voice answers speak in, and which engine to use — the
  // same settings the wall reads. A change in Settings lands here on the next open.
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

  const stop = useCallback(() => {
    genRef.current += 1; // supersede any in-flight fetch/play
    const audio = audioRef.current;
    audioRef.current = null;
    if (audio) {
      try {
        audio.pause();
      } catch {
        /* already torn down — nothing to pause */
      }
    }
    if (canSpeakNative()) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* engine unavailable — nothing to cancel */
      }
    }
    setPlay(null);
  }, [setPlay]);

  // Speak the whole turn with the browser's own voice — the "native" engine, and the
  // fallback piper mode uses when the box can't render.
  const speakNative = useCallback(
    (text: string, gen: number) => {
      if (!canSpeakNative()) {
        if (gen === genRef.current) setPlay(null);
        return;
      }
      try {
        window.speechSynthesis.cancel();
        const utt = new SpeechSynthesisUtterance(text);
        const settle = () => {
          if (gen === genRef.current) setPlay(null);
        };
        utt.onend = settle;
        utt.onerror = settle;
        window.speechSynthesis.speak(utt);
      } catch {
        if (gen === genRef.current) setPlay(null);
      }
    },
    [setPlay],
  );

  // Render + play one piper clip, then chain to the next when it ends. Each step re-checks
  // the generation token so a stop()/switch mid-fetch abandons the rest cleanly. If the
  // FIRST clip can't be rendered/played (box unreachable), fall back to the native voice
  // for the whole turn rather than leaving it silent.
  const playPiper = useCallback(
    (text: string, gen: number) => {
      const chunks = chunkForTts(text);
      const onFail = (idx: number) => {
        if (gen !== genRef.current) return;
        if (idx === 0 && canSpeakNative()) speakNative(text, gen);
        else setPlay(null);
      };
      const step = (idx: number): void => {
        if (gen !== genRef.current) return;
        const chunk = chunks[idx];
        if (chunk === undefined) {
          if (gen === genRef.current) setPlay(null); // finished on its own
          return;
        }
        api
          .brainTts(answerVoiceRef.current, chunk, idx === 0 ? undefined : 0)
          .then((blob) => {
            if (gen !== genRef.current) return;
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            audioRef.current = audio;
            audio.onended = () => {
              URL.revokeObjectURL(url);
              step(idx + 1);
            };
            audio.onerror = () => {
              URL.revokeObjectURL(url);
              onFail(idx);
            };
            void audio.play().catch(() => onFail(idx));
          })
          .catch(() => onFail(idx));
      };
      step(0);
    },
    [setPlay, speakNative],
  );

  const toggle = useCallback(
    (key: string, markdown: string) => {
      // Tapping the turn already playing pauses it.
      if (playingRef.current === key) {
        stop();
        return;
      }
      const text = speakableText(markdown);
      if (!text) return;
      stop(); // never overlap the previous turn
      const gen = genRef.current;
      setPlay(key);
      // Piper when it's the chosen engine and the box has voices we can play; otherwise
      // (native engine, or piper with no reachable voices) the device's own voice.
      if (engineRef.current === "piper" && hasVoicesRef.current && canPlayPiper()) {
        playPiper(text, gen);
      } else {
        speakNative(text, gen);
      }
    },
    [stop, setPlay, playPiper, speakNative],
  );

  // Native covers any device that can speak; piper covers a device that can play audio and
  // a box that has voices. Either path being possible makes read-aloud available.
  const available =
    settingOn && (canSpeakNative() || (engine === "piper" && hasVoices && canPlayPiper()));
  // Disabling the whole feature stops any in-flight audio immediately.
  useEffect(() => {
    if (!available) stop();
  }, [available, stop]);
  // Leaving the surface (unmount) stops audio too — "off mid-stream stops it".
  useEffect(() => stop, [stop]);

  return { available, playing, toggle, stop };
}
