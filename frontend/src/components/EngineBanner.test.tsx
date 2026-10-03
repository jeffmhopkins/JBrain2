import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type EngineSnapshot, resetEngineStore, setEngineState } from "../engineState";
import { EngineBanner, engineBanners } from "./EngineBanner";
import { engineState, switchStatus } from "./engineFixtures";

function snap(over: Partial<EngineSnapshot> = {}): EngineSnapshot {
  return {
    state: engineState(),
    error: null,
    lastOk: null,
    armed: null,
    dismissed: new Set(),
    ...over,
  };
}

const flashOn = engineState({
  desired: "flash-next",
  effective: "flash-next",
  running: ["flash-next"],
  switch: switchStatus({ stage: "done", ended_at: "2026-10-02T00:24:00Z" }),
});

beforeEach(() => resetEngineStore());
afterEach(() => resetEngineStore());

describe("engineBanners", () => {
  it("is silent on Standard with nothing running", () => {
    expect(engineBanners(snap())).toEqual([]);
    expect(engineBanners(snap({ state: null }))).toEqual([]);
  });

  it("is silent while Flash-Next serves normally", () => {
    expect(engineBanners(snap({ state: flashOn }))).toEqual([]);
  });

  it("is amber and live while a switch runs", () => {
    const [b] = engineBanners(
      snap({
        state: engineState({ switching: true, switch: switchStatus({ stage: "stopping" }) }),
      }),
    );
    expect(b?.tone).toBe("amber");
    expect(b?.live).toBe(true);
    expect(b?.title).toBe("Switching to Flash-Next");
    expect(b?.detail).toMatch(/stop standard · local AI paused/);
  });

  it("is rose after a rollback until that switch is dismissed", () => {
    const state = engineState({ switch: switchStatus({ id: "sw-7", stage: "rolled_back" }) });
    const [b] = engineBanners(snap({ state }));
    expect(b?.tone).toBe("rose");
    expect(b?.title).toBe("Switch to Flash-Next failed");
    expect(b?.detail).toBe("rolled back to Standard");
    expect(b?.dismiss).toBeDefined();
    expect(engineBanners(snap({ state, dismissed: new Set(["sw-7"]) }))).toEqual([]);
  });

  it("flags a strip read from a state the api can no longer confirm", () => {
    const state = engineState({ switching: true, switch: switchStatus({ stage: "starting" }) });
    const [b] = engineBanners(snap({ state, error: "Request failed: 502" }));
    expect(b?.detail).toMatch(/ · can't reach the engine$/);
    const [fresh] = engineBanners(snap({ state }));
    expect(fresh?.detail).not.toMatch(/can't reach/);
  });

  it("is silent after a cancel — neither amber nor rose", () => {
    const state = engineState({ switching: false, switch: switchStatus({ stage: "cancelled" }) });
    expect(engineBanners(snap({ state }))).toEqual([]);
  });

  it("is rose and not dismissable while no local engine is up", () => {
    const sw = switchStatus({ stage: "failed", no_engine_up: true });
    const [b] = engineBanners(snap({ state: engineState({ running: [], switch: sw }) }));
    expect(b?.tone).toBe("rose");
    expect(b?.title).toBe("No local engine is up");
    expect(b?.dismiss).toBeUndefined();
    // The flag outlives the outage on the switch record; what runs now decides.
    const [after] = engineBanners(snap({ state: engineState({ switch: sw }) }));
    expect(after?.title).toBe("Engine switch failed");
  });

  it("is amber for a fallback (desired ≠ effective)", () => {
    const [b] = engineBanners(snap({ state: engineState({ desired: "flash-next" }) }));
    expect(b?.tone).toBe("amber");
    expect(b?.detail).toBe("Standard serving (fallback)");
  });

  it("adds an amber strip for a perplexity job or a debug hold", () => {
    const perplexity = engineBanners(
      snap({ state: engineState({ perplexity_running: true, oneshot: "perplexity" }) }),
    );
    expect(perplexity.map((b) => b.title)).toEqual(["Perplexity test running"]);
    const held = engineBanners(
      snap({
        state: engineState({
          admission: { closed: true, reason: "debug: tool-probe sweep", until: null },
        }),
      }),
    );
    expect(held.map((b) => [b.tone, b.detail])).toEqual([["amber", "debug: tool-probe sweep"]]);
    // Shows on its own while Flash-Next serves normally (that state has no strip).
    expect(
      engineBanners(snap({ state: { ...flashOn, perplexity_running: true } })).map((b) => b.tone),
    ).toEqual(["amber"]);
  });
});

describe("EngineBanner", () => {
  it("renders nothing until the store has a reading", () => {
    const { container } = render(<EngineBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("dismisses a rollback strip", () => {
    render(<EngineBanner />);
    act(() =>
      setEngineState(engineState({ switch: switchStatus({ id: "sw-8", stage: "rolled_back" }) })),
    );
    expect(screen.getByText("Switch to Flash-Next failed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Switch to Flash-Next failed")).toBeNull();
  });
});
