import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NoteOut } from "../api/client";
import { type PendingNote, createMemoryStore, flushOutbox, localCaptureIso } from "./outbox";

function pendingNote(overrides: Partial<PendingNote> = {}): PendingNote {
  return {
    client_id: "c-1",
    domain: "general",
    destination: null,
    body: "hello",
    created_at: "2026-06-10T10:00:00.000Z",
    attachments: [],
    ...overrides,
  };
}

function noteOut(clientId: string): NoteOut {
  return {
    id: `srv-${clientId}`,
    client_id: clientId,
    domain: "general",
    destination: null,
    body: "hello",
    created_at: "2026-06-10T10:00:01.000Z",
    ingest_state: "pending",
    attachments: [],
    latitude: null,
    longitude: null,
    accuracy_m: null,
  };
}

function jsonResponse(body: unknown, status = 201): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("flushOutbox", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("posts pending notes and clears them from the store", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote());
    fetchMock.mockResolvedValue(jsonResponse(noteOut("c-1")));

    const result = await flushOutbox(store);

    expect(result).toEqual({ sent: 1, remaining: 0 });
    expect(await store.all()).toEqual([]);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toBe("/api/notes");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      client_id: "c-1",
      domain: "general",
      body: "hello",
    });
  });

  it("sends write-time coordinates with the note when the outbox row has them", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote({ latitude: 47.6, longitude: -122.3, accuracy_m: 25 }));
    fetchMock.mockResolvedValue(jsonResponse(noteOut("c-1")));

    await flushOutbox(store);

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(JSON.parse(String(init?.body))).toMatchObject({
      latitude: 47.6,
      longitude: -122.3,
      accuracy_m: 25,
    });
  });

  it("sends the write-time capture instant with its UTC offset", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote({ captured_at: "2026-06-10T17:11:42-06:00" }));
    fetchMock.mockResolvedValue(jsonResponse(noteOut("c-1")));

    await flushOutbox(store);

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(JSON.parse(String(init?.body))).toMatchObject({
      captured_at: "2026-06-10T17:11:42-06:00",
    });
  });

  it("omits captured_at for pre-0008 outbox rows", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote());
    fetchMock.mockResolvedValue(jsonResponse(noteOut("c-1")));

    await flushOutbox(store);

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(JSON.parse(String(init?.body))).not.toHaveProperty("captured_at");
  });

  it("omits location fields entirely when the note has no fix", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote());
    fetchMock.mockResolvedValue(jsonResponse(noteOut("c-1")));

    await flushOutbox(store);

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(JSON.parse(String(init?.body))).not.toHaveProperty("latitude");
  });

  it("keeps the note on network failure and retries with the same client_id", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote());
    fetchMock.mockRejectedValueOnce(new TypeError("network down"));

    expect(await flushOutbox(store)).toEqual({ sent: 0, remaining: 1 });
    expect(await store.all()).toHaveLength(1);

    // Retry: the server saw the first POST and answers idempotently.
    fetchMock.mockResolvedValueOnce(jsonResponse(noteOut("c-1")));
    expect(await flushOutbox(store)).toEqual({ sent: 1, remaining: 0 });

    const clientIds = fetchMock.mock.calls.map(
      ([, init]) => (JSON.parse(String(init?.body)) as { client_id: string }).client_id,
    );
    expect(clientIds).toEqual(["c-1", "c-1"]);
    expect(await store.all()).toEqual([]);
  });

  it("uploads queued attachment blobs after the note posts", async () => {
    const store = createMemoryStore();
    const blob = new Blob(["pdf bytes"], { type: "application/pdf" });
    await store.put(
      pendingNote({
        attachments: [{ filename: "labs.pdf", media_type: "application/pdf", blob }],
      }),
    );
    fetchMock.mockResolvedValueOnce(jsonResponse(noteOut("c-1"))).mockResolvedValueOnce(
      jsonResponse({
        id: "a1",
        filename: "labs.pdf",
        media_type: "application/pdf",
        size_bytes: 9,
      }),
    );

    await flushOutbox(store);

    const [url, init] = fetchMock.mock.calls[1] ?? [];
    expect(String(url)).toBe("/api/notes/srv-c-1/attachments");
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get("file")).toBeInstanceOf(Blob);
    expect(await store.all()).toEqual([]);
  });

  it("retries only the failed attachment, not already-uploaded ones", async () => {
    const store = createMemoryStore();
    const blob = (name: string) => new Blob([name], { type: "text/plain" });
    await store.put(
      pendingNote({
        attachments: [
          { filename: "one.txt", media_type: "text/plain", blob: blob("one") },
          { filename: "two.txt", media_type: "text/plain", blob: blob("two") },
        ],
      }),
    );
    fetchMock
      .mockResolvedValueOnce(jsonResponse(noteOut("c-1"))) // note
      .mockResolvedValueOnce(
        jsonResponse({ id: "a1", filename: "one.txt", media_type: "text/plain", size_bytes: 3 }),
      )
      .mockRejectedValueOnce(new TypeError("network down")); // second attachment

    expect(await flushOutbox(store)).toEqual({ sent: 0, remaining: 1 });
    const left = await store.all();
    expect(left[0]?.attachments.map((a) => a.filename)).toEqual(["two.txt"]);

    fetchMock
      .mockResolvedValueOnce(jsonResponse(noteOut("c-1"))) // idempotent re-POST
      .mockResolvedValueOnce(
        jsonResponse({ id: "a2", filename: "two.txt", media_type: "text/plain", size_bytes: 3 }),
      );
    expect(await flushOutbox(store)).toEqual({ sent: 1, remaining: 0 });
    // 5 calls total: note, att1, (failed att2), note again, att2.
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("drops permanently rejected notes instead of wedging the queue", async () => {
    const store = createMemoryStore();
    await store.put(
      pendingNote({ client_id: "c-bad", domain: "bogus", created_at: "2026-06-10T09:00:00.000Z" }),
    );
    await store.put(pendingNote({ client_id: "c-ok" }));
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "unknown domain" }, 400))
      .mockResolvedValueOnce(jsonResponse(noteOut("c-ok")));

    expect(await flushOutbox(store)).toEqual({ sent: 1, remaining: 0 });
    expect(await store.all()).toEqual([]);
  });

  it("localCaptureIso renders local wall-clock time with an explicit offset", () => {
    // The exact field shape: an evening capture must read as the LOCAL date.
    const iso = localCaptureIso(new Date(2026, 5, 10, 17, 11, 42));
    expect(iso.startsWith("2026-06-10T17:11:42")).toBe(true);
    expect(iso).toMatch(/[+-]\d{2}:\d{2}$/);
  });

  it("flushes oldest-first so the stream stays ordered", async () => {
    const store = createMemoryStore();
    await store.put(pendingNote({ client_id: "c-new", created_at: "2026-06-10T12:00:00.000Z" }));
    await store.put(pendingNote({ client_id: "c-old", created_at: "2026-06-10T08:00:00.000Z" }));
    fetchMock.mockImplementation(async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as { client_id: string };
      return jsonResponse(noteOut(body.client_id));
    });

    await flushOutbox(store);

    const order = fetchMock.mock.calls.map(
      ([, init]) => (JSON.parse(String(init?.body)) as { client_id: string }).client_id,
    );
    expect(order).toEqual(["c-old", "c-new"]);
  });
});
