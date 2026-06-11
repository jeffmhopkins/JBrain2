// Contract checks for the fixture backend so `npm run dev:mock` keeps
// working as screens evolve (mock states are part of a screen's definition
// of done — docs/DESIGN.md "UI development process").

import { describe, expect, it } from "vitest";
import type {
  EntityOut,
  LlmUsage,
  NoteAnalysis,
  NoteOut,
  ReviewItem,
  ReviewQueue,
  SearchOut,
} from "./client";
import { mockFetch } from "./mock";

async function call(path: string, init?: RequestInit): Promise<Response> {
  return mockFetch(path, init);
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

describe("mock API", () => {
  it("searches fixtures with literal <mark> snippets and match badges", async () => {
    const res = await call("/api/search?q=vitamin&limit=20");
    const out = (await res.json()) as SearchOut;
    expect(out.degraded).toBe(false);
    expect(out.results.length).toBeGreaterThan(0);
    expect(out.results[0]?.snippet).toContain("<mark>");
    expect(["semantic", "keyword", "both"]).toContain(out.results[0]?.match);
  });

  it('flips degraded keyword-only mode on a "degraded!" query', async () => {
    const res = await call("/api/search?q=degraded!%20vitamin");
    const out = (await res.json()) as SearchOut;
    expect(out.degraded).toBe(true);
    expect(out.results.every((r) => r.match === "keyword")).toBe(true);
  });

  it("filters search by domain", async () => {
    const res = await call("/api/search?q=vitamin&domain=finance");
    const out = (await res.json()) as SearchOut;
    expect(out.results.every((r) => r.domain === "finance")).toBe(true);
  });

  it("accepts capture location on create and echoes it back", async () => {
    const res = await call(
      "/api/notes",
      jsonInit("POST", {
        client_id: "mock-test-loc",
        domain: "general",
        body: "located note",
        latitude: 47.6,
        longitude: -122.3,
        accuracy_m: 30,
      }),
    );
    expect(res.status).toBe(201);
    const note = (await res.json()) as NoteOut;
    expect(note.latitude).toBe(47.6);
    expect(note.ingest_state).toBe("pending");
  });

  it("PATCH mutates the fixture, resets ingest_state, and 400s unknown domains", async () => {
    const created = (await (
      await call(
        "/api/notes",
        jsonInit("POST", { client_id: "mock-test-patch", domain: "general", body: "before" }),
      )
    ).json()) as NoteOut;

    const bad = await call(`/api/notes/${created.id}`, jsonInit("PATCH", { domain: "bogus" }));
    expect(bad.status).toBe(400);

    const ok = await call(
      `/api/notes/${created.id}`,
      jsonInit("PATCH", { body: "after", domain: "health", destination: "Labs" }),
    );
    const updated = (await ok.json()) as NoteOut;
    expect(updated).toMatchObject({
      body: "after",
      domain: "health",
      destination: "Labs",
      ingest_state: "pending",
    });
  });

  it("DELETE removes the note; a second delete 404s", async () => {
    const created = (await (
      await call(
        "/api/notes",
        jsonInit("POST", { client_id: "mock-test-delete", domain: "general", body: "doomed" }),
      )
    ).json()) as NoteOut;

    expect((await call(`/api/notes/${created.id}`, { method: "DELETE" })).status).toBe(204);
    expect((await call(`/api/notes/${created.id}`, { method: "DELETE" })).status).toBe(404);
  });

  it("hide drops the note from the stream; unhide restores it", async () => {
    const created = (await (
      await call(
        "/api/notes",
        jsonInit("POST", { client_id: "mock-test-hide", domain: "general", body: "hide me" }),
      )
    ).json()) as NoteOut;
    const inStream = async () =>
      ((await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] }).notes.some(
        (n) => n.id === created.id,
      );

    expect(await inStream()).toBe(true);
    expect((await call(`/api/notes/${created.id}/hide`, { method: "POST" })).status).toBe(204);
    expect(await inStream()).toBe(false);
    // Hidden but still directly fetchable — it lives on in Search.
    expect(((await (await call(`/api/notes/${created.id}`)).json()) as NoteOut).hidden).toBe(true);

    expect((await call(`/api/notes/${created.id}/unhide`, { method: "POST" })).status).toBe(204);
    expect(await inStream()).toBe(true);
    expect((await call(`/api/notes/${crypto.randomUUID()}/hide`, { method: "POST" })).status).toBe(
      404,
    );
  });

  it("serves a full analysis for the Dr. Patel note, empty analysis elsewhere", async () => {
    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const patel = page.notes.find((n) => n.body.includes("Saw Dr. Patel this morning"));
    if (!patel) throw new Error("patel fixture note missing");

    const analysis = (await (await call(`/api/notes/${patel.id}/analysis`)).json()) as NoteAnalysis;
    expect(analysis.analyzed_at).not.toBeNull();
    expect(analysis.tags.length).toBeGreaterThanOrEqual(3);
    expect(analysis.facts).toHaveLength(6);
    const kinds = new Set(analysis.facts.map((f) => f.kind));
    expect(kinds).toContain("measurement");
    expect(kinds).toContain("event");
    expect(analysis.facts.some((f) => f.status === "pending_review")).toBe(true);
    expect(analysis.facts.some((f) => f.pinned)).toBe(true);
    expect(analysis.temporal_tokens.some((t) => t.resolved_start?.startsWith("2026-09"))).toBe(
      true,
    );

    const other = page.notes.find((n) => n.id !== patel.id);
    if (!other) throw new Error("second fixture note missing");
    const empty = (await (await call(`/api/notes/${other.id}/analysis`)).json()) as NoteAnalysis;
    expect(empty.analyzed_at).toBeNull();
    expect(empty.facts).toHaveLength(0);
  });

  it("entity pages: Sarah carries the Denver→Austin address history, newest first", async () => {
    const sarah = (await (await call("/api/entities/ent-sarah")).json()) as EntityOut;
    const address = sarah.predicates.find((p) => p.predicate === "address");
    expect(address?.current?.value_json).toBe("Denver, CO");
    expect(address?.current?.status).toBe("pending_review");
    expect(address?.history.map((f) => f.value_json)).toEqual(["Denver, CO", "Austin, TX"]);
    expect(address?.history[1]?.status).toBe("superseded");
    expect((await call("/api/entities/ent-nobody")).status).toBe(404);
  });

  it("review: open queue covers every kind; resolve mutates fixture state", async () => {
    const queue = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(queue.items.length).toBeGreaterThanOrEqual(6);
    const kinds = new Set(queue.items.map((i) => i.kind));
    for (const kind of [
      "fact_conflict",
      "attribute_collision",
      "merge_proposal",
      "ambiguous_mention",
      "domain_promotion",
      "low_confidence",
      "split_proposal",
    ]) {
      expect(kinds).toContain(kind);
    }

    const first = queue.items[0];
    if (!first) throw new Error("empty review fixture");
    // Collisions advertise accept_a/accept_b choices and no footer verbs;
    // an unadvertised action is rejected like the real backend rejects it.
    expect(first.kind).toBe("attribute_collision");
    expect(first.payload.outcomes).toBeUndefined();
    const choices = first.payload.choices as { action: string }[];
    expect(choices.map((c) => c.action)).toEqual(["accept_a", "accept_b"]);
    expect(
      (await call(`/api/review/${first.id}/resolve`, jsonInit("POST", { action: "accept" })))
        .status,
    ).toBe(400);

    const res = await call(
      `/api/review/${first.id}/resolve`,
      jsonInit("POST", { action: "accept_b", payload: {} }),
    );
    expect(res.status).toBe(200);
    const updated = (await res.json()) as ReviewItem;
    expect(updated.status).toBe("resolved");
    expect(updated.resolved_at).not.toBeNull();
    expect(updated.resolution?.action).toBe("accept_b");
    // Effects are recorded like the backend records them, so reopen can
    // round-trip in dev:mock.
    const actions = (updated.resolution?.effects ?? []).map((e) => e.action);
    expect(actions).toEqual(["pinned", "retracted"]);

    const after = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(after.items.map((i) => i.id)).not.toContain(first.id);
    // Resolving twice conflicts rather than silently double-applying.
    expect(
      (
        await call(
          `/api/review/${first.id}/resolve`,
          jsonInit("POST", { action: "accept_a", payload: {} }),
        )
      ).status,
    ).toBe(409);
  });

  it("review: resolved log lists decisions newest-first with dismissals folded in", async () => {
    const log = (await (await call("/api/review?status=resolved")).json()) as ReviewQueue;
    expect(log.items.length).toBeGreaterThanOrEqual(4);
    const statuses = new Set(log.items.map((i) => i.status));
    expect(statuses).toContain("resolved");
    expect(statuses).toContain("dismissed");
    const decidedAt = (i: ReviewItem) => i.resolved_at ?? i.resolution?.reopened_at ?? i.created_at;
    const order = log.items.map(decidedAt);
    expect(order).toEqual([...order].sort((a, b) => b.localeCompare(a)));
    for (const item of log.items) {
      expect(item.resolution).not.toBeNull();
    }
  });

  it("review: reopen re-queues with a tombstone marker; double-reopen 409s", async () => {
    const reopened = await call("/api/review/rev-done-2/reopen", { method: "POST" });
    expect(reopened.status).toBe(200);
    const item = (await reopened.json()) as ReviewItem & { reopen_note: string | null };
    expect(item.status).toBe("open");
    expect(item.resolved_at).toBeNull();
    expect(item.resolution?.reopened_at).toBeDefined();
    expect(item.reopen_note).toBeNull();

    // Back in the open queue AND tombstoned in the resolved log.
    const open = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(open.items.map((i) => i.id)).toContain("rev-done-2");
    const log = (await (await call("/api/review?status=resolved")).json()) as ReviewQueue;
    const tomb = log.items.find((i) => i.id === "rev-done-2");
    expect(tomb?.status).toBe("open");

    expect((await call("/api/review/rev-done-2/reopen", { method: "POST" })).status).toBe(409);
    expect((await call("/api/review/rev-nope/reopen", { method: "POST" })).status).toBe(404);
  });

  it("review: reopening a rejected merge keeps the permanent distinct_from edge", async () => {
    const reopened = await call("/api/review/rev-done-3/reopen", { method: "POST" });
    expect(reopened.status).toBe(200);
    const item = (await reopened.json()) as ReviewItem & { reopen_note: string | null };
    expect(item.status).toBe("open");
    expect(item.reopen_note).toContain("permanent");
  });

  it("llm-usage: totals exercise k/M formatting and a null-cost task", async () => {
    const usage = (await (await call("/api/ops/llm-usage")).json()) as LlmUsage;
    expect(usage.today.input_tokens).toBeGreaterThan(1000);
    expect(usage.month.input_tokens).toBeGreaterThan(1_000_000);
    expect(usage.by_task.some((t) => t.cost_usd === null)).toBe(true);
    expect(usage.days).toHaveLength(7);
  });

  // Last on purpose: reset wipes the shared fixtures the tests above read.
  it("ops reset: zeroes content fixtures, keeps usage; status ticks running→exited", async () => {
    expect((await call("/api/ops/reset", { method: "POST" })).status).toBe(202);

    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    expect(page.notes).toHaveLength(0);
    const open = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(open.items).toHaveLength(0);
    expect((await call("/api/entities/ent-sarah")).status).toBe(404);

    // Spend telemetry survives resets, like app.llm_usage on the real stack.
    const usage = (await (await call("/api/ops/llm-usage")).json()) as LlmUsage;
    expect(usage.today.input_tokens).toBeGreaterThan(0);

    const first = (await (await call("/api/ops/reset/status")).json()) as { state: string };
    expect(first.state).toBe("running");
    await call("/api/ops/reset/status");
    const done = (await (await call("/api/ops/reset/status")).json()) as {
      state: string;
      exit_code: number | null;
      log_tail: string;
    };
    expect(done.state).toBe("exited");
    expect(done.exit_code).toBe(0);
    expect(done.log_tail).toContain("[reset] complete");
  });
});
