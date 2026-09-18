import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Read off disk rather than `import "./styles.css?raw"`: app code imports the same file
// as a stylesheet, and under vitest (which stubs CSS modules) the ?raw variant came back
// empty in a full run — which made the assertions below pass vacuously. vitest's working
// directory is the frontend package root.
const CSS = readFileSync("src/styles.css", "utf8");

// The text-size setting (Settings → Text size, --font-scale) is only honest if the
// surfaces it governs are sized in terms of it. The chat surface and the omnibox were
// both authored in flat px, so at the owner's 75% the type around them shrank and the
// turn — its bubble, and the Thinking/Worked strip at its foot — did not; the same
// fixed 18px glyph held the omnibox tab row open. This guard reads the stylesheet and
// fails if a size-defining declaration in those scopes goes back to a bare px.
//
// It checks the source, not layout: jsdom computes no geometry, so a rendered
// assertion here would pass on anything.

/** Properties that decide how large a thing reads. Borders, radii and shadows are
 *  absolutes — a hairline stays a hairline — so they are deliberately absent. */
const SIZE_PROPS = new Set([
  "font",
  "font-size",
  "padding",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "margin",
  "margin-top",
  "margin-right",
  "margin-bottom",
  "margin-left",
  "gap",
  "row-gap",
  "column-gap",
  "width",
  "height",
  "min-width",
  "min-height",
  "max-width",
  "max-height",
  "flex-basis",
]);

/** The scopes this guard covers: the agent turn and the omnibox that composes it. */
const SCOPED =
  /(\.fb-shell|\.bubble|\.omnibox|\.seg-row|\.seg-ic|\.dest-|\.composer-input|\.composer-foot|\.staged-files|\.ctx-|\.model-pill|\.foot-icons|\.edit-banner|\.edit-cancel|\.conv-empty)/;

/** Carve-outs, each for a reason that outlives this test:
 *  - tap targets and the visually-hidden clip box are absolutes, not type-relative;
 *  - the map / weather / hurricane / chart tool views draw into canvases and SVG
 *    viewBoxes and lay out on hand-tuned pixel grids (`grid-template-columns:
 *    44px 24px 42px 1fr`), so their cells cannot scale while their tracks do not;
 *  - `.onbox-*`, `.molt-*`, `.location-screen` and the settings segment rows reuse
 *    `.seg`/`.seg-row` on other screens that this pass did not cover. */
const EXEMPT =
  /(tv-wx|tv-hu|tv-cc|tv-bar|tv-plot|loc-map|loc-pc|session-tap|chat-search|fb-lightbox-close|fb-sr-only|onbox|molt-|llm-member-seg|model-effort-row|seg-count|location-screen)/;

/** A hairline and the 44px tap-target floor stay put at every scale. */
const ABSOLUTE = new Set(["1px", "44px"]);

/** Spacing whose job is to CLEAR an absolute tap target, keyed `selector|property`.
 *  It is tap-target geometry wearing a spacing property, so it is absolute for the same
 *  reason the 44px is: `.foot-icons`'s 24px is twice `.icon-btn`'s 8px bleed plus 8px of
 *  dead space between two 44px hit areas, and when this guard pushed it through the scale
 *  sweep the dead space is what paid — 2px at the shipped 75% default, and at 65% the
 *  paperclip's and send's hit areas OVERLAPPED by 0.4px, which is a near-miss on attach
 *  sending the note. Scaling one side of an arithmetic whose other side is absolute is
 *  not coverage; it is a regression this guard asked for. */
const TAP_SPACING = new Set([".foot-icons|gap"]);

const PX = /(?<![\w-])\d*\.?\d+px/g;

/** Drop `calc(…)` groups that carry the scale factor — the px inside them is the
 *  design value being scaled, not a hardcoded one. Brace-counted, so nested calls
 *  and `calc((14px + 38px) * var(--font-scale))` come out whole. */
