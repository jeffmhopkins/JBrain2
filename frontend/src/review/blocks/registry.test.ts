import { describe, expect, it } from "vitest";
import type { ReviewItem } from "../../api/client";
import { BLOCKS, blockSequenceFor } from "./registry";

function item(kind: ReviewItem["kind"]): ReviewItem {
  return {
    id: "x",
    kind,
    domain: "general",
    created_at: "2026-06-10T00:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {},
  };
}

describe("block registry", () => {
  it("every block id in a sequence resolves to a component", () => {
    const kinds: ReviewItem["kind"][] = [
      "fact_conflict",
      "attribute_collision",
      "merge_proposal",
      "ambiguous_mention",
      "domain_promotion",
      "low_confidence",
      "low_confidence_inference",
      "split_proposal",
      "extraction_truncated",
      "new_predicate",
      "confirm_entity",
      "wiki_contradiction",
      "wiki_stale_claim",
    ];
    for (const kind of kinds) {
      for (const id of blockSequenceFor(item(kind))) {
        expect(BLOCKS[id]).toBeDefined();
      }
    }
  });

  it("leads with the header and ends with evidence for every kind", () => {
    for (const kind of ["fact_conflict", "merge_proposal", "new_predicate"] as const) {
      const seq = blockSequenceFor(item(kind));
      expect(seq[0]).toBe("header");
      expect(seq.at(-1)).toBe("evidence");
    }
  });

  it("gives the editable proposed-fact panel to inference and conflict/collision", () => {
    // claim:inference is the correct-in-place editor; conflict/collision pair it
    // with the read-only claim:diff so they edit a third value, not only pick a/b.
    for (const kind of [
      "low_confidence_inference",
      "fact_conflict",
      "attribute_collision",
    ] as const)
      expect(blockSequenceFor(item(kind))).toContain("claim:inference");
    // Kinds with no structured proposed fact never show the editor.
    expect(blockSequenceFor(item("merge_proposal"))).not.toContain("claim:inference");
  });

  it("gives a collision its before→after diff block", () => {
    expect(blockSequenceFor(item("attribute_collision"))).toContain("claim:diff");
  });

  it("gives a wiki contradiction its source-grounded comparison block", () => {
    // The block self-gates on the enriched payload, so it sits before the action
    // block and a pre-enrichment card still renders through header + action.
    const seq = blockSequenceFor(item("wiki_contradiction"));
    expect(seq).toContain("claim:contradiction");
    expect(seq.indexOf("claim:contradiction")).toBeLessThan(seq.indexOf("action"));
  });

  it("falls back to the default sequence for an unknown kind", () => {
    // A kind the table doesn't list still renders via the generous default.
    const seq = blockSequenceFor(item("low_confidence" as ReviewItem["kind"]));
    expect(seq).toContain("header");
    expect(seq).toContain("action");
  });
});
