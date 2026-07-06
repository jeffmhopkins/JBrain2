// In-chat read-aloud for the chat surface: when the owner has turned on the
// brain_read_aloud setting (the same switch that gates the wall display's piper
// voices), each COMPLETED assistant turn gets a play/pause control next to its copy
// button. Tapping play speaks that one turn through the box's piper voice — the same
// engine the wall uses — rendered on the box and streamed back over the owner's
// authenticated api session (GET /api/brain/tts), so the voice matches the wall and
// picking a voice/speaker in Settings (brain_answer_voice) applies here too. The control
// flips to pause; tapping pause (or playing another turn, or leaving the surface) stops
// the audio at once. A long reply is split into sentence-sized clips so the first audio
// starts fast and the clips play back-to-back.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

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
  /** The brain_read_aloud setting is on AND the box has piper voices we can reach —
   * gates whether the bubbles show a play control at all. */
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

const canPlay = (): boolean => typeof window !== "undefined" && "Audio" in window;

export function useReadAloud(): ReadAloud {
  const [settingOn, setSettingOn] = useState(false);
  const [hasVoices, setHasVoices] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  const answerVoiceRef = useRef("en_US-amy-medium");
  const playingRef = useRef<string | null>(null);
  // The <audio> element in flight, so stop()/a switch can pause it at once.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // A generation token: every stop()/switch bumps it, so a still-in-flight async
  // fetch/play from a superseded turn sees the mismatch and bails without touching state.
  const genRef = useRef(0);

  // Whether the owner enabled read-aloud, and which voice answers speak in — the same
  // setting the wall reads. A voice/speaker change in Settings lands here on the next open.
  useEffect(() => {
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (stale) return;
        setSettingOn(s.brain_read_aloud);
        if (s.brain_answer_voice) answerVoiceRef.current = s.brain_answer_voice;
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  // Which piper voices the box actually has — read-aloud has nothing to speak with when
  // the display is unreachable (or has no models), so the control stays hidden then.
  useEffect(() => {
    let stale = false;
    api
      .brainVoices()
      .then((voices) => {
        if (!stale) setHasVoices(voices.length > 0);
      })
      .catch(() => {
        if (!stale) setHasVoices(false);
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
    setPlay(null);
  }, [setPlay]);

  const toggle = useCallback(
    (key: string, markdown: string) => {
      if (!canPlay()) return;
      // Tapping the turn already playing pauses it.
      if (playingRef.current === key) {
        stop();
        return;
      }
      const text = speakableText(markdown);
      if (!text) return;
      stop(); // never overlap the previous turn
      const gen = genRef.current;
      const chunks = chunkForTts(text);
      setPlay(key);

      // Render + play one clip, then chain to the next when it ends. Each step re-checks
      // the generation token so a stop()/switch mid-fetch abandons the rest cleanly.
      const playChunk = (idx: number): void => {
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
            const next = () => {
              URL.revokeObjectURL(url);
              playChunk(idx + 1);
            };
            audio.onended = next;
            audio.onerror = () => {
              URL.revokeObjectURL(url);
              if (gen === genRef.current) setPlay(null);
            };
            void audio.play().catch(() => {
              if (gen === genRef.current) setPlay(null);
            });
          })
          .catch(() => {
            // Box unreachable mid-play — stop rather than hang on a half-read reply.
            if (gen === genRef.current) setPlay(null);
          });
      };
      playChunk(0);
    },
    [stop, setPlay],
  );

  const available = settingOn && hasVoices && canPlay();
  // Disabling the whole feature stops any in-flight audio immediately.
  useEffect(() => {
    if (!available) stop();
  }, [available, stop]);
  // Leaving the surface (unmount) stops audio too — "off mid-stream stops it".
  useEffect(() => stop, [stop]);

  return { available, playing, toggle, stop };
}
