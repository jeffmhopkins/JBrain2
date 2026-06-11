import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ENTITY_TYPE_COLOR, EntityTypeIcon, resolveEntityKind } from "./kinds";

describe("resolveEntityKind", () => {
  it("keeps the canonical schema.org kinds", () => {
    expect(resolveEntityKind("Person")).toBe("Person");
    expect(resolveEntityKind("MedicalCondition")).toBe("MedicalCondition");
  });

  it("normalizes case, snake_case, and spacing onto one key", () => {
    expect(resolveEntityKind("person")).toBe("Person");
    expect(resolveEntityKind("medical_condition")).toBe("MedicalCondition");
    expect(resolveEntityKind("Medical Procedure")).toBe("MedicalProcedure");
  });

  it("folds the synonyms the extractor tends to emit", () => {
    expect(resolveEntityKind("appointment")).toBe("Event");
    expect(resolveEntityKind("medication")).toBe("Drug");
    expect(resolveEntityKind("company")).toBe("Organization");
    expect(resolveEntityKind("dog")).toBe("Animal");
  });

  it("falls back to Thing for anything unrecognized", () => {
    expect(resolveEntityKind("widget")).toBe("Thing");
    expect(resolveEntityKind("")).toBe("Thing");
  });
});

describe("EntityTypeIcon", () => {
  it("renders the resolved type's disc with its accent token and a glyph", () => {
    const { container } = render(<EntityTypeIcon kind="drug" />);
    const disc = container.querySelector(".etype-disc");
    expect(disc).toHaveAttribute("data-entity-kind", "Drug");
    // --etype drives both the disc tint and the glyph color.
    expect(disc?.getAttribute("style")).toContain(`--etype: ${ENTITY_TYPE_COLOR.Drug}`);
    expect(disc?.querySelector("svg")).toBeInTheDocument();
  });

  it("uses the Thing glyph for unknown kinds", () => {
    const { container } = render(<EntityTypeIcon kind="mystery" />);
    expect(container.querySelector(".etype-disc")).toHaveAttribute("data-entity-kind", "Thing");
  });

  it("scales the glyph to the requested disc size", () => {
    const { container } = render(<EntityTypeIcon kind="Person" size={50} />);
    const disc = container.querySelector(".etype-disc");
    expect(disc?.getAttribute("style")).toContain("width: 50px");
    expect(disc?.querySelector("svg")).toHaveAttribute("width", "28"); // round(50 * 0.56)
  });
});
