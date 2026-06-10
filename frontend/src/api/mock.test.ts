// Contract checks for the fixture backend so `npm run dev:mock` keeps
// working as screens evolve (mock states are part of a screen's definition
// of done — docs/DESIGN.md "UI development process").

import { describe, expect, it } from "vitest";
import type { NoteOut, SearchOut } from "./client";
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
});
