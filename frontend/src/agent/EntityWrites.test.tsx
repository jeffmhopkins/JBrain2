import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EntityWrites } from "./EntityWrites";
import type { FactWrite } from "./types";

function fact(over: Partial<FactWrite> = {}): FactWrite {
  return {
    kind: "fact",
    fact_id: "f1",
    label: "Me lives_in Marina District",
    domain: "general",
    status: "written",
    ...over,
  };
}

describe("the entity-modified rung", () => {
  it("renders the write as the shipped predicate → value edge", () => {
    render(
      <EntityWrites
        facts={[fact({ predicate: "lives_in", qualifier: null, value: "Marina District" })]}
        truncated={false}
      />,
    );
    expect(screen.getByText("lives_in")).toBeInTheDocument();
    expect(screen.getByText("Marina District")).toBeInTheDocument();
  });

  it("falls back to the whole statement when the edge parts are absent", () => {
    render(<EntityWrites facts={[fact()]} truncated={false} />);
    expect(screen.getByText("Me lives_in Marina District")).toBeInTheDocument();
  });

  it("names the domain in words beside its dot, never colour alone", () => {
    render(<EntityWrites facts={[fact({ domain: "health" })]} truncated={false} />);
    expect(screen.getByText("health")).toBeInTheDocument();
  });

  it("marks an attachment-sourced write (D12)", () => {
    render(<EntityWrites facts={[fact({ from_attachment: true })]} truncated={false} />);
    expect(screen.getByText("from a photo")).toBeInTheDocument();
  });

  it("a supersession renders through the app's ONE diff renderer", () => {
    render(
      <EntityWrites
        facts={[
          fact({
            status: "replaced",
            replaced: "Sunset District",
            predicate: "lives_in",
            value: "Marina District",
          }),
        ]}
        truncated={false}
      />,
    );
    const diff = screen.getByLabelText("before and after");
    expect(within(diff).getByText("Sunset District")).toBeInTheDocument();
    // A write that already landed must not read as still pending.
    expect(within(diff).getByText("↓ replaced by this note")).toBeInTheDocument();
    expect(within(diff).queryByText("↓ proposed")).toBeNull();
  });

  it("says a call wrote nothing rather than rendering an empty silence", () => {
    render(<EntityWrites facts={[]} truncated={false} />);
    expect(screen.getByText("nothing was written")).toBeInTheDocument();
  });

  it("says a truncated call showed only the part that ran", () => {
    render(<EntityWrites facts={[fact()]} truncated={true} />);
    expect(screen.getByText(/only the part of it that ran/)).toBeInTheDocument();
  });

  it("offers no edit affordance — correction is conversational (D3)", () => {
    render(
      <EntityWrites
        facts={[fact({ status: "held" }), fact({ fact_id: "f2", status: "replaced" })]}
        truncated={false}
      />,
    );
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  });
});
