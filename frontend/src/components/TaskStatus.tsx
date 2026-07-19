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
  /** Called ONCE, when the job is done, with the server-authored `resume_message`, so the
   * controller sends the auto-resume turn that prompts jerv. Fires whether or not this
   * card witnessed the running→done transition — including a card that mounts already-done
   * (a reopen after the job finished off-screen) — but only after WINNING the server-side
   * one-shot claim, so a reload or a second tab never double-prompts. This is the reliable
   * path that gets the transcript into the model's context even when the analysis finished
   * while nothing was watching the card (DEFERRED_TOOL_CALLS_PLAN.md P3). */
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
// A long analysis polls for minutes, so a single transient blip (a tunnel hiccup, a
// momentary 5xx) is essentially guaranteed and must NOT read as failure — only give up
// after this many CONSECUTIVE failures (~a few seconds of no contact). A success resets
// the count, so the card rides out blips and keeps following the job to completion.
const MAX_POLL_FAILURES = 8;
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
  // Auto-resume fires exactly once. This card attempts it as soon as the job is done —
  // whether or not it saw the transition — and a server-side atomic claim (`resumed_at`)
  // decides the single winner, so a reopen-after-finish still resumes and a reload/second
  // tab never double-prompts. `fired` guards against this card racing the claim with
  // itself across ticks.
  const onCompleteRef = useRef(onComplete);
  const fired = useRef(false);
  const failures = useRef(0);

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
        failures.current = 0; // a good read clears any transient-failure streak
        setData(next);
      } catch {
        // A blip is not a failure — the job runs detached and is almost certainly still
        // going. Only give up after several consecutive misses (the row may have been
        // reaped); the work itself is safe on the server regardless.
        if (!alive) return;
        failures.current += 1;
        if (failures.current >= MAX_POLL_FAILURES) setGone(true);
      }
    };
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [resultId, finished]);

  // The auto-resume, decoupled from the progress poll: once the job is done, claim the one
  // follow-up turn and fire it if we win the claim. Separate because it must run even when
  // the card MOUNTS already-done (the poll stops at a terminal state, so it can't carry
  // this) — the case a job that finished off-screen used to miss entirely. Fires at most
  // once (the `fired` guard + the server's atomic claim); a transient claim failure retries
  // on the poll cadence rather than silently dropping the resume.
  const doneWithResult = status === "done" && data?.result != null;
  useEffect(() => {
    if (!doneWithResult || !data?.result || fired.current) return;
    let alive = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const message = resumeMessage(data.result);
    const claim = async () => {
      try {
        const won = await api.claimDeferredResult(resultId);
        if (!alive) return;
        fired.current = true;
        if (won) onCompleteRef.current?.(message);
      } catch {
        if (alive) retry = setTimeout(claim, POLL_MS); // a blip — the claim is idempotent
      }
    };
    void claim();
    return () => {
      alive = false;
      if (retry) clearTimeout(retry);
    };
  }, [doneWithResult, data?.result, resultId]);

  // A live elapsed timer while the work runs. Anchored to the server's start time once a
  // poll returns it, so reopening the chat mid-run shows the TRUE elapsed (not time since
  // this mount) — the seamless-reconnect requirement. Falls back to mount time until the
  // first poll lands.
  const serverStart = data?.started_at ? Date.parse(data.started_at) : Number.NaN;
  const anchor = Number.isNaN(serverStart) ? startedAt.current : serverStart;
  useEffect(() => {
    if (finished) return;
    const id = setInterval(
      () => setElapsed(Math.max(0, Math.floor((Date.now() - anchor) / 1000))),
      1000,
    );
    return () => clearInterval(id);
  }, [finished, anchor]);

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

  const pill = gone
    ? "Offline"
    : status === "failed"
      ? "Failed"
      : status === "canceled"
        ? "Stopped"
        : "Running";
  // A poll give-up (gone) is a lost connection, not a failed job — an amber warn, not the
  // rose error tone reserved for an analysis that actually failed.
  const stateClass = gone ? "canceled" : status;

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
          {gone && status === "running"
            ? "Lost contact with the analysis — it's still running on the server; reopen this chat to see the result."
            : status === "canceled"
              ? "Stopped — the analysis was cancelled."
              : (data?.error ?? "That analysis couldn't be completed.")}
        </p>
      )}
    </div>
  );
}
