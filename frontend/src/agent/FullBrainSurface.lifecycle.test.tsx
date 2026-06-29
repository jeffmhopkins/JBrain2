// In-SPA lifecycle of the sub-agent research surface: switching session/screen and
// back WITHOUT a reload, a sessions-list refetch during a live fan, and the render
// branch that decides whether the fan shows. DOM-driven (the FullBrainSurface.test.tsx
// harness pattern: render(<Harness>) + fireEvent + findByText/waitFor on the real DOM,
// with a gate Promise to hold the stream open mid-fan), so a harness artifact can't
// masquerade as a product bug. Each test names the hypothesis it proves.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FullBrainSurface } from "./FullBrainSurface";
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

async function* noChat(_b: ChatRequest): AsyncGenerator<ChatEvent> {}

function deps(over: Partial<FullBrainDeps> = {}): FullBrainDeps {
  return {
    listSessions: vi.fn(async () => [
      session({ id: "A", title: "A", last_active_at: "2026-06-02T00:00:00Z" }),
      session({ id: "B", title: "B", last_active_at: "2026-06-01T00:00:00Z" }),
    ]),
    createSession: vi.fn(async () => session({ id: "new" })),
    chat: noChat,
    chatResume: async function* () {},
    cancelChatRun: vi.fn(async () => {}),
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
    ...over,
  };
}

// The home-screen composer plus the session-switch affordances a test needs to drive
// A→B→A in-SPA without a reload (each calls the hook's `open`).
function Harness({ d }: { d: FullBrainDeps }) {
  const fb = useFullBrain("fullbrain", d);
  const [text, setText] = useState("");
  return (
    <>
      <FullBrainSurface fb={fb} />
      <input aria-label="Composer" value={text} onChange={(e) => setText(e.target.value)} />
      <button type="button" onClick={() => fb.send(text)}>
        send
      </button>
      <button type="button" onClick={() => fb.open(session({ id: "A", title: "A" }))}>
        open-A
      </button>
      <button type="button" onClick={() => fb.open(session({ id: "B", title: "B" }))}>
        open-B
      </button>
      <span data-testid="active">{fb.active?.id ?? "none"}</span>
    </>
  );
}

// A turn that spawns one research child, holds the stream open mid-fan until released,
// then settles. `preSpawn` injects events before the spawn (e.g. an image tool, to probe
// the render branches).
// The realistic settle a fan emits: the child finishes, the spawn tool returns, and the
// backend's `subagent_synthesis` view lands (the roster card the UI swaps to on settle).
const SETTLE: ChatEvent[] = [
  {
    type: "subagent_done",
    tool_call_id: "sp1",
    child_id: "k1",
    ok: true,
    stop_reason: "end_turn",
    summary: "Troutdale: quiet week.",
    tree_spent: 100,
    tree_budget: 1000,
  },
  { type: "tool_result", tool_call_id: "sp1", ok: true, summary: "synthesized" },
  {
    type: "tool_view",
    tool_call_id: "sp1",
    view: {
      view: "subagent_synthesis",
      surface: "inline",
      data: {
        ran: 1,
        failed: 0,
        children: [
          {
            label: "Troutdale News",
            persona: "research",
            ok: true,
            summary: "quiet",
            session_id: "k1",
          },
        ],
      },
      refs: [],
    },
  },
];

function makeFanChat(opts: {
  gate: Promise<void>;
  preSpawn?: ChatEvent[];
  postRelease?: ChatEvent[];
}) {
  return async function* (): AsyncGenerator<ChatEvent> {
    yield { type: "run", run_id: "r1" };
    for (const e of opts.preSpawn ?? []) yield e;
    yield { type: "tool_call", id: "sp1", name: "spawn_subagent", arguments: { tasks: [] } };
    yield {
      type: "subagent_spawned",
      tool_call_id: "sp1",
      child_id: "k1",
      persona: "research",
      label: "Troutdale News",
      depth: 1,
    };
    yield {
      type: "subagent_progress",
      tool_call_id: "sp1",
      child_id: "k1",
      phase: "researching",
      step: 2,
      tree_spent: 50,
      tree_budget: 1000,
    };
    await opts.gate;
    for (const e of opts.postRelease ?? SETTLE) yield e;
    yield { type: "done", stop_reason: "end_turn" };
  };
}

