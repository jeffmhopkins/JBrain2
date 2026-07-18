// The task_status card (binding spec: docs/mocks/task-status-approved.html) — the
// reusable status surface for a deferred (turn-ending) tool call. A long tool kicks a
// background job, the turn ends, and THIS card takes over: it polls the job's live
// progress, shows a determinate bar + the current phase + a truthful phase checklist +
// a Stop, and on completion SWAPS to the finished result view (for analyze_stream, the
// existing video_analysis card). Reusable — any deferred tool renders it by naming its
// result view; only `renderResult` differs (DEFERRED_TOOL_CALLS_PLAN.md P3).
//
// Data-only, no model-authored markup (#1/#9): the card is driven entirely by the
// server's {status, progress, result} — the result is the same data the in-turn card
// would have received, so the two are indistinguishable once done.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { type DeferredResult, api } from "../api/client";
import { VideoIcon } from "./icons";

interface TaskStatusProps {
  resultId: string;
  /** Human label for the work ("Analyzing video"). */
  title: string;
  /** Renders the finished result once the job is done — for analyze_stream, the
   * video_analysis card built from the stored data. Keeps this component reusable: the
   * status chrome is generic, only the result view is per-tool. */
  renderResult: (result: Record<string, unknown>) => ReactNode;
  /** Called ONCE when THIS card observes the job finish (running → done), with the
   * server-authored `resume_message`, so the controller sends the auto-resume turn that
   * prompts jerv. Not called if the card mounts already-done (a reload) — so a re-open
   * never re-fires the follow-up (DEFERRED_TOOL_CALLS_PLAN.md P3). */
  onComplete?: ((resumeMessage: string) => void) | undefined;
}

// The truthful phase sequence a stream analysis moves through, matched from the server's
// progress label (which is the source of truth — "Opening stream…", "Analyzing frame
// 3/16", "Transcribing 2/8", "Writing summary…"). A phase before the current one is done,
// the current one spins, later ones wait — so the checklist never claims more than ran.
const PHASES: { key: string; label: string; match: RegExp }[] = [
  { key: "open", label: "Open the stream", match: /open/i },
  { key: "frames", label: "Analyze the frames", match: /frame|analyz|caption/i },
  { key: "audio", label: "Transcribe the audio", match: /transcrib|audio/i },
  { key: "summary", label: "Write the summary", match: /writ|summar|fus/i },
];

const POLL_MS = 1500;
const TERMINAL = new Set(["done", "failed", "canceled"]);

function currentPhase(label: string): number {
  // Later phases win (a label can loosely match earlier keywords), so scan back-to-front.
  for (let i = PHASES.length - 1; i >= 0; i--) {
    if (PHASES[i]?.match.test(label)) return i;
  }
  return 0;
}

function fmtElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** The server-authored report jerv auto-resumes with — the worker's `resume_message`
 * (summary + transcript excerpt), falling back to the summary line / summary if absent. */
function resumeMessage(result: Record<string, unknown>): string {
  for (const key of ["resume_message", "summary_line", "summary"]) {
    const v = result[key];
    if (typeof v === "string" && v.trim()) return v;
  }
  return "The analysis finished.";
}

export function TaskStatus({
  resultId,
  title,
  renderResult,
  onComplete,
}: TaskStatusProps): ReactNode {
  const [data, setData] = useState<DeferredResult | null>(null);
  const [gone, setGone] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef(Date.now());
  // Auto-resume fires exactly once, and only on a transition WE observed (running → done):
  // a card that mounts already-done (a reload) never saw running, so it never re-prompts.
  const onCompleteRef = useRef(onComplete);
  const sawRunning = useRef(false);
  const fired = useRef(false);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  });

  const status = data?.status ?? "running";
  const finished = TERMINAL.has(status) || gone;

  // Poll the deferred result until it reaches a terminal state, then stop. A 404 means the
  // row was reaped (or never existed) — surface a gentle terminal, don't spin forever.
  useEffect(() => {
    if (finished) return;
    let alive = true;
    const tick = async () => {
      try {
        const next = await api.deferredResult(resultId);
        if (!alive) return;
        setData(next);
        if (next.status === "running") sawRunning.current = true;
        if (next.status === "done" && next.result && sawRunning.current && !fired.current) {
          fired.current = true;
          onCompleteRef.current?.(resumeMessage(next.result));
        }
      } catch {
        if (alive) setGone(true);
      }
    };
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [resultId, finished]);

  // A live elapsed timer while the work runs (client-side; the card mounted when the turn
  // ended, which is ~when the job started).
  useEffect(() => {
    if (finished) return;
    const id = setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt.current) / 1000)),
      1000,
    );
    return () => clearInterval(id);
  }, [finished]);

  const onStop = useCallback(async () => {
    setStopping(true);
    try {
      await api.cancelDeferredResult(resultId);
      setData((d) => (d ? { ...d, status: "canceled" } : d));
    } catch {
      setStopping(false);
    }
  }, [resultId]);

  // Done → swap to the finished result view (the video_analysis card). This is the whole
  // point: the status card becomes the result in place, no new message needed.
  if (status === "done" && data?.result) {
    return renderResult(data.result);
  }

  const progress = data?.progress ?? {};
  const label = typeof progress.label === "string" && progress.label ? progress.label : "Starting…";
  const step = typeof progress.step === "number" ? progress.step : 0;
  const total = typeof progress.total === "number" ? progress.total : 0;
  const determinate = total > 0;
  const pct = determinate ? Math.min(100, Math.round((step / total) * 100)) : 0;
  const phase = currentPhase(label);

  const pill =
    status === "failed" || gone ? "Failed" : status === "canceled" ? "Stopped" : "Running";
  const stateClass = gone ? "failed" : status;

  return (
    <div className={`tv-task state-${stateClass}`}>
      <div className="tv-task-hd">
        <span className="tv-task-icon" aria-hidden="true">
          <VideoIcon />
        </span>
        <span className="tv-task-title">{title}</span>
        <span className="tv-task-kind">video analysis</span>
        <span className="tv-task-pill">{pill}</span>
      </div>

      {!finished && (
        <>
          {/* biome-ignore lint/a11y/useFocusableInteractive: a progressbar is a live status
              region, not a focus target — it announces progress, it isn't operated. */}
          <div
            className={`tv-task-bar ${determinate ? "is-determinate" : "is-indeterminate"}`}
            role="progressbar"
            aria-valuenow={determinate ? pct : undefined}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className="tv-task-fill" style={determinate ? { width: `${pct}%` } : undefined} />
          </div>
          <div className="tv-task-meta">
            <span className="tv-task-phase">{label}</span>
            <span className="tv-task-elapsed">{fmtElapsed(elapsed)}</span>
          </div>
          <ul className="tv-task-steps">
            {PHASES.map((p, i) => {
              const state = i < phase ? "done" : i === phase ? "active" : "pending";
              const detail = state === "active" && determinate ? ` ${step}/${total}` : "";
              return (
                <li key={p.key} className={`tv-task-step is-${state}`}>
                  <span className="tv-task-dot" aria-hidden="true" />
                  <span className="tv-task-step-label">
                    {p.label}
                    {detail}
                  </span>
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            className="tv-task-stop"
            onClick={onStop}
            disabled={stopping || status === "canceled"}
          >
            {stopping || status === "canceled" ? "Stopping…" : "Stop"}
          </button>
        </>
      )}

      {finished && status !== "done" && (
        <p className="tv-task-terminal">
          {status === "canceled"
            ? "Stopped — the analysis was cancelled."
            : (data?.error ?? "That analysis couldn't be completed.")}
        </p>
      )}
    </div>
  );
}
