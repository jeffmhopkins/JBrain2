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

  // The foot's play/copy buttons give up `min-height` so the strip can follow its type
  // — the 44px has to survive somewhere, or the control silently shrinks to its glyph.
  it("keeps the foot buttons' 44px reachable after they stop painting it", () => {
    const rules = topLevelRules(CSS);
    const overlay = rules.find(
      (r) => /fb-act-(play|copy)::after/.test(r.selector) && /height:\s*44px/.test(r.body),
    );
    expect(overlay, "foot buttons lost their 44px hit-area overlay").toBeDefined();
    for (const sel of [".fb-shell .fb-act-play", ".fb-shell .fb-act-copy"]) {
      const rule = rules.find((r) => r.selector === sel && r.body.includes("min-width"));
      expect(rule?.body, `${sel} must keep an absolute 44px width`).toMatch(/min-width:\s*44px/);
    }
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
