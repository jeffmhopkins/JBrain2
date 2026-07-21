import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeepResearchProgress } from "./FullBrainSurface";
import type { SubagentFan as Fan, SubagentChild, ToolActivity } from "./transcript";

function tool(progress: NonNullable<ToolActivity["progress"]>): ToolActivity {
  return { id: "t1", name: "deep_research", progress };
}

function child(over: Partial<SubagentChild> & { childId: string }): SubagentChild {
  return { persona: "research", label: "L", depth: 1, phase: "queued", status: "running", ...over };
}

function fan(children: SubagentChild[], over: Partial<Fan> = {}): Fan {
  return { children, treeSpent: 0, treeBudget: 0, ...over };
}

/** The checklist `<li>` for a named stage (each carries its own agents now). */
function stageLi(container: HTMLElement, name: string): HTMLElement {
  const li = [...container.querySelectorAll<HTMLElement>(".fb-drp-step")].find(
    (el) => el.querySelector(".fb-drp-name")?.textContent === name,
  );
  if (!li) throw new Error(`no stage row for ${name}`);
  return li;
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

  it("nests each stage's agents under its OWN bullet, not all under the live stage", () => {
    // Gap-fill (5) is live; the Research (2) and Cross-check (3) agents already finished.
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 5, total: 0, label: "Filling 2 gap(s)" })}
        running
        fan={fan([
          child({ childId: "g1", label: "Basic mechanism", status: "done", drStage: 2 }),
          child({ childId: "g2", label: "Main symptoms", status: "done", drStage: 2 }),
          child({
            childId: "x1",
            label: "cross-check",
            persona: "review",
            status: "done",
            drStage: 3,
          }),
          child({
            childId: "f1",
            label: "Define terms",
            status: "running",
            phase: "researching",
            drStage: 5,
          }),
        ])}
      />,
    );
    // Research keeps its two finished agents under itself (not dragged under Gap-fill).
    const research = stageLi(container, "Research");
    expect(research.querySelector(".fb-sa-sec-name")?.textContent).toBe("Research");
    expect(research.querySelector(".fb-sa-sec-c")?.textContent).toContain("2 agents");
    // Cross-check keeps its single agent.
    const cross = stageLi(container, "Cross-check");
    expect(cross.querySelector(".fb-sa-sec-c")?.textContent).toContain("1 agent");
    // The live Gap-fill agent sits under Gap-fill, not Research.
    const gap = stageLi(container, "Gap-fill");
    expect(within(gap).getByText("Define terms")).toBeInTheDocument();
    expect(within(research).queryByText("Define terms")).not.toBeInTheDocument();
  });

  it("collapses a completed stage's agents to a count, and expands them on click", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 5, total: 0, label: "Filling 1 gap(s)" })}
        running
        fan={fan([
          child({ childId: "g1", label: "Basic mechanism", status: "done", drStage: 2 }),
          child({ childId: "g2", label: "Main symptoms", status: "done", drStage: 2 }),
          child({ childId: "f1", label: "Gap one", status: "running", drStage: 5 }),
        ])}
      />,
    );
    const research = stageLi(container, "Research");
    // Folded on completion: the rows aren't rendered, only the count.
    expect(research.querySelector(".fb-sa-sec-body")).toBeNull();
    expect(screen.queryByText("Basic mechanism")).not.toBeInTheDocument();
    // Tapping the header opens the finished roster.
    const header = research.querySelector<HTMLElement>(".fb-sa-sec-h");
    expect(header).not.toBeNull();
    fireEvent.click(header as HTMLElement);
    expect(within(research).getByText("Basic mechanism")).toBeInTheDocument();
    expect(within(research).getByText("Main symptoms")).toBeInTheDocument();
  });

  it("keeps the live stage's agents expanded while they run", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 2, total: 0, label: "Researching 2 angle(s)" })}
        running
        fan={fan([
          child({
            childId: "g1",
            label: "Angle A",
            status: "running",
            phase: "researching",
            drStage: 2,
          }),
          child({
            childId: "g2",
            label: "Angle B",
            status: "running",
            phase: "researching",
            drStage: 2,
          }),
        ])}
      />,
    );
    const research = stageLi(container, "Research");
    expect(research.querySelector(".fb-sa-sec-body")).not.toBeNull();
    expect(within(research).getByText("Angle A")).toBeInTheDocument();
    expect(within(research).getByText("Angle B")).toBeInTheDocument();
  });

  it("homes a stageless (unstamped) child under the live stage so it is never orphaned", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 2, total: 0, label: "Researching" })}
        running
        fan={fan([
          child({ childId: "u1", label: "loose", status: "running", phase: "researching" }),
        ])}
      />,
    );
    // Research is the live stage (step 2); a drStage-less child rides along under it.
    const research = stageLi(container, "Research");
    expect(within(research).getByText("loose")).toBeInTheDocument();
  });

  it("shows the tree budget + a single cascade Stop once, above the checklist", () => {
    const onStop = vi.fn();
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 2, total: 0, label: "Researching" })}
        running
        onStop={onStop}
        fan={fan(
          [
            child({
              childId: "g1",
              label: "A",
              status: "running",
              phase: "researching",
              drStage: 2,
            }),
          ],
          {
            treeSpent: 318_000,
            treeBudget: 6_000_000,
          },
        )}
      />,
    );
    const bar = container.querySelector(".fb-drp-bar");
    expect(bar).toBeInTheDocument();
    expect(bar?.querySelector(".fb-sa-budget")).toBeInTheDocument();
    // Exactly one Stop for the whole run (not one per stage).
    const stops = screen.getAllByText("■ Stop");
    expect(stops).toHaveLength(1);
    fireEvent.click(stops[0] as HTMLElement);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("keeps a home for a fan that spawned before the first phase event (step 0)", () => {
    const { container } = render(
      <DeepResearchProgress
        tool={tool({ step: 0, total: 0 })}
        running
        fan={fan([
          child({ childId: "u1", label: "early", status: "running", phase: "researching" }),
        ])}
      />,
    );
    // Before any phase lands, Plan is the active host so the fan is never orphaned.
    expect(container.querySelectorAll(".fb-drp-step.done")).toHaveLength(0);
    const active = container.querySelector<HTMLElement>(".fb-drp-step.active");
    expect(active?.querySelector(".fb-drp-name")?.textContent).toBe("Plan");
    expect(within(active as HTMLElement).getByText("early")).toBeInTheDocument();
  });
});
