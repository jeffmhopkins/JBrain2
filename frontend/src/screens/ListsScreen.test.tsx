import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ListOut } from "../api/client";
import { type ListsDeps, ListsScreen } from "./ListsScreen";

function list(over: Partial<ListOut> = {}): ListOut {
  return { id: "L1", title: "Groceries", domain: "general", archived: false, items: [], ...over };
}

function deps(over: Partial<ListsDeps> = {}): ListsDeps {
  return {
    lists: vi.fn(async () => [list({ items: [{ id: "a", body: "eggs", checked: false }] })]),
    createList: vi.fn(async (title, domain) => list({ id: "new", title, domain, items: [] })),
    ...over,
  };
}

describe("ListsScreen", () => {
  it("renders list cards and opens one on tap", async () => {
    const onOpen = vi.fn();
    render(<ListsScreen onOpenList={onOpen} deps={deps()} />);
    await screen.findByText("Groceries");
    fireEvent.click(screen.getByText("Groceries"));
    expect(onOpen).toHaveBeenCalledWith("L1");
  });

  it("shows the empty state when there are no lists", async () => {
    render(<ListsScreen onOpenList={vi.fn()} deps={deps({ lists: vi.fn(async () => []) })} />);
    expect(await screen.findByText(/no lists yet/)).toBeInTheDocument();
  });

  it("creates a list with a chosen domain and opens it", async () => {
    const onOpen = vi.fn();
    const d = deps({ lists: vi.fn(async () => []) });
    render(<ListsScreen onOpenList={onOpen} deps={d} />);
    await screen.findByText("＋ New list");
    fireEvent.click(screen.getByText("＋ New list"));

    fireEvent.change(screen.getByLabelText("New list title"), { target: { value: "Trip" } });
    fireEvent.click(screen.getByRole("button", { name: /Medical/ })); // health domain
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(d.createList).toHaveBeenCalledWith("Trip", "health"));
    expect(onOpen).toHaveBeenCalledWith("new");
  });
});