function stripScaled(value: string): string {
  let out = value;
  for (;;) {
    let from = 0;
    let cut = false;
    for (;;) {
      const start = out.indexOf("calc(", from);
      if (start < 0) break;
      let depth = 1;
      let i = start + "calc(".length;
      for (; i < out.length && depth > 0; i++) {
        if (out[i] === "(") depth++;
        else if (out[i] === ")") depth--;
      }
      if (out.slice(start, i).includes("var(--font-scale)")) {
        out = out.slice(0, start) + out.slice(i);
        cut = true;
        break;
      }
      from = start + "calc(".length;
    }
    if (!cut) return out;
  }
}

/** Top-level rules only: at-rule bodies (@media, @keyframes, @supports) carry
 *  breakpoints and animation geometry, which the setting must not move. */
function topLevelRules(css: string): { selector: string; body: string }[] {
  let flat = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (;;) {
    const at = flat.indexOf("@");
    if (at < 0) break;
    const brace = flat.indexOf("{", at);
    if (brace < 0) break;
    let depth = 1;
    let i = brace + 1;
    for (; i < flat.length && depth > 0; i++) {
      if (flat[i] === "{") depth++;
      else if (flat[i] === "}") depth--;
    }
    flat = flat.slice(0, at) + flat.slice(i);
  }
  const rules: { selector: string; body: string }[] = [];
  for (const m of flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    rules.push({ selector: (m[1] ?? "").trim().replace(/\s+/g, " "), body: m[2] ?? "" });
  }
  return rules;
}

function unscaledDeclarations(): string[] {
  const found: string[] = [];
  for (const { selector, body } of topLevelRules(CSS)) {
    if (!SCOPED.test(selector) || EXEMPT.test(selector)) continue;
    for (const decl of body.split(";")) {
      const at = decl.indexOf(":");
      if (at < 0) continue;
      const prop = decl.slice(0, at).trim();
      const value = decl.slice(at + 1).trim();
      if (!SIZE_PROPS.has(prop)) continue;
      if (TAP_SPACING.has(`${selector}|${prop}`)) continue;
      const bare = stripScaled(value).match(PX) ?? [];
      if (bare.some((px) => !ABSOLUTE.has(px))) found.push(`${selector} { ${prop}: ${value} }`);
    }
  }
  return found;
}

/** Every tap-target floor in the sheet, whatever property carries it. */
function scaledTapTargets(): string[] {
  const found: string[] = [];
  for (const { selector, body } of topLevelRules(CSS)) {
    for (const decl of body.split(";")) {
      const at = decl.indexOf(":");
      if (at < 0) continue;
      const prop = decl.slice(0, at).trim();
      const value = decl.slice(at + 1).trim();
      if (!/^(min-)?(width|height)$/.test(prop)) continue;
      if (/44px\s*\*\s*var\(--font-scale\)/.test(value))
        found.push(`${selector} { ${prop}: ${value} }`);
    }
  }
  return found;
}

