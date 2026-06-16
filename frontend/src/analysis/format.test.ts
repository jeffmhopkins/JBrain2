import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { FactOut } from "../api/client";
import { dedupeTokens, factSpan, factValue, fmtQuantity, fmtTemporal, valueLabel } from "./format";

// The field bug only reproduces in a negative-offset zone: UTC-midnight
// calendar dates rendered locally slip to the previous evening. Node re-reads
// TZ at format time, so pinning it here makes the regression observable
// regardless of the CI host's zone.
beforeAll(() => {
  vi.stubEnv("TZ", "America/Denver");
});
afterAll(() => {
  vi.unstubAllEnvs();
});

describe("fmtTemporal", () => {
  it("day precision renders the stored UTC calendar day — no timezone shift", () => {
    expect(fmtTemporal("1986-03-19T00:00:00Z", "day")).toBe("Mar 19, 1986");
  });

  it("month precision renders the stored UTC month — March stays March", () => {
    expect(fmtTemporal("1986-03-01T00:00:00Z", "month")).toBe("Mar 1986");
  });

  it("year and era precision render the stored UTC year", () => {
    expect(fmtTemporal("1986-01-01T00:00:00Z", "year")).toBe("1986");
    expect(fmtTemporal("1986-01-01T00:00:00Z", "era")).toBe("1986");
  });

  it("unknown precision is treated as a calendar date, not an instant", () => {
    expect(fmtTemporal("1986-03-19T00:00:00Z", "unknown")).toBe("Mar 19, 1986");
  });

  it("instant precision renders local date AND time — a real moment", () => {
    // 03:00 UTC is the prior evening in Denver (UTC-6/-7): 9:00 PM the day before.
    expect(fmtTemporal("2026-06-10T03:00:00Z", "instant")).toBe("Jun 9, 2026, 9:00 PM");
    // An appointment instant shows its clock time, not just the date.
    expect(fmtTemporal("2026-06-16T20:00:00Z", "instant")).toBe("Jun 16, 2026, 2:00 PM");
  });

  it("null renders the em-dash placeholder", () => {
    expect(fmtTemporal(null, "day")).toBe("—");
  });
});

function fact(value_json: unknown): FactOut {
  return {
    id: "f1",
    entity_id: "e1",
    entity_name: "Jeff",
    predicate: "height",
    qualifier: null,
    kind: "attribute",
    statement: "Jeff is 6'4\" tall.",
    value_json,
    assertion: "asserted",
    status: "active",
    pinned: false,
    confidence: 0.9,
    valid_from: null,
    valid_to: null,
    reported_at: "2026-06-10T23:00:00-06:00",
    temporal_precision: "unknown",
    object_entity_id: null,
    object_entity_name: null,
    source_snippet: null,
  };
}

describe("fmtQuantity / factValue imperial display", () => {
  it("normalized inch lengths ≥ 24 read as feet'inches\"", () => {
    expect(fmtQuantity(76, "in")).toBe("6'4\"");
    expect(factValue(fact({ value: 76, unit: "in" }))).toBe("6'4\"");
  });

  it("short inch values stay in inches — parts, not people", () => {
    expect(fmtQuantity(23, "in")).toBe("23 in");
  });

  it("whole feet render a zero inch part", () => {
    expect(fmtQuantity(72, "in")).toBe("6'0\"");
  });

  it("non-inch units are untouched", () => {
    expect(factValue(fact({ value: 255, unit: "lb" }))).toBe("255 lb");
    expect(factValue(fact({ value: 193, unit: "cm" }))).toBe("193 cm");
  });

  it("blood pressure and statement fallback keep their rendering", () => {
    expect(factValue(fact({ systolic: 128, diastolic: 82, unit: "mmHg" }))).toBe("128/82 mmHg");
    expect(factValue(fact(null))).toBe("Jeff is 6'4\" tall.");
  });

  it("name and place shapes render their datum, not the statement sentence", () => {
    // The bug-3 fix: a populated value_json no longer falls back to prose.
    expect(factValue(fact({ value: "Jeffrey Mark Hopkins" }))).toBe("Jeffrey Mark Hopkins");
    expect(factValue(fact({ name: "Bella", species: "dog" }))).toBe("Bella");
    expect(factValue(fact({ place: "Denver" }))).toBe("Denver");
  });

  it("name.* facts stored under the backend's other name keys render the bare name", () => {
    // entities._NAME_VALUE_KEYS also accepts fullname/alias/text; a name fact
    // under those shapes must not fall through to "Full name Celine Kitina Hopkins.".
    expect(factValue(fact({ fullname: "Celine Kitina Hopkins" }))).toBe("Celine Kitina Hopkins");
    expect(factValue(fact({ alias: "Sammy" }))).toBe("Sammy");
    expect(factValue(fact({ text: "Sammy" }))).toBe("Sammy");
  });

  it("a relationship edge renders its object entity name, never the statement", () => {
    // The 'spouse → "I have a wife Celine Hopkins."' report: the value IS the
    // linked object node, so a resolved object name wins over the prose.
    const spouse: FactOut = {
      ...fact(null),
      predicate: "spouse",
      kind: "relationship",
      statement: "I have a wife Celine Hopkins.",
      object_entity_id: "ent-celine",
      object_entity_name: "Celine Hopkins",
    };
    expect(factValue(spouse)).toBe("Celine Hopkins");
  });

  it("a date-valued fact (scheduledTime) renders the time, not the sentence", () => {
    const sched: FactOut = {
      ...fact({ start: "2026-06-16T20:00:00Z", precision: "instant" }),
      predicate: "scheduledTime",
      kind: "state",
      temporal_precision: "instant",
      statement: "Hematologist appointment is scheduled for Tuesday at 2:00 PM.",
    };
    expect(factValue(sched)).toBe("Jun 16, 2026, 2:00 PM");
    // Falls back to the fact's temporal_precision when value_json omits it.
    expect(factValue({ ...sched, value_json: { start: "2026-06-16T20:00:00Z" } })).toBe(
      "Jun 16, 2026, 2:00 PM",
    );
  });
});

