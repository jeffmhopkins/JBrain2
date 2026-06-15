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
    expect(document.querySelector(".fb-worked-btn")?.textContent).toContain("1 source");
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

  it("shows an appointment step as humanized text, ids tucked behind the raw rung", async () => {
    async function* answer(): AsyncGenerator<ChatEvent> {
      yield { type: "tool_call", id: "c1", name: "read_appointments", arguments: {} };
      yield {
        type: "tool_result",
        tool_call_id: "c1",
        ok: true,
        summary: "- dentist — 2026-06-22 17:00 [health] id=ef15afd3-1ac4-4a5e-aacf-dda47e925a7e",
      };
      yield { type: "text_delta", text: "You see the dentist on the 22nd." };
      yield { type: "done", stop_reason: "end_turn" };
    }
    render(<Harness d={deps({ chat: answer })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "my appointments?" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The step carries the friendly label, not the raw tool name.
    fireEvent.click(await screen.findByRole("button", { name: /Worked/ }));
    fireEvent.click(screen.getByText("Checked your calendar"));

    // The result reads as a plain line; the raw uuid is not on display.
    expect(screen.getByText("dentist — 2026-06-22 17:00 (health)")).toBeInTheDocument();
    expect(screen.queryByText(/id=ef15afd3/)).toBeNull();

    // The verbatim text (with the id) is still reachable one rung down.
    fireEvent.click(screen.getByRole("button", { name: "raw result" }));
    expect(
      screen.getByText(
        "- dentist — 2026-06-22 17:00 [health] id=ef15afd3-1ac4-4a5e-aacf-dda47e925a7e",
      ),
    ).toBeInTheDocument();
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
    expect(chip.closest(".fb-worked")).not.toBeNull();
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
