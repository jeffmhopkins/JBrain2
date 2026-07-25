import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { highlightSource } from "./ClaimContradiction";

function marked(text: string, names: string[]): string[] {
  const { container } = render(
    <div>
      {highlightSource(
        text,
        names.map((name) => ({ name, cls: "steel" })),
      )}
    </div>,
  );
  return Array.from(container.querySelectorAll("mark.rc-hl")).map((m) => m.textContent ?? "");
}

describe("highlightSource", () => {
  it("does not highlight a short name inside other words", () => {
    // The self-entity "Me" must not tint "ca<me>ra", "geo<me>tric", "na<me>d".
    expect(marked("A camera and a geometric shirt; I have a wife named Celine.", ["Me"])).toEqual(
      [],
    );
  });

  it("highlights the name as a standalone token", () => {
    expect(marked("Me and my dog.", ["Me"])).toEqual(["Me"]);
  });

  it("matches case-insensitively and preserves source casing", () => {
    expect(marked("ME owns Nemo.", ["Me"])).toEqual(["ME"]);
  });

  it("still matches names flanked by punctuation, not just whitespace", () => {
    expect(marked("treated by Dr. Croft.", ["Dr. Croft"])).toEqual(["Dr. Croft"]);
  });

  it("prefers the longest matching name when names overlap", () => {
    expect(marked("Jeff Hopkins owns his ring.", ["Jeff", "Jeff Hopkins"])).toEqual([
      "Jeff Hopkins",
    ]);
  });
});
