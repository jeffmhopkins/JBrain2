import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { closeHomeBackLayer, useHomeBackDepth, useRegisterHomeBack } from "./homeBack";

// A stand-in for the home surface: two nested layers (an open "proposal" atop a "panel")
// that unwind one per back, exactly as HomeScreen registers them. The parent reads the
// depth and drives back() the way App folds it into overlay depth and calls the closer.
function Home() {
  const [panel, setPanel] = useState(false);
  const [proposal, setProposal] = useState(false);
  useRegisterHomeBack((panel ? 1 : 0) + (proposal ? 1 : 0), () => {
    if (proposal) {
      setProposal(false);
      return true;
    }
    if (panel) {
      setPanel(false);
      return true;
    }
    return false;
  });
  return (
    <div>
      <div>depth:{useHomeBackDepth()}</div>
      <button
        type="button"
        onClick={() => {
          setPanel(true);
          setProposal(true);
        }}
      >
        open
      </button>
    </div>
  );
}

function Harness() {
  return (
    <div>
      <Home />
      <button type="button" onClick={() => closeHomeBackLayer()}>
        back
      </button>
    </div>
  );
}

describe("homeBack", () => {
  it("closeHomeBackLayer is a no-op returning false when nothing is registered", () => {
    expect(closeHomeBackLayer()).toBe(false);
  });

  it("reports the surface's own depth and unwinds one layer per back", () => {
    render(<Harness />);
    expect(screen.getByText("depth:0")).toBeTruthy();

    fireEvent.click(screen.getByText("open"));
    expect(screen.getByText("depth:2")).toBeTruthy();

    // Back closes the proposal first (topmost), leaving the panel.
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByText("depth:1")).toBeTruthy();

    // Next back closes the panel; the surface is at the bare chat again.
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByText("depth:0")).toBeTruthy();

    // A further back finds nothing to close, so App falls through to its no-op.
    expect(closeHomeBackLayer()).toBe(false);
  });

  it("drops the registration on unmount so depth returns to zero", () => {
    const { unmount } = render(<Harness />);
    fireEvent.click(screen.getByText("open"));
    expect(screen.getByText("depth:2")).toBeTruthy();
    unmount();
    expect(closeHomeBackLayer()).toBe(false);
  });
});
