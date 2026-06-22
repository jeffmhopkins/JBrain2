// The audio-transcript viewer (binding spec: docs/mocks/audio-transcript-approved.html).
// A flowing-paragraph transcript whose words are tinted on a rose->amber->green
// confidence gradient, over a real <audio> player: the spoken word highlights in
// time (karaoke), and tapping a word seeks the audio. Reused in two places — a
// note's audio attachment (Analysis tab) and jerv's `transcribe` tool result —
// so it takes a plain `audioUrl` (each caller builds it from the attachment id;
// no URL ever rides the tool payload, invariant #9).

import {
  type MouseEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { PauseIcon, PlayIcon } from "./icons";

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
}: AudioTranscriptProps): ReactNode {
  const audioRef = useRef<HTMLAudioElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  // Prefer the audio element's real duration once metadata loads; the prop is the
  // pre-load fallback so the scrubber has a scale immediately.
  const [durMs, setDurMs] = useState(durationMs ?? 0);

  const currentIdx = useMemo(() => {
    for (let i = 0; i < words.length; i++) {
      const w = words[i];
      if (w && currentMs >= w.startMs && currentMs < w.endMs) return i;
    }
    return -1;
  }, [currentMs, words]);

  // Keep the spoken word in view as playback advances.
  useEffect(() => {
    if (currentIdx < 0 || !bodyRef.current) return;
    const el = bodyRef.current.querySelector<HTMLElement>(`[data-i="${currentIdx}"]`);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const br = bodyRef.current.getBoundingClientRect();
    if (r.bottom > br.bottom || r.top < br.top) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [currentIdx]);

  const seekTo = useCallback((ms: number) => {
    const a = audioRef.current;
    if (a) {
      a.currentTime = ms / 1000;
      setCurrentMs(ms);
    }
  }, []);

  function toggle() {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) void a.play();
    else a.pause();
  }

  function scrub(e: MouseEvent<HTMLDivElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    seekTo(frac * durMs);
  }

  const pct = durMs > 0 ? (currentMs / durMs) * 100 : 0;

  return (
    <div className="atx">
      {/* biome-ignore lint/a11y/useMediaCaption: the transcript below IS the caption. */}
      <audio
        ref={audioRef}
        src={audioUrl}
        preload="metadata"
        onTimeUpdate={() => audioRef.current && setCurrentMs(audioRef.current.currentTime * 1000)}
        onLoadedMetadata={() => {
          const d = audioRef.current?.duration;
          if (d && Number.isFinite(d)) setDurMs(d * 1000);
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
      />
      <div className="atx-hd">
        <span className="atx-fi" aria-hidden="true">
          ♪
        </span>
        <span className="atx-fn">{filename}</span>
        {model && <span className="atx-meta">{model}</span>}
      </div>
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
