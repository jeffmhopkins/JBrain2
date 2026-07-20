import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DeepResearchProgress } from "./FullBrainSurface";
import type { ToolActivity } from "./transcript";

function tool(progress: NonNullable<ToolActivity["progress"]>): ToolActivity {
  return { id: "t1", name: "deep_research", progress };
}

describe("DeepResearchProgress", () => {
  it("renders the full pipeline checklist with prior steps done and the active one live", () => {
    const { container } = render(
      <DeepResearchProgress tool={tool({ step: 6, total: 0, label: "Writing the report" })} />,
    );
    // All eight canonical stages are always visible (you-are-here + what's-left).
    for (const name of [
      "Plan",
      "Research",
      "Cross-check",
      "Coverage",
      "Gap-fill",
      "Write",
      "Critique",
      "Revise",
    ]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    // Step 6 (Write) is active; 1-5 done; 7-8 still to do.
    expect(container.querySelectorAll(".fb-drp-step.done")).toHaveLength(5);
    expect(container.querySelectorAll(".fb-drp-step.active")).toHaveLength(1);
    expect(container.querySelectorAll(".fb-drp-step.todo")).toHaveLength(2);
    // The active phase's live label shows too.
    expect(screen.getByText("Writing the report")).toBeInTheDocument();
  });

  it("streams the report markdown into a live pane during the Write phase", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({
          step: 6,
          total: 0,
          label: "Writing the report",
          preview: "# Findings\n\nGrid storage is cheap.",
        })}
      />,
    );
    // The accumulating report renders as markdown (a heading), not a blank spinner.
    expect(screen.getByRole("heading", { name: "Findings" })).toBeInTheDocument();
    expect(container.querySelector(".fb-drp-report")).toBeInTheDocument();
  });

  it("shows no report pane before the Write phase streams anything", () => {
    const { container } = render(
      <DeepResearchProgress tool={tool({ step: 2, total: 0, label: "Researching 4 angle(s)" })} />,
    );
    expect(container.querySelector(".fb-drp-report")).not.toBeInTheDocument();
    // At step 2 only the first stage is done and the second is active.
    expect(container.querySelectorAll(".fb-drp-step.done")).toHaveLength(1);
    expect(container.querySelectorAll(".fb-drp-step.active")).toHaveLength(1);
  });

  it("mounts the sub-agent fan inside the active stage's slot, not below the checklist", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 2, total: 0, label: "Researching 4 angle(s)" })}
        fan={<div data-testid="fan">roster</div>}
      />,
    );
    // The fan lives inside the ACTIVE step's panel — the seam the redesign hinges on.
    const activePanel = container.querySelector(".fb-drp-step.active .fb-drp-panel");
    expect(activePanel).toBeInTheDocument();
    expect(activePanel?.querySelector('[data-testid="fan"]')).toBeInTheDocument();
    // And it hangs under exactly one stage (the live one), never duplicated per row.
    expect(container.querySelectorAll('[data-testid="fan"]')).toHaveLength(1);
    expect(container.querySelectorAll(".fb-drp-panel")).toHaveLength(1);
  });

  it("keeps a home for a fan that spawned before the first phase event (step 0)", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 0, total: 0 })}
        fan={<div data-testid="fan">roster</div>}
      />,
    );
    // Before any phase lands, Plan is the active host so the fan is never orphaned.
    expect(container.querySelectorAll(".fb-drp-step.done")).toHaveLength(0);
    const active = container.querySelector(".fb-drp-step.active");
    expect(active?.querySelector(".fb-drp-name")?.textContent).toBe("Plan");
    expect(active?.querySelector('[data-testid="fan"]')).toBeInTheDocument();
  });
});
