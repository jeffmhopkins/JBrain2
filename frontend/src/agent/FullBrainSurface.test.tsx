import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { AgentStatusLine, FullBrainSurface } from "./FullBrainSurface";
import type { AgentStatus } from "./status";
import type { AgentSession, ChatEvent, ChatRequest, TranscriptTurn } from "./types";
import { type FullBrainDeps, useFullBrain } from "./useFullBrain";

function session(over: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "s1",
    title: "Recap",
    status: "active",
    agent: "curator",
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
    archiveSession: vi.fn(async () => {}),
    unarchiveSession: vi.fn(async () => {}),
    rescopeSession: vi.fn(async () => {}),
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
    // The persisted tool sources rebuild the bubble's "Worked" disclosure.
    expect(document.querySelector(".fb-act-work")?.textContent).toContain("1 source");
    expect(getTranscript).toHaveBeenCalledWith("s1");
  });

  it("replays a turn's proposal and entity chips, not just its note sources", async () => {
    const getTranscript = vi.fn(
      async (): Promise<TranscriptTurn[]> => [
        { role: "user", content: "who am i?", tools: [] },
        {
          role: "assistant",
          content: "You are Jeff Hopkins.",
          tools: [
            {
              id: "c1",
              name: "find_entity",
              ok: true,
              sources: [],
              proposal: { proposal_id: "p1", kind: "correction" },
              entities: [
                { kind: "entity", entity_id: "e1", label: "Jeff Hopkins", domain: "general" },
              ],
            },
          ],
        },
      ],
    );
    render(<Harness d={deps({ getTranscript })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    // The staged-proposal chip survives the reopen (it lives on the tool-use face).
    expect(await screen.findByRole("button", { name: /Review proposal/ })).toBeInTheDocument();
    // The entity links inline in the replayed answer (its name appears in prose);
    // the same entity is also reachable as a chip inside its Worked step.
    const links = await screen.findAllByRole("button", { name: "Jeff Hopkins" });
    expect(links.some((b) => b.classList.contains("md-entity"))).toBe(true);
    expect(links.some((b) => b.classList.contains("entity-chip"))).toBe(true);
  });

  it("replays a turn's tool view (e.g. a list_card)", async () => {
    const getTranscript = vi.fn(
      async (): Promise<TranscriptTurn[]> => [
        { role: "user", content: "my groceries?", tools: [] },
        {
          role: "assistant",
          content: "Here's your list.",
          tools: [
            {
              id: "c1",
              name: "read_list",
              ok: true,
              sources: [],
              view: {
                view: "list_card",
                surface: "inline",
                data: {
                  list_id: "L1",
                  title: "Groceries",
                  items: [{ id: "a", body: "eggs", checked: false }],
                },
                refs: [],
              },
            },
          ],
        },
      ],
    );
    render(<Harness d={deps({ getTranscript })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    // The persisted list_card view rebuilds on reopen.
    expect(await screen.findByText("Groceries")).toBeInTheDocument();
    expect(screen.getByText("eggs")).toBeInTheDocument();
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

  it("scopes the review inbox to the active session", async () => {
    const listProposals = vi.fn(async () => []);
    render(<Harness d={deps({ listProposals })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    // The inbox is the open chat's: its session id rides the request, so the
    // panel shows that chat's staged proposals (+ the session-less background ones).
    await waitFor(() => expect(listProposals).toHaveBeenCalledWith("s1"));
  });

  it("reloads the inbox scoped to the chat the owner switches to", async () => {
    const listProposals = vi.fn(async () => []);
    const listSessions = vi.fn(async () => [
      session({ id: "s1", title: "First" }),
      session({ id: "s2", title: "Second" }),
    ]);
    render(<Harness d={deps({ listSessions, listProposals })} />);
    await waitFor(() => expect(listProposals).toHaveBeenCalledWith("s1"));

    // Open the Chats panel (left swipe) and switch to the other chat.
    const shell = document.querySelector(".fb-shell") as Element;
    fireEvent.touchStart(shell, { touches: [{ clientX: 20, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 140, clientY: 205 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 140, clientY: 205 }] });
    fireEvent.click(screen.getByText("Second"));

    // Switching chats re-scopes the inbox to the chat now open.
    await waitFor(() => expect(listProposals).toHaveBeenCalledWith("s2"));
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

  it("opens the active session's chat with the panels closed", async () => {
    render(<Harness d={deps()} />);
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    // The session name now rides in the top bar (HomeScreen owns it); here the
    // surface just lands on the transcript with no panel pulled in.
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
    // The raw "search · …" dump is gone; the tools fold into the inline "Worked"
    // disclosure under the answer.
    expect(screen.queryByText(/search · /)).not.toBeInTheDocument();
    const worked = screen.getByRole("button", { name: /Worked/ });
    expect(worked).toHaveTextContent("1 step");
    expect(worked).toHaveAttribute("aria-expanded", "false");
  });

  it("streams reasoning into a Thinking disclosure, then collapses to a duration", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "reasoning_delta", text: "let me think about this" };
      yield { type: "text_delta", text: "the answer" };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "why?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The disclosure carries the trace; once the answer lands it settles to
    // "Thought …" and collapses (the trace stays a tap away).
    const thinking = await screen.findByRole("button", { name: /Thought/ });
    await waitFor(() => expect(screen.getByText("the answer")).toBeInTheDocument());
    expect(thinking).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(thinking);
    expect(thinking).toHaveAttribute("aria-expanded", "true");
    expect(document.querySelector(".fb-thinking-trace")?.textContent).toBe(
      "let me think about this",
    );
  });

  it("copies the settled answer (citations stripped) and confirms, even with no tools", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText } });
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "The drive is ~15 min 【13†L9-L13】." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "how long?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // A plain answer (no reasoning, no tools) still gets the copy affordance.
    const copy = await screen.findByRole("button", { name: "Copy response" });
    fireEvent.click(copy);
    // The copied text is the clean prose the owner read — the model citation is gone.
    expect(writeText).toHaveBeenCalledWith("The drive is ~15 min.");
    expect(await screen.findByText("Copied ✓")).toBeInTheDocument();
  });

  it("does not offer copy while the answer is still streaming", async () => {
    let resolve: () => void = () => {};
    const gate = new Promise<void>((r) => {
      resolve = r;
    });
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "partial…" };
      await gate;
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "go" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    await waitFor(() => expect(screen.getByText("partial…")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Copy response" })).not.toBeInTheDocument();
    act(() => resolve());
    await screen.findByRole("button", { name: "Copy response" });
  });

  it("shows thinking and worked on one activity line, thinking through the tools", async () => {
    // A turn that reasons, runs a tool, then answers: both segments live on the
    // single foot line, and "Thinking…" persists across the tool call until the
    // answer's first token actually arrives.
    let resolveTool: () => void = () => {};
    const toolGate = new Promise<void>((r) => {
      resolveTool = r;
    });
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "reasoning_delta", text: "I should search first" };
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield { type: "tool_result", tool_call_id: "c1", ok: true, summary: "1 note" };
      await toolGate; // hold before the answer so we can assert the live state
      yield { type: "text_delta", text: "here you go" };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what notes?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // Reasoning + a finished tool, but no answer yet → still "Thinking…", and the
    // Worked segment is already on the same line.
    const thinking = await screen.findByRole("button", { name: /Thinking…/ });
    expect(screen.getByRole("button", { name: /Worked/ })).toBeInTheDocument();
    expect(thinking.closest(".fb-activity")).toBe(
      screen.getByRole("button", { name: /Worked/ }).closest(".fb-activity"),
    );

    // Let the answer arrive → thinking settles to "Thought …".
    act(() => resolveTool());
    await screen.findByRole("button", { name: /Thought/ });
    expect(screen.getByText("here you go")).toBeInTheDocument();
  });

  it("replays a stored turn's reasoning as a collapsed Thinking disclosure", async () => {
    const getTranscript = vi.fn(async () => [
      { role: "user" as const, content: "why?", tools: [], reasoning: "" },
      {
        role: "assistant" as const,
        content: "the answer",
        tools: [],
        reasoning: "because of the note",
      },
    ]);
    render(<Harness d={deps({ getTranscript })} />);
    const thinking = await screen.findByRole("button", { name: /Thought/ });
    expect(thinking).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(thinking);
    expect(document.querySelector(".fb-thinking-trace")?.textContent).toBe("because of the note");
  });

  it("expands the Worked disclosure to a step whose source card opens the cited note", async () => {
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

    // The answer stays visible; the "Worked" line carries the source count.
    const worked = await screen.findByRole("button", { name: /Worked/ });
    expect(worked).toHaveTextContent("2 sources");
    // The answer prose stays visible (Markdown may split it across nodes).
    expect(document.querySelector(".bubble.ai")?.textContent).toContain(
      "You were born March 19, 1986.",
    );

    // Expand the disclosure, then the step, and the source card opens the note.
    fireEvent.click(worked);
    expect(worked).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByText("Searched your notes"));
    fireEvent.click(screen.getByText("I was born March 19, 1986"));
    expect(onOpenNote).toHaveBeenCalledWith("abc-1");
  });

  it("expands and collapses the Worked disclosure in place, answer always visible", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield { type: "tool_result", tool_call_id: "c1", ok: true, summary: "1 note" };
      yield { type: "text_delta", text: "Here you go." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "go" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    const worked = await screen.findByRole("button", { name: /Worked/ });
    // No flip, no hidden face: the answer is present throughout, the disclosure
    // just toggles aria-expanded on the same button.
    expect(screen.getByText("Here you go.")).toBeInTheDocument();
    expect(worked).toHaveAttribute("aria-expanded", "false");
    expect(document.querySelector(".fb-front")).toBeNull();

    fireEvent.click(worked);
    expect(worked).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Here you go.")).toBeInTheDocument();

    fireEvent.click(worked);
    expect(worked).toHaveAttribute("aria-expanded", "false");
  });

  it("drills a step down to its arguments and raw result, and copies the raw text", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText } });
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: { query: "born", limit: 8 } };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "- note abc-1 [general] 2026-06-12: I was <mark>born</mark> March 19, 1986",
      };
      yield { type: "text_delta", text: "March 19, 1986." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "when born?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    fireEvent.click(await screen.findByRole("button", { name: /Worked/ }));
    fireEvent.click(screen.getByText("Searched your notes"));
    // The arguments the call went out with are now shown (threaded through the
    // reducer), one level deep.
    expect(screen.getByText("query")).toBeInTheDocument();
    expect(screen.getByText("born")).toBeInTheDocument();
    expect(screen.getByText("limit")).toBeInTheDocument();

    // The raw rung reveals the verbatim backend text, mark tags stripped, and copies.
    fireEvent.click(screen.getByRole("button", { name: "raw result" }));
    fireEvent.click(screen.getByRole("button", { name: "copy raw result" }));
    expect(writeText).toHaveBeenCalledWith(
      "- note abc-1 [general] 2026-06-12: I was born March 19, 1986",
    );
  });

  it("auto-opens a failed step and shows its error text in the danger tone", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "read_note", arguments: { note_id: "0c41" } };
      yield { type: "tool_result", tool_call_id: "c1", ok: false, summary: "note 0c41 not found" };
      yield { type: "text_delta", text: "I couldn't find that note." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "read 0c41" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The Worked line flags the failure; the failed step is open without a tap.
    const worked = await screen.findByRole("button", { name: /Worked/ });
    expect(worked).toHaveTextContent("1 failed");
    fireEvent.click(worked);
    const step = document.querySelector(".fb-step.err");
    expect(step).not.toBeNull();
    expect(step?.querySelector(".fb-step-row")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("note 0c41 not found")).toHaveClass("err");
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
    // The tool label is pinned for a beat (TOOL_HOLD_MS); give the finish room to
    // land once the hold elapses.
    await waitFor(
      () => expect(screen.getByRole("status").textContent).toContain("Answered · 1 tool used"),
      { timeout: 2000 },
    );
  });

  it("pins a tool label for a beat so a fast call is readable, swapping on a new tool", () => {
    vi.useFakeTimers();
    try {
      const tool = (label: string, emphasis: string): AgentStatus => ({
        kind: "tool",
        label,
        emphasis,
      });
      const done: AgentStatus = { kind: "done", label: "Answered · 1 tool used" };

      const { rerender } = render(<AgentStatusLine status={tool("Searching", "your notes")} />);
      expect(screen.getByRole("status").textContent).toContain("Searching your notes");

      // The tool finishes almost at once; its label must stay up, not flash away.
      rerender(<AgentStatusLine status={done} />);
      act(() => vi.advanceTimersByTime(300));
      expect(screen.getByRole("status").textContent).toContain("Searching your notes");

      // A second tool inside the window swaps the label and re-arms the hold.
      rerender(<AgentStatusLine status={tool("Reading", "a note")} />);
      expect(screen.getByRole("status").textContent).toContain("Reading a note");
      rerender(<AgentStatusLine status={done} />);
      act(() => vi.advanceTimersByTime(900));
      expect(screen.getByRole("status").textContent).toContain("Reading a note");

      // Once the full hold elapses it settles on the current status.
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByRole("status").textContent).toContain("Answered · 1 tool used");
    } finally {
      vi.useRealTimers();
    }
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

  it("linkifies a named entity inline and opens it on tap", async () => {
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

    // Once the name lands in the prose it's linked inline (md-entity); the same
    // entity also sits in its Worked step, so disambiguate to the inline link.
    await waitFor(() =>
      expect(
        screen
          .getAllByRole("button", { name: "Celine" })
          .some((b) => b.classList.contains("md-entity")),
      ).toBe(true),
    );
    const inline = screen
      .getAllByRole("button", { name: "Celine" })
      .find((b) => b.classList.contains("md-entity"));
    fireEvent.click(inline as HTMLElement);
    expect(onOpenEntity).toHaveBeenCalledWith("e9");
  });

  it("surfaces an entity the answer never names as a link in the Worked step", async () => {
    const onOpenEntity = vi.fn();
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "find_entity", arguments: { name: "celine" } };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "- Celine [Person] (general) id=e9",
        entities: [{ kind: "entity", entity_id: "e9", label: "Celine", domain: "general" }],
      };
      yield { type: "text_delta", text: "Found one match in your notes." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} onOpenEntity={onOpenEntity} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "who is celine?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // Not a loose pill under the prose: the entity is a tappable link inside the
    // step that resolved it, reached through the Worked drop-down.
    const chip = await screen.findByRole("button", { name: "Celine" });
    expect(chip).toHaveClass("entity-chip");
    expect(chip.closest(".fb-act-body")).not.toBeNull();
    fireEvent.click(chip);
    expect(onOpenEntity).toHaveBeenCalledWith("e9");
    // The raw id stays out of sight behind the "raw result" rung.
    expect(screen.queryByText(/id=e9/)).toBeNull();
  });

  it("holds the bubble until the answer text begins — tools alone show only status", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "find_entity", arguments: { name: "celine" } };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "- Celine [Person] (general) id=e9",
        entities: [{ kind: "entity", entity_id: "e9", label: "Celine", domain: "general" }],
      };
      await gate; // a tool ran, but the answer text hasn't been written yet
      yield { type: "text_delta", text: "Found one match." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "who is celine?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // Mid-stream with only a tool run: no bubble, no Worked drop, no entity —
    // the status line above the omnibox carries it instead.
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Looking up"));
    expect(screen.queryByRole("button", { name: /Worked/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Celine" })).toBeNull();

    // Once the answer text lands, the bubble appears with its Worked drop, and
    // the unnamed entity is a link inside it.
    release();
    await waitFor(() => expect(screen.getByRole("button", { name: /Worked/ })).toBeInTheDocument());
    expect(await screen.findByRole("button", { name: "Celine" })).toHaveClass("entity-chip");
  });

  it("flags an ungrounded claim inline and shows its reason on tap", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        sources: [{ note_id: "n1", domain: "general", snippet: "cholesterol labs" }],
      };
      yield { type: "text_delta", text: "The roof needs replacing soon." };
      yield { type: "done", stop_reason: "end_turn" };
      yield {
        type: "verdict",
        passed: false,
        score: 0,
        issues: ["claim not grounded in retrieved sources: The roof needs replacing soon."],
        ungrounded_claims: ["The roof needs replacing soon."],
      };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what's up?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    const flag = await screen.findByRole("button", { name: "unverified claim" });
    expect(flag).toHaveClass("md-flag");
    // The reason is hidden until the flag is tapped.
    expect(screen.queryByRole("note")).toBeNull();
    fireEvent.click(flag);
    expect(screen.getByRole("note")).toHaveTextContent(/Not in your notes/);
    // No mis-anchored fallback — it landed inline.
    expect(document.querySelector(".md-flag-fallback")).toBeNull();
    // The flagged claim TEXT is highlighted, not just marked with the trailing ⚠.
    const claim = document.querySelector(".md-claim");
    expect(claim).not.toBeNull();
    expect(claim?.textContent).toBe("The roof needs replacing soon.");
  });

  it("renders no flag on a grounded turn (verdict passes / is absent)", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        sources: [{ note_id: "n1", domain: "general", snippet: "cholesterol labs" }],
      };
      yield { type: "text_delta", text: "Your cholesterol is elevated." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "labs?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    await waitFor(() =>
      expect(screen.getByText("Your cholesterol is elevated.")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "unverified claim" })).toBeNull();
    // A grounded turn highlights no claim text either.
    expect(document.querySelector(".md-claim")).toBeNull();
  });

  it("degrades to an end-of-bubble flag when the claim can't be located in the prose", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        sources: [{ note_id: "n1", domain: "general", snippet: "cholesterol labs" }],
      };
      yield { type: "text_delta", text: "Here is a different paraphrase of the answer." };
      yield { type: "done", stop_reason: "end_turn" };
      yield {
        type: "verdict",
        passed: false,
        score: 0,
        issues: ["claim not grounded in retrieved sources: A sentence not present verbatim."],
        ungrounded_claims: ["A sentence not present verbatim."],
      };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what's up?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The claim isn't in the prose verbatim, so a single end-of-bubble flag stands
    // in (graceful fallback) and still opens the reason.
    const fallback = await waitFor(() => {
      const el = document.querySelector(".md-flag-fallback");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    const flag = fallback.querySelector(".md-flag") as HTMLElement;
    fireEvent.click(flag);
    expect(screen.getByRole("note")).toHaveTextContent(/Not in your notes/);
  });

  it("shows a neutral 'general knowledge' chip on a no-retrieval answer, not an amber flag", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "Jeff is a short form of Jeffrey." };
      yield { type: "done", stop_reason: "end_turn" };
      yield { type: "general_knowledge" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what is jeff?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The calm neutral provenance note renders — and is steel/info, NOT the amber
    // warning flag (DESIGN.md: info=steel, warning=amber).
    const note = await screen.findByText(/From general knowledge — not your notes/);
    expect(note).toHaveClass("fb-genknow");
    // Distinct from the amber "unverified claim" flag — that must not appear.
    expect(screen.queryByRole("button", { name: "unverified claim" })).toBeNull();
    expect(document.querySelector(".md-flag")).toBeNull();
  });

  it("shows neither chip on a grounded (retrieved) answer", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "find_entity", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        entities: [{ kind: "entity", entity_id: "e1", label: "Jeff", domain: "general" }],
      };
      yield { type: "text_delta", text: "Your name is Jeff." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what is my name?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The entity "Jeff" linkifies inline, splitting the prose across nodes — assert
    // on the bubble's text content, as the other entity tests do.
    await waitFor(() =>
      expect(document.querySelector(".bubble.ai")?.textContent).toContain("Your name is Jeff."),
    );
    // A grounded retrieval shows no provenance chip and no amber flag.
    expect(screen.queryByText(/From general knowledge/)).toBeNull();
    expect(document.querySelector(".fb-genknow")).toBeNull();
    expect(screen.queryByRole("button", { name: "unverified claim" })).toBeNull();
  });

  it("an ungrounded retrieved claim shows the amber flag, never the neutral chip", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "1",
        sources: [{ note_id: "n1", domain: "general", snippet: "cholesterol labs" }],
      };
      yield { type: "text_delta", text: "The roof needs replacing soon." };
      yield { type: "done", stop_reason: "end_turn" };
      yield {
        type: "verdict",
        passed: false,
        score: 0,
        issues: ["claim not grounded in retrieved sources: The roof needs replacing soon."],
        ungrounded_claims: ["The roof needs replacing soon."],
      };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what's up?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The amber flag stands; the neutral provenance chip must not appear.
    expect(await screen.findByRole("button", { name: "unverified claim" })).toBeInTheDocument();
    expect(document.querySelector(".fb-genknow")).toBeNull();
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
    await waitFor(() => screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByText("＋ New chat"));
    fireEvent.click(screen.getByRole("button", { name: /Start/ }));

    // The created session becomes active: its transcript shows and the picker
    // closes behind it. (Its name surfaces in the top bar, tested at HomeScreen.)
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();
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
