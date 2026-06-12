import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { FullBrainDeps } from "../agent/useFullBrain";
import type { NoteActions } from "../notes/useNoteActions";
import type { NotesController } from "../notes/useNotes";
import { HomeScreen } from "./HomeScreen";

function fbDeps(): FullBrainDeps {
  return {
    listSessions: vi.fn(async () => [
      {
        id: "s1",
        title: "Recap",
        status: "active",
        domain_scopes: ["general"],
        subject_ids: [],
        created_at: "2026-06-12T00:00:00Z",
        last_active_at: "2026-06-12T00:00:00Z",
      },
    ]),
    createSession: vi.fn(),
    chat: async function* () {},
    listProposals: vi.fn(async () => []),
    getTranscript: vi.fn(async () => []),
    renameSession: vi.fn(async () => {}),
    deleteSession: vi.fn(async () => {}),
  };
}

function fakeController(): NotesController {
  return {
    items: [],
    syncStatus: "synced",
    send: vi.fn(async () => {}),
    update: vi.fn(async () => {}),
    remove: vi.fn(async () => {}),
    setHidden: vi.fn(async () => {}),
    byId: vi.fn(() => undefined),
    addAttachment: vi.fn(async () => ({
      id: "a1",
      filename: "f.txt",
      mediaType: "text/plain",
      sizeBytes: 1,
      hasExtracts: false,
      hasDescription: false,
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

function setup(notes: NotesController = fakeController()) {
  render(
    <HomeScreen
      notes={notes}
      actions={fakeActions()}
      onOpenNote={vi.fn()}
      onOpenSearch={vi.fn()}
      onOpenLauncher={vi.fn()}
      fbDeps={fbDeps()}
    />,
  );
}

function streamItem() {
  return {
    key: "k1",
    id: "n1",
    domain: "general",
    destination: null,
    body: "hide me",
    createdAt: new Date(),
    ingestState: "indexed",
    analyzed: true,
    attachments: [],
    pending: false,
    hidden: false,
  };
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

  it("swipe-Hide hides the note and an undo toast restores it", () => {
    const notes = { ...fakeController(), items: [streamItem()] };
    setup(notes);
    const bubble = screen.getByRole("button", { name: /hide me/ });
    fireEvent.touchStart(bubble, { touches: [{ clientX: 250, clientY: 50 }] });
    fireEvent.touchMove(bubble, { touches: [{ clientX: 60, clientY: 52 }] });
    fireEvent.touchEnd(bubble);

    fireEvent.click(screen.getByRole("button", { name: "hide" }));
    expect(notes.setHidden).toHaveBeenCalledWith("n1", true);
    expect(screen.getByText("note hidden")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "undo" }));
    expect(notes.setHidden).toHaveBeenCalledWith("n1", false);
  });

  it("Full Brain renders the live conversation surface inline; Entry sub-modes keep the stream", async () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Full Brain" }));
    // The real surface, not a placeholder: the session name and the transcript.
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(document.querySelector(".fb-title")?.textContent).toBe("Recap");

    // Back to Entry, then into the Medical sub-mode: still the note stream.
    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Medical" }));
    expect(
      screen.getByText("Nothing captured yet — write your first entry below."),
    ).toBeInTheDocument();
  });

  it("a Full Brain send from the omnibox streams into the inline transcript", async () => {
    const deps = fbDeps();
    deps.chat = async function* () {
      yield { type: "text_delta", text: "on it" };
      yield { type: "done", stop_reason: "end_turn" };
    };
    render(
      <HomeScreen
        notes={fakeController()}
        actions={fakeActions()}
        onOpenNote={vi.fn()}
        onOpenSearch={vi.fn()}
        onOpenLauncher={vi.fn()}
        fbDeps={deps}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Full Brain" }));
    await waitFor(() => screen.getByLabelText("Conversation"));

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "summarize my week" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText("on it")).toBeInTheDocument());
    expect(screen.getByText("summarize my week")).toBeInTheDocument();
  });
});
