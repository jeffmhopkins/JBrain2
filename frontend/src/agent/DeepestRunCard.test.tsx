import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DeepestRunCard } from "./FullBrainSurface";
import type { ToolActivity } from "./transcript";

function tool(progress: NonNullable<ToolActivity["progress"]>): ToolActivity {
  return { id: "t1", name: "deepest_research", progress };
}

const RUN = {
  round: 3,
  sources: 62,
  coverageLabel: "~70% covered",
  elapsedLabel: "24 min",
  status: "running" as const,
};

describe("DeepestRunCard (R8, variant A)", () => {
  it("wraps the deep_research timeline with the amber deepest identity + a round line", () => {
    const { container } = render(
      <DeepestRunCard run={RUN} tool={tool({ step: 4, total: 0, label: "Filling 2 gaps" })} />,
    );
    // The amber "deepest" identity + the background sub-label.
    expect(screen.getByText("Deepest research")).toBeInTheDocument();
    expect(screen.getByText("running in the background")).toBeInTheDocument();
    // The coarse per-round meta line (a background run advances per checkpoint tick).
    expect(screen.getByText(/round 3 · 62 sources · ~70% covered · 24 min/)).toBeInTheDocument();
    // It REUSES the deep_research timeline wholesale — all eight stages render, step 4 live.
    for (const name of ["Plan", "Research", "Cross-check", "Coverage", "Gap-fill", "Write"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    expect(container.querySelectorAll(".fb-drp-step.done")).toHaveLength(3);
    expect(container.querySelectorAll(".fb-drp-step.active")).toHaveLength(1);
    expect(screen.getByText("Filling 2 gaps")).toBeInTheDocument();
  });

  it("drops the live round line once the run is no longer running", () => {
    render(
      <DeepestRunCard
        run={{ ...RUN, status: "done" }}
        tool={tool({ step: 8, total: 0, label: "" })}
      />,
    );
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.queryByText(/round 3 ·/)).not.toBeInTheDocument();
  });

  it("mounts the sub-agent fan inside the active stage, like deep_research", () => {
    render(
      <DeepestRunCard
        run={RUN}
        tool={tool({ step: 2, total: 0, label: "" })}
        fan={<div data-testid="fan">task agents</div>}
      />,
    );
    expect(screen.getByTestId("fan")).toBeInTheDocument();
  });
});
