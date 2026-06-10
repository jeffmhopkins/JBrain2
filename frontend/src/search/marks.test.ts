import { describe, expect, it } from "vitest";
import { splitMarks } from "./marks";

describe("splitMarks", () => {
  it("splits a snippet into plain and marked segments", () => {
    expect(splitMarks("vitamin <mark>D</mark> daily")).toEqual([
      { text: "vitamin ", marked: false },
      { text: "D", marked: true },
      { text: " daily", marked: false },
    ]);
  });

  it("handles multiple marks and mark-first snippets", () => {
    expect(splitMarks("<mark>roof</mark> repair <mark>quote</mark>")).toEqual([
      { text: "roof", marked: true },
      { text: " repair ", marked: false },
      { text: "quote", marked: true },
    ]);
  });

  it("passes through snippets without marks", () => {
    expect(splitMarks("no highlights here")).toEqual([
      { text: "no highlights here", marked: false },
    ]);
  });

  it("leaves any other markup as inert text — never parsed as HTML", () => {
    const segments = splitMarks('<script>alert("x")</script> <mark>hit</mark>');
    expect(segments).toEqual([
      { text: '<script>alert("x")</script> ', marked: false },
      { text: "hit", marked: true },
    ]);
  });

  it("treats an unterminated mark as highlighted to the end", () => {
    expect(splitMarks("before <mark>rest")).toEqual([
      { text: "before ", marked: false },
      { text: "rest", marked: true },
    ]);
  });
});
