import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api/client";
import { engineState, switchStatus } from "./components/engineFixtures";
import {
  ENGINE_POLL_FAST_MS,
  ENGINE_POLL_IDLE_MS,
  activeSince,
  hhmm,
  nextDelay,
  peekEngineSnapshot,
  refreshEngine,
  resetEngineStore,
  switchInFlight,
  switchSteps,
  useEnginePolling,
  useEngineSnapshot,
} from "./engineState";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => resetEngineStore());
afterEach(() => {
  resetEngineStore();
  vi.unstubAllGlobals();
});

describe("engine state helpers", () => {
  it("reads a switch as in flight from either signal", () => {
    expect(switchInFlight(null)).toBe(false);
    expect(switchInFlight(engineState())).toBe(false);
    expect(switchInFlight(engineState({ switching: true }))).toBe(true);
    // Another process's switch: not our lock, but a non-terminal stage.
    expect(switchInFlight(engineState({ switch: switchStatus({ stage: "loading" }) }))).toBe(true);
    expect(switchInFlight(engineState({ switch: switchStatus({ stage: "done" }) }))).toBe(false);
  });

  it("dates Flash-Next's tenure only from a finished switch to it", () => {
    expect(activeSince(engineState())).toBeNull();
    const done = switchStatus({ stage: "done", ended_at: "2026-10-02T00:24:00Z" });
    expect(activeSince(engineState({ effective: "flash-next", switch: done }))).not.toBeNull();
    expect(activeSince(engineState({ effective: "standard", switch: done }))).toBeNull();
    // The server's own `effective_since` wins when present.
    expect(
      activeSince(engineState({ effective: "standard", effective_since: "2026-10-02T07:05:00Z" })),
    ).toBe(hhmm("2026-10-02T07:05:00Z"));
  });

  it("labels the five forward steps", () => {
    expect(switchSteps("standard", "flash-next", null).map((s) => s.label)).toEqual([
      "Drain local calls",
      "Stop Standard",
      "Start Flash-Next",
      "Load the test model",
      "Smoke test",
    ]);
  });
});

describe("engine store polling", () => {
  it("shares one request between concurrent refreshes", async () => {
    const fetchMock = vi.fn(async () => json(engineState()));
    vi.stubGlobal("fetch", fetchMock);
    await Promise.all([refreshEngine(), refreshEngine()]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("polls on mount and keeps the last state when a read fails", async () => {
    const fetchMock = vi.fn(async () => json(engineState({ desired: "flash-next" })));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => {
      useEnginePolling();
      return useEngineSnapshot();
    });
    await waitFor(() => expect(result.current.state?.desired).toBe("flash-next"));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "supervisor unreachable" }, 502)),
    );
    await refreshEngine();
    await waitFor(() => expect(result.current.error).toBe("supervisor unreachable"));
    expect(result.current.state?.desired).toBe("flash-next");
  });

  it("does not poll when disabled", async () => {
    const fetchMock = vi.fn(async () => json(engineState()));
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => useEnginePolling(false));
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("engine client", () => {
  it("GETs the engine state", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => json(engineState()));
    vi.stubGlobal("fetch", fetchMock);
    const state = await api.getEngineState();
    expect(state.effective).toBe("standard");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/settings/llm/engine");
  });

  it("POSTs the target and force, and returns the switch status", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      json(switchStatus({ stage: "draining" }), 202),
    );
    vi.stubGlobal("fetch", fetchMock);
    const status = await api.switchEngine("flash-next", true);
    expect(status.stage).toBe("draining");
    const [path, init] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe("/api/settings/llm/engine");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ engine: "flash-next", force: true });
  });

  it("cancel surfaces a past-draining 409 rather than falling through", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      json({ detail: "only a draining switch can be cancelled" }, 409),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await api.cancelEngineSwitch().catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(409);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("defaults force to false and carries a 409's detail on the error", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      json({ detail: "an engine switch is already in progress" }, 409),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await api.switchEngine("standard").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).message).toBe("an engine switch is already in progress");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      engine: "standard",
      force: false,
    });
  });
});

