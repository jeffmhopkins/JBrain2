import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BoxEvent, LiveTurn, LiveTurns } from "../api/client";
import { VitalsScreen, formatElapsed, perSecond } from "./VitalsScreen";

const opsTurns = vi.hoisted(() => vi.fn());
const history = vi.hoisted(() => ({
  samples: [] as { at: number; gpu: number | null; tps: number | null }[],
}));

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  api: { opsTurns, opsTurnDetail, opsVitalsHistory, opsVitalsEvents, opsReportClientVitals },
}));
vi.mock("../hostVitals", () => ({
  vitalsHistory: () => history.samples,
  seedVitalsHistory: seedSpy,
  vitalsDiagnostics: () => ({ frames: 0, sinceLastFrameMs: -1 }),
}));

const opsTurnDetail = vi.hoisted(() => vi.fn());
const opsVitalsHistory = vi.hoisted(() => vi.fn());
const opsVitalsEvents = vi.hoisted(() => vi.fn());
const seedSpy = vi.hoisted(() => vi.fn());
const opsReportClientVitals = vi.hoisted(() => vi.fn(async () => {}));

function turn(over: Partial<LiveTurn> = {}): LiveTurn {
  return {
    id: "run_parent",
    kind: "agent",
    status: "running",
    name: "agent",
    started_at: "2026-08-16T07:41:56Z",
    ended_at: null,
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

function event(over: Partial<BoxEvent> = {}): BoxEvent {
  return {
    at_ms: Date.now() - 12_000,
    ended_ms: null,
    kind: "model_load",
    subject: "gpt-oss-120b",
    detail: "",
    status: "running",
    source: "api",
    ...over,
  };
}

describe("VitalsScreen", () => {
  beforeEach(() => {
    history.samples = [];
    opsTurns.mockReset().mockResolvedValue(roster([turn()]));
    opsTurnDetail.mockReset().mockResolvedValue({ steps: [], output: null, prompt: null });
    opsVitalsHistory.mockReset().mockResolvedValue([]);
    opsVitalsEvents.mockReset().mockResolvedValue([]);
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

    expect(await screen.findByText(/No agent turns in the last/)).toBeInTheDocument();
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

    expect(await screen.findByText(/no live handle/)).toBeInTheDocument();
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

    // A 2s window, because sub-second offsets straddle the whole-second grid depending
    // on where `now` falls inside its second.
    expect(perSecond(samples, 2, (x) => x.tps, now)).toContain(48);
    // The GPU channel is unaffected by a gap in the other one.
    expect(perSecond(samples, 2, (x) => x.gpu, now)).toContain(91);
  });

  it("seeds the graph from the server's recorded history", async () => {
    // Without this the plot starts empty after a reload — you could not open the screen
    // to look at the spike you had just felt.
    opsVitalsHistory.mockResolvedValue([{ at_ms: 1_000_000, gpu: 61 }]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    await waitFor(() => expect(seedSpy).toHaveBeenCalledWith([{ at_ms: 1_000_000, gpu: 61 }]));
  });

  it("re-reads the box's record so a reconnect is not a hole in the trace", async () => {
    // The live stream is the graph's source, and nothing in the browser records the
    // seconds it is down for. The box recorded them; the merge only fills seconds this
    // session lacks, so re-reading a short window patches the gap instead of leaving it.
    vi.useFakeTimers();
    opsVitalsHistory.mockResolvedValue([]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(opsVitalsHistory).toHaveBeenCalledWith(900); // the whole ring, on arrival

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    // …and only the recent past on the timer: it is patching a reconnect, not reloading
    // a quarter of an hour of history every half minute.
    expect(opsVitalsHistory).toHaveBeenLastCalledWith(120);
    vi.useRealTimers();
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

  it("does not let a settled turn's stale response land under another turn", async () => {
    // Tapping a child row changes runId WITHOUT unmounting. The settled-turn path used
    // to return before setting `cancelled`, so the previous turn's in-flight fetch
    // could resolve last and render its output under the new turn's header.
    let resolveFirst: (v: unknown) => void = () => {};
    opsTurnDetail.mockImplementationOnce(
      () =>
        new Promise((res) => {
          resolveFirst = res;
        }),
    );
    // The roster must hold BOTH, or `selected` is null and no detail renders at all.
    opsTurns.mockResolvedValue(
      roster([turn({ status: "done" }), turn({ id: "kid", status: "done" })]),
    );
    const { rerender, container } = render(
      <VitalsScreen selectedTurnId="run_parent" onSelectTurn={vi.fn()} />,
    );
    await waitFor(() => expect(opsTurnDetail).toHaveBeenCalledWith("run_parent"));

    opsTurnDetail.mockResolvedValue({
      steps: [],
      output: { live: false, answer: "the CHILD's answer", reasoning: "", steps: [] },
      prompt: null,
    });
    rerender(<VitalsScreen selectedTurnId="kid" onSelectTurn={vi.fn()} />);
    await waitFor(() =>
      expect(container.querySelector(".vitals-raw pre")?.textContent).toContain("CHILD"),
    );

    // The first turn's response arrives late — it must not replace what is on screen.
    resolveFirst({
      steps: [],
      output: { live: false, answer: "the PARENT's answer", reasoning: "", steps: [] },
      prompt: null,
    });
    await act(async () => {});

    const shown = container.querySelector(".vitals-raw pre")?.textContent ?? "";
    expect(shown).not.toContain("PARENT");
    expect(shown).toContain("CHILD");
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

describe("perSecond", () => {
  const at = (secondsAgo: number, gpu: number | null) => ({
    at: NOW_MS - secondsAgo * 1000,
    gpu,
    tps: null,
  });
  const NOW_MS = 1_760_000_000_000;

  it("gives every second its own slot, at full resolution", () => {
    const slots = perSecond([at(2, 10), at(1, 20), at(0, 30)], 3, (s) => s.gpu, NOW_MS);

    expect(slots).toEqual([10, 20, 30]);
  });

  it("keeps a sample in the same slot as the window scrolls", () => {
    // The bucketer this replaced derived its edges from Date.now() on every tick, so the
    // partition slid and samples visibly hopped columns while the data sat still.
    const sample = at(5, 42);
    const first = perSecond([sample], 10, (s) => s.gpu, NOW_MS);
    const later = perSecond([sample], 10, (s) => s.gpu, NOW_MS + 3000);

    expect(first.indexOf(42)).toBe(4);
    expect(later.indexOf(42)).toBe(1); // moved left by exactly the 3s that elapsed
  });

  it("leaves a second with no reading null rather than zero", () => {
    const slots = perSecond([at(2, 10), at(0, 30)], 3, (s) => s.gpu, NOW_MS);

    expect(slots).toEqual([10, null, 30]);
  });

  it("drops samples outside the window", () => {
    expect(perSecond([at(600, 88)], 60, (s) => s.gpu, NOW_MS).every((v) => v === null)).toBe(true);
  });

  it("keeps the higher of two readings landing in one second", () => {
    // Both land in the second BEFORE `now` (NOW_MS is a whole second), so the window has
    // to be wide enough to include it.
    const slots = perSecond(
      [
        { at: NOW_MS - 400, gpu: 12, tps: null },
        { at: NOW_MS - 200, gpu: 97, tps: null },
      ],
      2,
      (s) => s.gpu,
      NOW_MS,
    );

    expect(slots).toEqual([97, null]);
  });
});

describe("what the box was doing", () => {
  beforeEach(() => {
    history.samples = [];
    opsTurns.mockReset().mockResolvedValue(roster([]));
    opsTurnDetail.mockReset().mockResolvedValue({ steps: [], output: null, prompt: null });
    opsVitalsHistory.mockReset().mockResolvedValue([]);
    opsVitalsEvents.mockReset().mockResolvedValue([]);
    seedSpy.mockReset();
  });

  it("says what is loading while it is still loading", async () => {
    // The line the whole surface exists for: the GPU is pinned, nothing is in the roster,
    // and the reason is a model reading tens of GB into memory RIGHT NOW.
    opsVitalsEvents.mockResolvedValue([event()]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("loading gpt-oss-120b…")).toBeInTheDocument();
  });

  it("says how far in a running load is, where a long name cannot clip it", async () => {
    // The elapsed count beside the row answers "how long has this been going". Only the
    // fraction answers "how much longer" — which on a load that reads tens of GB is the
    // question actually being asked while the trace above it sits pinned.
    //
    // OUTSIDE the label on purpose. It used to be appended to it, and the label truncates
    // with an ellipsis — so on a phone the percentage was the part that got cut off, which
    // is what the owner reported seeing (a row reading "loading gpt-oss-120b…" with the
    // figure clipped) while the box's own record held 12%.
    opsVitalsEvents.mockResolvedValue([event({ percent: 0.43 })]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("loading gpt-oss-120b…")).toBeInTheDocument();
    expect(screen.getByText("43%")).toBeInTheDocument();
  });

  it("shows a load with no parseable fraction without inventing one", async () => {
    // A gateway build that prints no progress line is the ordinary case, and "0%" on a
    // load that is halfway through reads as stuck.
    opsVitalsEvents.mockResolvedValue([event({ percent: null })]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("loading gpt-oss-120b…")).toBeInTheDocument();
  });

  it("puts no fraction on a load that has finished", async () => {
    // "loaded gpt-oss-120b 100%" would put a progress figure on a row whose whole point is
    // that there is no more progress to make.
    opsVitalsEvents.mockResolvedValue([
      event({ status: "ok", ended_ms: Date.now() - 2_000, percent: 1 }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("loaded gpt-oss-120b")).toBeInTheDocument();
  });

  it("says why a model was evicted, not just that it was", async () => {
    opsVitalsEvents.mockResolvedValue([
      event({
        kind: "model_unload",
        subject: "qwen35",
        detail: "to make room for gpt-oss-120b",
        status: "ok",
        ended_ms: Date.now() - 11_000,
      }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("unloaded qwen35")).toBeInTheDocument();
    expect(screen.getByText("to make room for gpt-oss-120b")).toBeInTheDocument();
  });

  it("says when the memory ledger disagreed with the gate that let a load through", async () => {
    // The reservation ledger runs in shadow: it records what it WOULD have refused and
    // refuses nothing. That verdict is the entire evidence base for whether it is ever allowed
    // to decide — and the owner runs this box remotely with no terminal, so a verdict that
    // only exists in a container log is one nobody can act on.
    opsVitalsEvents.mockResolvedValue([
      event({
        kind: "ledger_shadow_refusal",
        subject: "qwen3-coder-next",
        detail: "qwen3-coder-next needs 59.6 GB of host memory and there is 47.0 GB free",
        status: "failed",
        ended_ms: Date.now() - 5_000,
      }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(
      await screen.findByText("the memory ledger would have refused qwen3-coder-next"),
    ).toBeInTheDocument();
    // The arithmetic has to come with it, or it is a refusal the owner cannot act on.
    expect(
      screen.getByText("qwen3-coder-next needs 59.6 GB of host memory and there is 47.0 GB free"),
    ).toBeInTheDocument();
  });

  it("names an ENFORCED ledger refusal as an action, not a question", async () => {
    // The shadow kind's authoritative twin. Out of shadow, a refusal costs the owner a load,
    // so it must not read softer than what it did — and it must still carry the arithmetic.
    opsVitalsEvents.mockResolvedValue([
      event({
        kind: "ledger_refusal",
        subject: "qwen3-coder-next",
        detail: "qwen3-coder-next needs 59.6 GB of host memory and there is 47.0 GB free",
        status: "failed",
        ended_ms: Date.now() - 5_000,
      }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(
      await screen.findByText("the memory ledger refused qwen3-coder-next"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("qwen3-coder-next needs 59.6 GB of host memory and there is 47.0 GB free"),
    ).toBeInTheDocument();
  });

  it("labels the prompt-cache disk save and restore in the owner's language", async () => {
    // kv_prefix events explain latency the owner would otherwise puzzle over: a restore row
    // is WHY a turn was fast after a boot; its detail carries the numbers.
    opsVitalsEvents.mockResolvedValue([
      event({
        kind: "kv_prefix_restored",
        subject: "gpt-oss-120b",
        detail: "28757-token jerv prefix restored from disk in 1840 ms",
        status: "ok",
        ended_ms: Date.now() - 5_000,
      }),
      event({
        kind: "kv_prefix_saved",
        subject: "gpt-oss-120b",
        detail: "28757-token jerv prefix saved to disk",
        status: "ok",
        ended_ms: Date.now() - 9_000,
      }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(
      await screen.findByText("restored gpt-oss-120b's prompt cache from disk"),
    ).toBeInTheDocument();
    expect(screen.getByText("saved gpt-oss-120b's prompt cache to disk")).toBeInTheDocument();
    expect(
      screen.getByText("28757-token jerv prefix restored from disk in 1840 ms"),
    ).toBeInTheDocument();
  });

  it("says when a background job was given up on because its model cannot fit", async () => {
    // The subject is the JOB KIND: a directly-enqueued job has no run step, so this row is
    // the only trace of the failure the owner has, and the detail is its payload.
    opsVitalsEvents.mockResolvedValue([
      event({
        kind: "job_refused_no_room",
        subject: "ocr_attachment",
        detail: "gpt-oss-120b needs 200.0 GB of host memory and this box has 118.0 GB to give",
        status: "failed",
        ended_ms: Date.now() - 5_000,
      }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(
      await screen.findByText("gave up on the ocr attachment job — its model cannot fit"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "gpt-oss-120b needs 200.0 GB of host memory and this box has 118.0 GB to give",
      ),
    ).toBeInTheDocument();
  });

  it("marks work the background half of the box started", async () => {
    // A load nothing on screen asked for is the confusing one — say where it came from.
    opsVitalsEvents.mockResolvedValue([event({ source: "worker" })]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("background")).toBeInTheDocument();
  });

  it("puts a load still in flight above the history", async () => {
    opsVitalsEvents.mockResolvedValue([
      event({
        kind: "model_unload",
        subject: "qwen35",
        status: "ok",
        at_ms: Date.now() - 2_000,
        ended_ms: Date.now() - 1_000,
      }),
      event({ at_ms: Date.now() - 30_000 }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    await screen.findByText("loading gpt-oss-120b…");
    const rows = screen.getAllByText(/loading gpt-oss-120b…|unloaded qwen35/);
    expect(rows[0]).toHaveTextContent("loading gpt-oss-120b…");
  });

  it("does not claim a render finished when its process died under it", async () => {
    // The api restarted mid-render, so the row was never settled. Saying "rendered an
    // image" would claim a success nobody observed.
    opsVitalsEvents.mockResolvedValue([
      event({ kind: "image_render", subject: "qwen-image", status: "stale" }),
    ]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("qwen-image — render stopped reporting")).toBeInTheDocument();
    expect(screen.queryByText(/rendered an image/)).not.toBeInTheDocument();
  });

  it("does not draw an empty card on a quiet box", async () => {
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);
    await waitFor(() => expect(opsVitalsEvents).toHaveBeenCalled());

    expect(screen.queryByText(/On the box, last/)).not.toBeInTheDocument();
  });

  it("points the empty roster at the work that explains the GPU", async () => {
    opsTurns.mockResolvedValue(roster([], 94));
    opsVitalsEvents.mockResolvedValue([event({ kind: "image_render", subject: "qwen-image" })]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText(/because of the work listed above/)).toBeInTheDocument();
  });

  it("asks for the window the graph is showing", async () => {
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);
    await waitFor(() => expect(opsVitalsEvents).toHaveBeenCalledWith(60));

    fireEvent.click(screen.getByRole("button", { name: "15m" }));

    await waitFor(() => expect(opsVitalsEvents).toHaveBeenCalledWith(900));
  });

  it("survives the box having nothing to say about itself", async () => {
    // An explanation that breaks the screen it is explaining is worse than no explanation.
    opsVitalsEvents.mockRejectedValue(new Error("nope"));
    opsTurns.mockResolvedValue(roster([turn()]));
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("synthesising 6 sources")).toBeInTheDocument();
  });
});

describe("the roster over a window", () => {
  beforeEach(() => {
    opsTurnDetail.mockResolvedValue({ steps: [], output: null, prompt: null });
    opsVitalsHistory.mockResolvedValue([]);
    opsVitalsEvents.mockReset().mockResolvedValue([]);
  });

  it("asks the server for the window the graph is showing", async () => {
    opsTurns.mockResolvedValue(roster([]));
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);
    await waitFor(() => expect(opsTurns).toHaveBeenCalledWith(60));

    fireEvent.click(screen.getByRole("button", { name: "15m" }));

    // The list used to ignore the range entirely, so a 15-minute graph sat above a
    // roster that only ever showed what was running in that instant.
    await waitFor(() => expect(opsTurns).toHaveBeenCalledWith(900));
  });

  it("separates turns that are running from turns that merely ran", async () => {
    opsTurns.mockResolvedValue(
      roster([
        turn({ id: "run_live", name: "live one" }),
        turn({
          id: "run_done",
          name: "done one",
          status: "ok",
          ended_at: "2026-08-16T07:45:00Z",
          parent_run_id: null,
        }),
      ]),
    );
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("Running now")).toBeInTheDocument();
    expect(screen.getByText(/^Finished, last/)).toBeInTheDocument();
  });

  it("does not age a turn that has already finished", async () => {
    // Its elapsed time stopped when it did; ticking it would have every settled turn in
    // a 15-minute window appear to still be going.
    vi.useFakeTimers();
    try {
      opsTurns.mockResolvedValue(
        roster([
          turn({
            id: "run_done",
            status: "ok",
            ended_at: "2026-08-16T07:45:00Z",
            elapsed_ms: 42_000,
          }),
        ]),
      );
      render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);
      await vi.advanceTimersByTimeAsync(0);
      expect(screen.getByText("42s")).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(5000);

      expect(screen.getByText("42s")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("a payload that omits ended_at", () => {
  it("reads as still running, not as finished", async () => {
    // `undefined === null` is false, so a strict check dropped every turn from a payload
    // without the field into the settled group and badged live work as done.
    opsTurnDetail.mockResolvedValue({ steps: [], output: null, prompt: null });
    opsVitalsHistory.mockResolvedValue([]);
    const { ended_at: _omitted, ...withoutField } = turn();
    opsTurns.mockResolvedValue(roster([withoutField as LiveTurn]));
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("Running now")).toBeInTheDocument();
    expect(screen.queryByText(/^Finished, last/)).not.toBeInTheDocument();
  });
});

describe("formatElapsed", () => {
  it("reads in the unit that fits", () => {
    expect(formatElapsed(42_000)).toBe("42s");
    expect(formatElapsed(252_000)).toBe("4m 12s");
    expect(formatElapsed(7_500_000)).toBe("2h 05m");
  });
});

describe("stream health", () => {
  it("says the meter is blind when no frame has ever arrived", async () => {
    // The failure this exists for: the top bar blank while the box serves 97% happily. A
    // connection the browser declines to open leaves no server-side trace, so the browser
    // has to be the one that says so.
    opsTurns.mockResolvedValue(roster([]));
    opsVitalsHistory.mockResolvedValue([]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    expect(await screen.findByText("no frames")).toBeInTheDocument();
  });

  it("reports what the browser saw back to the box", async () => {
    // So it can be read through the debug token rather than needing someone to be holding
    // the phone when it happens.
    opsTurns.mockResolvedValue(roster([]));
    opsVitalsHistory.mockResolvedValue([]);
    render(<VitalsScreen selectedTurnId={null} onSelectTurn={vi.fn()} />);

    await waitFor(() => expect(opsReportClientVitals).toHaveBeenCalled());
  });
});
