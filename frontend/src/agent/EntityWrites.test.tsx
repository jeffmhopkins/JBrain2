import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EntityWrites } from "./EntityWrites";
import type { FactWrite } from "./types";

function fact(over: Partial<FactWrite> = {}): FactWrite {
  return {
    fact_id: "f1",
    label: "Me lives_in Marina District",
    domain: "general",
    status: "written",
    ...over,
  };
}

describe("the entity-modified rung", () => {
  it("leads with the STATEMENT, and keeps the edge behind it", () => {
    // ⟲ The edge led here, which is the graph's own form: on the entity page the owner
    // has already chosen an entity and a predicate is the column he reads down. In a
    // conversation he has chosen nothing — the sentence is the only thing saying which
    // of his facts this row is — and `size → 60in` over a note about a television reads
    // as a dump of his own words back at him.
    render(
      <EntityWrites
        facts={[
          fact({
            label: "Jeff's television is 60 inches.",
            predicate: "size",
            qualifier: null,
            value: '60"',
          }),
        ]}
        truncated={false}
      />,
    );
    expect(screen.getByText("Jeff's television is 60 inches.")).toBeInTheDocument();
    expect(screen.getByText("size")).toBeInTheDocument();
    expect(screen.getByText('60"')).toBeInTheDocument();
  });

  it("says a value already on file DISAGREED, which lived only in the model's result", () => {
    // `decide()` made the newest value live BY RULE. That unsticks the graph; it does
    // not establish which value is true, and nothing else raises it — no review card is
    // filed for a conversation write. On screen a contradicted supersession rendered
    // exactly like a clean one.
    render(
      <EntityWrites
        facts={[
          fact({
            status: "replaced",
            replaced: 'My tv is 58".',
            value: '60"',
            hold_reason: "attribute_collision",
          }),
        ]}
        truncated={false}
      />,
    );
    expect(screen.getByText(/your notes disagreed/)).toBeInTheDocument();
  });

  it("does not parade a reason the owner cannot act on", () => {
    render(
      <EntityWrites facts={[fact({ hold_reason: "some_internal_code" })]} truncated={false} />,
    );
    expect(screen.queryByText(/some_internal_code/)).not.toBeInTheDocument();
  });

  it("calls a fact it found already on file exactly that, never 'recorded'", () => {
    render(
      <EntityWrites facts={[fact({ outcome: "already" })]} truncated={false} />,
    );
    expect(screen.getByText("already on file")).toBeInTheDocument();
    expect(screen.queryByText("recorded")).not.toBeInTheDocument();
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
    expect(
      within(diff).getByText("↓ this note updated it — the old value is kept as history"),
    ).toBeInTheDocument();
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
