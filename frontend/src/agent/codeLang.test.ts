import { describe, expect, it } from "vitest";
import {
  MAX_HIGHLIGHT_CHARS,
  MAX_HIGHLIGHT_LINES,
  fenceLang,
  splitGutter,
  tokenFor,
  withinHighlightCaps,
} from "./codeLang";

describe("fenceLang", () => {
  it("reads the curated languages and their aliases", () => {
    expect(fenceLang("```python")).toBe("python");
    expect(fenceLang("```py")).toBe("python");
    expect(fenceLang("```ts")).toBe("typescript");
    expect(fenceLang("```tsx")).toBe("typescript");
    expect(fenceLang("```js")).toBe("javascript");
    expect(fenceLang("```sh")).toBe("bash");
    expect(fenceLang("```shell")).toBe("bash");
    expect(fenceLang("```console")).toBe("shell");
    expect(fenceLang("```yml")).toBe("yaml");
    expect(fenceLang("```c++")).toBe("cpp");
    expect(fenceLang("```html")).toBe("xml");
    expect(fenceLang("```toml")).toBe("ini");
    expect(fenceLang("```Dockerfile")).toBe("dockerfile");
    expect(fenceLang("```postgresql")).toBe("sql");
  });

  it("takes the first word of the info string, in any spelling a writer uses", () => {
    expect(fenceLang('```python title="advance.py"')).toBe("python");
    expect(fenceLang("  ```  Rust")).toBe("rust");
    expect(fenceLang("```{.go}")).toBe("go");
  });

  it("is null untagged and explicitly plain for a language it does not colour", () => {
    expect(fenceLang("```")).toBeNull();
    expect(fenceLang("```   ")).toBeNull();
    // A tag the author chose is respected: never auto-detected into a near-miss grammar.
    expect(fenceLang("```haskell")).toBe("text");
    expect(fenceLang("```text")).toBe("text");
  });
});

describe("withinHighlightCaps", () => {
  it("bounds the work by characters and by lines", () => {
    expect(withinHighlightCaps("x = 1")).toBe(true);
    expect(withinHighlightCaps("x".repeat(MAX_HIGHLIGHT_CHARS + 1))).toBe(false);
    expect(withinHighlightCaps(Array(MAX_HIGHLIGHT_LINES).fill("x").join("\n"))).toBe(true);
    expect(
      withinHighlightCaps(
        Array(MAX_HIGHLIGHT_LINES + 1)
          .fill("x")
          .join("\n"),
      ),
    ).toBe(false);
  });
});

describe("splitGutter", () => {
  it("splits the GitHub reader's blob view into numbers and code", () => {
    const g = splitGutter("  9  import os\n 10  \n 11  def f():\n 12      return 1");
    expect(g?.numbers).toEqual(["9", "10", "11", "12"]);
    // Two spaces of separator go; the code's own indentation stays.
    expect(g?.code).toBe("import os\n\ndef f():\n    return 1");
  });

  it("reads a quoted range with tabs, a trimmed blank line and an elision", () => {
    const g = splitGutter("161\tdef advance_floor(x):\n162\n163\t    return x\n…\n170\tdone()");
    expect(g?.numbers).toEqual(["161", "162", "163", "", "170"]);
    expect(g?.code).toBe("def advance_floor(x):\n\n    return x\n…\ndone()");
  });

  it("does not misfire on numbered data", () => {
    // One space: a list, not a listing.
    expect(splitGutter("1 apple\n2 pear\n3 plum")).toBeNull();
    // Not consecutive: a table of years.
    expect(splitGutter("1990  12.5\n2000  15.1\n2010  20.3")).toBeNull();
    // Going backwards.
    expect(splitGutter("3  c\n2  b\n1  a")).toBeNull();
    // A single line, and a block mostly without numbers.
    expect(splitGutter("42  answer")).toBeNull();
    expect(splitGutter("1  a\n2  b\nplain\nmore plain\nstill plain")).toBeNull();
    // Ordinary code.
    expect(splitGutter("def f():\n    return 1")).toBeNull();
  });
});

describe("tokenFor", () => {
  it("maps highlight.js scopes onto the closed token set", () => {
    expect(tokenFor(["hljs-keyword"])).toBe("k");
    expect(tokenFor(["hljs-string"])).toBe("s");
    expect(tokenFor(["hljs-number"])).toBe("n");
    expect(tokenFor(["hljs-title", "function_"])).toBe("f");
    expect(tokenFor(["hljs-comment"])).toBe("c");
    expect(tokenFor(["hljs-title", "class_", "inherited__"])).toBe("t");
    expect(tokenFor(["hljs-built_in"])).toBe("t");
    expect(tokenFor(["hljs-meta"])).toBe("m");
    expect(tokenFor(["hljs-variable", "language_"])).toBe("k");
  });

  it("leaves an unmapped scope to inherit its parent's colour", () => {
    expect(tokenFor(["hljs-params"])).toBeNull();
    expect(tokenFor(["hljs-subst"])).toBeNull();
    expect(tokenFor(["not-hljs"])).toBeNull();
  });
});
