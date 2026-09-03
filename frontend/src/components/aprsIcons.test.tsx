// The APRS symbol set.
//
// What is pinned here is the OVERLAY rule and the fallback, because those are the two
// places a symbol renderer quietly does the wrong thing: an overlay treated as a table
// draws the wrong icon, and an unassigned code either goes blank or borrows a
// neighbouring glyph and asserts something the station never said.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { APRS_GLYPHS } from "./aprsGlyphs";
import { AprsSymbol, glyphFor } from "./aprsIcons";

describe("resolving a transmitted symbol", () => {
  it("draws the alternate table and an overlay character, not a table called I", () => {
    // `I#` is how N4TDX — the station relaying three quarters of this channel — says it
    // is an IGate. Reading `I` as a table finds nothing at all.
    const igate = glyphFor("I", "#");

    expect(igate.overlay).toBe("I");
    expect(igate.known).toBe(true);
    expect(igate.nodes).toEqual(APRS_GLYPHS["\\#"]);
  });

  it("keeps the two real tables apart", () => {
    // One character apart, different tables, different things: `/$` is a phone and `\$`
    // is a bank.
    expect(glyphFor("/", "$").nodes).not.toEqual(glyphFor("\\", "$").nodes);
    expect(glyphFor("/", "$").overlay).toBeNull();
  });

  it("falls back visibly for a code with no assigned meaning", () => {
    // The spec's own instruction is the circle-and-slash, "meaning NOT". A blank icon
    // reads as a rendering bug; a borrowed one reads as a fact.
    const unknown = glyphFor("/", "");

    expect(unknown.known).toBe(false);
    expect(unknown.nodes.length).toBeGreaterThan(0);
  });

  it("clamps the overlay to one character", () => {
    // It is packet data: a station transmits it, and a malformed frame can carry
    // anything. Two characters in that slot spill out of the icon.
    expect(glyphFor("XY", "a").overlay).toHaveLength(1);
  });

  it("maps a compressed report's a-j overlay back to a digit", () => {
    expect(glyphFor("c", "#").overlay).toBe("2");
  });
});

describe("rendering", () => {
  it("carries a text alternative, because the icon is what says which station this is", () => {
    const { container } = render(<AprsSymbol table="I" code="#" label="IGate" />);

    expect(container.querySelector("title")?.textContent).toBe("IGate");
    expect(container.querySelector("svg")).toHaveAttribute("role", "img");
  });

  it("paints the overlay character on top of the base drawing", () => {
    const { container } = render(<AprsSymbol table="W" code="a" label="Winlink gateway" />);

    expect(container.querySelector("text")?.textContent).toBe("W");
    expect(container.querySelectorAll("path,circle,rect").length).toBeGreaterThan(0);
  });

  it("draws no overlay text for a plain primary-table symbol", () => {
    const { container } = render(<AprsSymbol table="/" code=">" label="Car" />);

    expect(container.querySelector("text")).toBeNull();
  });

  it("inherits colour and stroke so it themes itself", () => {
    // Single-colour outline at 1.5 is what lets one set work on any ground, and is why
    // the third-party raster sets were unusable here.
    const svg = render(
      <AprsSymbol table="/" code="_" label="Weather station" />,
    ).container.querySelector("svg");

    expect(svg).toHaveAttribute("stroke", "currentColor");
    expect(svg).toHaveAttribute("fill", "none");
    expect(svg).toHaveAttribute("viewBox", "0 0 24 24");
  });

  it("renders every symbol measured on the owner's channel", () => {
    // The working set. If one of these regresses, the owner sees a circle-and-slash
    // against traffic he actually receives.
    const measured = [
      "/`",
      "/_",
      "/S",
      "/z",
      "/r",
      "/-",
      "D&",
      "Da",
      "Wa",
      "I#",
      "/[",
      "/?",
      "/k",
      "/>",
      "/$",
    ];
    for (const pair of measured) {
      const { nodes } = glyphFor(pair.slice(0, 1), pair.slice(1, 2));
      expect(nodes.length, pair).toBeGreaterThan(0);
    }
  });
});

describe("the set as a whole", () => {
  it("holds a drawing for every code with a standard meaning", () => {
    expect(Object.keys(APRS_GLYPHS).length).toBeGreaterThanOrEqual(165);
  });

  it("is data rather than markup, so a malformed shape cannot become an injection", () => {
    for (const [key, nodes] of Object.entries(APRS_GLYPHS)) {
      expect(nodes.length, key).toBeGreaterThan(0);
      for (const node of nodes) {
        expect(["path", "circle", "rect", "text"], key).toContain(node.t);
      }
    }
  });
});
