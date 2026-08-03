// Types for the Math launcher (jlaunch). The spec/job shapes mirror the control server's
// public() dicts (proxied by the api's owner routes); the run/share shapes mirror the
// owner run-mirror (JlaunchRunRow) and the public results endpoint.

/** A registered, runnable computation (from GET /jlaunch/specs). */
export interface JlaunchSpec {
  name: string;
  title: string;
  repo: string;
  phases: string[];
  artifact: string;
  est_hours: string;
  disk_gb: number;
  all_cores: boolean;
  notes: string;
}

/** One phase's wall-clock (parsed from the run's timing file). */
export interface JlaunchPhaseTime {
  phase: string;
  seconds: number;
  rc?: number;
}

/** A live/finished job as the owner sees it (control-server status, reconciled). */
export interface JlaunchJob {
  id: string;
  spec_name: string;
  title: string;
  repo: string;
  // queued | cloning | running | succeeded | failed | killed
  state: string;
  phase: string;
  artifact_name: string;
  started_at: string | null;
  finished_at: string | null;
  verify_ok: boolean | null;
  headline: string[];
  machine: Record<string, unknown>;
  phase_times: JlaunchPhaseTime[];
  error: string | null;
  artifact_present: boolean;
  artifact_bytes: number | null;
  console_tail: string;
}

/** The owner's copy-link secret for a run — returned once by mint, never re-readable. */
export interface JlaunchShareToken {
  id: string;
  label: string | null;
  token: string;
}

/** A live share link in the owner's management list — metadata only, no secret. */
export interface JlaunchShare {
  id: string;
  label: string | null;
  created_at: string;
  revoked_at: string | null;
  last_viewed_at: string | null;
  view_count: number;
}

/** The public results a shared run exposes (GET /jlaunch-share/{token}). */
export interface PublicJlaunchRun {
  title: string;
  spec_name: string;
  repo: string;
  state: string;
  verify_ok: boolean | null;
  headline: string[];
  machine: Record<string, unknown>;
  phase_times: JlaunchPhaseTime[];
  artifact_name: string;
  artifact_bytes: number | null;
  has_artifact: boolean;
  created_at: string;
  finished_at: string;
}
