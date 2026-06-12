import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FullBrainShell } from "./FullBrainShell";
import type { AgentSession, ChatEvent, ChatRequest } from "./types";

function session(over: Partial<AgentSession>): AgentSession {
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
const noProposals = vi.fn(async () => []);

describe("FullBrainShell", () => {
  it("opens the Sessions panel when there is no active session", async () => {
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [])}
        createSession={vi.fn()}
        chat={noChat}
        listProposals={noProposals}
      />,
    );
    await waitFor(() => expect(document.querySelector(".panel.left.open")).toBeInTheDocument());
    expect(screen.getByText(/Choose a session to start/)).toBeInTheDocument();
  });

  it("shows the chat for the most recent active session", async () => {
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [session({})])}
        createSession={vi.fn()}
        chat={noChat}
        listProposals={noProposals}
      />,
    );
    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(screen.getByText("Full Brain · general")).toBeInTheDocument();
    // Panels start closed.
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();
  });

  it("creating a session from the picker opens its chat", async () => {
    const created = session({ id: "new", title: "labs", domain_scopes: ["general", "health"] });
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [])}
        createSession={vi.fn(async () => created)}
        chat={noChat}
        listProposals={noProposals}
      />,
    );
    await waitFor(() => screen.getByText("＋ New session — choose sources"));
    fireEvent.click(screen.getByText("＋ New session — choose sources"));
    fireEvent.click(screen.getByRole("button", { name: /Start session/ }));

    await waitFor(() => expect(screen.getByLabelText("Conversation")).toBeInTheDocument());
    expect(screen.getByText("Full Brain · general · health")).toBeInTheDocument();
  });

  it("the visible nav buttons open each lateral panel", async () => {
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [session({})])}
        createSession={vi.fn()}
        chat={noChat}
        listProposals={noProposals}
      />,
    );
    await waitFor(() => screen.getByLabelText("Conversation"));

    fireEvent.click(screen.getByRole("button", { name: "Proposals" }));
    expect(document.querySelector(".panel.right.open")).toBeInTheDocument();
    // Close it, then the Sessions button.
    fireEvent.click(screen.getByRole("button", { name: "Sessions" }));
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();
  });

  it("swipes shuttle the panels in and the opposite swipe sends them back", async () => {
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [session({})])}
        createSession={vi.fn()}
        chat={noChat}
        listProposals={noProposals}
      />,
    );
    await waitFor(() => screen.getByLabelText("Conversation"));
    const shell = document.querySelector(".fb-shell") as Element;

    // Swipe right → Sessions in.
    fireEvent.touchStart(shell, { touches: [{ clientX: 20, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 140, clientY: 205 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 140, clientY: 205 }] });
    expect(document.querySelector(".panel.left.open")).toBeInTheDocument();

    // Swipe left (opposite) → Sessions back out.
    fireEvent.touchStart(shell, { touches: [{ clientX: 200, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 80, clientY: 203 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 80, clientY: 203 }] });
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();

    // Swipe left → Proposals in; swipe right (opposite) sends it back out.
    fireEvent.touchStart(shell, { touches: [{ clientX: 300, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 180, clientY: 203 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 180, clientY: 203 }] });
    expect(document.querySelector(".panel.right.open")).toBeInTheDocument();

    fireEvent.touchStart(shell, { touches: [{ clientX: 60, clientY: 200 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 200, clientY: 203 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 200, clientY: 203 }] });
    expect(document.querySelector(".panel.right.open")).not.toBeInTheDocument();
  });

  it("a swipe starting in the composer doesn't trigger a panel", async () => {
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [session({})])}
        createSession={vi.fn()}
        chat={noChat}
        listProposals={noProposals}
      />,
    );
    await waitFor(() => screen.getByLabelText("Conversation"));
    const shell = document.querySelector(".fb-shell") as Element;
    const composer = document.querySelector(".fb-composer") as Element;

    fireEvent.touchStart(composer, { touches: [{ clientX: 20, clientY: 400 }] });
    fireEvent.touchMove(shell, { touches: [{ clientX: 200, clientY: 402 }] });
    fireEvent.touchEnd(shell, { changedTouches: [{ clientX: 200, clientY: 402 }] });
    expect(document.querySelector(".panel.left.open")).not.toBeInTheDocument();
  });

  it("seeds the composer with a draft carried from the home box", async () => {
    render(
      <FullBrainShell
        listSessions={vi.fn(async () => [session({})])}
        createSession={vi.fn()}
        chat={noChat}
        listProposals={noProposals}
        initialDraft="what did I eat?"
      />,
    );
    await waitFor(() => screen.getByLabelText("Conversation"));
    expect(screen.getByLabelText("Message")).toHaveValue("what did I eat?");
  });
});
