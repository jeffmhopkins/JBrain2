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
