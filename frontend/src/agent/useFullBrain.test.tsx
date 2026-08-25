// The in-flight turn is keyed by its own session, so switching chats while a render
// streams — then switching back — must still show the running turn (it isn't in the
// stored transcript yet). Regression guard for "start an image, look at another
// session, come back, the render is gone".

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentSession, ChatEvent, ChatRequest, TranscriptTurn } from "./types";
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
    chatResume: async function* () {},
    sessionLiveRun: vi.fn(async () => null),
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
    getChatCapabilities: vi.fn(async () => ({
      supports_vision: true,
      can_analyze_images: true,
      context_window: 262144,
    })),
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

  it("exposes activeTurn — rendering while an image tool runs, cleared when idle", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "tool_call", id: "c1", name: "generate_image", arguments: {} };
      yield { type: "tool_progress", tool_call_id: "c1", step: 3, total: 20 };
      await gate;
      yield { type: "text_delta", text: "done" };
      yield { type: "done", stop_reason: "end_turn" };
    }

    const d = deps({ chat });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    expect(result.current.activeTurn).toBeNull();

    await act(async () => {
      await result.current.send("draw");
    });
    // The running image tool reads as a render, scoped to its chat.
    await waitFor(() => expect(result.current.activeTurn?.kind).toBe("rendering"));
    expect(result.current.activeTurn?.sessionId).toBe("A");

    await act(async () => {
      release();
      await Promise.resolve();
    });
    // Settled → no live turn.
    await waitFor(() => expect(result.current.activeTurn).toBeNull());
  });

  it("resumes the live stream on a drop instead of falling back to the transcript", async () => {
    // The reconnect path: the stream drops mid-turn, the client reconnects (chatResume)
    // and folds the live tail onto the partial — no transcript recovery needed.
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "text_delta", text: "partial " };
      throw new Error("network drop");
    }
    const chatResume = vi.fn(async function* (): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "and the rest" };
      yield { type: "done", stop_reason: "end_turn" };
    });

    const d = deps({ chat, chatResume });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));

    await act(async () => {
      await result.current.send("go");
    });
    // The resumed tail continues the same bubble, and the turn settles live.
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe("partial and the rest"));
    expect(result.current.messages.at(-1)?.streaming).toBe(false);
    // It reconnected from the count of server frames already folded — the synthetic `run`
    // event isn't a server frame, so only the one text_delta counts (after=1).
    expect(chatResume).toHaveBeenCalledWith("r1", 1, expect.anything());
  });

  it("reattaches to a still-running detached turn after a reload", async () => {
    // The rejoin bug: a full PWA reload loses the in-memory run handle, so a turn still
    // running server-side would show nothing (the stored transcript has no in-flight turn)
    // and the sub-agent rail would read "done". On open, a session whose last_run_status is
    // "running" is looked up and reattached — the live tail streams back onto a fresh bubble.
    const sessionLiveRun = vi.fn(async () => ({ runId: "r9", snapshot: null, frameIndex: 0 }));
    const chatResume = vi.fn(async function* (): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "back live" };
      yield { type: "done", stop_reason: "end_turn" };
    });
    const listSessions = vi.fn(async () => [session({ id: "A", last_run_status: "running" })]);
    const d = deps({ sessionLiveRun, chatResume, listSessions });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    // The live run is discovered and its stream resumed onto a fresh bubble, which settles.
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe("back live"));
    expect(sessionLiveRun).toHaveBeenCalledWith("A");
    // No snapshot → seed an empty bubble and resume from frame 0.
    expect(chatResume).toHaveBeenCalledWith("r9", 0, expect.anything());
    expect(result.current.messages.at(-1)?.streaming).toBe(false);
  });

  it("seeds the reattach bubble from the snapshot and resumes at its frame offset", async () => {
    // The eviction case: a long deep-research turn emitted more frames than the live buffer
    // holds, so a fresh reload can't replay from frame 0 (the card's early frames are gone).
    // The live-run lookup returns a SNAPSHOT of the render so far + the frame offset it
    // reaches; the bubble seeds from the snapshot at once, and the stream resumes AFTER it.
    const snapshot: TranscriptTurn = {
      role: "assistant",
      content: "TTP seizures arise from",
      tools: [{ id: "t1", name: "deep_research", ok: null, sources: [] }],
      reasoning: "planning the fan",
    };
    const sessionLiveRun = vi.fn(async () => ({ runId: "r9", snapshot, frameIndex: 48352 }));
    const chatResume = vi.fn(async function* (): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: " microthrombi." };
      yield { type: "done", stop_reason: "end_turn" };
    });
    const listSessions = vi.fn(async () => [session({ id: "A", last_run_status: "running" })]);
    const d = deps({ sessionLiveRun, chatResume, listSessions });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    // The snapshot's card + partial answer show immediately, then the live tail appends —
    // no blank window, no dependence on the evicted early frames.
    await waitFor(() =>
      expect(result.current.messages.at(-1)?.text).toBe("TTP seizures arise from microthrombi."),
    );
    expect(result.current.messages.at(-1)?.tools[0]?.name).toBe("deep_research");
    // The stream resumes at the snapshot's absolute offset, not 0 — no replay, no gap.
    expect(chatResume).toHaveBeenCalledWith("r9", 48352, expect.anything());
    expect(result.current.messages.at(-1)?.streaming).toBe(false);
  });

  it("does not reattach when the session has no live run (stale status)", async () => {
    // last_run_status can lag reality — a crashed/reaped run still reads "running" until the
    // boot reaper closes it. The lookup returns null, so we leave the stored transcript be
    // rather than seeding a phantom streaming bubble.
    const sessionLiveRun = vi.fn(async () => null);
    const chatResume = vi.fn(async function* (): AsyncGenerator<ChatEvent> {});
    const listSessions = vi.fn(async () => [session({ id: "A", last_run_status: "running" })]);
    const getTranscript = vi.fn(
      async (): Promise<TranscriptTurn[]> => [
        { role: "user", content: "hi", tools: [] },
        { role: "assistant", content: "prior answer", tools: [] },
      ],
    );
    const d = deps({ sessionLiveRun, chatResume, listSessions, getTranscript });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe("prior answer"));
    expect(sessionLiveRun).toHaveBeenCalledWith("A");
    expect(chatResume).not.toHaveBeenCalled();
    // No streaming bubble was seeded — the chat reads as settled.
    expect(result.current.messages.at(-1)?.streaming).toBe(false);
    expect(result.current.activeTurn).toBeNull();
  });

  it("reloads the active chat's transcript when the PWA resumes from the background", async () => {
    // The reopen-staleness bug: a plan continuation lands while the PWA is hidden, but the
    // id-keyed transcript reload doesn't re-fire (activeId unchanged, no remount), so the new
    // turn is missing and a stale streaming bubble lingers until the owner switches chats. A
    // visibilitychange handler reconciles the active chat's transcript on resume.
    let turns: TranscriptTurn[] = [
      { role: "user", content: "make a plan", tools: [] },
      { role: "assistant", content: "plan ready", tools: [] },
    ];
    const getTranscript = vi.fn(async (): Promise<TranscriptTurn[]> => turns);
    const listSessions = vi.fn(async () => [session({ id: "A" })]);
    const d = deps({ getTranscript, listSessions });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.messages.length).toBe(2));

    // A continuation step lands while hidden — the settled transcript now has a third turn.
    turns = [...turns, { role: "assistant", content: "Step 1 done", tools: [] }];
    act(() => {
      Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    // On resume the handler refetches, so the missing turn appears without a chat switch.
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe("Step 1 done"));
    expect(result.current.messages.length).toBe(3);
  });

  it("refreshes the sessions list when a sub-agent spawns (live rail)", async () => {
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} };
      yield {
        type: "subagent_spawned",
        tool_call_id: "c1",
        child_id: "k1",
        persona: "research",
        label: "L",
        depth: 1,
      };
      yield { type: "text_delta", text: "done" };
      yield { type: "done", stop_reason: "end_turn" };
    }
    const listSessions = vi.fn(async () => [session({ id: "A" })]);
    const d = deps({ chat, listSessions });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    const before = listSessions.mock.calls.length;
    await act(async () => {
      await result.current.send("go");
    });
    // The spawn-triggered reload fires on top of the settle reload, so the list is
    // refetched at least twice during the turn (spawn + settle).
    expect(listSessions.mock.calls.length - before).toBeGreaterThanOrEqual(2);
  });

  it("recovers a dropped turn from the transcript WITHOUT cancelling the detached run", async () => {
    // A flaky network drops both the stream and the reconnect, but the turn runs detached
    // server-side and finishes on its own. We must NOT force-cancel it: an implicit cancel
    // once orphaned a healthy long sub-agent fan — it killed the parent (persisting a blank
    // spawn step) while the child ran on. Instead, recover the COMPLETED exchange from the
    // transcript once it lands. Only the explicit Stop may cancel.
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "text_delta", text: "partial " };
      throw new Error("network drop");
    }
    const chatResume = vi.fn((): AsyncGenerator<ChatEvent> => {
      throw new Error("reconnect failed");
    });
    const cancelChatRun = vi.fn(async () => {});
    // Empty on open, then the detached run lands its COMPLETE turn (the finished review),
    // which the recovery loop picks up on its next transcript poll.
    let nth = 0;
    const getTranscript = vi.fn(
      async (): Promise<TranscriptTurn[]> =>
        nth++ === 0 ? [] : [{ role: "assistant", content: "the full answer", tools: [] }],
    );
    const d = deps({ chat, chatResume, cancelChatRun, getTranscript });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    await act(async () => {
      await result.current.send("go");
    });
    // The completed turn is recovered and rendered; the healthy run is never cancelled.
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe("the full answer"));
    expect(cancelChatRun).not.toHaveBeenCalled();
  });

  it("reports a non-image tool as kind 'thinking', not rendering", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    async function* chat(): AsyncGenerator<ChatEvent> {
      yield { type: "run", run_id: "r1" };
      yield { type: "tool_call", id: "c1", name: "search", arguments: {} };
      await gate; // a non-image tool is in flight
      yield { type: "tool_result", tool_call_id: "c1", ok: true, summary: "2 notes" };
      yield { type: "text_delta", text: "found it" };
      yield { type: "done", stop_reason: "end_turn" };
    }

    const d = deps({ chat });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));

    await act(async () => {
      await result.current.send("look it up");
    });
    // A running non-image tool reads as thinking (only image tools are "rendering").
    await waitFor(() => expect(result.current.activeTurn?.kind).toBe("thinking"));

    await act(async () => {
      release();
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.activeTurn).toBeNull());
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

