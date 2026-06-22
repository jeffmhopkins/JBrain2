// The in-flight turn is keyed by its own session, so switching chats while a render
// streams — then switching back — must still show the running turn (it isn't in the
// stored transcript yet). Regression guard for "start an image, look at another
// session, come back, the render is gone".

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentSession, ChatEvent, TranscriptTurn } from "./types";
import { type FullBrainDeps, useFullBrain } from "./useFullBrain";

function session(over: Partial<AgentSession> = {}): AgentSession {
  return {
    id: "A",
    title: "A",
    status: "active",
    agent: "curator",
    domain_scopes: ["general"],
    subject_ids: [],
    created_at: "2026-06-01T00:00:00Z",
    last_active_at: "2026-06-01T00:00:00Z",
    ...over,
  };
}

function deps(over: Partial<FullBrainDeps> = {}): FullBrainDeps {
  return {
    listSessions: vi.fn(async () => [
      session({ id: "A", title: "A", last_active_at: "2026-06-02T00:00:00Z" }),
      session({ id: "B", title: "B", last_active_at: "2026-06-01T00:00:00Z" }),
    ]),
    createSession: vi.fn(async () => session({ id: "new" })),
    chat: async function* () {},
    listProposals: vi.fn(async () => []),
    getTranscript: vi.fn(async (): Promise<TranscriptTurn[]> => []),
    renameSession: vi.fn(async () => {}),
    deleteSession: vi.fn(async () => {}),
    archiveSession: vi.fn(async () => {}),
    unarchiveSession: vi.fn(async () => {}),
    rescopeSession: vi.fn(async () => {}),
    uploadChatAttachment: vi.fn(async (_s, f: File) => ({
      id: `att-${f.name}`,
      filename: f.name,
      media_type: f.type,
      size_bytes: f.size,
    })),
    getChatCapabilities: vi.fn(async () => ({ supports_vision: true, can_edit_images: true })),
    cancelChatRun: vi.fn(async () => {}),
    ...over,
  };
}

function liveProgress(ms: { tools: { progress?: { step: number } }[] }[]): number | undefined {
  // The image-progress step on the last (assistant) bubble's generate_image tool.
  return ms.at(-1)?.tools.at(-1)?.progress?.step;
}

describe("useFullBrain — a turn stays attached to its own chat", () => {
  afterEach(() => vi.restoreAllMocks());

  it("keeps the in-flight render when switching chats away and back", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    // A render that streams progress, then HOLDS in flight until released.
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "tool_call", id: "c1", name: "generate_image", arguments: { prompt: "cat" } };
      yield { type: "tool_progress", tool_call_id: "c1", step: 7, total: 20 };
      await gate;
      yield {
        type: "tool_view",
        tool_call_id: "c1",
        view: { view: "generated_image", surface: "inline", data: { image_id: "img1" }, refs: [] },
      };
      yield { type: "text_delta", text: "here's your cat" };
      yield { type: "done", stop_reason: "end_turn" };
    }

    const d = deps({ chat });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));

    // The newest chat (A) auto-opens.
    await waitFor(() => expect(result.current.active?.id).toBe("A"));

    // Start the render in A; its progress shows on the live bubble.
    await act(async () => {
      await result.current.send("draw a cat");
    });
    await waitFor(() => expect(liveProgress(result.current.messages)).toBe(7));

    // Look at chat B — A's render must NOT bleed into B's (empty) view.
    act(() => result.current.open(session({ id: "B", title: "B" })));
    await waitFor(() => expect(result.current.active?.id).toBe("B"));
    expect(liveProgress(result.current.messages)).toBeUndefined();
    expect(result.current.messages).toHaveLength(0);

    // Back to A — the still-running render is right where we left it.
    act(() => result.current.open(session({ id: "A", title: "A" })));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    expect(liveProgress(result.current.messages)).toBe(7);
    expect(result.current.messages.at(-1)?.streaming).toBe(true);

    // Finish the render — the final image and reply land on A.
    await act(async () => {
      release();
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.messages.at(-1)?.streaming).toBe(false));
    expect(result.current.messages.at(-1)?.views.some((v) => v.view === "generated_image")).toBe(
      true,
    );
    expect(result.current.messages.at(-1)?.text).toContain("here's your cat");
  });

  it("keeps a thinking turn (reasoning, no answer yet) across a chat switch", async () => {
    // Same scoping, but a turn that's mid-THOUGHT rather than rendering: the live
    // reasoning/thinking state must survive A→B→A just like an image render does.
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "reasoning_delta", text: "let me reason about this" };
      await gate;
      yield { type: "text_delta", text: "the answer" };
      yield { type: "done", stop_reason: "end_turn" };
    }

    const d = deps({ chat });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));

    await act(async () => {
      await result.current.send("think hard");
    });
    // Thinking: reasoning is accruing, no answer text yet, still streaming.
    await waitFor(() => expect(result.current.messages.at(-1)?.reasoning).toContain("reason"));
    expect(result.current.messages.at(-1)?.thinking).toBe(true);
    expect(result.current.messages.at(-1)?.text).toBe("");

    act(() => result.current.open(session({ id: "B", title: "B" })));
    await waitFor(() => expect(result.current.active?.id).toBe("B"));
    expect(result.current.messages).toHaveLength(0);

    // Back to A — the thinking turn is intact and still streaming.
    act(() => result.current.open(session({ id: "A", title: "A" })));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    expect(result.current.messages.at(-1)?.reasoning).toContain("reason");
    expect(result.current.messages.at(-1)?.streaming).toBe(true);

    await act(async () => {
      release();
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.messages.at(-1)?.streaming).toBe(false));
    expect(result.current.messages.at(-1)?.text).toContain("the answer");
  });
});
