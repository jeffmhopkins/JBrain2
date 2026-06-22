// The analyze_video card (binding spec: docs/mocks/analyze-video-approved.html).
// One <video> drives one shared clock across three tab panels — Summary, Moments
// (a karaoke caption feed), and Transcript (the approved AudioTranscript reader) —
// plus a marker rail under the player whose ticks jump to each analysed moment. The
// rail shows timestamps, not frame thumbnails: a chat-tool analysis is computed
// inline and not persisted, and the frame blobs are content-addressed with no
// per-blob firewall, so there is no safe id to serve a thumbnail by (invariant #3/#9).
// The <video> itself is the visual; the rail/feed/transcript are the AI overlay.
//
// Reused from a jerv `analyze_video` tool result; it takes a plain `videoUrl` (the
// caller builds it from the attachment id — no URL ever rides the payload, #9).

import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TranscriptBody, type TranscriptWord, currentWordIndex } from "./AudioTranscript";
import { VideoIcon } from "./icons";

export interface VideoFrame {
  tMs: number;
  caption: string;
  /** The frame thumbnail src, built from its blob id by the caller; absent when the
   * source can't address a thumbnail (then the frame renders as a marker). */
  thumbUrl?: string | undefined;
}

interface VideoAnalysisProps {
  videoUrl: string;
  filename: string;
  summary: string;
  frames: VideoFrame[];
  words: TranscriptWord[];
  /** Plain transcript fallback when there is no per-word data. */
  transcriptText?: string | undefined;
}

type Tab = "summary" | "moments" | "transcript";

interface Moment {
  tMs: number;
  caption: string;
  thumbUrl?: string | undefined;
  /** The words spoken in this frame's window, joined — the moment's "said" line. */
  said: string;
}

