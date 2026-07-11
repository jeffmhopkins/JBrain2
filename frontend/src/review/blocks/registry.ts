// The block registry: the id→component lookup the detail renderer maps over,
// and the kind→sequence table that declares which blocks each review kind shows
// and in what order. Adding a review kind is "declare a sequence", not "add a
// screen branch". Listed blocks self-gate (render null when their data is
// absent), so a sequence can be generous; the table reads as the kind's intent.

import type { ReviewItem } from "../../api/client";
import { Action } from "./Action";
import { ClaimContradiction } from "./ClaimContradiction";
import { ClaimDiff } from "./ClaimDiff";
import { ClaimInference } from "./ClaimInference";
import { ClaimNotice } from "./ClaimNotice";
import { Evidence } from "./Evidence";
import { Header } from "./Header";
import { Trace } from "./Trace";
import type { BlockId, ReviewBlock } from "./types";

export const BLOCKS: Record<BlockId, ReviewBlock> = {
  header: Header,
  "claim:inference": ClaimInference,
  trace: Trace,
  "claim:diff": ClaimDiff,
  "claim:notice": ClaimNotice,
  "claim:contradiction": ClaimContradiction,
  action: Action,
  evidence: Evidence,
};

// Canonical body order (the footer is appended by the detail, lane-driven).
// Kinds list their blocks in this order; the default carries every gated block
// so an unmapped kind still renders correctly.
const DEFAULT_SEQUENCE: BlockId[] = [
  "header",
  "trace",
  "claim:diff",
  "claim:notice",
  "action",
  "evidence",
];

const SEQUENCE: Partial<Record<ReviewItem["kind"], BlockId[]>> = {
  // claim:diff is the read-only context (current → from this note); claim:inference
  // is the editable proposed-fact panel, so a conflict corrects in place too.
  fact_conflict: ["header", "trace", "claim:diff", "claim:inference", "action", "evidence"],
  attribute_collision: ["header", "trace", "claim:diff", "claim:inference", "action", "evidence"],
  low_confidence_inference: ["header", "claim:inference", "trace", "action", "evidence"],
  ambiguous_mention: ["header", "claim:notice", "action", "evidence"],
  new_predicate: ["header", "action", "evidence"],
  merge_proposal: ["header", "action", "evidence"],
  domain_promotion: ["header", "action", "evidence"],
  confirm_entity: ["header", "action", "evidence"],
  extraction_truncated: ["header", "action", "evidence"],
  low_confidence: ["header", "action", "evidence"],
  split_proposal: ["header", "action", "evidence"],
  // wiki_contradiction renders the source-grounded claim:contradiction block (chosen via the
  // three-mock GUI gate, docs/mocks/review-wiki-contradiction-b-source.html): the raw source is hero
  // and each paired record's facts hang beneath, so the owner rules in place. It self-gates on the
  // enriched payload, so a pre-enrichment card still renders (header + dismiss/correct action).
  wiki_contradiction: ["header", "claim:contradiction", "action", "evidence"],
  // wiki_stale_claim stays on the generic blocks (single-entity, no side-by-side to compare).
  wiki_stale_claim: ["header", "action", "evidence"],
};

export function blockSequenceFor(item: ReviewItem): BlockId[] {
  return SEQUENCE[item.kind] ?? DEFAULT_SEQUENCE;
}
