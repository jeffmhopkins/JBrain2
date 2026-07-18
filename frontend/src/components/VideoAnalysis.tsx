// The analyze_video card (binding spec: docs/mocks/analyze-video-approved.html).
// One <video> drives one shared clock across a horizontal filmstrip scrubber (the
// sampled frame thumbnails ARE the timeline) and two tab panels — Summary and
// Transcript (the approved AudioTranscript reader). A live "now" line under the
// filmstrip shows the active frame's caption as playback advances.
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
  /** The scrubbable video src, built from an attachment id by the caller. Absent for a
   * stream source (a live/remote URL is not a playable local attachment) — then the
   * card drops the <video> and the filmstrip frames are the whole timeline. */
  videoUrl?: string | undefined;
  /** A YouTube video id (analyze_stream, YouTube source). When set, the card embeds the
   * YouTube player instead of a <video> and drives the shared clock from it via
   * postMessage — so the filmstrip + transcript sync to playback. Takes precedence over
   * videoUrl. Server-derived id, never model-authored (the #9 exception, ASSISTANT.md). */
  youtubeId?: string | undefined;
  /** True when the source is a live stream — shows a LIVE badge in the header. */
  isLive?: boolean | undefined;
  /** The stream's page URL (analyze_stream) — a tappable source chip. For a non-YouTube
   * stream (no embed) it's the way to go watch; server-derived, never model-authored. */
  sourceUrl?: string | undefined;
  filename: string;
  summary: string;
  frames: VideoFrame[];
  words: TranscriptWord[];
  /** Plain transcript fallback when there is no per-word data. */
  transcriptText?: string | undefined;
  /** Where the transcript came from: "captions" (the provider's own) or "whisper" (the
   * local transcription). Shown as a small note on the transcript tab so the owner knows
   * which source produced it (and can ask to re-run with the other). */
  transcriptSource?: string | undefined;
}

type Tab = "summary" | "transcript";

function fmtTime(ms: number): string {
  const s = Number.isFinite(ms) && ms > 0 ? ms / 1000 : 0;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/** A short label for where the transcript came from — the provider's own captions vs the
 * local whisper transcription — or "" when unknown (then no note renders). */
function transcriptSourceLabel(source: string | undefined): string {
  if (source === "captions") return "From the source's own captions";
  if (source === "whisper") return "Transcribed locally";
  return "";
}

/** The bare host of a source URL for the chip label (e.g. "youtube.com"), or "source"
 * when it can't be parsed. Never renders the full URL, which can be long/opaque. */
function sourceHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "source";
  }
}

/** The index of the latest frame at or before `currentMs` (the active frame), or -1. */
export function activeFrameIndex(frames: VideoFrame[], currentMs: number): number {
  let idx = -1;
  for (let i = 0; i < frames.length; i++) {
    const f = frames[i];
    if (f && currentMs >= f.tMs) idx = i;
  }
  return idx;
}

/** The YouTube embed origin — the cookieless host, so the player sets no cookie until
 * play and our origin loads no third-party JS (we talk to it only via postMessage). */
const YT_EMBED_ORIGIN = "https://www.youtube-nocookie.com";

/** Whether a postMessage came from a YouTube embed (guards the time-sync listener
 * against spoofed messages from any other frame). */
function isYouTubeOrigin(origin: string): boolean {
  try {
    return /(^|\.)youtube(-nocookie)?\.com$/.test(new URL(origin).hostname);
  } catch {
    return false;
  }
}

