import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type RunDetail, type RunSummary, type SweepTrigger, api } from "../api/client";
import { RunsScreen } from "./RunsScreen";

const NOW = new Date().toISOString();

const RUNS: RunSummary[] = [
  {
    id: "r1",
    kind: "integration",
    status: "running",
    name: "integrate_note",
    started_at: NOW,
    duration_ms: null,
    step_count: 3,
    cost_tokens: 4100,
    last_error: null,
  },
  {
    id: "r3",
    kind: "pipeline",
    status: "error",
    name: "predicate_sweep",
    started_at: NOW,
    duration_ms: 31000,
    step_count: 4,
    cost_tokens: 6700,
    last_error: "ocr_attachment",
  },
];

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
  steps: [
    {
      idx: 0,
      kind: "model",
      name: "classify domain",
      ok: true,
      cost_tokens: 300,
      job_id: null,
      error: null,
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

function mount(opts: { runs?: RunSummary[]; sweeps?: SweepTrigger[] } = {}) {
  vi.spyOn(api, "runs").mockResolvedValue(opts.runs ?? RUNS);
  vi.spyOn(api, "run").mockResolvedValue(DETAIL);
  vi.spyOn(api, "sweepTriggers").mockResolvedValue(opts.sweeps ?? SWEEPS);
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

  it("raises the split panel with the step tree on tapping a run", async () => {
    mount();
    fireEvent.click(await screen.findByText("predicate_sweep"));
    // The shared Sheet hosts the panel: its step tree + the failing step's error.
    const sheet = await screen.findByRole("dialog");
    expect(await within(sheet).findByText("classify domain")).toBeInTheDocument();
    expect(within(sheet).getByText("ocr_attachment")).toBeInTheDocument();
    expect(within(sheet).getByText(/vision adapter timeout/)).toBeInTheDocument();
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