describe("useFullBrain — per-conversation model override", () => {
  afterEach(() => vi.restoreAllMocks());

  function recordingChat(bodies: ChatRequest[]) {
    return vi.fn(async function* (body: ChatRequest): AsyncGenerator<ChatEvent> {
      bodies.push(body);
      yield { type: "done", stop_reason: "end_turn" };
    });
  }

  it("rides the chosen model on every send, and clears back to the default", async () => {
    const bodies: ChatRequest[] = [];
    const d = deps({ chat: recordingChat(bodies) });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));

    // Default route: nothing on the wire, nothing shown.
    await act(async () => {
      await result.current.send("hi");
    });
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]?.model).toBeUndefined();
    expect(bodies[0]?.reasoning_effort).toBeUndefined();
    expect(result.current.modelOverride).toBeNull();

    // Pick a model (with a reasoning level) → it shows on the override and rides the
    // next turn, level and all.
    act(() =>
      result.current.setModelOverride({
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        effort: "high",
      }),
    );
    expect(result.current.modelOverride?.id).toBe("gpt-oss-120b");
    await act(async () => {
      await result.current.send("again");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]?.model).toBe("gpt-oss-120b");
    expect(bodies[1]?.reasoning_effort).toBe("high");

    // Clear → back to the default route.
    act(() => result.current.setModelOverride(null));
    expect(result.current.modelOverride).toBeNull();
    await act(async () => {
      await result.current.send("more");
    });
    await waitFor(() => expect(bodies).toHaveLength(3));
    expect(bodies[2]?.model).toBeUndefined();
  });

  it("scopes the pick to its own conversation", async () => {
    const bodies: ChatRequest[] = [];
    const d = deps({ chat: recordingChat(bodies) });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));

    act(() => result.current.setModelOverride({ id: "gpt-oss-120b", label: "GPT-OSS 120B" }));

    // Chat B has no pick — a send there carries no model.
    act(() => result.current.open(session({ id: "B", title: "B" })));
    await waitFor(() => expect(result.current.active?.id).toBe("B"));
    expect(result.current.modelOverride).toBeNull();
    await act(async () => {
      await result.current.send("from B");
    });
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]?.model).toBeUndefined();

    // Back to A — its pick is intact.
    act(() => result.current.open(session({ id: "A", title: "A" })));
    await waitFor(() => expect(result.current.active?.id).toBe("A"));
    expect(result.current.modelOverride?.id).toBe("gpt-oss-120b");
  });
});

