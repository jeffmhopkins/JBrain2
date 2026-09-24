/** EVERY CUSTOM PROPERTY A STYLESHEET USES HAS TO EXIST SOMEWHERE.
 *
 * `var(--nope)` is not an error. It resolves to nothing, and what happens next depends on the
 * property: `color: var(--nope)` inherits, so the text renders in whatever the parent was, and
 * `background: color-mix(in srgb, var(--nope) 78%, transparent)` is invalid at computed-value
 * time and the declaration is dropped whole. Both are silent. Neither shows up in a build, a
 * typecheck, a lint or a jsdom test, because jsdom computes no cascade.
 *
 * This gate exists because `--text-dim` was referenced SEVENTEEN TIMES across two stylesheets
 * and defined nowhere — in the Panels tab of jpanel, and in the Ops fleet card. Every line
 * written to recede (a version string, a timestamp, a hint under a field) rendered at full
 * `--text` instead, which is most of why the Panels tab read as flat and unfinished next to the
 * tabs either side of it. Nobody could see the bug by reading either file: the name looks
 * exactly like the tokens beside it.
 *
 * Writing the gate turned up five more, all of the same shape and none of them noticed:
 * `--fs-sm` (three plot labels, which silently inherited their size), `--mono` (monospace that
 * was not), `--border-2`, `--surface-1` (a waveform label whose background dropped entirely,
 * leaving white-on-waveform) and a dead `--teal-tint` fallback.
 *
 * What counts as existing: declared in any stylesheet, or set at runtime from TS. A `var()`
 * with a fallback is fine by construction — the fallback is the definition.
 */

import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

/** Every file under `src/` with one of these extensions. `node:fs`'s own `globSync` is newer
 *  than the @types/node this package pins, and a stylesheet list is not worth a dependency. */
function under(dir: string, exts: string[]): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) out.push(...under(path, exts));
    else if (exts.some((e) => entry.name.endsWith(e))) out.push(path);
  }
  return out.sort();
}

// Read off disk rather than importing: app code imports these as stylesheets, and under vitest
// (which stubs CSS modules) a `?raw` import came back EMPTY in a full run — which is how
// `fontScaleCoverage.test.ts` nearly shipped a guard that passed vacuously. vitest's working
// directory is the frontend package root.
const SHEETS = under("src", [".css"]);
const SOURCES = under("src", [".ts", ".tsx"]);

/** `--name:` at the head of a declaration, in any stylesheet. */
const DECLARED = /(?<![\w-])(--[\w-]+)\s*:/g;