export function VideoAnalysis({
  videoUrl,
  youtubeId,
  isLive,
  sourceUrl,
  filename,
  summary,
  frames,
  words,
  transcriptText,
  transcriptSource,
}: VideoAnalysisProps): ReactNode {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);

  // Command the embedded YouTube player over postMessage (no YouTube JS in our origin).
  const postToPlayer = useCallback((func: string, args: unknown[] = []) => {
    iframeRef.current?.contentWindow?.postMessage(
      JSON.stringify({ event: "command", func, args }),
      YT_EMBED_ORIGIN,
    );
  }, []);

  // Drive the shared clock from the YouTube player: once it's listening it posts
  // `infoDelivery` frames carrying currentTime, which advance the filmstrip + karaoke.
  useEffect(() => {
    if (!youtubeId) return;
    const onMessage = (e: MessageEvent) => {
      if (typeof e.data !== "string" || !isYouTubeOrigin(e.origin)) return;
      try {
        const msg = JSON.parse(e.data);
        const t = msg?.info?.currentTime;
        if (typeof t === "number") setCurrentMs(t * 1000);
      } catch {
        // non-JSON player chatter — ignore
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [youtubeId]);

  const hasTranscript = words.length > 0 || Boolean(transcriptText);
  const hasFrames = frames.length > 0;
  // The tabs that have content, in display order; the first is the default.
  const tabs = useMemo<Tab[]>(
    () => (hasTranscript ? ["summary", "transcript"] : ["summary"]),
    [hasTranscript],
  );
  const [tab, setTab] = useState<Tab>("summary");
  const active = tabs.includes(tab) ? tab : "summary";

  const activeFrame = useMemo(() => activeFrameIndex(frames, currentMs), [frames, currentMs]);
  const currentIdx = useMemo(() => currentWordIndex(words, currentMs), [words, currentMs]);

  // timeupdate fires only ~4×/s; sample the clock every animation frame while playing
  // so the filmstrip + karaoke highlight stay tight (the AudioTranscript posture).
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

  const seekTo = useCallback(
    (ms: number) => {
      const v = videoRef.current;
      if (v) v.currentTime = ms / 1000;
      // Seek the embedded YouTube player too, so tapping a frame jumps playback.
      if (youtubeId) postToPlayer("seekTo", [ms / 1000, true]);
      // Update the clock even without a local <video>, so a stream card still
      // highlights the tapped frame and surfaces its caption immediately.
      setCurrentMs(ms);
    },
    [youtubeId, postToPlayer],
  );

  const onTimeUpdate = () => videoRef.current && setCurrentMs(videoRef.current.currentTime * 1000);

  const nowCaption = activeFrame >= 0 ? frames[activeFrame]?.caption : undefined;

  return (
    <div className="tv-vid">
      <div className="tv-vid-hd">
        <span className="tv-vid-fi" aria-hidden="true">
          <VideoIcon size={18} />
        </span>
        {isLive && <span className="tv-vid-live">LIVE</span>}
        <span className="tv-vid-fn">{filename}</span>
        {sourceUrl && (
          <a
            className="tv-vid-src"
            href={sourceUrl}
            target="_blank"
            rel="noreferrer"
            title={sourceUrl}
          >
            {sourceHost(sourceUrl)} ↗
          </a>
        )}
        {hasFrames && (
          <span className="tv-vid-meta">
            {frames.length} frame{frames.length === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {youtubeId ? (
        // The YouTube embed (cookieless host). enablejsapi lets us start `listening`
        // for currentTime and send seekTo — over postMessage only, so no YouTube JS
        // runs in our origin and the iframe is browser origin-isolated (the #9
        // exception, ASSISTANT.md). Server-derived id, never model-authored.
        <iframe
          className="tv-vid-video"
          ref={iframeRef}
          title={filename}
          src={`${YT_EMBED_ORIGIN}/embed/${encodeURIComponent(youtubeId)}?enablejsapi=1&playsinline=1&origin=${encodeURIComponent(typeof window !== "undefined" ? window.location.origin : "")}`}
          allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
          onLoad={() =>
            iframeRef.current?.contentWindow?.postMessage(
              JSON.stringify({ event: "listening" }),
              YT_EMBED_ORIGIN,
            )
          }
        />
      ) : videoUrl ? (
        // biome-ignore lint/a11y/useMediaCaption: the transcript tab IS the caption.
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
      ) : null}
      {hasFrames && (
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
              {t === "summary" ? "Summary" : "Transcript"}
            </button>
          ))}
        </div>
      )}

      <div className={`tv-vid-panel${active === "transcript" ? " tv-vid-panel-flush" : ""}`}>
        {active === "summary" && (
          <p className="tv-vid-summary">{summary || "No summary was produced for this video."}</p>
        )}
        {active === "transcript" && (
          <>
            {transcriptSourceLabel(transcriptSource) && (
              <p className="tv-vid-tsrc">{transcriptSourceLabel(transcriptSource)}</p>
            )}
            <TranscriptBody
              words={words}
              currentIdx={currentIdx}
              onSeek={seekTo}
              text={transcriptText}
            />
          </>
        )}
      </div>
    </div>
  );
}
