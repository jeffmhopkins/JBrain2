import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ProposalsPanel } from "./ProposalsPanel";
import type { ProposalSummary } from "./types";

const PROPOSAL: ProposalSummary = {
  id: "p1",
  kind: "wiki-restructure",
  status: "staged",
  domain: "health",
  title: "Restructure — Health wiki cleanup",
  node_count: 6,
};

describe("ProposalsPanel", () => {
  it("shows the awaiting-nothing state when empty", () => {
    render(<ProposalsPanel proposals={[]} onOpen={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByText(/it never writes on its own/)).toBeInTheDocument();
  });

  it("renders staged proposals and opens one on tap", () => {
    const onOpen = vi.fn();
    render(<ProposalsPanel proposals={[PROPOSAL]} onOpen={onOpen} onClose={vi.fn()} />);
    expect(screen.getByText("Restructure — Health wiki cleanup")).toBeInTheDocument();
    expect(screen.getByText("wiki-restructure · 6 operations")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Restructure — Health wiki cleanup"));
    expect(onOpen).toHaveBeenCalledWith(PROPOSAL);
  });

  it("singularises a one-operation proposal", () => {
    render(
      <ProposalsPanel
        proposals={[{ ...PROPOSAL, node_count: 1, kind: "correction" }]}
        onOpen={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("correction · 1 operation")).toBeInTheDocument();
  });

  it("returns to chat via the back control", () => {
    const onClose = vi.fn();
    render(<ProposalsPanel proposals={[]} onOpen={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByLabelText("Back to chat"));
    expect(onClose).toHaveBeenCalled();
  });
});
