import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type ListOut, api } from "../api/client";
import { ListDetailScreen, reorderItems } from "./ListDetailScreen";

function list(): ListOut {
  return {
    id: "L1",
    title: "Groceries",
    domain: "general",
    archived: false,
    items: [
      { id: "a", body: "eggs", checked: false },
      { id: "b", body: "milk", checked: true },
    ],
  };
}

function mount() {
  vi.spyOn(api, "getList").mockResolvedValue(list());
  const onClose = vi.fn();
  render(<ListDetailScreen listId="L1" syncStatus="synced" onClose={onClose} />);
  return { onClose };
}

describe("reorderItems", () => {
  it("moves an item before another, or to the end on null", () => {
    const xs = [
      { id: "a", body: "1", checked: false },
      { id: "b", body: "2", checked: false },
      { id: "c", body: "3", checked: false },
    ];
    expect(reorderItems(xs, "c", "a").map((i) => i.id)).toEqual(["c", "a", "b"]);
    expect(reorderItems(xs, "a", null).map((i) => i.id)).toEqual(["b", "c", "a"]);
    expect(reorderItems(xs, "missing", "a")).toBe(xs);
  });
});

describe("ListDetailScreen", () => {
  it("loads the list and renders its items", async () => {
    mount();
    expect(await screen.findByText("eggs")).toBeInTheDocument();
    expect(screen.getByText("milk").closest(".ld-toggle")).toHaveClass("checked");
  });

  it("toggles a whole row, optimistically", async () => {
    const check = vi.spyOn(api, "setListItemChecked").mockResolvedValue();
    mount();
    fireEvent.click(await screen.findByText("eggs"));
    expect(screen.getByText("eggs").closest(".ld-toggle")).toHaveClass("checked");
    await waitFor(() => expect(check).toHaveBeenCalledWith("a", true));
  });

  it("adds an item", async () => {
    const add = vi
      .spyOn(api, "addListItem")
      .mockResolvedValue({ id: "c", body: "bread", checked: false });
    mount();
    await screen.findByText("eggs");
    const input = screen.getByLabelText("Add item");
    fireEvent.change(input, { target: { value: "bread" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(add).toHaveBeenCalledWith("L1", "bread"));
    expect(await screen.findByText("bread")).toBeInTheDocument();
  });

  it("renames and deletes items in edit mode", async () => {
    const rename = vi.spyOn(api, "renameListItem").mockResolvedValue();
    const remove = vi.spyOn(api, "removeListItem").mockResolvedValue();
    mount();
    await screen.findByText("eggs");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const edit = screen.getByLabelText("Edit eggs");
    fireEvent.change(edit, { target: { value: "free eggs" } });
    fireEvent.blur(edit);
    expect(rename).toHaveBeenCalledWith("a", "free eggs");

    fireEvent.click(screen.getByLabelText("Delete milk"));
    expect(remove).toHaveBeenCalledWith("b");
  });

  it("renames the list and deletes it (closing the layer)", async () => {
    const renameList = vi.spyOn(api, "renameList").mockResolvedValue();
    const deleteList = vi.spyOn(api, "deleteList").mockResolvedValue();
    const { onClose } = mount();
    await screen.findByText("eggs");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const title = screen.getByLabelText("List title");
    fireEvent.change(title, { target: { value: "Food" } });
    fireEvent.blur(title);
    expect(renameList).toHaveBeenCalledWith("L1", "Food");

    fireEvent.click(screen.getByRole("button", { name: "Delete list" }));
    expect(deleteList).toHaveBeenCalledWith("L1");
    expect(onClose).toHaveBeenCalled();
  });
});
