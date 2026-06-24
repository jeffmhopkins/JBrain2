import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatRequest } from "../agent/types";
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
        agent: "curator",
        domain_scopes: ["general"],
        subject_ids: [],
        created_at: "2026-06-12T00:00:00Z",
        last_active_at: "2026-06-12T00:00:00Z",
        turn_count: 3,
      },
    ]),
    createSession: vi.fn(async (body) => ({
      id: "new",
      title: "",
      status: "active",
      agent: body.agent ?? "curator",
      domain_scopes: body.domain_scopes,
      subject_ids: [],
      created_at: "2026-06-13T00:00:00Z",
      last_active_at: "2026-06-13T00:00:00Z",
    })),
    chat: async function* () {},
    chatResume: async function* () {},
    cancelChatRun: vi.fn(async () => {}),
    listProposals: vi.fn(async () => []),
    getTranscript: vi.fn(async () => []),
    renameSession: vi.fn(async () => {}),
    deleteSession: vi.fn(async () => {}),
    archiveSession: vi.fn(async () => {}),
    unarchiveSession: vi.fn(async () => {}),
    rescopeSession: vi.fn(async () => {}),
    uploadChatAttachment: vi.fn(async (_sid: string, file: File) => ({
      id: `att-${file.name}`,
      filename: file.name,
      media_type: file.type,
      size_bytes: file.size,
    })),
    getChatCapabilities: vi.fn(async () => ({ supports_vision: true, can_edit_images: false })),
  };
}

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

