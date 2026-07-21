import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type DeferredResult, api } from "../api/client";
import { TaskStatus } from "./TaskStatus";

// The card polls the deferred-result endpoint; stub it so each test scripts a sequence
// of poll outcomes (running / rejected / done) and drives them with fake timers. The
// auto-resume claim is stubbed too — its boolean decides whether THIS card fires.
vi.mock("../api/client", () => ({
  api: {
    deferredResult: vi.fn(),
    cancelDeferredResult: vi.fn(async () => {}),
    claimDeferredResult: vi.fn(async () => true),
  },
}));
const deferredResult = vi.mocked(api.deferredResult);
const claimDeferredResult = vi.mocked(api.claimDeferredResult);

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

const running = (label = "Analyzing frame 2/8"): DeferredResult => ({
  result_id: "r1",
  status: "running",
  progress: { step: 2, total: 8, label },
  result: null,
  error: null,
});
const done = (): DeferredResult => ({
  result_id: "r1",
  status: "done",
  progress: {},
  result: { summary: "ok", resume_message: "Done: the rocket launched." },
  error: null,
});

function renderCard(onComplete = vi.fn()) {
  render(
    <TaskStatus
      resultId="r1"
      title="Analyzing video"
      renderResult={(r) => <div>RESULT {String(r.summary)}</div>}
      onComplete={onComplete}
    />,
  );
  return onComplete;
}

describe("TaskStatus", () => {
  it("maps 'Reading captions…' to the open stage, not frame analysis", async () => {
    // The caption fetch runs before frames (captions-first), so its label must light the
    // "Open the stream" step — not "Analyze the frames" (the old `caption` match bug, which
    // showed frames active while the label still said "Reading captions…").
    vi.useFakeTimers();
    deferredResult.mockResolvedValue(running("Reading captions…"));
    renderCard();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const open = screen.getByText(/Open the stream/).closest("li");
    const frames = screen.getByText(/Analyze the frames/).closest("li");
    expect(open?.className).toContain("is-active");
    expect(frames?.className).not.toContain("is-active");
  });

  it("rides out a transient poll failure and still reaches the result", async () => {
    // A single failed poll between good reads is a blip, not a failure: the card keeps
    // polling and reaches the finished result (the exact production bug — a 7-min job's
    // one tunnel hiccup falsely showed "Failed").
    vi.useFakeTimers();
    deferredResult
      .mockResolvedValueOnce(running())
      .mockRejectedValueOnce(new Error("blip"))
      .mockResolvedValue(done());
    const onComplete = renderCard();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    }); // first read: running
    expect(screen.getByText(/Analyzing frame/)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    }); // the failing poll — must NOT declare failure
    expect(screen.queryByText(/Offline|couldn't be completed/)).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    }); // next read: done → swap to the result view
    expect(screen.getByText(/RESULT ok/)).toBeInTheDocument();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onComplete).toHaveBeenCalledWith("Done: the rocket launched.");
  });

  it("only gives up after many consecutive failures, and says it's still running", async () => {
    vi.useFakeTimers();
    deferredResult.mockResolvedValueOnce(running()).mockRejectedValue(new Error("down"));
    renderCard();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    }); // running
    // A handful of consecutive failures is tolerated — still no terminal state.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600 * 4);
    });
    expect(screen.queryByText(/Lost contact/)).toBeNull();

    // Past the consecutive-failure ceiling it gives up — as "still running on the server",
    // not a failed job (the work is safe; the connection isn't).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600 * 8);
    });
    expect(screen.getByText(/Lost contact/)).toBeInTheDocument();
    expect(screen.getByText("Offline")).toBeInTheDocument();
  });

  it("resumes on a card that mounts already-done, once it wins the claim", async () => {
    // The job finished while nothing was watching the card (a backgrounded/killed tab
    // reopens minutes later, already-done). The card must still auto-resume — claim the
    // one follow-up and, having won, prompt jerv with the transcript. This is the fix: the
    // transcript reaches the model even without an observed running→done transition.
    vi.useFakeTimers();
    deferredResult.mockResolvedValue(done());
    claimDeferredResult.mockResolvedValue(true);
    const onComplete = renderCard();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText(/RESULT ok/)).toBeInTheDocument();
    expect(claimDeferredResult).toHaveBeenCalledWith("r1");
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onComplete).toHaveBeenCalledWith("Done: the rocket launched.");
  });

  it("does not re-prompt when the claim was already taken (a reload / second tab)", async () => {
    // Exactly-once is server-side: a reload or a second tab attempts the claim but loses
    // it (the row was already resumed), so this card shows the result WITHOUT re-prompting.
    vi.useFakeTimers();
    deferredResult.mockResolvedValue(done());
    claimDeferredResult.mockResolvedValue(false);
    const onComplete = renderCard();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText(/RESULT ok/)).toBeInTheDocument();
    expect(claimDeferredResult).toHaveBeenCalledWith("r1");
    expect(onComplete).not.toHaveBeenCalled();
  });
});
