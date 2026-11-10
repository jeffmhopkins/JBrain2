// The chat-turn glue on the client: the run id the PWA needs to Stop a detached
// turn rides as an `X-Run-Id` response header, which `api.chat` surfaces as a
// synthetic first `run` event (the detached turn no longer dies when the SSE stream
// closes, so Stop targets it by id via `cancelChatRun`). MOCK_MODE is off under
// vitest, so `request` uses the global fetch — stub it to drive these paths.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatEvent } from "../agent/types";
import { api } from "./client";

function sseBody(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
}

describe("api.chat run id", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("surfaces the X-Run-Id header as a synthetic run event ahead of the stream", async () => {
    const body = sseBody([
      'data: {"type":"text_delta","text":"hi"}\n\n',
      'data: {"type":"done","stop_reason":"end_turn"}\n\n',
    ]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { status: 200, headers: { "X-Run-Id": "run-42" } })),
    );

    const events: ChatEvent[] = [];
    for await (const event of api.chat({ session_id: "s1", message: "hi" })) events.push(event);

    // The run event leads, so the hook can capture the id before any content.
    expect(events[0]).toEqual({ type: "run", run_id: "run-42" });
    expect(events).toContainEqual({ type: "text_delta", text: "hi" });
    expect(events.at(-1)).toEqual({ type: "done", stop_reason: "end_turn" });
  });

  it("omits the run event when the header is absent", async () => {
    const body = sseBody(['data: {"type":"done","stop_reason":"end_turn"}\n\n']);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { status: 200 })),
    );

    const events: ChatEvent[] = [];
    for await (const event of api.chat({ session_id: "s1", message: "hi" })) events.push(event);

    expect(events.some((e) => e.type === "run")).toBe(false);
  });

  it("cancelChatRun POSTs the run's cancel endpoint", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.cancelChatRun("run-9");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/chat/runs/run-9/cancel",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("chatResume GETs the run's stream from the given offset and parses events", async () => {
    const body = sseBody([
      'data: {"type":"text_delta","text":"more"}\n\n',
      'data: {"type":"done","stop_reason":"end_turn"}\n\n',
    ]);
    const fetchMock = vi.fn(async () => new Response(body, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const events: ChatEvent[] = [];
    for await (const event of api.chatResume("run-9", 3)) events.push(event);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/chat/runs/run-9/stream?after=3",
      expect.anything(),
    );
    // No synthetic run event — the caller already holds the id; just the parsed frames.
    expect(events).toEqual([
      { type: "text_delta", text: "more" },
      { type: "done", stop_reason: "end_turn" },
    ]);
  });

  it("sessionLiveRun returns the run id + render snapshot + frame offset, or null", async () => {
    // A reloaded PWA maps a session back to its still-running detached turn so it can
    // reattach: it seeds the bubble from `snapshot` and resumes the stream at `frameIndex`.
    // A 404 (no live run) resolves to null rather than throwing.
    const snapshot = {
      role: "assistant",
      content: "partial answer",
      tools: [{ id: "t1", name: "deep_research", ok: null, sources: [] }],
      reasoning: "thinking…",
    };
    const hit = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ run_id: "run-7", snapshot, frame_index: 42, elapsed_ms: 90_000 }),
          {
            status: 200,
          },
        ),
    );
    vi.stubGlobal("fetch", hit);
    expect(await api.sessionLiveRun("s1")).toEqual({
      runId: "run-7",
      snapshot,
      frameIndex: 42,
      elapsedMs: 90_000,
    });
    expect(hit).toHaveBeenCalledWith("/api/chat/sessions/s1/live-run", expect.anything());

    // A server that hasn't attached a snapshot yet (task just started) → null snapshot, 0 offset.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ run_id: "run-8", snapshot: null, frame_index: 0 }), {
            status: 200,
          }),
      ),
    );
    expect(await api.sessionLiveRun("s1")).toEqual({
      runId: "run-8",
      snapshot: null,
      frameIndex: 0,
      // An older server that sends no elapsed figure reads as "just started".
      elapsedMs: 0,
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 404 })),
    );
    expect(await api.sessionLiveRun("s1")).toBeNull();
  });

  it("sessionLiveRun carries the owner's words that began the turn", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ run_id: "r", snapshot: null, frame_index: 0, question: "How big?" }),
            { status: 200 },
          ),
      ),
    );
    expect((await api.sessionLiveRun("s1"))?.question).toBe("How big?");
  });
});

describe("api.chatEvents", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("yields each announced run and skips anything else on the stream", async () => {
    const body = [
      ": open\n\n",
      'data: {"type":"run_started","session_id":"s1","run_id":"r1"}\n\n',
      ": keepalive\n\n",
      'data: {"type":"something_else","session_id":"s1","run_id":"r1"}\n\n',
      'data: {"type":"run_ended","session_id":"s1"}\n\n',
      'data: {"type":"run_ended","session_id":"s1","run_id":"r1"}\n\n',
    ].join("");
    const hit = vi.fn(async () => new Response(body, { status: 200 }));
    vi.stubGlobal("fetch", hit);
    const seen = [];
    for await (const ev of api.chatEvents()) seen.push(ev);
    expect(seen).toEqual([
      { type: "run_started", session_id: "s1", run_id: "r1" },
      { type: "run_ended", session_id: "s1", run_id: "r1" },
    ]);
    expect(hit).toHaveBeenCalledWith("/api/chat/events", expect.anything());
  });
});

