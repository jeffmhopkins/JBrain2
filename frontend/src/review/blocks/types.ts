// The block-registry contract. A review detail is assembled from a sequence of
// typed blocks (docs/DESIGN.md "Review inbox" → "Detail composition"); each one
// is a `ReviewBlock` that reads the shared `BlockCtx` and renders its slice, or
// returns null when its data is absent — so the kind→sequence table can be
// generous and the blocks self-gate.

import type { ReactElement } from "react";
import type { ReviewFilter, ReviewItem } from "../../api/client";
import type { Parsed } from "../payload";
import type { ReviewQueueController } from "../useReviewQueue";

/** A low-confidence inference, corrected in place. Hoisted to the detail so the
 * proposed-fact panel (claim:inference) and the approve button (action) share
 * one edit state — editing either the predicate or the value flips approve →
 * approve correction. The predicate picker offers `predicateSuggestions` (the
 * canonicals nearest the proposed relation, weighted by similarity) and free
 * entry; the value is free text, or an enum predicate's members as chips. */
export interface InferenceEdit {
  isInference: boolean;
  originalValue: string;
  editValue: string;
  setEditValue: (v: string) => void;
  editingValue: boolean;
  setEditingValue: (b: boolean) => void;
  valueEdited: boolean;
  // predicate (the relation) — the weighted-picker side of correct-in-place.
  originalPredicate: string;
  editPredicate: string;
  setEditPredicate: (v: string) => void;
  editingPredicate: boolean;
  setEditingPredicate: (b: boolean) => void;
  predicateEdited: boolean;
  predicateSuggestions: { name: string; score: number }[];
  // True when either side was changed — drives the approve-correction flip.
  edited: boolean;
}

export interface BlockCtx {
  item: ReviewItem;
  parsed: Parsed;
  lane: ReviewFilter;
  queue: ReviewQueueController;
  // Shared armed-tap key-space for the detail's destructive controls.
  armed: string | null;
  tap: (key: string) => boolean;
  // Back to the list.
  onClose: () => void;
  // Advance to the next unresolved item after a decision (triage flow).
  onAdvance: () => void;
  inference: InferenceEdit;
  // The correction-note composer state, shared by the action block (the
  // textarea) and the footer (the "correct it" toggle).
  composing: boolean;
  setComposing: (b: boolean) => void;
  draft: string;
  setDraft: (v: string) => void;
}

export type ReviewBlock = ({ ctx }: { ctx: BlockCtx }) => ReactElement | null;

export type BlockId =
  | "header"
  | "claim:inference"
  | "trace"
  | "claim:diff"
  | "claim:notice"
  | "action"
  | "evidence";
