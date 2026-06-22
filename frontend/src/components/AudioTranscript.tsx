// The media-transcript viewer (binding spec: docs/mocks/audio-transcript-approved.html).
// A flowing-paragraph transcript whose words are tinted on a rose->amber->green
// confidence gradient, over a real media element: the spoken word highlights in
// time (karaoke), and tapping a word seeks playback. Audio uses the card's custom
// player (play button + slim scrubber); video renders a native-controls <video>
// (owner choice: standard scrub/fullscreen/volume). The body caps at ~5 lines and
// keeps the active word centered as playback advances. Reused in two places — a
// note's audio attachment (Analysis tab) and jerv's `transcribe` tool result — so
// it takes a plain `audioUrl` (each caller builds it from the attachment id; no URL
// ever rides the tool payload, invariant #9).

import {
  type MouseEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { PauseIcon, PlayIcon, VideoIcon } from "./icons";

export interface TranscriptWord {
  text: string;
  startMs: number;
  endMs: number;
  confidence: number;
}

interface AudioTranscriptProps {
  audioUrl: string;
  filename: string;
  words: TranscriptWord[];
  /** Shown when `words` is empty (e.g. an older transcript with no per-word data). */
  text?: string | undefined;
  durationMs?: number | null | undefined;
  model?: string | undefined;
  /** "video" renders a native-controls <video>; default "audio" uses the custom player. */
  media?: "audio" | "video" | undefined;
}

const ROSE = [207, 138, 143];
const AMBER = [201, 163, 106];
const GREEN = [143, 188, 154];

function lerp(a: number[], b: number[], t: number): [number, number, number] {
  const at = (xs: number[], i: number) => xs[i] ?? 0;
  return [0, 1, 2].map((i) => Math.round(at(a, i) + (at(b, i) - at(a, i)) * t)) as [
    number,
    number,
    number,
  ];
}

/** Confidence (0..1) → the rose→amber→green gradient color (matches the legend). */
export function confidenceColor(c: number): string {
  const x = c < 0 ? 0 : c > 1 ? 1 : c;
  const [r, g, b] = x < 0.5 ? lerp(ROSE, AMBER, x / 0.5) : lerp(AMBER, GREEN, (x - 0.5) / 0.5);
  return `rgb(${r}, ${g}, ${b})`;
}

/** Map the API/tool-view word shape ({text, start_ms, end_ms, confidence}) to the
 * component's props. Tolerant of a missing/garbled `words` value → []. */
export function transcriptWords(value: unknown): TranscriptWord[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((w): TranscriptWord[] => {
    if (typeof w !== "object" || w === null) return [];
    const o = w as Record<string, unknown>;
    if (typeof o.text !== "string") return [];
    return [
      {
        text: o.text,
        startMs: typeof o.start_ms === "number" ? o.start_ms : 0,
        endMs: typeof o.end_ms === "number" ? o.end_ms : 0,
        confidence: typeof o.confidence === "number" ? o.confidence : 0.6,
      },
    ];
  });
}

function fmtTime(seconds: number): string {
  const s = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

export function AudioTranscript({
  audioUrl,
  filename,
  words,
  text,
  durationMs,
  model,
  media = "audio",
}: AudioTranscriptProps): ReactNode {
  // Audio and video are both HTMLMediaElement; one ref drives sync/seek for either.
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  // Prefer the media element's real duration once metadata loads; the prop is the
  // pre-load fallback so the scrubber has a scale immediately.
  const [durMs, setDurMs] = useState(durationMs ?? 0);
  const isVideo = media === "video";

  const currentIdx = useMemo(() => {
    for (let i = 0; i < words.length; i++) {
      const w = words[i];
      if (w && currentMs >= w.startMs && currentMs < w.endMs) return i;
    }
    return -1;
  }, [currentMs, words]);

  // Keep the spoken word centered in the (≈5-line) body as playback advances.
  // Scroll the body itself — never the page — so a long transcript karaoke-scrolls
  // in place. offsetTop is relative to the body (it's position:relative).
  useEffect(() => {
    const body = bodyRef.current;
    if (currentIdx < 0 || !body) return;
    const el = body.querySelector<HTMLElement>(`[data-i="${currentIdx}"]`);
    if (!el) return;
    const target = Math.max(0, el.offsetTop - body.clientHeight / 2 + el.offsetHeight / 2);
    if (typeof body.scrollTo === "function") {
      body.scrollTo({ top: target, behavior: "smooth" });
    } else {
      body.scrollTop = target; // jsdom / older engines: no smooth scrollTo
    }
  }, [currentIdx]);

  const seekTo = useCallback((ms: number) => {
    const m = mediaRef.current;
    if (m) {
      m.currentTime = ms / 1000;
      setCurrentMs(ms);
    }
  }, []);

  function toggle() {
    const m = mediaRef.current;
    if (!m) return;
    if (m.paused) void m.play();
    else m.pause();
  }

  function scrub(e: MouseEvent<HTMLDivElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    seekTo(frac * durMs);
  }

  const pct = durMs > 0 ? (currentMs / durMs) * 100 : 0;

  // Shared media-element wiring (timeupdate drives the karaoke highlight; metadata
  // sets the real duration; play/pause track the custom button state).
  const setMedia = (el: HTMLMediaElement | null) => {
    mediaRef.current = el;
  };
  const onTimeUpdate = () => mediaRef.current && setCurrentMs(mediaRef.current.currentTime * 1000);
  const onLoadedMetadata = () => {
    const d = mediaRef.current?.duration;
    if (d && Number.isFinite(d)) setDurMs(d * 1000);
  };

  return (
    <div className="atx">
      <div className="atx-hd">
        <span className="atx-fi" aria-hidden="true">
          {isVideo ? <VideoIcon size={18} /> : "♪"}
        </span>
        <span className="atx-fn">{filename}</span>
        {model && <span className="atx-meta">{model}</span>}
      </div>
      {isVideo ? (
        // biome-ignore lint/a11y/useMediaCaption: the transcript below IS the caption.
        <video
          className="atx-video"
          ref={setMedia}
          src={audioUrl}
          controls
          preload="metadata"
          onTimeUpdate={onTimeUpdate}
          onLoadedMetadata={onLoadedMetadata}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
        />
      ) : (
        <>
          {/* biome-ignore lint/a11y/useMediaCaption: the transcript below IS the caption. */}
          <audio
            ref={setMedia}
            src={audioUrl}
            preload="metadata"
            onTimeUpdate={onTimeUpdate}
            onLoadedMetadata={onLoadedMetadata}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onEnded={() => setPlaying(false)}
          />
          <div className="atx-player">
            <button
              type="button"
              className="atx-play"
              onClick={toggle}
              aria-label={playing ? "Pause" : "Play"}
            >
              {playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
            </button>
            {/* biome-ignore lint/a11y/useKeyWithClickEvents: arrow-key seek is a nice-to-have; tap/drag is the control. */}
            <div
              className="atx-track"
              onClick={scrub}
              role="slider"
              tabIndex={0}
              aria-label="Seek"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(pct)}
            >
              <div className="atx-fill" style={{ width: `${pct}%` }} />
              <div className="atx-knob" style={{ left: `${pct}%` }} />
            </div>
            <span className="atx-time">
              {fmtTime(currentMs / 1000)} / {fmtTime(durMs / 1000)}
            </span>
          </div>
        </>
      )}
      {words.length > 0 ? (
        <>
          <div className="atx-body" ref={bodyRef}>
            {words.map((w, i) => (
              <button
                // biome-ignore lint/suspicious/noArrayIndexKey: words are static for this transcript.
                key={i}
                type="button"
                data-i={i}
                className={`atx-w${i === currentIdx ? " now" : ""}`}
                style={i === currentIdx ? undefined : { color: confidenceColor(w.confidence) }}
                onClick={() => seekTo(w.startMs)}
              >
                {w.text}{" "}
              </button>
            ))}
          </div>
          <div className="atx-legend">
            <span>low</span>
            <span className="atx-grad" aria-hidden="true" />
            <span>high confidence</span>
            <span className="atx-hint">tap a word to jump</span>
          </div>
        </>
      ) : (
        <div className="atx-body atx-plain">{text ?? ""}</div>
      )}
    </div>
  );
}