describe("useFullBrain — sub-agent children survive the mode filter", () => {
  afterEach(() => vi.restoreAllMocks());

  // A spawned child carries its persona as its agent ("research"/…), not the tab's
  // spawner agent, so a naive mode filter would drop it and the SessionsPanel rail
  // would never see it. The exposed list must keep children whose parent is a
  // mode-visible chat (so the panel can nest them) — and only those.
  const jerv = (over: Partial<AgentSession> = {}): AgentSession =>
    session({ id: "jerv1", agent: "jerv", title: "compare towns", ...over });
  const child = (id: string, over: Partial<AgentSession> = {}): AgentSession =>
    session({ id, agent: "research", parent_session_id: "jerv1", title: id, ...over });

  it("keeps research-persona children under their jerv parent in research mode", async () => {
    const d = deps({
      listSessions: vi.fn(async () => [jerv(), child("k1"), child("k2", { agent: "summarize" })]),
    });
    const { result } = renderHook(() => useFullBrain("research", d));
    await waitFor(() => expect(result.current.sessions.length).toBe(3));
    const ids = result.current.sessions.map((s) => s.id).sort();
    expect(ids).toEqual(["jerv1", "k1", "k2"]);
  });

  it("does not leak a research-mode child as an orphan into the fullbrain tab", async () => {
    const d = deps({
      // A curator chat for the fullbrain tab, plus a jerv chat and its child (research tab).
      listSessions: vi.fn(async () => [
        session({ id: "cur1", agent: "curator", title: "brain" }),
        jerv(),
        child("k1"),
      ]),
    });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await waitFor(() => expect(result.current.sessions.length).toBe(1));
    // Only the curator chat — jerv and its orphan-parented child stay out of this tab.
    expect(result.current.sessions.map((s) => s.id)).toEqual(["cur1"]);
  });
});

describe("useFullBrain — live plan-continuation discovery", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("polls the active session's live-run while its plan is working", async () => {
    vi.useFakeTimers();
    const sessionLiveRun = vi.fn(async () => null);
    const d = deps({
      listSessions: vi.fn(async () => [session({ id: "A", plan_status: "in_work" })]),
      sessionLiveRun,
    });
    const { result } = renderHook(() => useFullBrain("fullbrain", d));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.active?.id).toBe("A");
    // No running turn to reattach and no poll has elapsed yet.
    expect(sessionLiveRun).not.toHaveBeenCalled();

    // One poll interval later, it looks for a continuation turn started server-side.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(sessionLiveRun).toHaveBeenCalledWith("A");
  });

  it("does not poll when the active session has no plan", async () => {
    vi.useFakeTimers();
    const sessionLiveRun = vi.fn(async () => null);
    const d = deps({
      listSessions: vi.fn(async () => [session({ id: "A" })]),
      sessionLiveRun,
    });
    renderHook(() => useFullBrain("fullbrain", d));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(sessionLiveRun).not.toHaveBeenCalled();
  });
});