describe("valueLabel (shared by factValue and the review card)", () => {
  it("renders scalar shapes the same as the entity page", () => {
    expect(valueLabel({ name: "Jeff" }, "People call me Jeff.")).toBe("Jeff");
    expect(valueLabel({ value: 95, unit: "mg/dL" }, "s")).toBe("95 mg/dL");
    expect(valueLabel("Jeff Hopkins", "s")).toBe("Jeff Hopkins");
  });

  it("falls back to the statement for shapes with no scalar datum", () => {
    expect(valueLabel({ street: "99 Pine Ave" }, "Lives at 99 Pine Ave.")).toBe(
      "Lives at 99 Pine Ave.",
    );
    expect(valueLabel(null, "Sarah works for Ridgeline.")).toBe("Sarah works for Ridgeline.");
  });

  it("dates a bare {start} value with the fallback precision", () => {
    expect(valueLabel({ start: "1986-03-19T00:00:00Z" }, "s", "day")).toBe("Mar 19, 1986");
  });
});

describe("dedupeTokens", () => {
  function tok(start: string | null, phrase: string, end: string | null = null) {
    return { resolved_start: start, resolved_end: end, surface_phrase: phrase };
  }

  it("collapses tokens that resolve to the same instant, keeping the first", () => {
    const out = dedupeTokens([
      tok("2026-06-16T20:00:00Z", "Tuesday"),
      tok("2026-06-16T20:00:00Z", "Tuesday"),
      tok("2026-06-16T20:00:00Z", "1400 on Tuesday"),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]?.surface_phrase).toBe("Tuesday");
  });

  it("keeps distinct instants and distinct unresolved phrases", () => {
    const out = dedupeTokens([
      tok("2026-06-16T20:00:00Z", "Tuesday"),
      tok("2026-06-18T20:00:00Z", "Thursday"),
      tok(null, "sometime"),
      tok(null, "later"),
    ]);
    expect(out).toHaveLength(4);
  });
});

describe("factSpan (validity span — null bounds stay vague, never '—')", () => {
  const fact = (valid_from: string | null, valid_to: string | null): FactOut =>
    ({ valid_from, valid_to, temporal_precision: "year" }) as FactOut;

  it("shows both bounds when both are known", () => {
    expect(factSpan(fact("2023-03-01T12:00:00Z", "2026-06-01T12:00:00Z"))).toBe("2023 → 2026");
  });

  it("an unknown start with a known end reads 'until <end>', not '— → <end>'", () => {
    expect(factSpan(fact(null, "2026-06-01T12:00:00Z"))).toBe("until 2026");
  });

  it("a known start that's still open reads 'since <start>'", () => {
    expect(factSpan(fact("2023-03-01T12:00:00Z", null))).toBe("since 2023");
  });

  it("a wholly undated fact has no span", () => {
    expect(factSpan(fact(null, null))).toBe("");
  });
});
