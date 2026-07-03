// The block-registry contract. A review detail is assembled from a sequence of
// typed blocks (docs/reference/DESIGN.md "Review inbox" → "Detail composition"); each one
// is a `ReviewBlock` that reads the shared `BlockCtx` and renders its slice, or
// returns null when its data is absent — so the kind→sequence table can be
// generous and the blocks self-gate.

import type { ReactElement } from "react";
import type { ReviewFilter, ReviewItem } from "../../api/client";
import type { Parsed } from "../payload";
import type { ReviewQueueController } from "../useReviewQueue";

/** A fact-bearing card corrected in place. Hoisted to the detail so the
 * proposed-fact panel (claim:inference) and the action block share one edit
 * state — editing the predicate, value, or modality flips the decision to a
 * correction (filed as a note, the #7 channel, never a hand-written fact). The
 * predicate picker offers `predicateSuggestions` (the canonicals nearest the
 * proposed relation, weighted by similarity) and free entry; the value is free
 * text, or an enum predicate's members as chips. `editable` is true for a
 * low-confidence inference AND for fact_conflict / attribute_collision — both
 * carry a structured proposed fact, so both correct in place. */
export interface InferenceEdit {
  editable: boolean;
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
  // modality (the fact's assertion) — a closed-enum corrected in place, so the
  // owner can fix "asserted" → "hypothetical"/"negated"/… when the pipeline
  // mis-read the claim's stance.
  originalModality: string;
  editModality: string;
  setEditModality: (v: string) => void;
  modalityEdited: boolean;
  // True when any side was changed — drives the approve-correction flip.
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
