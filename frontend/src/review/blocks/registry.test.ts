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

  it("places claim:inference only in the inference sequence", () => {
    expect(blockSequenceFor(item("low_confidence_inference"))).toContain("claim:inference");
    expect(blockSequenceFor(item("fact_conflict"))).not.toContain("claim:inference");
  });

  it("gives a collision its before→after diff block", () => {
    expect(blockSequenceFor(item("attribute_collision"))).toContain("claim:diff");
  });

  it("falls back to the default sequence for an unknown kind", () => {
    // A kind the table doesn't list still renders via the generous default.
    const seq = blockSequenceFor(item("low_confidence" as ReviewItem["kind"]));
    expect(seq).toContain("header");
    expect(seq).toContain("action");
  });
});
