import { describe, expect, it } from "vitest";
import { MODES, ROWS, type SegState, tapSegment } from "./modes";

describe("tapSegment", () => {
  const main: SegState = { row: "main", mode: "entry" };

  it("morphs to the entry sub-types when active Entry is tapped", () => {
    expect(tapSegment(main, "entry")).toEqual({ row: "sub", mode: "entry" });
  });

  it("morphs back to the main row on a second Entry tap", () => {
    const sub = tapSegment(main, "entry");
    expect(tapSegment(sub, "entry")).toEqual({ row: "main", mode: "entry" });
  });

  it("selects a sibling mode without changing rows", () => {
    expect(tapSegment(main, "research")).toEqual({ row: "main", mode: "research" });
    const sub: SegState = { row: "sub", mode: "entry" };
    expect(tapSegment(sub, "medical")).toEqual({ row: "sub", mode: "medical" });
  });

  it("re-selects Entry from a sub-type without flipping the row", () => {
    const medical: SegState = { row: "sub", mode: "medical" };
    expect(tapSegment(medical, "entry")).toEqual({ row: "sub", mode: "entry" });
  });

  it("keeps the active mode renderable after any tap sequence", () => {
    let state: SegState = { row: "main", mode: "entry" };
    for (const tap of ["entry", "medical", "entry", "entry", "fullbrain", "entry"] as const) {
      state = tapSegment(state, tap);
      expect(ROWS[state.row]).toContain(state.mode);
    }
  });
});

describe("MODES", () => {
  it("maps capture modes to backend domain codes", () => {
    expect(MODES.entry.domain).toBe("general");
    expect(MODES.medical.domain).toBe("health");
    expect(MODES.financial.domain).toBe("finance");
    expect(MODES.research.domain).toBeNull();
    expect(MODES.fullbrain.domain).toBeNull();
  });
});
