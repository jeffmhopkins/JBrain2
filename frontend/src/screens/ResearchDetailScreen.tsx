// The Research Library detail layer (docs/reference/DESIGN.md "Research Library"). A
// slide-up layer over the list, like the list/wiki/entity views: own TopBar + swipe-down
// exit. A report renders its provenance strip + report_md through the shared <Markdown>
// path (the same renderer an assistant turn and the deep_research_report view use); a video
// renders the settled <VideoAnalysis> card (YouTube embed via video_id, filmstrip, summary
// + transcript). Wave R3 adds the per-item actions (open-in-jerv / copy / download /
// open-source) via the ⋯ in the detail, where the full item data is loaded — each shown
// only when applicable to the source.

import { useEffect, useRef, useState } from "react";
import { Markdown } from "../agent/markdown";
import { type ReportDetail, type VideoDetail, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import { TopBar } from "../components/TopBar";
import { VideoAnalysis } from "../components/VideoAnalysis";
import { ClipIcon, FileIcon, GlobeIcon, MessageIcon, MoreIcon } from "../components/icons";
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
  /** Seed a new Full Brain (jerv) conversation with a reference to this item. */
  onOpenInJerv: (text: string) => void;
}

/** Copy text to the clipboard, resolving false when the clipboard API is unavailable
 * (an insecure context or a headless test) so the caller can report honestly. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Trigger a client-side download of `text` as `name` (a Blob + a transient <a>). */
function downloadText(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** A filesystem-safe short slug from a title, for the download filename. */
function slug(s: string): string {
  return (
    s
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48) || "report"
  );
}

export function ResearchDetailScreen({
  kind,
  id,
  syncStatus,
  onClose,
  onOpenInJerv,
}: ResearchDetailScreenProps) {
  const [state, setState] = useState<State>({ phase: "loading" });
  const [actionsOpen, setActionsOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
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

  // Auto-dismiss the copy/download feedback toast.
  useEffect(() => {
    if (toast === null) return;
    const t = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(t);
  }, [toast]);

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
  const loaded = state.phase === "report" || state.phase === "video";

  function flash(msg: string): void {
    setToast(msg);
    setActionsOpen(false);
  }

  async function copyAction(what: string, text: string): Promise<void> {
    flash((await copyText(text)) ? `${what} copied.` : "Couldn't copy — clipboard unavailable.");
  }

  return (
    <div className="subscreen" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <TopBar title={title} onBack={onClose} syncStatus={syncStatus} onBolt={onClose} />
      <div className="screen-body rl-detail">
        {loaded && (
          <div className="rl-detail-bar">
            <button
              type="button"
              className="rl-kebab rl-detail-kebab"
              onClick={() => setActionsOpen(true)}
              aria-label="Item actions"
            >
              <MoreIcon size={20} />
            </button>
          </div>
        )}
        {state.phase === "loading" && <p className="muted rl-empty">Loading…</p>}
        {state.phase === "error" && (
          <p className="muted rl-empty">Couldn't load this — reopen to retry.</p>
        )}
        {state.phase === "report" && <ReportDetailBody report={state.report} />}
        {state.phase === "video" && <VideoDetailBody video={state.video} />}
      </div>

      {actionsOpen && state.phase === "report" && (
        <Sheet title={state.report.question} onClose={() => setActionsOpen(false)}>
          <div className="rl-actions">
            <button
              type="button"
              className="rl-action"
              onClick={() =>
                onOpenInJerv(`Let's continue from my research report: "${state.report.question}".`)
              }
            >
              <MessageIcon size={19} /> Open in jerv conversation
            </button>
            <button
              type="button"
              className="rl-action"
              onClick={() => void copyAction("Report", state.report.report_md)}
            >
              <ClipIcon size={19} /> Copy report
            </button>
            <button
              type="button"
              className="rl-action"
              onClick={() => {
                downloadText(`${slug(state.report.question)}.md`, state.report.report_md);
                flash("Downloading report.md…");
              }}
            >
              <FileIcon size={19} /> Download report (.md)
            </button>
          </div>
        </Sheet>
      )}

      {actionsOpen && state.phase === "video" && (
        <Sheet title={state.video.title} onClose={() => setActionsOpen(false)}>
          <div className="rl-actions">
            <button
              type="button"
              className="rl-action"
              onClick={() =>
                onOpenInJerv(
                  `Let's discuss the analysed video "${state.video.title}" (${state.video.url}).`,
                )
              }
            >
              <MessageIcon size={19} /> Open in jerv conversation
            </button>
            <button
              type="button"
              className="rl-action"
              onClick={() => void copyAction("Summary", state.video.summary)}
            >
              <ClipIcon size={19} /> Copy summary
            </button>
            <button
              type="button"
              className="rl-action"
              onClick={() =>
                void copyAction("Transcript", state.video.windows.map((w) => w.text).join("\n"))
              }
            >
              <ClipIcon size={19} /> Copy transcript
            </button>
            {state.video.url && (
              <button
                type="button"
                className="rl-action"
                onClick={() => {
                  window.open(state.video.url, "_blank", "noopener,noreferrer");
                  setActionsOpen(false);
                }}
              >
                <GlobeIcon size={19} /> Open source ↗
              </button>
            )}
          </div>
        </Sheet>
      )}

      {toast !== null && (
        <output className="rl-toast">
          <span className="rl-toast-msg">{toast}</span>
          <button type="button" className="rl-toast-undo" onClick={() => setToast(null)}>
            Dismiss
          </button>
        </output>
      )}
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
