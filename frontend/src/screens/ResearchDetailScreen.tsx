// The Research Library detail layer (docs/reference/DESIGN.md "Research Library"). A
// slide-up layer over the list, like the list/wiki/entity views: own TopBar + swipe-down
// exit. A report renders its provenance strip + report_md through the shared <Markdown>
// path (the same renderer an assistant turn and the deep_research_report view use); a video
// renders the settled <VideoAnalysis> card (YouTube embed via video_id, filmstrip, summary
// + transcript). Wave R2 is view-only; Wave R3 adds the item actions (open-in-jerv / copy /
// download / open-source) here and on the list row's ⋯.

import { useEffect, useRef, useState } from "react";
import { Markdown } from "../agent/markdown";
import { type ReportDetail, type VideoDetail, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { VideoAnalysis } from "../components/VideoAnalysis";
import type { SyncStatus } from "../notes/useNotes";
import type { ResearchKind } from "./ResearchScreen";

const SWIPE_DOWN_PX = 112;

type State =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "report"; report: ReportDetail }
  | { phase: "video"; video: VideoDetail };

interface ResearchDetailScreenProps {
  kind: ResearchKind;
  /** A report uuid, or a video's video_id. */
  id: string;
  syncStatus: SyncStatus;
  onClose: () => void;
}

export function ResearchDetailScreen({ kind, id, syncStatus, onClose }: ResearchDetailScreenProps) {
  const [state, setState] = useState<State>({ phase: "loading" });
  const swipeStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    let stale = false;
    setState({ phase: "loading" });
    const load =
      kind === "report"
        ? api.researchReport(id).then((report) => ({ phase: "report", report }) as const)
        : api.researchVideo(id).then((video) => ({ phase: "video", video }) as const);
    load
      .then((next) => {
        if (!stale) setState(next);
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [kind, id]);

  // Swipe-down-at-top climbs back, like every card layer (an input/scroll opts out).
  function onTouchStart(event: React.TouchEvent): void {
    const target = event.target as HTMLElement;
    const scroller = event.currentTarget.querySelector<HTMLElement>(".rl-detail");
    if (target.closest("input, textarea, video, iframe, .tv-vid-strip")) return;
    if ((scroller?.scrollTop ?? 0) > 4) return;
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }
  function onTouchMove(event: React.TouchEvent): void {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    if (
      t.clientY - start.y > SWIPE_DOWN_PX &&
      t.clientY - start.y > Math.abs(t.clientX - start.x) * 2
    ) {
      swipeStart.current = null;
      onClose();
    }
  }

  const title = kind === "report" ? "Report" : "Video analysis";

  return (
    <div className="subscreen" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <TopBar title={title} onBack={onClose} syncStatus={syncStatus} onBolt={onClose} />
      <div className="screen-body rl-detail">
        {state.phase === "loading" && <p className="muted rl-empty">Loading…</p>}
        {state.phase === "error" && (
          <p className="muted rl-empty">Couldn't load this — reopen to retry.</p>
        )}
        {state.phase === "report" && <ReportDetailBody report={state.report} />}
        {state.phase === "video" && <VideoDetailBody video={state.video} />}
      </div>
    </div>
  );
}

function ReportDetailBody({ report }: { report: ReportDetail }) {
  return (
    <article className="rl-report">
      <h2 className="rl-report-q">{report.question}</h2>
      <div className="rl-prov">
        <span className={`rl-cx rl-cx-${report.complexity}`}>{report.complexity}</span>
        <span className="rl-chip">{report.sub_agents} agents</span>
        <span className="rl-chip">
          {report.rounds} round{report.rounds === 1 ? "" : "s"}
        </span>
        <span className="rl-chip">
          {report.sources.length} source{report.sources.length === 1 ? "" : "s"}
        </span>
        {report.analyzed && <span className="rl-flag rl-flag-ok">cross-checked</span>}
        {report.revised && <span className="rl-flag rl-flag-ok">revised</span>}
        {report.coverage_limited && <span className="rl-flag rl-flag-warn">coverage limited</span>}
        {report.truncated && <span className="rl-flag rl-flag-danger">truncated</span>}
      </div>
      <div className="rl-md">
        <Markdown text={report.report_md} />
      </div>
    </article>
  );
}

function VideoDetailBody({ video }: { video: VideoDetail }) {
  // Build the transcript text from the ordered windows when there's no word-level cue data;
  // the card renders the plain reader in that case. YouTube sources embed by video_id.
  const words =
    video.cued_transcript?.words?.map((w) => ({
      text: w.text,
      startMs: w.start_ms,
      endMs: w.end_ms,
      confidence: 1,
    })) ?? [];
  const transcriptText =
    words.length === 0 ? video.windows.map((w) => w.text).join("\n") : undefined;
  return (
    <VideoAnalysis
      youtubeId={video.provider === "youtube" ? video.video_id : undefined}
      sourceUrl={video.url}
      filename={video.title}
      summary={video.summary}
      frames={video.frames.map((f) => ({ tMs: f.t_ms ?? 0, caption: f.caption ?? "" }))}
      words={words}
      transcriptText={transcriptText}
      transcriptSource={video.transcript_source}
    />
  );
}