describe("FullBrainSurface lifecycle — fan render guards & in-SPA navigation", () => {
  afterEach(() => vi.restoreAllMocks());

  // ── H1: a live fan started in A survives an A→B→A switch (no reload) — the per-session
  // store + the turnSessionRef skip in the transcript-reload effect preserve it.
  it("[H1] keeps a live sub-agent fan rendering after an A→B→A switch", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const chat = makeFanChat({ gate });
    const { container } = render(<Harness d={deps({ chat })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("A"));

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "compare towns" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    // The live fan is on A's screen, still streaming.
    await waitFor(() => expect(container.querySelector(".fb-sa")).not.toBeNull());
    expect(screen.getByText("Troutdale News")).toBeInTheDocument();

    // Switch to B — A's fan must not be on screen there.
    fireEvent.click(screen.getByRole("button", { name: "open-B" }));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("B"));
    expect(container.querySelector(".fb-sa")).toBeNull();

    // Back to A — the running fan is right where we left it, still streaming.
    fireEvent.click(screen.getByRole("button", { name: "open-A" }));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("A"));
    await waitFor(() => expect(container.querySelector(".fb-sa")).not.toBeNull());
    expect(screen.getByText("Troutdale News")).toBeInTheDocument();

    // On settle the live fan stands down and the synthesis roster card takes its place —
    // the single, consistent surface (same as a reopen would show).
    act(() => release());
    await waitFor(() => expect(container.querySelector(".tv-syn")).not.toBeNull());
    expect(container.querySelector(".fb-sa")).toBeNull();
    expect(screen.getByText("Troutdale News")).toBeInTheDocument();
  });

  // ── H2: while A's fan streams and B is on screen, B shows NO fan (the fold is scoped to
  // the originating session; B never picks up A's subagent_* frames).
  it("[H2] shows no fan in B while A's fan streams in the background", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const chat = makeFanChat({ gate });
    const { container } = render(<Harness d={deps({ chat })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("A"));

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "compare towns" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await waitFor(() => expect(container.querySelector(".fb-sa")).not.toBeNull());

    // Switch to B while A's fan keeps streaming — B's transcript is empty, no fan.
    fireEvent.click(screen.getByRole("button", { name: "open-B" }));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("B"));
    expect(container.querySelector(".fb-sa")).toBeNull();
    expect(screen.queryByText("Troutdale News")).toBeNull();

    // Drive the fan to settle (more frames into A) while B is on screen — B stays empty.
    act(() => release());
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("B"));
    expect(container.querySelector(".fb-sa")).toBeNull();
    expect(screen.queryByText("Troutdale News")).toBeNull();
  });

  // ── H5: a turn that produced an image AND a fan renders BOTH. The image-split branch
  // used to early-`return` before the fan render path, dropping the research roster from
  // an image+research turn; the shared `fanBlocks` (rendered in both branches) plus the
  // synthesis view in `otherViews` fix it. On settle the image bubble AND the synthesis
  // roster are both present.
  it("[H5] an image turn still shows its sub-agent research roster", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    // Preamble + an image tool that yields a generated_image view, THEN the spawn + child —
    // a full-tool turn that both draws and fans out research.
    const chat = makeFanChat({
      gate,
      preSpawn: [
        { type: "text_delta", text: "Here's a sketch." },
        { type: "tool_call", id: "g1", name: "generate_image", arguments: { prompt: "fox" } },
        { type: "tool_result", tool_call_id: "g1", ok: true, summary: "generated" },
        {
          type: "tool_view",
          tool_call_id: "g1",
          view: {
            view: "generated_image",
            surface: "inline",
            data: { image_id: "img_1", kind: "generate", prompt: "fox", width: 768, height: 768 },
            refs: [],
          },
        },
      ],
      postRelease: [...SETTLE, { type: "text_delta", text: " Done." }],
    });
    const { container } = render(<Harness d={deps({ chat })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "draw + research" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    act(() => release());
    // The image renders in its own media bubble, the reply ("Done.") settles…
    await waitFor(() => expect(container.querySelector(".bubble.ai.bubble-media")).not.toBeNull());
    await waitFor(() => expect(screen.getByText(/Done\./)).toBeInTheDocument());
    // …and the research roster is NOT lost — the synthesis card shows alongside the image.
    await waitFor(() => expect(container.querySelector(".tv-syn")).not.toBeNull());
    expect(screen.getByText("Troutdale News")).toBeInTheDocument();
  });

  // ── H3: a sessions-list refetch during a live fan (fired on subagent_spawned and on
  // settle) must not clobber the active live transcript — the refetch only touches
  // sessions/active identity, never messagesBySession.
  it("[H3] a sessions reload during a live fan does not clobber the active transcript", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const chat = makeFanChat({ gate });
    // A fresh array each call (a real refetch) — the live buffer must be unaffected by the
    // spawn-triggered reloadSessions.
    const listSessions = vi.fn(async () => [
      session({ id: "A", title: "A", last_active_at: "2026-06-02T00:00:00Z" }),
      session({ id: "B", title: "B", last_active_at: "2026-06-01T00:00:00Z" }),
    ]);
    const { container } = render(<Harness d={deps({ chat, listSessions })} />);
    await waitFor(() => screen.getByLabelText("Conversation"));
    await waitFor(() => expect(screen.getByTestId("active").textContent).toBe("A"));
    const before = listSessions.mock.calls.length;

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "compare" } });
    fireEvent.click(screen.getByRole("button", { name: "send" }));

    // The spawn fired a reload (the list refetched again)…
    await waitFor(() => expect(listSessions.mock.calls.length).toBeGreaterThan(before));
    // …yet the live fan is intact on A, still streaming.
    await waitFor(() => expect(container.querySelector(".fb-sa")).not.toBeNull());
    expect(screen.getByText("Troutdale News")).toBeInTheDocument();
    // And it settles to the synthesis card, transcript intact (the reload never clobbered it).
    act(() => release());
    await waitFor(() => expect(container.querySelector(".tv-syn")).not.toBeNull());
    expect(container.querySelector(".fb-sa")).toBeNull();
  });
});
