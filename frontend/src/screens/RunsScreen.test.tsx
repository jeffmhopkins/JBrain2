import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type RunDetail, type RunSummary, type SweepTrigger, api } from "../api/client";
import { RunsScreen } from "./RunsScreen";

const NOW = new Date().toISOString();

const RUNNING: RunSummary = {
  id: "r1",
  kind: "integration",
  status: "running",
  name: "integrate_note",
  started_at: NOW,
  duration_ms: null,
  step_count: 3,
  cost_tokens: 4100,
  last_error: null,
  progress_note: "processed 12 of 30 emails",
};

const PIPELINE_RUN: RunSummary = {
  id: "r3",
  kind: "pipeline",
  status: "error",
  name: "predicate_sweep",
  started_at: NOW,
  duration_ms: 31000,
  step_count: 4,
  cost_tokens: 6700,
  last_error: "ocr_attachment",
  progress_note: null,
};

const RUNS: RunSummary[] = [RUNNING, PIPELINE_RUN];

const DETAIL: RunDetail = {
  id: "r3",
  kind: "pipeline",
  status: "error",
  name: "predicate_sweep",
  started_at: NOW,
  duration_ms: 31000,
  step_count: 4,
  cost_tokens: 6700,
  stop_reason: "step_error",
  progress_note: null,
  steps: [
    {
      idx: 0,
      kind: "model",
      name: "classify domain",
      ok: true,
      cost_tokens: 300,
      job_id: null,
      error: null,
      detail: [
        { event: "llm.complete", task: "note.extract", timestamp: "2026-06-24T02:00:01Z" },
        { event: "integration.done", committed: 9, review: 1, timestamp: "2026-06-24T02:00:03Z" },
      ],
    },
    {
      idx: 1,
      kind: "job",
      name: "ocr_attachment",
      ok: false,
      cost_tokens: 1100,
      job_id: "job-7",
      error: "TimeoutError: vision adapter timeout after 30s",
    },
  ],
};

const SWEEPS: SweepTrigger[] = [
  { id: "t1", pipeline: "consolidate_predicates", label: "Consolidate" },
];

function mount(opts: { runs?: RunSummary[]; sweeps?: SweepTrigger[]; queueDepth?: number } = {}) {
  vi.spyOn(api, "runs").mockResolvedValue(opts.runs ?? RUNS);
  vi.spyOn(api, "run").mockResolvedValue(DETAIL);
  vi.spyOn(api, "sweepTriggers").mockResolvedValue(opts.sweeps ?? SWEEPS);
  vi.spyOn(api, "queueDepth").mockResolvedValue(opts.queueDepth ?? 0);
  const onClose = vi.fn();
  render(<RunsScreen onClose={onClose} />);
  return { onClose };
}

