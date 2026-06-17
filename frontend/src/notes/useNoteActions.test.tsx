import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useNoteActions } from "./useNoteActions";
import type { NotesController } from "./useNotes";

function fakeController(): NotesController {
  return {
    items: [],
    syncStatus: "synced",
    refresh: vi.fn(async () => {}),
    send: vi.fn(async () => {}),
    update: vi.fn(async () => {}),
    remove: vi.fn(async () => {}),
    setHidden: vi.fn(async () => {}),
    byId: vi.fn(() => undefined),
    fetchById: vi.fn(async () => null),
    addAttachment: vi.fn(async () => ({
      id: "a1",
      filename: "f.txt",
      mediaType: "text/plain",
      sizeBytes: 1,
      hasExtracts: false,
      hasDescription: false,
    })),
    removeAttachment: vi.fn(async () => undefined),
  };
}

describe("useNoteActions", () => {
  it("edit flow: loads the note, then PATCHes {body} and leaves edit mode", async () => {
    const notes = fakeController();
    const { result } = renderHook(() => useNoteActions(notes));

    act(() =>
      result.current.startEdit({
        id: "n1",
        body: "original",
        domain: "general",
        createdAt: new Date(),
        attachments: [],
      }),
    );
    expect(result.current.editing).toMatchObject({ id: "n1", body: "original" });

    await act(() => result.current.submitEdit("corrected"));
    expect(notes.update).toHaveBeenCalledWith("n1", { body: "corrected" });
    expect(result.current.editing).toBeNull();
  });

  it("cancel drops edit mode without a PATCH", () => {
    const notes = fakeController();
    const { result } = renderHook(() => useNoteActions(notes));
    act(() =>
      result.current.startEdit({
        id: "n1",
        body: "original",
        domain: "general",
        createdAt: new Date(),
        attachments: [],
      }),
    );
    act(() => result.current.cancelEdit());
    expect(result.current.editing).toBeNull();
    expect(notes.update).not.toHaveBeenCalled();
  });

  it("move flow: PATCHes {domain, destination} for the sheet's target", async () => {
    const notes = fakeController();
    const { result } = renderHook(() => useNoteActions(notes));

    act(() => result.current.startMove({ id: "n2", domain: "general", destination: null }));
    await act(() => result.current.submitMove("health", "Labs"));
    expect(notes.update).toHaveBeenCalledWith("n2", { domain: "health", destination: "Labs" });
    expect(result.current.moveTarget).toBeNull();
  });

  it("moving to a destination-less domain clears the destination explicitly", async () => {
    const notes = fakeController();
    const { result } = renderHook(() => useNoteActions(notes));

    act(() => result.current.startMove({ id: "n3", domain: "finance", destination: "Receipts" }));
    await act(() => result.current.submitMove("general", null));
    expect(notes.update).toHaveBeenCalledWith("n3", { domain: "general", destination: null });
  });

  it("remove delegates to the controller", async () => {
    const notes = fakeController();
    const { result } = renderHook(() => useNoteActions(notes));
    await act(() => result.current.remove("n4"));
    expect(notes.remove).toHaveBeenCalledWith("n4");
  });
});
