// Single fetch wrapper for the backend API. Auth is a httpOnly session
// cookie, so every request sends credentials and a 401 anywhere means the
// session is gone — the app-level handler flips back to the login screen.
// Types are hand-written until Phase 1 introduces OpenAPI-generated clients
// (docs/DEVELOPMENT.md, "Code standards / TypeScript").
//
// `npm run dev:mock` (VITE_MOCK=1) swaps the transport for in-memory
// fixtures so UI work never needs a backend (docs/DESIGN.md, "UI
// development process"). The flag is a build-time constant and the mock
// module loads via dynamic import, so fixtures never ship in real builds.

export interface Principal {
  principal_id: string;
  kind: string;
  label: string;
}

export interface ContainerStatus {
  service: string;
  state: string;
  health: string | null;
  started_at: string | null;
  image: string;
}

export interface OpsStatus {
  containers: ContainerStatus[];
}

export interface OpsMetrics {
  mem_total_bytes: number;
  mem_available_bytes: number;
  swap_total_bytes: number;
  swap_free_bytes: number;
  disk_total_bytes: number;
  disk_free_bytes: number;
  load_1m: number;
  load_5m: number;
  load_15m: number;
  uptime_seconds: number;
  containers: { service: string; mem_bytes: number }[];
  db: {
    db_size_bytes: number;
    note_count: number;
    attachment_count: number;
    attachment_bytes: number;
  } | null;
  blobs: { file_count: number; total_bytes: number } | null;
}

export interface UpdateStatus {
  state: "none" | "running" | "exited";
  exit_code: number | null;
  log_tail: string;
}

export interface AttachmentOut {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
}

export interface NoteOut {
  id: string;
  client_id: string;
  domain: string;
  destination: string | null;
  body: string;
  created_at: string;
  ingest_state: string;
  attachments: AttachmentOut[];
  // Owner-eyes capture location (Phase 7 scoped tokens never receive these).
  latitude: number | null;
  longitude: number | null;
  accuracy_m: number | null;
}

export interface NotesPage {
  notes: NoteOut[];
  next_cursor: string | null;
}

export interface NoteCreate {
  client_id: string;
  domain: string;
  destination?: string | null;
  body: string;
  latitude?: number;
  longitude?: number;
  accuracy_m?: number;
}

export interface NoteUpdate {
  body?: string;
  domain?: string;
  /** Explicit null clears the destination; an absent key leaves it. */
  destination?: string | null;
}

export type SearchMatch = "semantic" | "keyword" | "both";

export interface SearchResult {
  note_id: string;
  chunk_id: string;
  snippet: string;
  match: SearchMatch;
  score: number;
  domain: string;
  destination: string | null;
  created_at: string;
  body_preview: string;
  attachment_count: number;
  source_kind: string;
  source_anchor: string | null;
}

export interface SearchOut {
  degraded: boolean;
  results: SearchResult[];
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type UnauthorizedHandler = () => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

export const MOCK_MODE = import.meta.env.VITE_MOCK === "1";

// Resolve `fetch` at call time so test stubs (vi.stubGlobal) take effect.
const liveFetch: typeof fetch = (input, init) => fetch(input, init);

let transportPromise: Promise<typeof fetch> | null = null;
function getTransport(): Promise<typeof fetch> {
  transportPromise ??= MOCK_MODE
    ? import("./mock").then((m) => m.mockFetch)
    : Promise.resolve(liveFetch);
  return transportPromise;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const transport = await getTransport();
  const response = await transport(path, { credentials: "same-origin", ...init });
  if (response.status === 401) {
    unauthorizedHandler?.();
    throw new ApiError(401, "Not authenticated");
  }
  if (!response.ok) {
    throw new ApiError(response.status, `Request failed: ${response.status}`);
  }
  return response;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function attachmentUrl(id: string): string {
  return `/api/attachments/${encodeURIComponent(id)}`;
}

export const api = {
  async login(ownerKey: string, deviceLabel: string): Promise<void> {
    await request(
      "/api/auth/session",
      jsonInit("POST", { owner_key: ownerKey, device_label: deviceLabel }),
    );
  },

  async me(): Promise<Principal> {
    const response = await request("/api/auth/me");
    return (await response.json()) as Principal;
  },

  async logout(): Promise<void> {
    await request("/api/auth/session", { method: "DELETE" });
  },

  // Idempotent on client_id: retrying after a lost response returns the
  // already-created note instead of duplicating it.
  async createNote(note: NoteCreate): Promise<NoteOut> {
    const response = await request("/api/notes", jsonInit("POST", note));
    return (await response.json()) as NoteOut;
  },

  async getNote(id: string): Promise<NoteOut> {
    const response = await request(`/api/notes/${encodeURIComponent(id)}`);
    return (await response.json()) as NoteOut;
  },

  async listNotes(limit = 50, before?: string): Promise<NotesPage> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (before) params.set("before", before);
    const response = await request(`/api/notes?${params.toString()}`);
    return (await response.json()) as NotesPage;
  },

  // PATCH resets ingest_state to "pending" server-side — the stream's
  // indexing chip reappears until the worker re-chunks the note.
  async updateNote(id: string, patch: NoteUpdate): Promise<NoteOut> {
    const response = await request(
      `/api/notes/${encodeURIComponent(id)}`,
      jsonInit("PATCH", patch),
    );
    return (await response.json()) as NoteOut;
  },

  async deleteNote(id: string): Promise<void> {
    await request(`/api/notes/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  async search(q: string, domain?: string, limit = 20): Promise<SearchOut> {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (domain) params.set("domain", domain);
    const response = await request(`/api/search?${params.toString()}`);
    return (await response.json()) as SearchOut;
  },

  async uploadAttachment(noteId: string, blob: Blob, filename: string): Promise<AttachmentOut> {
    const form = new FormData();
    form.append("file", blob, filename);
    const response = await request(`/api/notes/${encodeURIComponent(noteId)}/attachments`, {
      method: "POST",
      body: form,
    });
    return (await response.json()) as AttachmentOut;
  },

  async deleteAttachment(id: string): Promise<void> {
    await request(`/api/attachments/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  async opsMetrics(): Promise<OpsMetrics> {
    const response = await request("/api/ops/metrics");
    return (await response.json()) as OpsMetrics;
  },

  async opsUpdateStart(): Promise<{ updater: string }> {
    const response = await request("/api/ops/update", { method: "POST" });
    return (await response.json()) as { updater: string };
  },

  async opsUpdateStatus(): Promise<UpdateStatus> {
    const response = await request("/api/ops/update/status");
    return (await response.json()) as UpdateStatus;
  },

  async opsStatus(): Promise<OpsStatus> {
    const response = await request("/api/ops/status");
    return (await response.json()) as OpsStatus;
  },

  async opsRestart(service: string): Promise<void> {
    await request("/api/ops/restart", jsonInit("POST", { service }));
  },

  async opsLogs(service: string, tail: number): Promise<string> {
    const response = await request(`/api/ops/logs/${encodeURIComponent(service)}?tail=${tail}`);
    return await response.text();
  },

  // EventSource cannot surface a 401, so a dead stream only shows as a
  // connection error in the viewer rather than forcing logout. In mock mode
  // the stream simply errors — log-following is out of mock scope.
  opsLogStream(service: string): EventSource {
    return new EventSource(`/api/ops/logs/${encodeURIComponent(service)}/stream`);
  },
};
