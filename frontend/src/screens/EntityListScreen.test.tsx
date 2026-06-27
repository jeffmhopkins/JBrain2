import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { EntityListItem } from "../api/client";
import { EntityListScreen } from "./EntityListScreen";

/** The name truncates against the full row, so it lives in its own span; a
 * confirmed entity's name and its wrapper share text, so disambiguate by it. */
const NAME = ".entity-row-name-text";

let seq = 0;
function item(overrides: Partial<EntityListItem> = {}): EntityListItem {
  seq += 1;
  return {
    id: `ent-${seq}`,
    kind: "Person",
    canonical_name: `Entity ${seq}`,
    status: "confirmed",
    fact_count: 3,
    mention_count: 2,
    last_seen: "2026-06-10T09:40:00Z",
    ...overrides,
  };
}

const SARAH = item({ id: "ent-sarah", canonical_name: "Sarah Hopkins" });
const FOLLOWUP = item({
  id: "ent-followup",
  kind: "appointment",
  canonical_name: "Dr. Patel follow-up",
  status: "provisional",
  fact_count: 1,
  last_seen: null,
});

function setup(items: EntityListItem[] = [SARAH, FOLLOWUP]) {
  const list = vi.fn(async (q?: string, kind?: string) => ({
    items: items
      .filter((i) => !q || i.canonical_name.toLowerCase().includes(q.toLowerCase()))
      .filter((i) => !kind || i.kind === kind),
  }));
  const onOpenEntity = vi.fn();
  render(<EntityListScreen onOpenEntity={onOpenEntity} list={list} />);
  return { list, onOpenEntity };
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText("loading entities…")).not.toBeInTheDocument());
}

describe("EntityListScreen", () => {
  it("renders rows: name, provisional chip, and a kind · facts · last-seen subtitle", async () => {
    setup();
    await loaded();
    expect(screen.getByText("Sarah Hopkins", { selector: NAME })).toBeInTheDocument();
    expect(screen.getByText("provisional")).toHaveClass("fact-chip", "fact-chip-muted");
    // Each row leads with a type-tinted icon disc; the appointment folds to Event.
    const discs = document.querySelectorAll(".etype-disc");
    expect(discs.length).toBe(2);
    expect(discs[0]).toHaveAttribute("data-entity-kind", "Person");
    expect(discs[1]).toHaveAttribute("data-entity-kind", "Event");
    // The subtitle is one muted line: lowercased kind, facts, then the last-seen
    // LOCAL calendar day (last_seen is an instant — the day is what a row shows).
    const day = new Date(SARAH.last_seen as string).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
    const sub = screen.getByText(`person · 3 facts · last seen ${day}`);
    expect(sub).toHaveClass("entity-row-sub");
    // Null last_seen and a singular count stay null-safe and grammatical.
    expect(screen.getByText("appointment · 1 fact")).toBeInTheDocument();
  });

  it("filters as you type after the debounce, not per keystroke", async () => {
    vi.useFakeTimers();
    try {
      const { list } = setup();
      await act(async () => {
        vi.advanceTimersByTime(10); // the unfiltered mount load
      });
      expect(list).toHaveBeenCalledWith(undefined, undefined);
      fireEvent.change(screen.getByLabelText("Filter entities"), { target: { value: "sar" } });
      fireEvent.change(screen.getByLabelText("Filter entities"), { target: { value: "sarah" } });
      expect(list).toHaveBeenCalledTimes(1); // still inside the debounce window
      await act(async () => {
        vi.advanceTimersByTime(300);
      });
      expect(list).toHaveBeenCalledTimes(2);
      expect(list).toHaveBeenLastCalledWith("sarah", undefined);
      expect(screen.getByText("Sarah Hopkins", { selector: NAME })).toBeInTheDocument();
      expect(screen.queryByText("Dr. Patel follow-up")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("derives kind chips from the data and filters on tap", async () => {
    const { list } = setup();
    await loaded();
    // Chips come from the loaded set: Person + appointment, no hardcoding.
    const chips = screen.getByLabelText("Kind filter");
    expect(chips).toHaveTextContent("All");
    fireEvent.click(screen.getByRole("button", { name: "appointment" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(undefined, "appointment"));
    expect(screen.queryByText("Sarah Hopkins")).not.toBeInTheDocument();
    expect(screen.getByText("Dr. Patel follow-up")).toBeInTheDocument();
    // The chip row survives the narrowed load, so you can switch back.
    expect(screen.getByRole("button", { name: "Person" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await waitFor(() =>
      expect(screen.getByText("Sarah Hopkins", { selector: NAME })).toBeInTheDocument(),
    );
  });

  it("opens the entity layer for a tapped row", async () => {
    const { onOpenEntity } = setup();
    await loaded();
    fireEvent.click(
      screen.getByText("Sarah Hopkins", { selector: NAME }).closest("button") as HTMLElement,
    );
    expect(onOpenEntity).toHaveBeenCalledWith("ent-sarah");
  });

  it("shows the calm empty state before any analysis has run", async () => {
    setup([]);
    await loaded();
    expect(
      screen.getByText("no entities yet — they appear as notes are analyzed."),
    ).toBeInTheDocument();
  });

  it("shows the quiet error state when the list fails", async () => {
    const list = vi.fn(async () => {
      throw new Error("down");
    });
    render(<EntityListScreen onOpenEntity={vi.fn()} list={list} />);
    await waitFor(() =>
      expect(
        screen.getByText("couldn't load entities — check the connection."),
      ).toBeInTheDocument(),
    );
  });
});
