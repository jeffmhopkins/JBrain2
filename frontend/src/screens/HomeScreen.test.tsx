import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { NoteActions } from "../notes/useNoteActions";
import type { NotesController } from "../notes/useNotes";
import { HomeScreen } from "./HomeScreen";

function fakeController(): NotesController {
  return {
    items: [],
    syncStatus: "synced",
    send: vi.fn(async () => {}),
    update: vi.fn(async () => {}),
    remove: vi.fn(async () => {}),
    byId: vi.fn(() => undefined),
    addAttachment: vi.fn(async () => ({
      id: "a1",
      filename: "f.txt",
      mediaType: "text/plain",
      sizeBytes: 1,
      hasExtracts: false,
    })),
    removeAttachment: vi.fn(async () => undefined),
    fetchById: vi.fn(async () => null),
  };
}

function fakeActions(): NoteActions {
  return {
    editing: null,
    startEdit: vi.fn(),
    cancelEdit: vi.fn(),
    submitEdit: vi.fn(async () => {}),
    moveTarget: null,
    startMove: vi.fn(),
    cancelMove: vi.fn(),
    submitMove: vi.fn(async () => {}),
    remove: vi.fn(async () => {}),
  };
}

function setup() {
  render(
    <HomeScreen
      notes={fakeController()}
      actions={fakeActions()}
      onOpenNote={vi.fn()}
      onOpenSearch={vi.fn()}
      onOpenLauncher={vi.fn()}
    />,
  );
}

describe("HomeScreen mode scoping", () => {
  it("Entry shows the note stream", () => {
    setup();
    expect(
      screen.getByText("Nothing captured yet — write your first entry below."),
    ).toBeInTheDocument();
  });

  it("Research swaps the stream for the Phase 4 conversation empty state", () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    expect(
      screen.getByText("conversations arrive in Phase 4 — typing starts one then"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Nothing captured yet — write your first entry below."),
    ).not.toBeInTheDocument();
  });

  it("Full Brain shows the same placeholder; Entry sub-modes keep the stream", () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Full Brain" }));
    expect(
      screen.getByText("conversations arrive in Phase 4 — typing starts one then"),
    ).toBeInTheDocument();

    // Back to Entry, then into the Medical sub-mode: still the note stream.
    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Medical" }));
    expect(
      screen.getByText("Nothing captured yet — write your first entry below."),
    ).toBeInTheDocument();
  });
});