describe("engine store cadence", () => {
  let visibility: DocumentVisibilityState = "visible";
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** A fetch stub whose GET answer the test can swap between beats. */
  function engineFetch(first: () => Response) {
    const ctl = { reply: first, calls: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        ctl.calls += 1;
        return ctl.reply();
      }),
    );
    return ctl;
  }

  it("polls every 30 s idle and every 2 s while a switch runs", async () => {
    const ctl = engineFetch(() => json(engineState()));
    renderHook(() => useEnginePolling());
    await vi.advanceTimersByTimeAsync(0);
    expect(ctl.calls).toBe(1);
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS - 1);
    expect(ctl.calls).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(ctl.calls).toBe(2);
    ctl.reply = () => json(engineState({ switching: true, switch: switchStatus() }));
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS);
    expect(ctl.calls).toBe(3);
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_FAST_MS);
    expect(ctl.calls).toBe(4);
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_FAST_MS);
    expect(ctl.calls).toBe(5);
  });

  it("backs off 2 → 4 → 8 → 16 → 30 s on errors and resets on success", async () => {
    const ctl = engineFetch(() => json(engineState()));
    renderHook(() => useEnginePolling());
    await vi.advanceTimersByTimeAsync(0);
    ctl.reply = () => json({ detail: "bad gateway" }, 502);
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS); // first failure
    expect(ctl.calls).toBe(2);
    const gaps: number[] = [];
    for (const want of [2000, 4000, 8000, 16000, 30000, 30000]) {
      expect(nextDelay()).toBe(want);
      gaps.push(want);
      await vi.advanceTimersByTimeAsync(want);
    }
    expect(ctl.calls).toBe(2 + gaps.length);
    // The last reading is kept, flagged stale.
    expect(peekEngineSnapshot().state?.effective).toBe("standard");
    expect(peekEngineSnapshot().error).toBe("bad gateway");
    ctl.reply = () => json(engineState());
    await vi.advanceTimersByTimeAsync(30000);
    expect(peekEngineSnapshot().error).toBeNull();
    expect(nextDelay()).toBe(ENGINE_POLL_IDLE_MS);
  });

  it("treats a 404 as transient but a 403 as a stop, until the next foreground signal", async () => {
    const ctl = engineFetch(() => json({ detail: "Not Found" }, 404));
    renderHook(() => useEnginePolling());
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(2000);
    expect(ctl.calls).toBe(2); // 404 keeps retrying
    ctl.reply = () => json({ detail: "owner only" }, 403);
    await vi.advanceTimersByTimeAsync(4000);
    expect(ctl.calls).toBe(3);
    await vi.advanceTimersByTimeAsync(120000);
    expect(ctl.calls).toBe(3); // latched
    ctl.reply = () => json(engineState());
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(ctl.calls).toBe(4);
    expect(peekEngineSnapshot().state).not.toBeNull();
  });

  it("stops while hidden and resumes at once on return", async () => {
    const ctl = engineFetch(() => json(engineState()));
    renderHook(() => useEnginePolling());
    await vi.advanceTimersByTimeAsync(0);
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS * 4);
    expect(ctl.calls).toBe(1);
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(ctl.calls).toBe(2);
  });

  it("makes one request per beat for two subscribers, and leaks no timer after unmount", async () => {
    const ctl = engineFetch(() => json(engineState()));
    const a = renderHook(() => useEnginePolling());
    const b = renderHook(() => useEnginePolling());
    await vi.advanceTimersByTimeAsync(0);
    expect(ctl.calls).toBe(1);
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS);
    expect(ctl.calls).toBe(2);
    a.unmount();
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS);
    expect(ctl.calls).toBe(3);
    b.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(ENGINE_POLL_IDLE_MS * 3);
    expect(ctl.calls).toBe(3);
  });
});
