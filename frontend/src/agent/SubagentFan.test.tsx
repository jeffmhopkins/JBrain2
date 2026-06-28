import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SubagentFan } from "./SubagentFan";
import type { SubagentFan as Fan, SubagentChild } from "./transcript";

function child(over: Partial<SubagentChild> & { childId: string }): SubagentChild {
  return { persona: "research", label: "L", depth: 1, phase: "queued", status: "running", ...over };
}

function fan(children: SubagentChild[], over: Partial<Fan> = {}): Fan {
  return { children, treeSpent: 0, treeBudget: 0, ...over };
}

describe("SubagentFan", () => {
  it("renders a row per child with neutral persona tags and state status words", () => {
    render(
      <SubagentFan
        running
        fan={fan([
          child({ childId: "k1", label: "Pricing", phase: "researching", status: "running" }),
          child({
            childId: "k2",
            label: "Security",
            persona: "research",
            status: "failed",
            phase: "error",
          }),
          child({
            childId: "k3",
            label: "Cross-check",
            persona: "review",
            status: "done",
            stopReason: "end_turn",
          }),
        ])}
      />,
    );
    expect(screen.getByText("Pricing")).toBeInTheDocument();
    expect(screen.getByText("researching")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    // Persona is a neutral tag (text), never a color class.
    expect(screen.getByText("review")).toBeInTheDocument();
    // Header counts running agents while live.
    expect(screen.getByText(/3 agents/)).toBeInTheDocument();
  });

  it("rolls up done · ran · failed when all children have settled", () => {
    render(
      <SubagentFan
        running={false}
        fan={fan([
          child({ childId: "k1", status: "done" }),
          child({ childId: "k2", status: "failed" }),
        ])}
      />,
    );
    expect(screen.getByText(/done · 2 ran · 1 failed/)).toBeInTheDocument();
  });

  it("expands a child to show its summary", () => {
    render(
      <SubagentFan
        running={false}
        fan={fan([child({ childId: "k1", label: "Pricing", status: "done", summary: "3 tiers" })])}
      />,
    );
    expect(screen.queryByText("3 tiers")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Pricing"));
    expect(screen.getByText("3 tiers")).toBeInTheDocument();
  });

  it("shows a budget meter that goes danger at the ceiling", () => {
    render(
      <SubagentFan
        running
        fan={fan([child({ childId: "k1" })], { treeSpent: 1200, treeBudget: 1200 })}
      />,
    );
    const meter = screen.getByRole("meter");
    expect(meter).toHaveClass("danger");
    expect(screen.getByText("budget exhausted")).toBeInTheDocument();
  });

  it("shows a cascade Stop only while running and fires onStop", () => {
    const onStop = vi.fn();
    const { rerender } = render(
      <SubagentFan running fan={fan([child({ childId: "k1" })])} onStop={onStop} />,
    );
    fireEvent.click(screen.getByText("■ Stop"));
    expect(onStop).toHaveBeenCalledOnce();
    // Once settled the Stop is gone.
    rerender(
      <SubagentFan
        running={false}
        fan={fan([child({ childId: "k1", status: "done" })])}
        onStop={onStop}
      />,
    );
    expect(screen.queryByText("■ Stop")).not.toBeInTheDocument();
  });

  it("contains a long fan behind 'show N more'", () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      child({ childId: `k${i}`, label: `Child ${i}` }),
    );
    render(<SubagentFan running fan={fan(many)} />);
    expect(screen.queryByText("Child 11")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("show 4 more"));
    expect(screen.getByText("Child 11")).toBeInTheDocument();
  });
});