describe("tap targets", () => {
  // The first font-scale sweep excluded `min-height: 44px` but not `min-width`, and
  // shipped two 44px targets scaled to 28.6px wide at 65%. A thumb target is an
  // absolute; it never multiplies by the text-size setting, on any property.
  it("never scales a 44px floor by --font-scale", () => {
    expect(scaledTapTargets()).toEqual([]);
  });

  // The foot's play/copy buttons paint neither dimension of their target any more: the
  // height floor went so the strip could follow its type, the width floor went so the two
  // glyphs could pair at the row's end. Both live on the overlay now, so if the overlay
  // loses its height or stops reaching outward the controls silently shrink to their
  // glyphs (~13px) with nothing else failing. Measured through elementFromPoint when this
  // landed: play 38x38 and copy 28x38 at 65%, 59x44 and 41x44 at 100%.
  it("carries the foot buttons' tap area on the overlay, since the box no longer does", () => {
    const rules = topLevelRules(CSS);
    const base = rules.find(
      (r) => /fb-act-play::after/.test(r.selector) && /fb-act-copy::after/.test(r.selector),
    );
    expect(base?.body, "foot buttons lost their 44px-tall hit-area overlay").toMatch(
      /height:\s*44px/,
    );
    for (const sel of [".fb-shell .fb-act-play::after", ".fb-shell .fb-act-copy::after"]) {
      const rule = rules.find((r) => r.selector === sel);
      expect(rule, `${sel} must widen the target outward`).toBeDefined();
      // A negative left/right is the target reaching into the empty space beside the pair.
      expect(rule?.body, `${sel} must reach outward, not just wrap the glyph`).toMatch(
        /(left|right):\s*calc\(-\d/,
      );
    }
  });
});

/** The design px inside `calc(<n>px * var(--font-scale))`, in source order. */
function scaledPx(body: string, prop: string): number[] {
  const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:([^;]*)`).exec(body);
  if (!m?.[1]) return [];
  return [...m[1].matchAll(/calc\((-?\d*\.?\d+)px \* var\(--font-scale\)\)/g)].map((x) =>
    Number(x[1]),
  );
}

describe("status line", () => {
  // The live status line sits BETWEEN the last turn and the omnibox, and nothing centres
  // it — its own padding has to make up the difference between the chat's floor above and
  // the dock's gutter below. Those three rules are in three different parts of the sheet,
  // so a change to any one silently tips it (the dock's gutter not scaling once left it
  // 6.5px above against 13.2px below at 65%). The arithmetic is the invariant.
  it("sits equally between the last turn and the omnibox, at every text size", () => {
    const rules = topLevelRules(CSS);
    const body = (sel: string) => rules.find((r) => r.selector === sel)?.body ?? "";
    const chat = scaledPx(body(".fb-shell .fb-chat"), "padding");
    const status = scaledPx(body(".fb-shell .fb-status"), "padding");
    const dock = scaledPx(body(".dock"), "padding");
    // padding shorthands: [top, sides, bottom]
    expect(chat, ".fb-chat padding must be three scaled values").toHaveLength(3);
    expect(status, ".fb-status padding must be three scaled values").toHaveLength(3);
    expect(dock.length, ".dock padding must scale").toBeGreaterThanOrEqual(2);
    const above = (chat[2] ?? 0) + (status[0] ?? 0);
    const below = (status[2] ?? 0) + (dock[0] ?? 0);
    expect(below, `above=${above} below=${below} — status line is off-centre`).toBe(above);
  });
});

describe("font-scale coverage", () => {
  it("actually read the stylesheet", () => {
    // Without this the checks below are vacuously true on an empty read.
    expect(CSS).toContain(".fb-shell .fb-act-chip");
    expect(topLevelRules(CSS).length).toBeGreaterThan(1000);
  });

  it("sizes the agent turn and the omnibox in terms of --font-scale", () => {
    expect(unscaledDeclarations()).toEqual([]);
  });

  it("scopes the check to rules that exist", () => {
    // A typo in SCOPED would make the assertion above vacuous.
    const scoped = topLevelRules(CSS).filter(
      (r) => SCOPED.test(r.selector) && !EXEMPT.test(r.selector),
    );
    expect(scoped.length).toBeGreaterThan(300);
  });

  it("recognises a scaled value and rejects a bare one", () => {
    expect(stripScaled("calc(11px * var(--font-scale))").match(PX)).toBeNull();
    expect(stripScaled("calc(2 * 1.4em + (14px + 38px) * var(--font-scale))").match(PX)).toBeNull();
    expect(stripScaled("11px").match(PX)).toEqual(["11px"]);
    // A calc without the scale factor is still hardcoded.
    expect(stripScaled("calc(100% - 22px)").match(PX)).toEqual(["22px"]);
  });
});
