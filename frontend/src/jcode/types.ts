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
  created_at: string;
  last_active_at: string;
}

export type JcodeEventType = "text" | "tool_use" | "tool_result" | "done" | "error";

// `data` is an opportunistic, optional hint bag the renderer reads when present:
// for a tool frame it may carry `command` (the displayed label) and/or `ok`. The
// real shape depends on the on-box SDK→TurnEvent mapping (JCODE_PLAN.md open
// decision 1); the UI falls back to the tool name when a hint is absent, so a
// thinner real frame degrades gracefully.
export type JcodeEvent =
  | { type: "run"; run_id: string }
  | {
      type: JcodeEventType;
      text?: string;
      tool?: string;
      data?: { command?: string; ok?: boolean } & Record<string, unknown>;
    };

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
}

// Whether the coder model is resident in the on-box gateway — drives the session
// screen's "loading model" bar. `loaded` flips true once the (cold ~tens-of-GB) load
// finishes; `size_gb` lets the bar estimate progress; `hosting` is false off-box.
export interface JcodeModelStatus {
  model: string;
  served: string;
  loaded: boolean;
  hosting: boolean;
  size_gb: number;
}