describe("RunsScreen", () => {
  it("renders the run log and derived status tiles", async () => {
    mount();
    expect(await screen.findByText("integrate_note")).toBeInTheDocument();
    expect(screen.getByText("predicate_sweep")).toBeInTheDocument();
    // 'error' status renders as the "failed" label, never the raw word.
    expect(screen.getAllByText("failed").length).toBeGreaterThan(0);
    // Tiles: one active (running), one failed today.
    const active = screen.getByText("runs active now").closest(".runs-tile");
    expect(within(active as HTMLElement).getByText("1")).toBeInTheDocument();
    const failed = screen.getByText("failed today").closest(".runs-tile");
    expect(within(failed as HTMLElement).getByText("1")).toBeInTheDocument();
  });

  it("counts a queued run as waiting, not active, and shows the job-queue depth", async () => {
    const queued: RunSummary = {
      id: "rq",
      kind: "pipeline",
      status: "queued",
      name: "daily_inbox_triage",
      started_at: NOW,
      duration_ms: null,
      step_count: 2,
      cost_tokens: 0,
      last_error: null,
      progress_note: null,
    };
    mount({ runs: [RUNNING, queued], queueDepth: 4 });
    expect(await screen.findByText("daily_inbox_triage")).toBeInTheDocument();
    // The queued run waits behind the worker: "active now" stays at the one running.
    const active = screen.getByText("runs active now").closest(".runs-tile");
    expect(within(active as HTMLElement).getByText("1")).toBeInTheDocument();
    // Its row reads "queued" and "waiting to start", not a 0-token summary.
    expect(screen.getByText("queued")).toBeInTheDocument();
    expect(screen.getByText(/waiting to start/)).toBeInTheDocument();
    // The "jobs queued" tile now shows a real backlog instead of "—".
    const queue = screen.getByText("jobs queued").closest(".runs-tile");
    expect(within(queue as HTMLElement).getByText("4")).toBeInTheDocument();
  });

  it("shows a running run's live progress note in place of the stats line", async () => {
    mount();
    // The in-flight run surfaces its "processed X of Y" note; the done/failed run
    // still shows the duration/steps/tokens summary.
    expect(await screen.findByText("processed 12 of 30 emails")).toBeInTheDocument();
    expect(screen.getByText(/4 steps/)).toBeInTheDocument();
  });

  it("raises the split panel with the step tree on tapping a run", async () => {
    mount();
    fireEvent.click(await screen.findByText("predicate_sweep"));
    // The shared Sheet hosts the panel: its step tree + the failing step's error.
    const sheet = await screen.findByRole("dialog");
    expect(await within(sheet).findByText("classify domain")).toBeInTheDocument();
    expect(within(sheet).getByText("ocr_attachment")).toBeInTheDocument();
    expect(within(sheet).getByText(/vision adapter timeout/)).toBeInTheDocument();
  });

  it("shows a step's captured log trace (the full-logs view)", async () => {
    mount();
    fireEvent.click(await screen.findByText("predicate_sweep"));
    const sheet = await screen.findByRole("dialog");
    // The trace is a collapsed disclosure summarizing its event count...
    expect(await within(sheet).findByText("2 log events")).toBeInTheDocument();
    // ...whose body carries each event's message + its structured fields.
    expect(within(sheet).getByText(/integration\.done/)).toBeInTheDocument();
    expect(within(sheet).getByText(/committed=9/)).toBeInTheDocument();
  });

  it("fires a sweep trigger through B's endpoint", async () => {
    const runTrigger = vi.spyOn(api, "runTrigger").mockResolvedValue();
    mount();
    fireEvent.click(await screen.findByText("Consolidate"));
    await waitFor(() => expect(runTrigger).toHaveBeenCalledWith("t1"));
    expect(await screen.findByRole("status")).toHaveTextContent(/Fired/);
  });

  it("hides the sweep row and shows an empty state when there are none", async () => {
    mount({ runs: [], sweeps: [] });
    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument();
    expect(screen.queryByText("Run a sweep now")).not.toBeInTheDocument();
  });

  const AGENT_RUN: RunSummary = {
    id: "ra",
    kind: "agent",
    status: "done",
    name: "agent",
    started_at: NOW,
    duration_ms: 48000,
    step_count: 9,
    cost_tokens: 21400,
    last_error: null,
    progress_note: null,
  };
  const RECONCILE: RunSummary = {
    id: "rc",
    kind: "pipeline",
    status: "done",
    name: "reconcile_pending_notes",
    started_at: NOW,
    duration_ms: 105,
    step_count: 1,
    cost_tokens: 0,
    last_error: null,
    progress_note: null,
  };

  it("hides a kind when its show/hide chip is toggled off", async () => {
    mount({ runs: [AGENT_RUN, RUNNING, PIPELINE_RUN] });
    // The pipeline run (predicate_sweep) shows until its chip is switched off.
    expect(await screen.findByText("predicate_sweep")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Pipeline"));
    expect(screen.queryByText("predicate_sweep")).not.toBeInTheDocument();
    // The other kinds stay put, and the count line advertises what's hidden.
    expect(screen.getByText("integrate_note")).toBeInTheDocument();
    expect(screen.getByText(/pipeline hidden/)).toBeInTheDocument();
  });

  it("hides reconcile sweeps from the filter sheet", async () => {
    mount({ runs: [RECONCILE, RUNNING] });
    expect(await screen.findByText("reconcile_pending_notes")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Filter runs"));
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText("Hide reconcile sweeps"));
    // The 0-token housekeeping run drops out; the real integration stays.
    expect(screen.queryByText("reconcile_pending_notes")).not.toBeInTheDocument();
    expect(screen.getByText("integrate_note")).toBeInTheDocument();
  });

  it("restores the full list when the filter is reset", async () => {
    mount({ runs: [AGENT_RUN, PIPELINE_RUN] });
    await screen.findByText("predicate_sweep");
    fireEvent.click(screen.getByText("Pipeline"));
    expect(screen.queryByText("predicate_sweep")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("reset"));
    expect(screen.getByText("predicate_sweep")).toBeInTheDocument();
  });

  it("returns to Ops via the back control", async () => {
    const { onClose } = mount();
    await screen.findByText("integrate_note");
    fireEvent.click(screen.getByLabelText("Back to Ops"));
    expect(onClose).toHaveBeenCalled();
  });

  it("swallows touch events so the parent's down-swipe dismiss never arms", async () => {
    // Mounted inside Ops's subscreen, App's down-swipe handler would otherwise
    // receive these and climb out from under the overlay.
    const onParentTouchStart = vi.fn();
    const onParentTouchMove = vi.fn();
    vi.spyOn(api, "runs").mockResolvedValue(RUNS);
    vi.spyOn(api, "run").mockResolvedValue(DETAIL);
    vi.spyOn(api, "sweepTriggers").mockResolvedValue(SWEEPS);
    render(
      // Stands in for App's swipe-armed subscreen wrapper.
      <div onTouchStart={onParentTouchStart} onTouchMove={onParentTouchMove}>
        <RunsScreen onClose={vi.fn()} />
      </div>,
    );
    const surface = (await screen.findByText("Runs")).closest(".runs-screen");
    fireEvent.touchStart(surface as HTMLElement, { touches: [{ clientX: 0, clientY: 0 }] });
    fireEvent.touchMove(surface as HTMLElement, { touches: [{ clientX: 0, clientY: 200 }] });
    expect(onParentTouchStart).not.toHaveBeenCalled();
    expect(onParentTouchMove).not.toHaveBeenCalled();
  });
});
