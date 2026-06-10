// Single fetch wrapper for the backend API. Auth is a httpOnly session
// cookie, so every request sends credentials and a 401 anywhere means the
// session is gone — the app-level handler flips back to the login screen.
// Types are hand-written until Phase 1 introduces OpenAPI-generated clients
// (docs/DEVELOPMENT.md, "Code standards / TypeScript").

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

async function request(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
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
  // connection error in the viewer rather than forcing logout.
  opsLogStream(service: string): EventSource {
    return new EventSource(`/api/ops/logs/${encodeURIComponent(service)}/stream`);
  },
};
