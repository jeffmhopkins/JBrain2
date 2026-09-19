import { describe, expect, it } from "vitest";
import { PANEL_H, PANEL_W } from "./draw";
import {
  CARD_MM_W,
  NOMINAL_CARD_PX,
  PANEL_MM_H,
  PANEL_MM_W,
  clampCardPx,
  cssPpi,
  pxPerMm,
  stageSize,
} from "./scale";

describe("panel geometry", () => {
  it("matches the datasheet: 1.8 inch diagonal at 368x448", () => {
    const diagMm = Math.hypot(PANEL_MM_W, PANEL_MM_H);
    expect(diagMm / 25.4).toBeCloseTo(1.8, 2);
  });

  it("is 322 ppi, which is why type has a floor", () => {
    const ppi = PANEL_W / (PANEL_MM_W / 25.4);
    expect(Math.round(ppi)).toBe(322);
  });
});

describe("pxPerMm / cssPpi", () => {
  it("CSS's own nominal calibration reads back as 96 ppi", () => {
    expect(Math.round(cssPpi(NOMINAL_CARD_PX))).toBe(96);
  });

  it("a card measured wider means a denser screen", () => {
    expect(cssPpi(NOMINAL_CARD_PX * 1.6)).toBeGreaterThan(cssPpi(NOMINAL_CARD_PX));
  });

  it("one card width is one card width", () => {
    expect(pxPerMm(NOMINAL_CARD_PX) * CARD_MM_W).toBeCloseTo(NOMINAL_CARD_PX, 6);
  });
});

describe("clampCardPx", () => {
  it("falls back to the nominal for a value that is not a number", () => {
    expect(clampCardPx(Number.NaN)).toBe(NOMINAL_CARD_PX);
  });

  it("clamps rather than accepting an absurd measurement", () => {
    expect(clampCardPx(5)).toBeGreaterThan(5);
    expect(clampCardPx(100_000)).toBeLessThan(100_000);
  });
});

describe("stageSize", () => {
  const base = { panelW: PANEL_W, panelH: PANEL_H };

  it("fit is the panel's own pixel count, for working on", () => {
    expect(stageSize("fit", { ...base, dpr: 3, cardPx: 400 })).toEqual({ w: 368, h: 448 });
  });

  it("pixels divides by devicePixelRatio — the shipped bug was not doing this", () => {
    expect(stageSize("pixels", { ...base, dpr: 3, cardPx: null })).toEqual({
      w: 368 / 3,
      h: 448 / 3,
    });
  });

  it("pixels survives a nonsense devicePixelRatio rather than dividing by zero", () => {
    expect(stageSize("pixels", { ...base, dpr: 0, cardPx: null })).toEqual({ w: 368, h: 448 });
  });

  it("actual renders the real millimetres at the measured density", () => {
    const cardPx = 500; // a dense phone: ~148 CSS ppi
    const got = stageSize("actual", { ...base, dpr: 3, cardPx });
    expect(got.w).toBeCloseTo((PANEL_MM_W * cardPx) / CARD_MM_W, 6);
    expect(got.h).toBeCloseTo((PANEL_MM_H * cardPx) / CARD_MM_W, 6);
  });

  it("actual keeps the panel's aspect ratio", () => {
    const got = stageSize("actual", { ...base, dpr: 2, cardPx: 460 });
    expect(got.w / got.h).toBeCloseTo(PANEL_W / PANEL_H, 3);
  });

  it("a denser screen draws the same millimetres in more pixels", () => {
    const sparse = stageSize("actual", { ...base, dpr: 1, cardPx: 323 });
    const dense = stageSize("actual", { ...base, dpr: 3, cardPx: 520 });
    expect(dense.w).toBeGreaterThan(sparse.w);
  });

  it("actual without a calibration does NOT guess — it falls back to the working size", () => {
    // Guessing would silently reinstate the CSS-mm lie this module exists to remove.
    expect(stageSize("actual", { ...base, dpr: 3, cardPx: null })).toEqual({ w: 368, h: 448 });
  });

  it("the shipped CSS rule under-drew the panel by a third on a phone", () => {
    // `width: 29.02mm` resolves through CSS's fixed 96px/in anchor whatever the screen is, so
    // on a ~153 CSS-ppi phone it lays down 96 px where 153 fit: the panel came out at ~96/153.
    const cssMmWidth = (PANEL_MM_W * 96) / 25.4;
    const honest = stageSize("actual", { ...base, dpr: 3, cardPx: 520 }).w;
    expect(cssMmWidth / honest).toBeCloseTo(96 / cssPpi(520), 2);
    expect(cssMmWidth / honest).toBeLessThan(0.7);
  });
});
