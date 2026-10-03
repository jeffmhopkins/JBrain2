// The top bar's right cluster is a READOUT of what the box is doing — the vitals
// chart, and the radio when one is held. The radio icon used to sit in the composer's
// icon row next to Attach, which put a live-state indicator among controls you reach
// for; these pin where it lives now and, more importantly, that it still means exactly
// one thing.

import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { resetEngineStore, setEngineState } from "../engineState";
import { TopBar } from "./TopBar";
import { engineState, switchStatus } from "./engineFixtures";

describe("the top bar's radio icon", () => {
  it("is absent when no radio is held", () => {
    // The icon IS the lease. A radio button showing with nothing tuned would be two
    // facts that can disagree, which is the bug this shape exists to make impossible.
    render(<TopBar syncStatus="synced" />);

    expect(screen.queryByRole("button", { name: "Tuned radio" })).not.toBeInTheDocument();
  });

  it("appears while one is, and opens the sheet", () => {
    const onOpen = vi.fn();
    render(<TopBar syncStatus="synced" radio={{ onOpen }} />);

    screen.getByRole("button", { name: "Tuned radio" }).click();

    expect(onOpen).toHaveBeenCalled();
  });

  it("sits in the right cluster, AFTER the vitals", () => {
    // Outermost, and that is the point rather than a preference: a slot that comes and
    // goes between the wordmark and the chart would shove the chart sideways every time
    // a lease starts or ends. On the edge it only ever grows the row.
    const { container } = render(<TopBar syncStatus="synced" radio={{ onOpen: vi.fn() }} />);

    const right = container.querySelector(".top-bar-right");
    const kids = [...(right?.children ?? [])];
    const vitals = kids.findIndex((el) => el.querySelector(".vitals-gpu") ?? el.matches("button"));
    const radio = kids.findIndex((el) => el.classList.contains("sdr-btn"));
    expect(radio).toBeGreaterThan(-1);
    expect(radio).toBe(kids.length - 1);
    expect(vitals).toBeLessThan(radio);
  });

  it("carries no live dot", () => {
    // The icon's PRESENCE already says a radio is held — it exists only while the lease
    // does. A pulsing dot beside it was a second mark for the same fact, animated, in a
    // row whose whole job is to be glanceable.
    const { container } = render(<TopBar syncStatus="synced" radio={{ onOpen: vi.fn() }} />);

    expect(container.querySelector(".sdr-live")).toBeNull();
  });

  it("is not in the composer's icon row any more", () => {
    const { container } = render(<TopBar syncStatus="synced" radio={{ onOpen: vi.fn() }} />);

    expect(container.querySelector(".foot-icons")).toBeNull();
  });
});

describe("the engine banner under the top bar", () => {
  it("rides every top bar, directly beneath it, once the engine store has a reading", () => {
    resetEngineStore();
    const { container } = render(<TopBar title="Ops" syncStatus="synced" />);
    expect(container.querySelector(".engine-banners")).toBeNull();
    act(() =>
      setEngineState(engineState({ switching: true, switch: switchStatus({ stage: "stopping" }) })),
    );
    const bar = container.querySelector(".top-bar");
    expect(bar?.nextElementSibling?.classList.contains("engine-banners")).toBe(true);
    expect(screen.getByText("Switching to Flash-Next")).toBeInTheDocument();
    resetEngineStore();
  });
});
