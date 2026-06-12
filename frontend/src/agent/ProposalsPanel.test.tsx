import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type ProposalSummary, ProposalsPanel } from "./ProposalsPanel";

const PROPOSAL: ProposalSummary = {
  id: "p1",
  kind: "wiki-restructure",
  title: "Restructure — Health wiki cleanup",
  subtitle: "6 operations across 3 articles",
  meta: "from this chat",
  domain: "health",
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
    expect(screen.getByText("6 operations across 3 articles")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Restructure — Health wiki cleanup"));
    expect(onOpen).toHaveBeenCalledWith(PROPOSAL);
  });

  it("returns to chat via the back control", () => {
    const onClose = vi.fn();
    render(<ProposalsPanel proposals={[]} onOpen={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByLabelText("Back to chat"));
    expect(onClose).toHaveBeenCalled();
  });
});
