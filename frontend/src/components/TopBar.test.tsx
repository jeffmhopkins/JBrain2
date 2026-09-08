// The top bar's right cluster is a READOUT of what the box is doing — the vitals
// chart, and the radio when one is held. The radio icon used to sit in the composer's
// icon row next to Attach, which put a live-state indicator among controls you reach
// for; these pin where it lives now and, more importantly, that it still means exactly
// one thing.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TopBar } from "./TopBar";

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

  it("sits in the right cluster, beside the vitals", () => {
    // Position is the whole point of the move, so it is worth an assertion: the eye
    // looks for the box's state in one place, and this belongs with it.
    const { container } = render(<TopBar syncStatus="synced" radio={{ onOpen: vi.fn() }} />);

    const right = container.querySelector(".top-bar-right");
    expect(right?.querySelector(".sdr-btn")).not.toBeNull();
  });

  it("is not in the composer's icon row any more", () => {
    const { container } = render(<TopBar syncStatus="synced" radio={{ onOpen: vi.fn() }} />);

    expect(container.querySelector(".foot-icons")).toBeNull();
  });
});
