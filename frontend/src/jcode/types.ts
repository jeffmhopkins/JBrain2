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

export type JcodeEvent =
  | { type: "run"; run_id: string }
  | {
      type: JcodeEventType;
      text?: string;
      tool?: string;
      data?: Record<string, unknown>;
    };

export interface NewSessionInput {
  repo: string;
  branch: string;
  work_branch: string;
}
