import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { FullBrainSurface } from "./FullBrainSurface";
import type { AgentSession, ChatEvent, ChatRequest, TranscriptTurn } from "./types";
import { type FullBrainDeps, useFullBrain } from "./useFullBrain";

function session(over: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "s1",
    title: "Recap",
    status: "active",
    domain_scopes: ["general"],
    subject_ids: [],
    created_at: "2026-06-12T00:00:00Z",
    last_active_at: "2026-06-12T00:00:00Z",
    ...over,
  };
}

async function* noChat(_body: ChatRequest): AsyncGenerator<ChatEvent> {}

function deps(over: Partial<FullBrainDeps> = {}): FullBrainDeps {
  return {
    listSessions: vi.fn(async () => [session()]),
    createSession: vi.fn(async () => session({ id: "new" })),
    chat: noChat,
    listProposals: vi.fn(async () => []),
    getTranscript: vi.fn(async () => []),
    renameSession: vi.fn(async () => {}),
    deleteSession: vi.fn(async () => {}),
    ...over,
  };
}

// The omnibox stands in as the external composer the home screen provides.
function Harness({
  d,
  onOpenNote,
  onOpenEntity,
}: {
  d: FullBrainDeps;
  onOpenNote?: (id: string) => void;
  onOpenEntity?: (id: string) => void;
}) {
  const fb = useFullBrain(true, d);
  const [text, setText] = useState("");
  return (
    <>
      <FullBrainSurface fb={fb} onOpenNote={onOpenNote} onOpenEntity={onOpenEntity} />
      <input aria-label="Composer" value={text} onChange={(e) => setText(e.target.value)} />
      <button type="button" onClick={() => fb.send(text)}>
        send
      </button>
    </>
  );
}