describe("HomeScreen compose handoff", () => {
  it("flips to Full Brain and seeds the composer from a compose prompt", async () => {
    const onComposeConsumed = vi.fn();
    render(
      <HomeScreen
        notes={fakeController()}
        actions={fakeActions()}
        onOpenNote={vi.fn()}
        onOpenSearch={vi.fn()}
        onOpenLauncher={vi.fn()}
        fbDeps={fbDeps()}
        compose={{ text: 'Reschedule my "Dentist" appointment to ' }}
        onComposeConsumed={onComposeConsumed}
      />,
    );
    await waitFor(() =>
      expect(screen.getByDisplayValue(/Reschedule my "Dentist"/)).toBeInTheDocument(),
    );
    expect(onComposeConsumed).toHaveBeenCalled();
  });

  it("attaches the appointment pill and sends its id with the turn", async () => {
    const calls: ChatRequest[] = [];
    const deps = fbDeps();
    deps.chat = async function* (body) {
      calls.push(body);
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
        compose={{
          text: 'About my "Dentist" appointment: ',
          appt: { id: "A1", title: "Dentist" },
        }}
        onComposeConsumed={vi.fn()}
      />,
    );
    // The pill names the appointment in the composer's attach row.
    await screen.findByRole("button", { name: /Remove appointment Dentist/ });

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "is it confirmed?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.appointment_id).toBe("A1");
  });

  it("starts a full-domain Curator session when the handoff finds none", async () => {
    const createSession = vi.fn(async () => ({
      id: "new",
      title: "",
      status: "active",
      agent: "curator",
      domain_scopes: ["general", "health", "finance", "location"],
      subject_ids: [],
      created_at: "2026-06-13T00:00:00Z",
      last_active_at: "2026-06-13T00:00:00Z",
    }));
    const deps = { ...fbDeps(), listSessions: vi.fn(async () => []), createSession };
    render(
      <HomeScreen
        notes={fakeController()}
        actions={fakeActions()}
        onOpenNote={vi.fn()}
        onOpenSearch={vi.fn()}
        onOpenLauncher={vi.fn()}
        fbDeps={deps}
        compose={{ text: 'Cancel my "Dentist" appointment.' }}
        onComposeConsumed={vi.fn()}
      />,
    );
    // No Curator chat existed, so Full Brain auto-starts one with full domain access.
    await waitFor(() =>
      expect(createSession).toHaveBeenCalledWith({
        domain_scopes: ["general", "health", "finance", "location"],
        agent: "curator",
      }),
    );
  });
});

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
    provenance: "human",
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

  it("Research opens the live conversation surface (no more Phase 4 stub)", async () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(
      screen.queryByText("conversations arrive in Phase 4 — typing starts one then"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Nothing captured yet — write your first entry below."),
    ).not.toBeInTheDocument();
  });

  it("Research with no prior chat auto-starts a Jerv session", async () => {
    // Only a Curator chat exists (Full Brain's), so Research has none of its own.
    const deps = fbDeps();
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
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    await waitFor(() =>
      expect(deps.createSession).toHaveBeenCalledWith({ domain_scopes: [], agent: "jerv" }),
    );
  });

  it("Full Brain reopens the last Curator chat; re-clicking it starts a fresh one", async () => {
    const deps = fbDeps();
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
    // First entry reopens the existing Curator chat — no new session.
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() =>
      expect(document.querySelector(".session-title")?.textContent).toBe("Recap"),
    );
    expect(deps.createSession).not.toHaveBeenCalled();

    // Re-clicking Full Brain starts a fresh full-domain Curator chat.
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() =>
      expect(deps.createSession).toHaveBeenCalledWith({
        domain_scopes: ["general", "health", "finance", "location"],
        agent: "curator",
      }),
    );
  });

  it("renames the Research tab to Teacher while a Teacher chat is open", async () => {
    const deps = fbDeps();
    deps.listSessions = vi.fn(async () => [
      {
        id: "t1",
        title: "",
        status: "active",
        agent: "teacher",
        domain_scopes: [],
        subject_ids: [],
        created_at: "2026-06-12T00:00:00Z",
        last_active_at: "2026-06-12T00:00:00Z",
      },
    ]);
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
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    // The research slot now reads "Teacher" — no plain "Research" tab remains.
    await waitFor(() => expect(screen.getByRole("tab", { name: "Teacher" })).toBeInTheDocument());
    expect(screen.queryByRole("tab", { name: "Research" })).not.toBeInTheDocument();
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
    // Entry mode shows the wordmark; no session has taken the top bar yet.
    expect(document.querySelector(".session-title")).not.toBeInTheDocument();
    expect(document.querySelector(".wordmark")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    // The real surface, not a placeholder: the transcript, with the session name
    // standing in for the wordmark up in the top bar (no extra title row).
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(document.querySelector(".session-title")?.textContent).toBe("Recap");
    expect(document.querySelector(".wordmark")).not.toBeInTheDocument();

    // Back to Entry, then into the Medical sub-mode: still the note stream.
    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Medical" }));
    expect(
      screen.getByText("Nothing captured yet — write your first entry below."),
    ).toBeInTheDocument();
  });

  it("tapping the session name in the top bar reopens the Sessions panel", async () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() => screen.getByLabelText("Conversation"));
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Recap" }));
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();
  });

  it("shuttles the panels on an omnibox swipe but not on a chat-window swipe", async () => {
    setup();
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() => screen.getByLabelText("Conversation"));

    // A swipe across the transcript itself is inert — it never opens a panel.
    const shell = document.querySelector(".fb-shell") as Element;
    fireEvent.touchStart(shell, { touches: [{ clientX: 20, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 140, clientY: 205 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 140, clientY: 205 }] });
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();

    // The same rightward swipe on the omnibox's segment row pulls the Sessions
    // panel in (gestures initiate from that row only, not the composer body).
    const box = document.querySelector(".omnibox .seg-row") as Element;
    fireEvent.touchStart(box, { touches: [{ clientX: 20, clientY: 40 }] });
    fireEvent.touchEnd(box, { changedTouches: [{ clientX: 140, clientY: 44 }] });
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();

    // The opposite (leftward) swipe sends it back out.
    fireEvent.touchStart(box, { touches: [{ clientX: 200, clientY: 40 }] });
    fireEvent.touchEnd(box, { changedTouches: [{ clientX: 80, clientY: 44 }] });
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();

    // A leftward swipe from rest pulls the Proposals panel in from the right.
    fireEvent.touchStart(box, { touches: [{ clientX: 200, clientY: 40 }] });
    fireEvent.touchEnd(box, { changedTouches: [{ clientX: 80, clientY: 44 }] });
    expect(document.querySelector(".panel.right.open")).toBeInTheDocument();
  });

  it("gates the chat paperclip on the model's vision capability", async () => {
    // Vision off: the conversation composer hides the paperclip and shows the hint.
    const offDeps = {
      ...fbDeps(),
      getChatCapabilities: vi.fn(async () => ({ supports_vision: false, can_edit_images: false })),
    };
    const { unmount } = render(
      <HomeScreen
        notes={fakeController()}
        actions={fakeActions()}
        onOpenNote={vi.fn()}
        onOpenSearch={vi.fn()}
        onOpenLauncher={vi.fn()}
        fbDeps={offDeps}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() => screen.getByLabelText("Conversation"));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Attach files" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/This model can't read images/)).toBeInTheDocument();
    unmount();

    // Vision on: the paperclip is offered, no hint.
    const onDeps = {
      ...fbDeps(),
      getChatCapabilities: vi.fn(async () => ({ supports_vision: true, can_edit_images: false })),
    };
    render(
      <HomeScreen
        notes={fakeController()}
        actions={fakeActions()}
        onOpenNote={vi.fn()}
        onOpenSearch={vi.fn()}
        onOpenLauncher={vi.fn()}
        fbDeps={onDeps}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Attach files" })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/This model can't read images/)).not.toBeInTheDocument();
  });

  it("keeps the Research paperclip when image tools exist even if the model can't see", async () => {
    // jerv's mode: a blind agent model, but ComfyUI configured — attach stays offered
    // because jerv can analyze_image / edit_image an attachment by id.
    const deps = {
      ...fbDeps(),
      getChatCapabilities: vi.fn(async () => ({ supports_vision: false, can_edit_images: true })),
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
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    await waitFor(() => screen.getByLabelText("Conversation"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Attach files" })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/This model can't read images/)).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("tab", { name: "Brain" }));
    await waitFor(() => screen.getByLabelText("Conversation"));

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "summarize my week" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText("on it")).toBeInTheDocument());
    expect(screen.getByText("summarize my week")).toBeInTheDocument();
  });
});
