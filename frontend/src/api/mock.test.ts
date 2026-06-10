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
    const res = await call(
      `/api/review/${first.id}/resolve`,
      jsonInit("POST", { action: "accept", payload: {} }),
    );
    expect(res.status).toBe(200);
    const updated = (await res.json()) as ReviewItem;
    expect(updated.payload.resolution).toBe("accept");

    const after = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(after.items.map((i) => i.id)).not.toContain(first.id);
    // Resolving twice conflicts rather than silently double-applying.
    expect(
      (
        await call(
          `/api/review/${first.id}/resolve`,
          jsonInit("POST", { action: "accept", payload: {} }),
        )
      ).status,
    ).toBe(409);
  });

  it("llm-usage: totals exercise k/M formatting and a null-cost task", async () => {
    const usage = (await (await call("/api/ops/llm-usage")).json()) as LlmUsage;
    expect(usage.today.input_tokens).toBeGreaterThan(1000);
    expect(usage.month.input_tokens).toBeGreaterThan(1_000_000);
    expect(usage.by_task.some((t) => t.cost_usd === null)).toBe(true);
    expect(usage.days).toHaveLength(7);
  });
});
