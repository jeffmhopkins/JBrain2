// In-memory fixture backend for `npm run dev:mock` (VITE_MOCK=1).
// Mirrors the real API contract closely enough for UI work: idempotent
// note creation, cursor pagination, multipart attachments, always-on auth.

import type {
  AttachmentOut,
  ContainerStatus,
  NoteOut,
  Principal,
  SearchMatch,
  SearchResult,
} from "./client";

const PRINCIPAL: Principal = {
  principal_id: "mock-owner",
  kind: "owner_device",
  label: "Mock device",
};

const mockUpdate = { state: "none" as "none" | "running" | "exited", ticks: 0 };

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
  ingestState = "indexed",
): NoteOut {
  return {
    id: id("note"),
    client_id: id("client"),
    domain,
    destination,
    body,
    created_at: createdAt,
    ingest_state: ingestState,
    attachments,
    latitude: null,
    longitude: null,
    accuracy_m: null,
  };
}

// Oldest-first internally; the list endpoint serves newest-first.
const notes: NoteOut[] = [
  seedNote(
    "general",
    null,
    "Dad: grandpa worked at the mill in Ohio before the war — family history follow-up",
    daysAgo(8, 19, 12),
  ),
  seedNote(
    "finance",
    "Statements",
    "Q2 brokerage statement filed — rebalance overdue",
    daysAgo(8, 20, 40),
    [makeAttachment("brokerage-q2.pdf", "application/pdf")],
  ),
  seedNote(
    "health",
    "Medications",
    "Started vitamin D 2000 IU daily per Dr. Akin",
    daysAgo(3, 8, 5),
  ),
  seedNote("general", null, "Book rec from Sam: The Beginning of Infinity", daysAgo(3, 13, 30)),
  seedNote(
    "finance",
    "Receipts",
    "Roof repair quote — $4,200 incl. flashing. Get second opinion?",
    daysAgo(1, 10, 22),
    [makeAttachment("roof-quote.jpg", "image/jpeg")],
  ),
  seedNote("general", null, "Garage door keypad battery replaced — CR2032", daysAgo(1, 17, 48)),
  seedNote(
    "general",
    null,
    "Groceries: eggs, coffee, olive oil, that bread Mom liked",
    daysAgo(0, 8, 15),
  ),
  seedNote(
    "health",
    "Labs",
    "Annual physical — BP 118/76. Lab orders attached.\n\nDr. Akin wants a follow-up fasting panel in 3 months; book the draw early morning. Ask about the vitamin D dose then too.",
    daysAgo(0, 10, 5),
    [makeAttachment("lab-orders.pdf", "application/pdf")],
    "failed",
  ),
  seedNote(
    "general",
    null,
    "Long-form capture to exercise the 3-line clamp: the contractor said the south fence posts are rotted at the base, the gate hinge needs a longer lag bolt, and the section behind the shed should really be replaced whole rather than patched — he can quote both options next week, but materials prices change monthly so don't sit on it.",
    daysAgo(0, 11, 40),
  ),
  seedNote(
    "general",
    null,
    "Call the dentist about the crown — left side",
    daysAgo(0, 12, 10),
    [],
    "pending",
  ),
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

// Fake passage search over the note fixtures: substring match per term, a
// literal <mark> around the first hit (exercising the UI's mark-splitting),
// and a rotating match badge. `degraded!` anywhere in the query flips the
// keyword-only degraded banner on.
function mockSearch(params: URLSearchParams): { degraded: boolean; results: SearchResult[] } {
  const rawQ = params.get("q") ?? "";
  const domain = params.get("domain");
  const limit = Number(params.get("limit") ?? "20");
  const degraded = rawQ.includes("degraded!");
  const q = rawQ.replace(/degraded!/g, "").trim();
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);

  const matches = notes.filter((n) => {
    if (domain && n.domain !== domain) return false;
    if (terms.length === 0) return true;
    const body = n.body.toLowerCase();
    return terms.some((t) => body.includes(t));
  });

  const badges: SearchMatch[] = ["semantic", "keyword", "both"];
  const results = matches.slice(0, limit).map((n, i): SearchResult => {
    const term = terms.find((t) => n.body.toLowerCase().includes(t));
    const at = term ? n.body.toLowerCase().indexOf(term) : -1;
    const snippet =
      at >= 0 && term
        ? `${n.body.slice(Math.max(0, at - 60), at)}<mark>${n.body.slice(at, at + term.length)}</mark>${n.body.slice(at + term.length, at + term.length + 80)}`
        : n.body.slice(0, 140);
    const fromAttachment = n.attachments.length > 0 && i % 2 === 1;
    return {
      note_id: n.id,
      chunk_id: id("chunk"),
      snippet,
      match: degraded ? "keyword" : (badges[i % badges.length] ?? "keyword"),
      score: 1 - i * 0.07,
      domain: n.domain,
      destination: n.destination,
      created_at: n.created_at,
      body_preview: n.body.slice(0, 120),
      attachment_count: n.attachments.length,
      source_kind: fromAttachment ? "attachment" : "note",
      source_anchor: fromAttachment ? `${n.attachments[0]?.filename ?? "file"} · p.1` : null,
    };
  });
  return { degraded, results };
}

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
      latitude?: number;
      longitude?: number;
      accuracy_m?: number;
    };
    const domain = body.domain ?? "general";
    if (!VALID_DOMAINS.has(domain)) return json({ detail: "unknown domain" }, 400);
    const existing = notes.find((n) => n.client_id === body.client_id);
    if (existing) return json(existing, 201);
    const note = seedNote(
      domain,
      body.destination ?? null,
      body.body,
      new Date().toISOString(),
      [],
      "pending",
    );
    note.client_id = body.client_id;
    note.latitude = body.latitude ?? null;
    note.longitude = body.longitude ?? null;
    note.accuracy_m = body.accuracy_m ?? null;
    notes.push(note);
    return json(note, 201);
  }

  const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
  if (noteMatch && method === "PATCH") {
    const note = notes.find((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
    if (!note) return json({ detail: "note not found" }, 404);
    const patch = JSON.parse(String(init?.body)) as {
      body?: string;
      domain?: string;
      destination?: string | null;
    };
    if (patch.domain !== undefined && !VALID_DOMAINS.has(patch.domain))
      return json({ detail: "unknown domain" }, 400);
    if (patch.body !== undefined) note.body = patch.body;
    if (patch.domain !== undefined) note.domain = patch.domain;
    if ("destination" in patch) note.destination = patch.destination ?? null;
    // Mirrors the real PATCH: edits re-trigger ingestion.
    note.ingest_state = "pending";
    return json(note);
  }

  if (noteMatch && method === "DELETE") {
    const index = notes.findIndex((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
    if (index < 0) return json({ detail: "note not found" }, 404);
    notes.splice(index, 1);
    return new Response(null, { status: 204 });
  }

  if (path === "/api/search" && method === "GET") {
    return json(mockSearch(url.searchParams));
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
  if (blobMatch && method === "DELETE") {
    const attId = decodeURIComponent(blobMatch[1] ?? "");
    const owner = notes.find((n) => n.attachments.some((a) => a.id === attId));
    if (!owner) return json({ detail: "unknown attachment" }, 404);
    owner.attachments = owner.attachments.filter((a) => a.id !== attId);
    owner.ingest_state = "pending";
    attachmentBlobs.delete(attId);
    return new Response(null, { status: 204 });
  }
  if (blobMatch) {
    const blob = attachmentBlobs.get(decodeURIComponent(blobMatch[1] ?? ""));
    if (!blob) return json({ detail: "unknown attachment" }, 404);
    return new Response(blob, { status: 200, headers: { "Content-Type": blob.type } });
  }

  {
    const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
    if (noteMatch && (!init?.method || init.method === "GET")) {
      const note = notes.find((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
      return note ? json(note) : json({ detail: "note not found" }, 404);
    }
  }
  if (path === "/api/ops/update" && init?.method === "POST") {
    mockUpdate.state = "running";
    mockUpdate.ticks = 0;
    return json({ updater: "jbrain-updater-mock" }, 202);
  }
  if (path === "/api/ops/update/status") {
    if (mockUpdate.state === "running" && ++mockUpdate.ticks >= 3) {
      mockUpdate.state = "exited";
    }
    return json({
      state: mockUpdate.state,
      exit_code: mockUpdate.state === "exited" ? 0 : null,
      log_tail:
        mockUpdate.state === "none"
          ? ""
          : `[update] starting\n[update] building images\n${
              mockUpdate.state === "exited" ? "[update] complete" : ""
            }`,
    });
  }
  if (path === "/api/ops/metrics") {
    return json({
      mem_total_bytes: 4 * 2 ** 30,
      mem_available_bytes: 1.2 * 2 ** 30,
      swap_total_bytes: 2 * 2 ** 30,
      swap_free_bytes: 1.9 * 2 ** 30,
      disk_total_bytes: 40 * 2 ** 30,
      disk_free_bytes: 24 * 2 ** 30,
      load_1m: 0.42,
      load_5m: 0.31,
      load_15m: 0.2,
      uptime_seconds: 3 * 86400 + 7 * 3600,
      containers: CONTAINERS.map((c, i) => ({
        service: c.service,
        mem_bytes: (i + 1) * 90 * 2 ** 20,
      })),
      db: {
        db_size_bytes: 38 * 2 ** 20,
        note_count: 47,
        attachment_count: 9,
        attachment_bytes: 21 * 2 ** 20,
      },
      blobs: { file_count: 9, total_bytes: 21 * 2 ** 20 },
    });
  }
  if (path === "/api/ops/status") return json({ containers: CONTAINERS });
  if (path === "/api/ops/restart") return new Response(null, { status: 204 });
  if (path.startsWith("/api/ops/logs/")) {
    const lines = Array.from(
      { length: 40 },
      (_, i) => `${new Date().toISOString()} mock log line ${i + 1}`,
    );
    return new Response(lines.join("\n"), { status: 200 });
  }

  return json({ detail: `mock: no route for ${method} ${path}` }, 404);
};