describe("errorDetail", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("says a validation failure's messages rather than its status (C8)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              detail: [
                { loc: ["body", "text"], msg: "String should have at most 600 characters" },
                { loc: ["body", "to_device"], msg: "Field required" },
              ],
            }),
            { status: 422, headers: { "Content-Type": "application/json" } },
          ),
      ),
    );
    await expect(api.sendJpanelMessage("panel-quorra", "x")).rejects.toThrow(
      "String should have at most 600 characters; Field required",
    );
  });

  it("falls back to the status when the list carries nothing to say", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: [{}] }), { status: 422 })),
    );
    await expect(api.sendJpanelMessage("panel-quorra", "x")).rejects.toThrow("Request failed: 422");
  });
});

describe("api GrowBot", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stub(body: unknown, status = 200) {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("reads the status, the setup and the job from their routes", async () => {
    const fetchMock = stub({ job: null });
    expect(await api.growbotFlashJob()).toBeNull();
    await api.growbotStatus();
    await api.growbotFlashSetup();
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual([
      "/api/growbot/flash",
      "/api/growbot/status",
      "/api/growbot/flash/setup",
    ]);
  });

  it("starts a flash, revokes and tickets with POSTs carrying their bodies", async () => {
    const fetchMock = stub({ job: "job-1" }, 202);
    await api.startGrowbotFlash({ mode: "rekey", target: "gb-3fa91c" });
    await api.revokeGrowbotRobot("gb-3fa91c");
    await api.growbotTicket("gb-3fa91c");
    const calls = fetchMock.mock.calls.map((c) => [String(c[0]), c[1]?.method, c[1]?.body]);
    expect(calls).toEqual([
      ["/api/growbot/flash", "POST", JSON.stringify({ mode: "rekey", target: "gb-3fa91c" })],
      ["/api/growbot/robots/gb-3fa91c/revoke", "POST", undefined],
      ["/api/growbot/ticket", "POST", JSON.stringify({ code: "gb-3fa91c" })],
    ]);
  });

  it("reaches the persona routes with their methods and bodies", async () => {
    const fetchMock = stub({ persona: {}, checkpoint: null });
    const id = "00000000-0000-4000-8000-000000000001";
    await api.growbotPersonas();
    await api.newGrowbotPersona("Wren", "cp-1");
    await api.renameGrowbotPersona(id, "Sprouty");
    await api.editGrowbotRegion(id, "expression", { face: "ostrich" });
    await api.saveGrowbotCheckpoint(id, "Picnic");
    await api.restoreGrowbotCheckpoint(id, "cp-1");
    await api.importGrowbotPersona({ soul_format: 3 }, { over: id });
    await api.setGrowbotRider("gb-3fa91c", null);
    await api.growbotTicket({ persona: id, code: "gb-3fa91c" });
    const calls = fetchMock.mock.calls.map((c) => [String(c[0]), c[1]?.method, c[1]?.body]);
    expect(calls).toEqual([
      ["/api/growbot/personas", undefined, undefined],
      ["/api/growbot/personas", "POST", JSON.stringify({ name: "Wren", seed: "cp-1" })],
      [`/api/growbot/personas/${id}`, "PATCH", JSON.stringify({ name: "Sprouty" })],
      [
        `/api/growbot/personas/${id}/regions/expression`,
        "PUT",
        JSON.stringify({ value: { face: "ostrich" } }),
      ],
      [`/api/growbot/personas/${id}/checkpoints`, "POST", JSON.stringify({ name: "Picnic" })],
      [`/api/growbot/personas/${id}/checkpoints/cp-1/restore`, "POST", undefined],
      [
        "/api/growbot/personas/import",
        "POST",
        JSON.stringify({ soul: { soul_format: 3 }, over: id }),
      ],
      ["/api/growbot/body/gb-3fa91c", "PUT", JSON.stringify({ persona: null })],
      ["/api/growbot/ticket", "POST", JSON.stringify({ persona: id, code: "gb-3fa91c" })],
    ]);
  });

  it("names an exported soul file after the box's download name", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response("{}", {
            headers: { "Content-Disposition": 'attachment; filename="sprout-soul.json"' },
          }),
      ),
    );
    const out = await api.exportGrowbotPersona("00000000-0000-4000-8000-000000000001");
    expect(out.filename).toBe("sprout-soul.json");
  });

  it("surfaces the box's refusal sentence as the error", async () => {
    stub({ detail: "The box has no readable certificate — it won't flash a robot." }, 409);
    await expect(
      api.startGrowbotFlash({ mode: "flash", target: "new", name: "Pip", networks: [] }),
    ).rejects.toThrow("The box has no readable certificate — it won't flash a robot.");
  });
});
