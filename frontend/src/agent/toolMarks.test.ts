import { describe, expect, it } from "vitest";
import { toolMarks } from "./toolMarks";
import type { ToolActivity } from "./transcript";

function tool(id: string, name: string, textOffset?: number, result?: string): ToolActivity {
  return {
    id,
    name,
    ok: true,
    ...(textOffset === undefined ? {} : { textOffset }),
    ...(result ? { result } : {}),
  };
}

describe("toolMarks", () => {
  it("marks each point the model stopped to use a tool, by its label", () => {
    const marks = toolMarks([tool("a", "web_fetch", 12), tool("b", "clock", 30)], 50);
    expect(marks.map((m) => [m.offset, m.label, m.ids])).toEqual([
      [12, "Read a web page", ["a"]],
      [30, "clock", ["b"]],
    ]);
  });

  it("gives no mark at the start, at the end, or without an offset", () => {
    const marks = toolMarks(
      [tool("a", "web_search", 0), tool("b", "web_search", 40), tool("c", "web_search")],
      40,
    );
    expect(marks).toEqual([]);
    // Past the revealed text (the paced reveal is behind the stream): not yet.
    expect(toolMarks([tool("d", "web_search", 45)], 40)).toEqual([]);
  });

  it("groups consecutive tools at one offset: ×N for the same tool, a count otherwise", () => {
    const marks = toolMarks(
      [
        tool("a", "web_search", 10),
        tool("b", "web_search", 10),
        tool("c", "web_fetch", 20),
        tool("d", "web_search", 20),
        tool("e", "clock", 20),
      ],
      60,
    );
    expect(marks.map((m) => [m.label, m.ids, m.name, m.key])).toEqual([
      ["Searched the web ×2", ["a", "b"], "web_search", "a"],
      ["3 tools used", ["c", "d", "e"], undefined, "c"],
    ]);
  });

  it("adds a browse step's verdict from its result brief", () => {
    const [ok] = toolMarks([tool("a", "browse", 5, "verified · 6 steps")], 20);
    expect(ok?.label).toBe("Browsed a site");
    expect(ok?.brief).toEqual({ text: "verified", ok: true });
    const [bad] = toolMarks([tool("b", "browse", 5, "unverified · 3 steps")], 20);
    expect(bad?.brief).toEqual({ text: "unverified", ok: false });
    // No brief yet (in flight), or not a browse step: just the label.
    expect(toolMarks([tool("c", "browse", 5)], 20)[0]?.brief).toBeUndefined();
    expect(toolMarks([tool("d", "web_fetch", 5, "200 OK")], 20)[0]?.brief).toBeUndefined();
    expect(toolMarks([tool("e", "browse", 5, " · x")], 20)[0]?.brief).toBeUndefined();
  });

  it("names no verdict on a grouped browse", () => {
    const [mark] = toolMarks(
      [tool("a", "browse", 5, "verified · 2 steps"), tool("b", "browse", 5, "verified · 2 steps")],
      20,
    );
    expect(mark?.label).toBe("Browsed a site ×2");
    expect(mark?.brief).toBeUndefined();
  });
});