describe("FullBrainSurface", () => {
  it("opens the Sessions panel when there is no active session", async () => {
    render(<Harness d={deps({ listSessions: vi.fn(async () => []) })} />);
    await waitFor(() => expect(document.querySelector(".panel.left.open")).toBeInTheDocument());
    expect(screen.getByText(/Choose a session to start/)).toBeInTheDocument();
  });

  it("replays the active session's stored transcript on open", async () => {
    const getTranscript = vi.fn(
      async (): Promise<TranscriptTurn[]> => [
        { role: "user", content: "remind me?", tools: [] },
        {
          role: "assistant",
          content: "Here is the recap.",
          tools: [
            {
              id: "c1",
              name: "search",
              ok: true,
              sources: [{ note_id: "n1", domain: "general", snippet: "the note" }],
            },
          ],
        },
      ],
    );
    render(<Harness d={deps({ getTranscript })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    await waitFor(() => expect(screen.getByText("remind me?")).toBeInTheDocument());
    expect(screen.getByText("Here is the recap.")).toBeInTheDocument();
    // The persisted tool sources rebuild the Worked block.
    expect(screen.getByRole("button", { name: /Worked/ }).textContent).toContain("1 source");
    expect(getTranscript).toHaveBeenCalledWith("s1");
  });

  it("keeps the active session's history when it is re-opened from the list", async () => {
    const getTranscript = vi.fn(
      async (): Promise<TranscriptTurn[]> => [{ role: "user", content: "kept", tools: [] }],
    );
    render(<Harness d={deps({ getTranscript })} />);
    await waitFor(() => expect(screen.getByText("kept")).toBeInTheDocument());

    // Re-open the already-active session from the (in-DOM) sessions list.
    fireEvent.click(document.querySelector(".session-tap") as HTMLElement);

    // Its history must not be blanked, and we don't re-fetch the same id.
    expect(screen.getByText("kept")).toBeInTheDocument();
    expect(getTranscript).toHaveBeenCalledTimes(1);
  });

  it("refreshes the review inbox after a turn (a turn can stage a proposal)", async () => {
    const listProposals = vi.fn(async () => []);
    render(<Harness d={deps({ listProposals })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    const initial = listProposals.mock.calls.length;

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "name that note" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    await waitFor(() => expect(listProposals.mock.calls.length).toBeGreaterThan(initial));
  });

  it("shows the active session's name up top with panels closed", async () => {
    render(<Harness d={deps()} />);
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(document.querySelector(".fb-title")?.textContent).toBe("Recap");
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();
  });

  it("streams an external send into the transcript and folds tools into a Worked line", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "checking" };
      yield { type: "tool_call", id: "c1", name: "search", arguments: { q: "labs" } };
      yield { type: "tool_result", tool_call_id: "c1", ok: true, summary: "2 notes" };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what labs?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    await waitFor(() => expect(screen.getByText("checking")).toBeInTheDocument());
    expect(screen.getByText("what labs?")).toBeInTheDocument();
    // The raw "search · …" dump is gone; the tools collapse into one line.
    expect(screen.queryByText(/search · /)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Worked/ })).toBeInTheDocument();
  });

  it("expands the Worked block to source cards that open the cited note", async () => {
    const onOpenNote = vi.fn();
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary:
          "- note abc-1 [general] 2026-06-12: I was <mark>born</mark> March 19, 1986\n" +
          "- note def-2 [general] 2026-01-02: My name is Jeff",
      };
      yield { type: "text_delta", text: "You were born March 19, 1986." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} onOpenNote={onOpenNote} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "when born?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // Collapsed by default — the source text isn't shown until expanded.
    const worked = await screen.findByRole("button", { name: /Worked/ });
    expect(worked.textContent).toContain("1 step");
    expect(worked.textContent).toContain("2 sources");
    expect(screen.queryByText("I was born March 19, 1986")).not.toBeInTheDocument();

    fireEvent.click(worked);
    expect(screen.getByText("Searched your notes")).toBeInTheDocument();
    fireEvent.click(screen.getByText("I was born March 19, 1986"));
    expect(onOpenNote).toHaveBeenCalledWith("abc-1");
  });

  it("drives the status line through the turn and drops the floating dots", async () => {
    let releaseStart!: () => void;
    let releaseTool!: () => void;
    const startGate = new Promise<void>((r) => {
      releaseStart = r;
    });
    const toolGate = new Promise<void>((r) => {
      releaseTool = r;
    });
    async function* answer(): AsyncGenerator<ChatEvent> {
      await startGate;
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      await toolGate;
      yield { type: "tool_result", tool_call_id: "c1", ok: true, summary: "2 notes" };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what labs?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // Before any event: thinking, and no old in-bubble "…".
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Thinking"));
    expect(document.querySelector(".fb-typing")).toBeNull();

    releaseStart();
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain("Searching your notes"),
    );

    releaseTool();
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain("Answered · 1 tool used"),
    );
  });

  it("surfaces a staged proposal as a Review chip routed to the Proposals panel", async () => {
    // Opening the proposal renders ProposalTree, which fetches it; hold the fetch
    // so the panel opens without a (rejected) network call in the test.
    vi.spyOn(api, "getProposal").mockReturnValue(new Promise(() => {}));
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "propose_correction", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "staged",
        proposal: { proposal_id: "p1", kind: "correction" },
      };
      yield { type: "text_delta", text: "Staged it for your approval." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "name that note" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    const chip = await screen.findByRole("button", { name: /Review proposal/ });
    fireEvent.click(chip);
    await waitFor(() => expect(document.querySelector(".panel.right.open")).toBeInTheDocument());
  });

  it("a [^1] citation in the answer opens the cited source note", async () => {
    const onOpenNote = vi.fn();
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        sources: [{ note_id: "n7", domain: "general", snippet: "born then" }],
      };
      yield { type: "text_delta", text: "You were born then.[^1]" };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} onOpenNote={onOpenNote} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "when born?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    fireEvent.click(await screen.findByRole("button", { name: "1" }));
    expect(onOpenNote).toHaveBeenCalledWith("n7");
  });

  it("surfaces resolved entities as chips that open the entity page", async () => {
    const onOpenEntity = vi.fn();
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "find_entity", arguments: { name: "celine" } };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        entities: [{ kind: "entity", entity_id: "e9", label: "Celine", domain: "general" }],
      };
      yield { type: "text_delta", text: "That is Celine." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} onOpenEntity={onOpenEntity} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "who is celine?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    fireEvent.click(await screen.findByRole("button", { name: "Celine" }));
    expect(onOpenEntity).toHaveBeenCalledWith("e9");
  });

  it("a send with no chosen session surfaces the picker instead", async () => {
    const chat = vi.fn(noChat);
    render(<Harness d={deps({ listSessions: vi.fn(async () => []), chat })} />);
    await waitFor(() => screen.getByText(/Choose a session/));

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    expect(chat).not.toHaveBeenCalled();
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();
  });

  it("creating a session from the picker opens its chat", async () => {
    const created = session({ id: "new", title: "labs", domain_scopes: ["general", "health"] });
    render(
      <Harness
        d={deps({ listSessions: vi.fn(async () => []), createSession: vi.fn(async () => created) })}
      />,
    );
    await waitFor(() => screen.getByText("＋ New session — choose sources"));
    fireEvent.click(screen.getByText("＋ New session — choose sources"));
    fireEvent.click(screen.getByRole("button", { name: /Start session/ }));

    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(document.querySelector(".fb-title")?.textContent).toBe("labs");
  });

  it("tapping the session name reopens the Sessions panel", async () => {
    render(<Harness d={deps()} />);
    await waitFor(() => screen.getByLabelText("Conversation"));

    fireEvent.click(screen.getByRole("button", { name: "Recap" }));
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();
  });

  it("swipes shuttle the panels in and the opposite swipe sends them back", async () => {
    render(<Harness d={deps()} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    const shell = document.querySelector(".fb-shell") as Element;

    fireEvent.touchStart(shell, { touches: [{ clientX: 20, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 140, clientY: 205 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 140, clientY: 205 }] });
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();

    fireEvent.touchStart(shell, { touches: [{ clientX: 200, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 80, clientY: 203 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 80, clientY: 203 }] });
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();

    fireEvent.touchStart(shell, { touches: [{ clientX: 300, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 180, clientY: 203 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 180, clientY: 203 }] });
    expect(document.querySelector(".panel.right.open")).toBeInTheDocument();

    fireEvent.touchStart(shell, { touches: [{ clientX: 60, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 200, clientY: 203 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 200, clientY: 203 }] });
    expect(document.querySelector(".panel.right.open")).not.toBeInTheDocument();
  });
});
