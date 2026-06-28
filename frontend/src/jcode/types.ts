// Types for code mode (jcode), Wave J3. The session shape mirrors the api's
// owner-only jcode_sessions index (JcodeSessionRow); the event shape mirrors the
// control server's turn frames (jcode_ctl.agent.TurnEvent), plus the synthetic
// `run` event the client yields from the X-Jcode-Run-Id header.

export interface JcodeSession {
  id: string;
  repo: string;
  branch: string;
  work_branch: string;
  status: string;
  // The owner's optional label (the launcher shows the repo when blank); `archived`
  // tidies a session out of the live list without deleting it. Both managed from the
  // launcher's swipe rail, mirroring the agent-sessions manager.
  title: string;
  archived: boolean;
  created_at: string;
  last_active_at: string;
}

// The owner's copy-link secret for a session — returned once by mint, never re-readable.
export interface JcodeShareToken {
  id: string;
  label: string;
  expires_at: string | null;
  token: string;
}

// A live share link as the owner's management list sees it — metadata only, no secret.
// `redeemed_at` is set once the link has been claimed (single-use), so the UI can show
// "opened" vs "unused".
export interface JcodeShare {
  id: string;
  label: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  redeemed_at: string | null;
}

// An external-LLM session: a token-gated public endpoint exposing the on-box coder to a
// remote Claude. `enabled` is the on/off toggle; the counters are cumulative usage.
export interface ExternalSession {
  id: string;
  label: string;
  enabled: boolean;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  in_tokens: number;
  out_tokens: number;
  requests: number;
}

// The mint result — the bearer secret + endpoint URL, shown EXACTLY once.
export interface ExternalMint {
  id: string;
  label: string;
  expires_at: string | null;
  token: string;
  url: string;
}

export interface NewSessionInput {
  repo: string;
  branch: string;
  work_branch: string;
}

// The per-session web preview (Wave J4): `enabled` is the server feature flag;
// `url` is the live ephemeral tunnel URL (null when no preview is open).
export interface JcodePreview {
  enabled: boolean;
  url: string | null;
  // "host" = a per-session hostname under the box's own tunnel (the Preview tab shows
  // the stable URL + the dev port, with no tunnel to open/stop); "tunnel" = the legacy
  // per-session quick-tunnel (open/close flow). Absent on older servers → treated as tunnel.
  mode?: "tunnel" | "host";
  // Host mode only: the dev port the session's server should bind ($PORT in the shell).
  port?: number | null;
}

// Whether the coder model is resident in the on-box gateway — drives the session
// screen's "loading model" bar. `warming` is true while the api's warm task runs (the
// real load window, and the bar's primary signal); `loaded` flips true once the model is
// resident but races true early, so it's not the bar's trigger. `progress` is the real
// load fraction (0..1, weights actually read in) parsed from the gateway logs, or null
// when there's no parseable signal — the bar follows it when present and falls back to a
// `size_gb`-based time estimate otherwise. `hosting` is false off-box.
export interface JcodeModelStatus {
  model: string;
  served: string;
  loaded: boolean;
  warming: boolean;
  /** Real load fraction (0..1) while warming, or null when no parseable signal yet. */
  progress: number | null;
  hosting: boolean;
  size_gb: number;
  /** The served context window the coder runs with (full native 256k for the coder). */
  context_window: number;
  /** Served-model names currently on the box — what a swap to the coder would evict. */
  resident: string[];
}