function fmtTime(ms: number): string {
  const s = Number.isFinite(ms) && ms > 0 ? ms / 1000 : 0;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/** Pair each frame with the transcript words spoken in its window [tMs, nextTMs),
 * so a moment shows both what was on screen and what was said then. */
export function buildMoments(frames: VideoFrame[], words: TranscriptWord[]): Moment[] {
  return frames.map((f, i) => {
    const next = frames[i + 1];
    const end = next ? next.tMs : Number.POSITIVE_INFINITY;
    const said = words
      .filter((w) => w.startMs >= f.tMs && w.startMs < end)
      .map((w) => w.text)
      .join(" ");
    return { tMs: f.tMs, caption: f.caption, thumbUrl: f.thumbUrl, said };
  });
}

/** The index of the latest frame at or before `currentMs` (the active moment), or -1. */
export function activeFrameIndex(frames: VideoFrame[], currentMs: number): number {
  let idx = -1;
  for (let i = 0; i < frames.length; i++) {
    const f = frames[i];
    if (f && currentMs >= f.tMs) idx = i;
  }
  return idx;
}

export function VideoAnalysis({
  videoUrl,
  filename,
  summary,
  frames,
  words,
  transcriptText,
}: VideoAnalysisProps): ReactNode {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);

  const hasTranscript = words.length > 0 || Boolean(transcriptText);
  const hasMoments = frames.length > 0;
  // The tabs that have content, in display order; the first is the default.
  const tabs = useMemo<Tab[]>(() => {
    const t: Tab[] = ["summary"];
    if (hasMoments) t.push("moments");
    if (hasTranscript) t.push("transcript");
    return t;
  }, [hasMoments, hasTranscript]);
  const [tab, setTab] = useState<Tab>("summary");
  const active = tabs.includes(tab) ? tab : "summary";

  const moments = useMemo(() => buildMoments(frames, words), [frames, words]);
  const activeFrame = useMemo(() => activeFrameIndex(frames, currentMs), [frames, currentMs]);
  const currentIdx = useMemo(() => currentWordIndex(words, currentMs), [words, currentMs]);

  // timeupdate fires only ~4×/s; sample the clock every animation frame while playing
  // so the moment + karaoke highlight stay tight (the AudioTranscript posture).
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = () => {
      const v = videoRef.current;
      if (v) setCurrentMs(v.currentTime * 1000);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  // Keep the active frame centered in the (horizontal) filmstrip as playback advances.
  useEffect(() => {
    const strip = stripRef.current;
    if (activeFrame < 0 || !strip) return;
    const el = strip.querySelector<HTMLElement>(`[data-i="${activeFrame}"]`);
    if (!el) return;
    const target = Math.max(0, el.offsetLeft - strip.clientWidth / 2 + el.offsetWidth / 2);
    if (typeof strip.scrollTo === "function") strip.scrollTo({ left: target, behavior: "smooth" });
    else strip.scrollLeft = target;
  }, [activeFrame]);

  // Keep the active moment centered in the (scrolling) feed as playback advances.
  useEffect(() => {
    if (active !== "moments") return;
    const feed = feedRef.current;
    if (activeFrame < 0 || !feed) return;
    const el = feed.querySelector<HTMLElement>(`[data-i="${activeFrame}"]`);
    if (!el) return;
    const target = Math.max(0, el.offsetTop - feed.clientHeight / 2 + el.offsetHeight / 2);
    if (typeof feed.scrollTo === "function") feed.scrollTo({ top: target, behavior: "smooth" });
    else feed.scrollTop = target;
  }, [activeFrame, active]);

  const seekTo = useCallback((ms: number) => {
    const v = videoRef.current;
    if (v) {
      v.currentTime = ms / 1000;
      setCurrentMs(ms);
    }
  }, []);

  const onTimeUpdate = () => videoRef.current && setCurrentMs(videoRef.current.currentTime * 1000);

  const nowCaption = activeFrame >= 0 ? frames[activeFrame]?.caption : undefined;

  return (
    <div className="tv-vid">
      <div className="tv-vid-hd">
        <span className="tv-vid-fi" aria-hidden="true">
          <VideoIcon size={18} />
        </span>
        <span className="tv-vid-fn">{filename}</span>
        {frames.length > 0 && (
          <span className="tv-vid-meta">
            {frames.length} frame{frames.length === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {/* biome-ignore lint/a11y/useMediaCaption: the transcript tab IS the caption. */}
      <video
        className="tv-vid-video"
        ref={videoRef}
        src={videoUrl}
        controls
        preload="metadata"
        onTimeUpdate={onTimeUpdate}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
      />
      {hasMoments && (
        // The filmstrip is the scrubber: sampled frames are the timeline. Tap a frame
        // to seek; the active frame lifts and the strip scrolls to keep it centered.
        <div className="tv-vid-strip" ref={stripRef}>
          {frames.map((f, i) => (
            <button
              type="button"
              // biome-ignore lint/suspicious/noArrayIndexKey: frames are static for this card.
              key={i}
              data-i={i}
              className={`tv-vid-frame${i === activeFrame ? " on" : ""}`}
              onClick={() => seekTo(f.tMs)}
              aria-label={`Jump to ${fmtTime(f.tMs)}`}
              title={`${fmtTime(f.tMs)} · ${f.caption}`}
            >
              {f.thumbUrl ? (
                <img className="tv-vid-frame-img" src={f.thumbUrl} alt="" loading="lazy" />
              ) : (
                <span className="tv-vid-frame-ph" aria-hidden="true" />
              )}
              <span className="tv-vid-frame-t">{fmtTime(f.tMs)}</span>
            </button>
          ))}
        </div>
      )}
      {nowCaption && (
        <div className="tv-vid-now">
          <span className="tv-vid-now-t">{fmtTime(frames[activeFrame]?.tMs ?? 0)}</span>
          <span className="tv-vid-now-cap">{nowCaption}</span>
        </div>
      )}

      {tabs.length > 1 && (
        <div className="tv-vid-tabs" role="tablist">
          {tabs.map((t) => (
            <button
              type="button"
              key={t}
              role="tab"
              aria-selected={t === active}
              className={`tv-vid-tab${t === active ? " on" : ""}`}
              onClick={() => setTab(t)}
            >
              {t === "summary" ? "Summary" : t === "moments" ? "Moments" : "Transcript"}
            </button>
          ))}
        </div>
      )}

      <div className={`tv-vid-panel${active === "transcript" ? " tv-vid-panel-flush" : ""}`}>
        {active === "summary" && (
          <p className="tv-vid-summary">{summary || "No summary was produced for this video."}</p>
        )}
        {active === "moments" && (
          <div className="tv-vid-feed" ref={feedRef}>
            {moments.map((m, i) => (
              <button
                type="button"
                // biome-ignore lint/suspicious/noArrayIndexKey: moments are static for this card.
                key={i}
                data-i={i}
                className={`tv-vid-moment${i === activeFrame ? " on" : ""}`}
                onClick={() => seekTo(m.tMs)}
              >
                {m.thumbUrl ? (
                  <img className="tv-vid-moment-thumb" src={m.thumbUrl} alt="" loading="lazy" />
                ) : (
                  <span className="tv-vid-moment-t">{fmtTime(m.tMs)}</span>
                )}
                <span className="tv-vid-moment-body">
                  <span className="tv-vid-moment-cap">{m.caption}</span>
                  {m.said && <span className="tv-vid-moment-said">“{m.said}”</span>}
                </span>
              </button>
            ))}
          </div>
        )}
        {active === "transcript" && (
          <TranscriptBody
            words={words}
            currentIdx={currentIdx}
            onSeek={seekTo}
            text={transcriptText}
          />
        )}
      </div>
    </div>
  );
}
