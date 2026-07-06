// Local read-aloud for the chat surface: when the owner has turned on the
// brain_read_aloud setting (the same switch that gates the wall display's piper
// voices), each COMPLETED assistant turn gets a play/pause control next to its copy
// button. Tapping play speaks that one turn on THIS device via the browser's Web
// Speech engine — no server round-trip, so it works wherever the PWA is open —
// and the control flips to pause; tapping pause (or playing another turn, or
// leaving the surface) stops the speech at once.

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

export interface ReadAloud {
  /** The brain_read_aloud setting is on AND this browser can speak — gates whether the
   * bubbles show a play control at all. */
  available: boolean;
  /** Key of the turn currently being spoken aloud, or null when silent — a bubble is
   * "playing" (shows pause) only when its key matches. */
  playing: string | null;
  /** Play one turn (markdown in; stripped to prose), or pause it if it's the turn
   * already playing. Starting a new turn stops any other in flight so they never
   * overlap. */
  toggle: (key: string, markdown: string) => void;
  /** Stop any in-flight speech at once. */
  stop: () => void;
}

const canSpeak = (): boolean => typeof window !== "undefined" && "speechSynthesis" in window;

export function useReadAloud(): ReadAloud {
  const [settingOn, setSettingOn] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  // The utterance in flight — used to ignore an `onend` fired by our own cancel()
  // when a later toggle has already replaced it.
  const activeRef = useRef<SpeechSynthesisUtterance | null>(null);
  const playingRef = useRef<string | null>(null);

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
    activeRef.current = null;
    setPlay(null);
    if (canSpeak()) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* speech engine unavailable — nothing to cancel */
      }
    }
  }, [setPlay]);

  const toggle = useCallback(
    (key: string, markdown: string) => {
      if (!canSpeak()) return;
      // Tapping the turn already playing pauses it.
      if (playingRef.current === key) {
        stop();
        return;
      }
      const text = speakableText(markdown);
      if (!text) return;
      try {
        window.speechSynthesis.cancel(); // never overlap the previous turn
        const utt = new SpeechSynthesisUtterance(text);
        // Clear the pause state once this turn finishes (or errors) on its own — but
        // only while it's still the active one, so a cancel from a later toggle
        // doesn't wipe the newer turn's playing state.
        const settle = () => {
          if (activeRef.current === utt) {
            activeRef.current = null;
            setPlay(null);
          }
        };
        utt.onend = settle;
        utt.onerror = settle;
        activeRef.current = utt;
        window.speechSynthesis.speak(utt);
        setPlay(key);
      } catch {
        /* speak failed — leave the turn silent */
        activeRef.current = null;
        setPlay(null);
      }
    },
    [stop, setPlay],
  );

  const available = settingOn && canSpeak();
  // Disabling the whole feature stops any in-flight speech immediately.
  useEffect(() => {
    if (!available) stop();
  }, [available, stop]);
  // Leaving the surface (unmount) stops speech too — "off mid-stream stops it".
  useEffect(() => stop, [stop]);

  return { available, playing, toggle, stop };
}
