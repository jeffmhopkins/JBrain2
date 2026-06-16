import { describe, expect, it } from "vitest";
import type { ReviewItem } from "../api/client";
import { OTHER_GROUP_KEY, groupByEntity, reviewSubject } from "./grouping";

function item(id: string, kind: string, payload: Record<string, unknown>): ReviewItem {
  return {
    id,
    kind: kind as ReviewItem["kind"],
    domain: "general",
    created_at: "2026-06-10T09:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload,
  };
}

describe("reviewSubject", () => {
  it("reads entity_name and entity_kind from a confirm_entity card", () => {
    const s = reviewSubject(
      item("1", "confirm_entity", { entity_name: "Zane", entity_kind: "Person" }),
    );
    expect(s).toEqual({ label: "Zane", kind: "Person" });
  });

  it("reads subject from a new_predicate card", () => {
    expect(reviewSubject(item("2", "new_predicate", { subject: "Jeff" }))?.label).toBe("Jeff");
  });

  it("reads name from an ambiguous_mention card", () => {
    expect(reviewSubject(item("3", "ambiguous_mention", { name: "Sam" }))?.label).toBe("Sam");
  });

  it("title-cases an entity_ref slug from an inference card", () => {
    expect(
      reviewSubject(item("4", "low_confidence_inference", { entity_ref: "celine_dubois" })),
    ).toEqual({ label: "Celine Dubois", kind: "Thing" });
    expect(reviewSubject(item("5", "low_confidence_inference", { entity_ref: "me" }))?.label).toBe(
      "Me",
    );
  });

  it("reads the entity_ref a fact_conflict / attribute_collision carries", () => {
    expect(
      reviewSubject(item("6", "fact_conflict", { entity_ref: "Me", predicate: "worksFor" }))?.label,
    ).toBe("Me");
    expect(
      reviewSubject(
        item("7", "attribute_collision", { entity_ref: "sarah", predicate: "birthDate" }),
      )?.label,
    ).toBe("Sarah");
  });

  it("returns null when the payload names no single subject", () => {
    expect(reviewSubject(item("8", "merge_proposal", { entity_a: "x", entity_b: "y" }))).toBeNull();
    // A collision missing its entity_ref still falls through to Other.
    expect(reviewSubject(item("9", "attribute_collision", { predicate: "birthDate" }))).toBeNull();
  });
});

describe("groupByEntity", () => {
  it("folds items under their subject and sinks subjectless items into Other", () => {
    const groups = groupByEntity([
      item("a", "low_confidence_inference", { entity_ref: "celine" }),
      item("b", "merge_proposal", {}),
      item("c", "new_predicate", { subject: "Celine" }),
      item("d", "ambiguous_mention", { name: "Marcus" }),
    ]);

    // Celine wins on size (2 items), Marcus next, Other last regardless of size.
    expect(groups.map((g) => g.label)).toEqual(["Celine", "Marcus", "Other"]);
    expect(groups[0]?.items.map((i) => i.id)).toEqual(["a", "c"]);
    expect(groups.at(-1)?.key).toBe(OTHER_GROUP_KEY);
  });

  it("matches a slug ref and a display name onto the same group, case-insensitively", () => {
    const groups = groupByEntity([
      item("a", "low_confidence_inference", { entity_ref: "celine" }),
      item("b", "new_predicate", { subject: "Celine" }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.items).toHaveLength(2);
  });

  it("upgrades a group's type from Thing when a later item knows it", () => {
    const groups = groupByEntity([
      item("a", "low_confidence_inference", { entity_ref: "zane" }),
      item("b", "confirm_entity", { entity_name: "Zane", entity_kind: "Person" }),
    ]);
    expect(groups[0]?.kind).toBe("Person");
  });

  it("sorts equal-sized groups alphabetically", () => {
    const groups = groupByEntity([
      item("a", "new_predicate", { subject: "Marcus" }),
      item("b", "new_predicate", { subject: "Celine" }),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["Celine", "Marcus"]);
  });
});
