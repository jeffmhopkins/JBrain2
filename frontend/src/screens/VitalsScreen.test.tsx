import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveTurn, LiveTurns } from "../api/client";
import { VitalsScreen, bucket, formatElapsed } from "./VitalsScreen";

const opsTurns = vi.hoisted(() => vi.fn());
const history = vi.hoisted(() => ({ samples: [] as { at: number; gpu: number | null }[] }));

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  api: { opsTurns },
}));
vi.mock("../hostVitals", () => ({ vitalsHistory: () => history.samples }));

function turn(over: Partial<LiveTurn> = {}): LiveTurn {
  return {
    id: "run_parent",
    kind: "agent",
    status: "running",
    name: "agent",
    started_at: "2026-08-16T07:41:56Z",
    elapsed_ms: 252_000,
    step_count: 9,
    cost_tokens: 38_200,
    progress_note: "synthesising 6 sources",
    parent_run_id: null,
    session_id: "sess_4b1e",
    domain_code: null,
    ran_as: "scoped",
    prompt_version: "fullbrain@v14",
    trigger_pipeline: null,
    call: {
      provider: "anthropic",
      model: "claude-opus-4-6",
      reasoning_effort: "high",
      context_window: 200_000,
      persona: "jerv",
      tools: ["notes.search", "web.fetch"],
      user_message: "Dig into heat pump sizing.",
    },
    ...over,
  };
}

function roster(turns: LiveTurn[], gpu: number | null = 40): LiveTurns {
  return { turns, gpu_busy_percent: gpu };
}

describe("VitalsScreen", () => {
  beforeEach(() => {
    history.samples = [];
    opsTurns.mockReset().mockResolvedValue(roster([turn()]));
  });
  afterEach(() => vi.useRealTimers());

  it("lists what is running with its progress", async () => {
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("synthesising 6 sources")).toBeInTheDocument();
    expect(screen.getByText("Running now")).toBeInTheDocument();
  });

  it("nests a fan's children under the turn that spawned them", async () => {
    opsTurns.mockResolvedValue(
      roster([
        turn(),
        turn({ id: "kid_a", kind: "subagent", name: "spec sweep", parent_run_id: "run_parent" }),
      ]),
    );
    const { container } = render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    await screen.findByText("spec sweep");

    expect(container.querySelectorAll(".vitals-row.child")).toHaveLength(1);
  });

  it("opens a turn's detail level when its row is tapped", async () => {
    const onSelect = vi.fn();
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={onSelect} />);
    await screen.findByText("synthesising 6 sources");

    fireEvent.click(screen.getByRole("button", { name: /synthesising 6 sources/ }));

    expect(onSelect).toHaveBeenCalledWith("run_parent");
  });

  it("shows the call a turn was set up with", async () => {
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/claude-opus-4-6 · anthropic/)).toBeInTheDocument();
    expect(screen.getByText("200,000 tokens")).toBeInTheDocument();
    expect(screen.getByText("Dig into heat pump sizing.")).toBeInTheDocument();
  });

  it("says the prompt is not stored rather than implying it has it", async () => {
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/not stored, so it is not shown/)).toBeInTheDocument();
  });

  it("tells the truth when the GPU is busy with no turns running", async () => {
    // GPU busy covers the whole box — image generation, model loads — so this is a
    // real state, not a bug, and the screen has to say which it is.
    opsTurns.mockResolvedValue(roster([], 94));
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("No agent turns running.")).toBeInTheDocument();
    expect(screen.getByText(/94%/)).toBeInTheDocument();
    expect(screen.getByText(/counts everything the box does/)).toBeInTheDocument();
  });

  it("renders a run that carries no call stamp", async () => {
    opsTurns.mockResolvedValue(
      roster([turn({ call: null, kind: "pipeline", trigger_pipeline: "nightly-reconcile" })]),
    );
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/before its call was recorded/)).toBeInTheDocument();
    expect(screen.getByText("nightly-reconcile")).toBeInTheDocument();
  });

  it("distinguishes a wildcard toolset from an empty one", async () => {
    // null is the registry wildcard ("every in-scope tool"); [] is genuinely none.
    // Rendering both the same would tell the owner a jerv turn holds no tools.
    opsTurns.mockResolvedValue(roster([turn({ call: { tools: null } })]));
    const wildcard = render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);
    expect(await screen.findByText("every in-scope tool")).toBeInTheDocument();
    wildcard.unmount();

    opsTurns.mockResolvedValue(roster([turn({ call: { tools: [] } })]));
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("none")).toBeInTheDocument();
  });

  it("stops polling the roster when unmounted", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { unmount } = render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);
    await waitFor(() => expect(opsTurns).toHaveBeenCalled());
    const calls = opsTurns.mock.calls.length;

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(12_000);
    });

    expect(opsTurns.mock.calls.length).toBe(calls);
  });
});

describe("bucket", () => {
  it("takes the peak of each column, not the mean", () => {
    // A 15-minute mean flattens a 10-second spike into nothing — hiding exactly what
    // the screen was opened to find.
    const now = Date.now();
    const samples = [
      { at: now - 900, gpu: 4 },
      { at: now - 800, gpu: 97 },
      { at: now - 700, gpu: 6 },
    ];

    const columns = bucket(samples, 1, 1);

    expect(columns[0]).toBe(97);
  });

  it("leaves a gap where nothing was sampled rather than drawing a zero", () => {
    const columns = bucket([], 60, 4);

    expect(columns).toEqual([null, null, null, null]);
  });

  it("ignores samples older than the window", () => {
    const now = Date.now();
    const columns = bucket([{ at: now - 600_000, gpu: 88 }], 60, 2);

    expect(columns).toEqual([null, null]);
  });
});

describe("formatElapsed", () => {
  it("reads in the unit that fits", () => {
    expect(formatElapsed(42_000)).toBe("42s");
    expect(formatElapsed(252_000)).toBe("4m 12s");
    expect(formatElapsed(7_500_000)).toBe("2h 05m");
  });
});
