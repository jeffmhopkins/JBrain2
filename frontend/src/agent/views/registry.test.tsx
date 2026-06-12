import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ViewPayload } from "../types";
import { ToolView, isKnownView } from "./registry";

function payload(over: Partial<ViewPayload>): ViewPayload {
  return { view: "", surface: "inline", data: {}, refs: [], ...over };
}

describe("ToolView registry", () => {
  it("renders nothing for an unknown component name (the invariant)", () => {
    const { container } = render(<ToolView payload={payload({ view: "evil_widget" })} />);
    expect(container.firstChild).toBeNull();
    expect(isKnownView("evil_widget")).toBe(false);
  });

  it("renders a stat_block from data-only slots", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "stat_block",
          data: { label: "LDL", value: "118", unit: "mg/dL", tone: "warn" },
        })}
      />,
    );
    expect(getByText("LDL")).toBeInTheDocument();
    expect(getByText("118")).toBeInTheDocument();
    expect(getByText("mg/dL")).toBeInTheDocument();
  });

  it("renders a data_table with header and rows", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "data_table",
          data: { columns: ["date", "value"], rows: [["2026-01-01", "5.4"]] },
        })}
      />,
    );
    expect(getByText("date")).toBeInTheDocument();
    expect(getByText("2026-01-01")).toBeInTheDocument();
    expect(getByText("5.4")).toBeInTheDocument();
  });

  it("renders citation chips from refs, pointer-not-copy", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "citation_card",
          data: { title: "Sources" },
          refs: [
            { kind: "note", note_id: "n1", label: "lab note" },
            { kind: "entity", entity_id: "e1", label: "Dr. Lin", domain: "health" },
          ],
        })}
      />,
    );
    expect(getByText("Sources")).toBeInTheDocument();
    expect(getByText("lab note")).toBeInTheDocument();
    expect(getByText("Dr. Lin")).toBeInTheDocument();
  });

  it("renders a list_card checklist with checked state", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "list_card",
          data: {
            list_id: "L1",
            title: "Groceries",
            items: [
              { id: "a", body: "eggs", checked: false },
              { id: "b", body: "milk", checked: true },
            ],
          },
        })}
      />,
    );
    expect(getByText("Groceries")).toBeInTheDocument();
    expect(getByText("eggs")).toBeInTheDocument();
    // The checked item carries the checked row class (theme draws the tick).
    expect(getByText("milk").closest(".tv-list-row")).toHaveClass("checked");
  });

  it("tolerates missing/extra slots without crashing", () => {
    const { container } = render(<ToolView payload={payload({ view: "data_table" })} />);
    expect(container.querySelector("table")).toBeInTheDocument();
    // A list_card with no items renders the empty row, not a crash.
    const empty = render(<ToolView payload={payload({ view: "list_card" })} />);
    expect(empty.getByText("empty")).toBeInTheDocument();
  });
});
