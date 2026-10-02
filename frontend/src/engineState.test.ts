import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api/client";
import { engineState, switchStatus } from "./components/engineFixtures";
import {
  activeSince,
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
