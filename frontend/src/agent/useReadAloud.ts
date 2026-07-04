// Local read-aloud for the chat surface: when the owner has turned on the
// brain_read_aloud setting (the same switch that gates the wall display's piper
// voices), the omnibox offers a volume toggle that speaks each COMPLETED assistant
// turn on THIS device via the browser's Web Speech engine — no server round-trip,
// so it works wherever the PWA is open. Turning it off (or leaving the surface)
// cancels any in-flight speech immediately.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

const PLAYBACK_KEY = "readAloudPlayback";

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
   * omnibox shows the volume toggle at all. */
  available: boolean;
  /** The device-local playback toggle (persisted). Meaningful only while `available`. */
  on: boolean;
  toggle: () => void;
  /** Speak one completed turn's answer (markdown in; stripped to prose). Cancels any
   * prior utterance so turns never overlap. */
  speak: (markdown: string) => void;
  /** Stop any in-flight speech at once. */
  stop: () => void;
}

const canSpeak = (): boolean => typeof window !== "undefined" && "speechSynthesis" in window;

export function useReadAloud(): ReadAloud {
  const [settingOn, setSettingOn] = useState(false);
  const [on, setOn] = useState<boolean>(() => {
    try {
      return localStorage.getItem(PLAYBACK_KEY) === "1";
    } catch {
      return false;
    }
  });

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

  const stop = useCallback(() => {
    if (canSpeak()) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* speech engine unavailable — nothing to cancel */
      }
    }
  }, []);

  const speak = useCallback((markdown: string) => {
    if (!canSpeak()) return;
    const text = speakableText(markdown);
    if (!text) return;
    try {
      window.speechSynthesis.cancel(); // never overlap the previous turn
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
    } catch {
      /* speak failed — leave the turn silent */
    }
  }, []);

  const toggle = useCallback(() => {
    setOn((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(PLAYBACK_KEY, next ? "1" : "0");
      } catch {
        /* private mode — the toggle just won't persist */
      }
      return next;
    });
  }, []);

  const available = settingOn && canSpeak();
  // Turning playback off, disabling the whole feature, or leaving the surface
  // (unmount) stops any in-flight speech immediately — "off mid-stream stops it".
  useEffect(() => {
    if (!on || !available) stop();
  }, [on, available, stop]);
  useEffect(() => stop, [stop]);

  return { available, on, toggle, speak, stop };
}
