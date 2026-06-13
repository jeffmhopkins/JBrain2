import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ListOut, api } from "../../api/client";
import type { ViewPayload } from "../types";
import { resetLiveLists } from "./liveList";
import { ToolView, isKnownView } from "./registry";

function payload(over: Partial<ViewPayload>): ViewPayload {
  return { view: "", surface: "inline", data: {}, refs: [], ...over };
}

function listOut(over: Partial<ListOut> = {}): ListOut {
  return { id: "L1", title: "Groceries", domain: "general", archived: false, items: [], ...over };
}

// Keep the shared live-list store (and api spies) from leaking across cases.
afterEach(() => {
  resetLiveLists();
  vi.restoreAllMocks();
});

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

  function listCard(items: { id: string; body: string; checked: boolean }[]) {
    return payload({
      view: "list_card",
      data: { list_id: "L1", title: "Groceries", items },
    });
  }

  it("renders a list_card checklist with checked state", () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline")); // keep the snapshot
    const { getByText } = render(
      <ToolView
        payload={listCard([
          { id: "a", body: "eggs", checked: false },
          { id: "b", body: "milk", checked: true },
        ])}
      />,
    );
    expect(getByText("Groceries")).toBeInTheDocument();
    expect(getByText("eggs")).toBeInTheDocument();
    // The checked item carries the checked row class (theme draws the tick).
    expect(getByText("milk").closest(".tv-list-row")).toHaveClass("checked");
  });

  it("tapping a list_card checkbox toggles the item via the API", async () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline"));
    const setChecked = vi.spyOn(api, "setListItemChecked").mockResolvedValue();
    const { getByLabelText, getByText } = render(
      <ToolView payload={listCard([{ id: "a", body: "eggs", checked: false }])} />,
    );
    fireEvent.click(getByLabelText("Check eggs"));
    // Optimistic: the row flips immediately, and the write is sent.
    expect(getByText("eggs").closest(".tv-list-row")).toHaveClass("checked");
    await waitFor(() => expect(setChecked).toHaveBeenCalledWith("a", true));
    expect(getByLabelText("Uncheck eggs")).toBeInTheDocument();
  });

  it("reverts a list_card toggle when the write fails", async () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "setListItemChecked").mockRejectedValue(new Error("boom"));
    const { getByLabelText, getByText } = render(
      <ToolView payload={listCard([{ id: "a", body: "eggs", checked: false }])} />,
    );
    fireEvent.click(getByLabelText("Check eggs"));
    // It flips optimistically, then snaps back once the write rejects.
    await waitFor(() =>
      expect(getByText("eggs").closest(".tv-list-row")).not.toHaveClass("checked"),
    );
  });

  it("replaces the snapshot with live list state", async () => {
    // The card's payload says milk is open, but the live list has it checked.
    vi.spyOn(api, "getList").mockResolvedValue(
      listOut({ items: [{ id: "b", body: "milk", checked: true }] }),
    );
    const { getByText } = render(
      <ToolView payload={listCard([{ id: "b", body: "milk", checked: false }])} />,
    );
    await waitFor(() => expect(getByText("milk").closest(".tv-list-row")).toHaveClass("checked"));
  });

  it("keeps two cards of the same list in sync on a toggle", async () => {
    vi.spyOn(api, "getList").mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "setListItemChecked").mockResolvedValue();
    const card = listCard([{ id: "a", body: "eggs", checked: false }]);
    // Two cards of list L1 in the same transcript.
    const { container } = render(
      <>
        <ToolView payload={card} />
        <ToolView payload={card} />
      </>,
    );
    const rows = () => [...container.querySelectorAll(".tv-list-row")];
    expect(rows()).toHaveLength(2);
    // Toggle the first card; the second must follow (shared live store).
    fireEvent.click(rows()[0]?.querySelector("button") as HTMLElement);
    await waitFor(() => expect(rows().every((r) => r.classList.contains("checked"))).toBe(true));
  });

  it("renders an appointment_card with status flag, location, repeat, attendees", () => {
    const { getByText } = render(
      <ToolView
        payload={payload({
          view: "appointment_card",
          data: {
            id: "A1",
            title: "Dentist",
            start: "2026-06-15T14:00:00+00:00",
            status: "tentative",
            location: "123 Main St",
            recurring: true,
            attendees: ["Dr. Nguyen"],
          },
        })}
      />,
    );
    expect(getByText("Dentist")).toBeInTheDocument();
    expect(getByText("123 Main St")).toBeInTheDocument();
    expect(getByText("repeats")).toBeInTheDocument();
    expect(getByText("with Dr. Nguyen")).toBeInTheDocument();
    // Status is a flag enum the theme colors, never a model-authored color.
    expect(getByText("tentative")).toHaveClass("flag-tentative");
  });

  it("tolerates missing/extra slots without crashing", () => {
    const { container } = render(<ToolView payload={payload({ view: "data_table" })} />);
    expect(container.querySelector("table")).toBeInTheDocument();
    // A list_card with no items renders the empty row, not a crash.
    const empty = render(<ToolView payload={payload({ view: "list_card" })} />);
    expect(empty.getByText("empty")).toBeInTheDocument();
    // An appointment_card with only a view name falls back to a default title and
    // a confirmed status, no crash.
    const bare = render(<ToolView payload={payload({ view: "appointment_card" })} />);
    expect(bare.getByText("Appointment")).toBeInTheDocument();
    expect(bare.getByText("confirmed")).toHaveClass("flag-confirmed");
  });
});
