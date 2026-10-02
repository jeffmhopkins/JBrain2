import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type EngineSnapshot,
  hhmm,
  resetEngineStore,
  setEngineNavigator,
  setEngineState,
  useEngineSnapshot,
} from "../engineState";
import { EngineBanner, engineBanners } from "./EngineBanner";
import { engineState, switchStatus } from "./engineFixtures";

function snap(over: Partial<EngineSnapshot> = {}): EngineSnapshot {
  return { state: engineState(), error: null, armed: null, dismissed: new Set(), ...over };
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

  it("is steel while Flash-Next serves, with since and Switch back", () => {
    const [b] = engineBanners(snap({ state: flashOn }));
    expect(b?.tone).toBe("steel");
    expect(b?.title).toBe("Flash-Next active");
    expect(b?.detail).toBe(`since ${hhmm("2026-10-02T00:24:00Z")}`);
    expect(b?.action.label).toBe("Switch back");
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
    // Stacks under the engine strip rather than replacing it.
    expect(
      engineBanners(snap({ state: { ...flashOn, perplexity_running: true } })).map((b) => b.tone),
    ).toEqual(["steel", "amber"]);
  });
});

describe("EngineBanner", () => {
  it("renders nothing until the store has a reading", () => {
    const { container } = render(<EngineBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("Switch back opens Ops with the Standard switch armed", () => {
    const ops = vi.fn();
    setEngineNavigator({ ops, models: vi.fn() });
    let armed: string | null = null;
    function Probe() {
      armed = useEngineSnapshot().armed;
      return null;
    }
    render(
      <>
        <EngineBanner />
        <Probe />
      </>,
    );
    act(() => setEngineState(flashOn));
    expect(screen.getByText("Flash-Next active")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Switch back" }));
    expect(ops).toHaveBeenCalledTimes(1);
    expect(armed).toBe("standard");
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
