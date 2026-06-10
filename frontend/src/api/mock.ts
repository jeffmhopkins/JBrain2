// In-memory fixture backend for `npm run dev:mock` (VITE_MOCK=1).
// Mirrors the real API contract closely enough for UI work: idempotent
// note creation, cursor pagination, multipart attachments, always-on auth.

import type { AttachmentOut, ContainerStatus, NoteOut, Principal } from "./client";

const PRINCIPAL: Principal = {
  principal_id: "mock-owner",
  kind: "owner_device",
  label: "Mock device",
};

const CONTAINERS: ContainerStatus[] = [
  {
    service: "api",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 5).toISOString(),
    image: "jbrain/api:edge",
  },
  {
    service: "postgres",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 30).toISOString(),
    image: "postgres:17",
  },
  {
    service: "worker",
    state: "exited",
    health: null,
    started_at: null,
    image: "jbrain/worker:edge",
  },
];

let nextId = 1;
function id(prefix: string): string {
  return `${prefix}-${nextId++}`;
}

const attachmentBlobs = new Map<string, Blob>();

function makeAttachment(filename: string, mediaType: string): AttachmentOut {
  const att = {
    id: id("att"),
    filename,
    media_type: mediaType,
    size_bytes: 24_120,
  };
  attachmentBlobs.set(att.id, new Blob([`mock contents of ${filename}`], { type: mediaType }));
  return att;
}

function daysAgo(days: number, hour: number, minute = 0): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

function seedNote(
  domain: string,
  destination: string | null,
  body: string,
  createdAt: string,
  attachments: AttachmentOut[] = [],
): NoteOut {
  return { id: id("note"), client_id: id("client"), domain, destination, body, created_at: createdAt, attachments };
}

// Oldest-first internally; the list endpoint serves newest-first.
const notes: NoteOut[] = [
  seedNote("general", null, "Dad: grandpa worked at the mill in Ohio before the war — family history follow-up", daysAgo(8, 19, 12)),
  seedNote("finance", "Statements", "Q2 brokerage statement filed — rebalance overdue", daysAgo(8, 20, 40), [makeAttachment("brokerage-q2.pdf", "application/pdf")]),
  seedNote("health", "Medications", "Started vitamin D 2000 IU daily per Dr. Akin", daysAgo(3, 8, 5)),
  seedNote("general", null, "Book rec from Sam: The Beginning of Infinity", daysAgo(3, 13, 30)),
  seedNote("finance", "Receipts", "Roof repair quote — $4,200 incl. flashing. Get second opinion?", daysAgo(1, 10, 22), [makeAttachment("roof-quote.jpg", "image/jpeg")]),
  seedNote("general", null, "Garage door keypad battery replaced — CR2032", daysAgo(1, 17, 48)),
  seedNote("general", null, "Groceries: eggs, coffee, olive oil, that bread Mom liked", daysAgo(0, 8, 15)),
  seedNote("health", "Labs", "Annual physical — BP 118/76. Lab orders attached.", daysAgo(0, 10, 5), [makeAttachment("lab-orders.pdf", "application/pdf")]),
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const LATENCY_MS = 120;
const sleep = () => new Promise((resolve) => setTimeout(resolve, LATENCY_MS));

const VALID_DOMAINS = new Set(["general", "health", "finance", "location"]);

export const mockFetch: typeof fetch = async (input, init) => {
  await sleep();
  const url = new URL(String(input instanceof Request ? input.url : input), "http://mock");
  const path = url.pathname;
  const method = (init?.method ?? "GET").toUpperCase();

  if (path === "/api/auth/session") return new Response(null, { status: 204 });
  if (path === "/api/auth/me") return json(PRINCIPAL);

  if (path === "/api/notes" && method === "GET") {
    const limit = Number(url.searchParams.get("limit") ?? "50");
    const before = url.searchParams.get("before");
    let pool = [...notes].sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (before) pool = pool.filter((n) => n.created_at < before);
    const page = pool.slice(0, limit);
    const last = page[page.length - 1];
    return json({ notes: page, next_cursor: pool.length > limit && last ? last.created_at : null });
  }

  if (path === "/api/notes" && method === "POST") {
    const body = JSON.parse(String(init?.body)) as {
      client_id: string;
      domain?: string;
      destination?: string | null;
      body: string;
    };
    const domain = body.domain ?? "general";
    if (!VALID_DOMAINS.has(domain)) return json({ detail: "unknown domain" }, 400);
    const existing = notes.find((n) => n.client_id === body.client_id);
    if (existing) return json(existing, 201);
    const note = seedNote(domain, body.destination ?? null, body.body, new Date().toISOString());
    note.client_id = body.client_id;
    notes.push(note);
    return json(note, 201);
  }

  const attachMatch = path.match(/^\/api\/notes\/([^/]+)\/attachments$/);
  if (attachMatch && method === "POST") {
    const note = notes.find((n) => n.id === decodeURIComponent(attachMatch[1] ?? ""));
    if (!note) return json({ detail: "unknown note" }, 404);
    const file = init?.body instanceof FormData ? init.body.get("file") : null;
    if (!(file instanceof Blob)) return json({ detail: "missing file" }, 422);
    const filename = file instanceof File ? file.name : "upload.bin";
    const att: AttachmentOut = {
      id: id("att"),
      filename,
      media_type: file.type || "application/octet-stream",
      size_bytes: file.size,
    };
    attachmentBlobs.set(att.id, file);
    note.attachments.push(att);
    return json(att, 201);
  }

  const blobMatch = path.match(/^\/api\/attachments\/([^/]+)$/);
  if (blobMatch) {
    const blob = attachmentBlobs.get(decodeURIComponent(blobMatch[1] ?? ""));
    if (!blob) return json({ detail: "unknown attachment" }, 404);
    return new Response(blob, { status: 200, headers: { "Content-Type": blob.type } });
  }

  if (path === "/api/ops/status") return json({ containers: CONTAINERS });
  if (path === "/api/ops/restart") return new Response(null, { status: 204 });
  if (path.startsWith("/api/ops/logs/")) {
    const lines = Array.from({ length: 40 }, (_, i) => `${new Date().toISOString()} mock log line ${i + 1}`);
    return new Response(lines.join("\n"), { status: 200 });
  }

  return json({ detail: `mock: no route for ${method} ${path}` }, 404);
};