/** `var(--name` and, when it is there, the comma that opens a fallback. */
const REFERENCED = /var\(\s*(--[\w-]+)\s*(,)?/g;

/** Set from TS onto an element or the document root — `setProperty("--font-scale", …)` and the
 *  inline-style form `{ "--etype": … }`. Those are real definitions; they just do not live in
 *  a stylesheet, and a gate that could not see them would push authors to declare a dead
 *  placeholder in CSS purely to satisfy it. */
function runtimeDefined(): Set<string> {
  const out = new Set<string>();
  for (const path of SOURCES) {
    if (path.endsWith(".test.ts") || path.endsWith(".test.tsx")) continue;
    const src = readFileSync(path, "utf8");
    for (const m of src.matchAll(/setProperty\(\s*"(--[\w-]+)"/g)) out.add(m[1] as string);
    for (const m of src.matchAll(/"(--[\w-]+)"\s*:/g)) out.add(m[1] as string);
  }
  return out;
}

describe("the design tokens a stylesheet reaches for", () => {
  it("finds the stylesheets at all", () => {
    // The whole gate is a loop over this list, so an empty one passes everything. That is the
    // failure mode of the `?raw` import this file was written to avoid, and it belongs in an
    // assertion rather than in a comment.
    expect(SHEETS.length).toBeGreaterThan(5);
    expect(SHEETS).toContain("src/styles/tokens.css");
    expect(SHEETS).toContain("src/screens/jpanel.css");
  });

  it("defines every one of them, or gives it a fallback", () => {
    const declared = new Set<string>();
    for (const path of SHEETS) {
      for (const m of readFileSync(path, "utf8").matchAll(DECLARED)) declared.add(m[1] as string);
    }
    const known = new Set([...declared, ...runtimeDefined()]);

    const missing: string[] = [];
    for (const path of SHEETS) {
      const src = readFileSync(path, "utf8");
      for (const m of src.matchAll(REFERENCED)) {
        const name = m[1] as string;
        // A fallback IS the definition: `var(--maybe, 12px)` always computes to something.
        if (m[2] !== undefined) continue;
        if (known.has(name)) continue;
        const line = src.slice(0, m.index).split("\n").length;
        missing.push(`${path}:${line} var(${name})`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("knows what it would have caught", () => {
    /* The gate is a negation, so it passes on an empty repo and on a broken regex alike. This
       runs it over the bug it was written for, spelled as it actually shipped. */
    const declared = new Set(["--text", "--text-2"]);
    const hit = [...":root { color: var(--text-dim); }".matchAll(REFERENCED)].filter(
      (m) => m[2] === undefined && !declared.has(m[1] as string),
    );
    expect(hit).toHaveLength(1);
    // And it does NOT fire on the two shapes that are fine.
    const ok = [..."a { color: var(--text-2); border: var(--nope, 1px); }".matchAll(REFERENCED)];
    expect(ok.filter((m) => m[2] === undefined && !declared.has(m[1] as string))).toEqual([]);
  });
});

describe("the jpanel stylesheet", () => {
  const CSS = readFileSync("src/screens/jpanel.css", "utf8");

  /** Selectors that head a TOP-LEVEL rule, one entry per name in a comma list.
   *
   * Depth matters, which is why this walks braces rather than matching lines. A selector
   * restated inside `@media (prefers-reduced-motion: reduce)` is an override — it is supposed
   * to repeat the name it overrides, and `.jp-mic-live` legitimately does. Two rules for one
   * selector at the SAME depth is the collision. Comments are stripped first: a brace inside
   * one would throw the count off for everything after it. */
  function selectors(): string[] {
    const src = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const out: string[] = [];
    let depth = 0;
    let head = "";
    for (const ch of src) {
      if (ch === "{") {
        if (depth === 0 && !head.trim().startsWith("@")) {
          for (const part of head.split(",")) {
            const name = part.trim();
            if (name) out.push(name);
          }
        }
        depth += 1;
        head = "";
      } else if (ch === "}") {
        depth = Math.max(0, depth - 1);
        head = "";
      } else if (depth === 0) {
        head += ch;
      }
    }
    return out;
  }

  it("declares the thread card and the unit card under different names", () => {
    /* THE BUG: `.jp-panel`, `.jp-panel-head` and `.jp-panel-name` were each declared TWICE —
       once for the message-thread cards on the Messages tab, and again, 280 lines later, for
       the unit cards on the Panels tab. Later wins, so for as long as both existed the Panels
       tab was quietly restyling the Messages tab: thread cards came out on `--surface-2` at
       12px radius under a 600-weight heading that had lost `--fs-title`. Nothing on Messages
       asked for that and nothing there could have explained it.

       Two components sharing one set of names is the defect, so the gate is that they do not —
       not that the duplicates happen to have been merged. `.jp-msgs` is exempt and says why in
       the sheet: one block sets the list, a second caps its height, deliberately and with the
       reasoning attached. */
    const seen = new Map<string, number>();
    for (const s of selectors()) seen.set(s, (seen.get(s) ?? 0) + 1);
    const twice = [...seen].filter(([s, n]) => n > 1 && s !== ".jp-msgs").map(([s]) => s);
    expect(twice).toEqual([]);
  });

  it("gives the unit actions a 44px box", () => {
    /* `docs/reference/DESIGN.md` is binding: touch targets ≥ 44×44px. This row held borderless
       text at 0.82rem — about 10px of glyph with no box at the shipped scale — and one of the
       three is Revoke, which cannot be undone. */
    const rule = CSS.slice(CSS.indexOf(".jp-unit-actions button {"));
    expect(rule.slice(0, rule.indexOf("}"))).toMatch(/min-height:\s*44px/);
  });

  it("marks the destructive action as destructive", () => {
    /* Revoke was styled exactly like Rename and Its pet beside it, so the one irreversible
       control on the screen was the least distinguishable thing on it. */
    const rule = CSS.slice(CSS.indexOf(".jp-unit-actions button.danger {"));
    expect(rule.slice(0, rule.indexOf("}"))).toMatch(/color:\s*var\(--danger\)/);
  });

  it("sizes its type from the scale, so Settings → Text size reaches this tab", () => {
    /* Every font-size in the Panels half was a bare rem — 0.72, 0.74, 0.76, 0.78, 0.82, 0.88 —
       so the one tab the owner reads standing in front of a panel ignored the text-size setting
       entirely, and did so at sizes below every token in the scale. */
    const bare = [...CSS.matchAll(/font-size:\s*([\d.]+(?:rem|px))/g)].map((m) => m[1] as string);
    expect(bare).toEqual([]);
  });
});
