import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveTurn, LiveTurns } from "../api/client";
import { VitalsScreen, bucket, formatElapsed } from "./VitalsScreen";

const opsTurns = vi.hoisted(() => vi.fn());
const history = vi.hoisted(() => ({
  samples: [] as { at: number; gpu: number | null; tps: number | null }[],
}));

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  api: { opsTurns, opsTurnDetail, opsVitalsHistory },
}));
vi.mock("../hostVitals", () => ({
  vitalsHistory: () => history.samples,
  seedVitalsHistory: seedSpy,
}));

const opsTurnDetail = vi.hoisted(() => vi.fn());
const opsVitalsHistory = vi.hoisted(() => vi.fn());
const seedSpy = vi.hoisted(() => vi.fn());

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
    opsTurnDetail.mockReset().mockResolvedValue({ steps: [], output: null, prompt: null });
    opsVitalsHistory.mockReset().mockResolvedValue([]);
    seedSpy.mockReset();
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

  it("says a turn's prompt was not recorded rather than leaving a blank", async () => {
    // Prompts live in the server's memory for the life of the process, so a run from
    // before a restart genuinely has none — the screen explains that.
    opsTurnDetail.mockResolvedValue({ steps: [], output: null, prompt: null });
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/Not recorded for this turn/)).toBeInTheDocument();
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

  it("shows the step trail and the streaming raw output", async () => {
    opsTurnDetail.mockResolvedValue({
      steps: [
        {
          idx: 0,
          kind: "notes.search",
          name: '"heat pump" · 11 hits',
          ok: true,
          cost_tokens: 400,
          error: null,
        },
        {
          idx: 1,
          kind: "web.fetch",
          name: "acca.org/manual-j",
          ok: null,
          cost_tokens: 0,
          error: null,
        },
      ],
      output: {
        live: true,
        answer: "Short version: the heuristic is oversizing you.",
        reasoning: "Two sources disagree on the sizing rule.",
        steps: [],
      },
    });
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("acca.org/manual-j")).toBeInTheDocument();
    // A step still in flight reads as live, not as failed.
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("streaming")).toBeInTheDocument();
    expect(screen.getByText(/the heuristic is oversizing you/)).toBeInTheDocument();
    expect(screen.getByText(/Two sources disagree/)).toBeInTheDocument();
  });

  it("says a turn has produced nothing yet rather than showing a blank box", async () => {
    opsTurnDetail.mockResolvedValue({ steps: [], output: null });
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/has not returned a first token/)).toBeInTheDocument();
  });

  it("marks output read from the transcript rather than implying it is live", async () => {
    // A sub-agent has no live handle, so its output comes from the stored transcript.
    // Labelling that as streaming would be a lie.
    opsTurnDetail.mockResolvedValue({
      steps: [],
      output: { live: false, answer: "MXZ-SM36: rated 36,000 BTU.", reasoning: "", steps: [] },
    });
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/Read from the stored transcript/)).toBeInTheDocument();
    expect(screen.queryByText("streaming")).toBeNull();
  });

  it("stops polling a settled turn's output", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    opsTurns.mockResolvedValue(roster([turn({ status: "error" })]));
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);
    await waitFor(() => expect(opsTurnDetail).toHaveBeenCalled());
    const calls = opsTurnDetail.mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });

    // A settled turn's output cannot change, so one fetch is the whole job.
    expect(opsTurnDetail.mock.calls.length).toBe(calls);
  });

  it("plots the token rate as its own channel on the same axis", () => {
    const now = Date.now();
    const samples = [
      { at: now - 500, gpu: 90, tps: 48 },
      { at: now - 400, gpu: 91, tps: null },
    ];

    expect(bucket(samples, 1, 2, (x) => x.tps)).toContain(48);
    // The GPU channel is unaffected by a gap in the other one.
    expect(bucket(samples, 1, 2, (x) => x.gpu)).toContain(91);
  });

  it("seeds the graph from the server's recorded history", async () => {
    // Without this the plot starts empty after a reload — you could not open the screen
    // to look at the spike you had just felt.
    opsVitalsHistory.mockResolvedValue([{ at_ms: 1_000_000, gpu: 61 }]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    await waitFor(() => expect(seedSpy).toHaveBeenCalledWith([{ at_ms: 1_000_000, gpu: 61 }]));
  });

  it("survives having no recorded history", async () => {
    opsVitalsHistory.mockRejectedValue(new Error("nope"));
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    // The graph still fills from the live stream; a missing past is not an error state.
    expect(await screen.findByText("Running now")).toBeInTheDocument();
  });

  it("shows the prompt the model actually received, behind a toggle", async () => {
    opsTurnDetail.mockResolvedValue({
      steps: [],
      output: null,
      prompt: {
        system: "You are jerv.",
        messages: [{ role: "user", content: "size the heat pump" }],
        tools: ["web.fetch"],
        round_index: 3,
        truncated: false,
        system_chars: 4200,
        message_chars: 18_400,
      },
    });
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    // Collapsed by default — it is the largest thing on the screen.
    const toggle = await screen.findByRole("button", { name: /Show the .* actually sent/ });
    expect(screen.queryByText("You are jerv.")).toBeNull();

    fireEvent.click(toggle);

    expect(screen.getByText(/You are jerv./)).toBeInTheDocument();
    expect(screen.getByText(/size the heat pump/)).toBeInTheDocument();
    expect(screen.getByText("round 3")).toBeInTheDocument();
  });

  it("says a clipped prompt was clipped rather than showing it as short", async () => {
    opsTurnDetail.mockResolvedValue({
      steps: [],
      output: null,
      prompt: {
        system: "s",
        messages: [{ role: "user", content: "x" }],
        tools: [],
        round_index: 1,
        truncated: true,
        system_chars: 4000,
        message_chars: 500_000,
      },
    });
    render(<VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /Show the .* actually sent/ }));

    // The size reported is the REAL one, not the clipped length.
    expect(screen.getByText(/Clipped for display/)).toHaveTextContent("504k chars");
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
      { at: now - 900, gpu: 4, tps: null },
      { at: now - 800, gpu: 97, tps: null },
      { at: now - 700, gpu: 6, tps: null },
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
    const columns = bucket([{ at: now - 600_000, gpu: 88, tps: null }], 60, 2);

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
