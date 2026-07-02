// Hand-written API types for the guided-intake recipient surface (no OpenAPI generator
// exists — these mirror backend/src/jbrain/api/intake.py; keep them in lockstep).

import type { ChatEvent } from "../agent/types";

export type { ChatEvent };

/** POST /api/intake/redeem response — the session config the stepper renders. */
export interface IntakeConfig {
  session_id: string;
  link_id: string;
  opening_blurb: string;
  capture_enterer_name: boolean;
  disclose_owner_identity: boolean;
}

/** POST /api/intake/confirm response. */
export interface IntakeConfirmOut {
  submission_id: string;
}

// --- Owner-side management (W6). Mirror the LinkOut/SessionOut/SubmissionOut shapes
// in backend/src/jbrain/api/intake.py; keep in lockstep. ---

/** GET /api/intake/links — a minted link's metadata (never its secret). `status` is
 * 'active' | 'revoked' | 'exhausted'. */
export interface IntakeLink {
  id: string;
  /** Null for a general collection not about a specific person (a recipe, general info). */
  subject_id: string | null;
  domain_code: string;
  label: string;
  fields_brief: string;
  persona_brief: string;
  opening_blurb: string;
  max_runs: number;
  runs_used: number;
  max_opens: number;
  opens_used: number;
  bind_on_first: boolean;
  capture_enterer_name: boolean;
  disclose_owner_identity: boolean;
  status: string;
  created_at: string;
  expires_at: string;
}

/** GET /api/intake/links/{id}/sessions — an opened recipient session. `status` is
 * 'drafting' | 'submitted' | 'abandoned'. */
export interface IntakeSessionRow {
  id: string;
  link_id: string;
  opened_at: string;
  status: string;
}

/** GET /api/intake/links/{id}/submissions — a confirmed capture. `status` is
 * 'submitted' (awaiting the owner) | 'proposed' (materialized into a Proposal). */
export interface IntakeSubmission {
  id: string;
  link_id: string;
  session_id: string;
  enterer_name: string;
  draft: Record<string, unknown>;
  status: string;
  proposal_id: string | null;
  note_ids: string[];
  created_at: string;
  updated_at: string;
}

/** GET /api/intake/submissions/{id} — one submission with its full transcript. */
export interface IntakeSubmissionDetail extends IntakeSubmission {
  transcript: IntakeTranscriptTurn[];
}

/** A stored interview turn ({role,text}); role 'recipient' is the visitor, anything
 * else is the interviewer. */
export interface IntakeTranscriptTurn {
  role?: string;
  text?: string;
}

/** MintLinkOut — the show-once secret, returned at mint / re-mint / from-proposal. */
export interface IntakeMintResult {
  id: string;
  label: string;
  expires_at: string;
  secret: string;
}

/** POST /api/intake/links — the full mint request (used for re-mint: clone a link's
 * config to a fresh secret). */
export interface IntakeMintRequest {
  /** Omit / null for a general collection not about a specific person. */
  subject_id?: string | null;
  domain_code: string;
  fields_brief: string;
  persona_brief?: string;
  opening_blurb?: string;
  label?: string;
  max_runs: number;
  max_opens?: number;
  bind_on_first: boolean;
  capture_enterer_name?: boolean;
  disclose_owner_identity?: boolean;
  ttl_hours?: number;
}

/** PATCH /api/intake/proposals/nodes/{id}/config — the constrained, owner-editable
 * fields of a staged intake-link Proposal (subject/domain are NOT editable; they are
 * fixed at staging and re-validated at mint). */
export interface IntakeConfigPatch {
  opening_blurb?: string;
  label?: string;
  persona_brief?: string;
  fields_brief?: string;
  max_runs?: number;
  max_opens?: number;
  bind_on_first?: boolean;
  ttl_hours?: number;
  capture_enterer_name?: boolean;
  disclose_owner_identity?: boolean;
}
