import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GrowbotHouseMap, readMap } from "./GrowbotHouseMap";

// The persona's sketch map (growbot-map.js writes it into the soul): drawn as an SVG with a
// line per room, and nothing trusted that the phone's JSON did not get right.

const MAP = {
  v: 1,
  t: 1,
  rooms: [
    { n: "living room", x: 0, y: 0.7, w: 4, d: 2.6 },
    { n: "kitchen", x: 0, y: -2.6, w: 4, d: 4 },
  ],
  doors: [{ x: 0, y: -0.6, a: "living room", b: "kitchen" }],
  things: [
    { n: "piano", x: -0.8, y: 1.9, room: "living room", t: 1 },
    { n: "fridge", x: 1, y: -3, room: "kitchen", t: 1, rough: 1 },
    { n: "lamp", x: 9, y: 9, room: "", t: 1 },
  ],
  here: { x: 0, y: 0, h: 90, room: "living room" },
};

describe("GrowbotHouseMap", () => {
  it("draws the rooms, doorways, things and where it is, and lists them", () => {
    const { container } = render(<GrowbotHouseMap soul={{ map: MAP }} name="Dave" />);
    expect(screen.getByRole("img", { name: "Dave's map of the house" })).toBeTruthy();
    expect(container.querySelectorAll(".gb-map-room")).toHaveLength(2);
    expect(container.querySelector(".gb-map-room.here")).toBeTruthy();
    expect(container.querySelectorAll(".gb-map-door")).toHaveLength(1);
    expect(container.querySelectorAll(".gb-map-thing")).toHaveLength(3);
    expect(container.querySelector(".gb-map-thing.rough")).toBeTruthy();
    expect(container.querySelector(".gb-map-me")).toBeTruthy();
    const items = [...container.querySelectorAll(".gb-map-list li")].map((li) => li.textContent);
    expect(items[0]).toContain("living room (it is here)");
    expect(items[0]).toContain("piano");
    expect(items[0]).toContain("doorways to kitchen");
    expect(items[1]).toContain("fridge");
    expect(items[2]).toContain("Not in a room · lamp");
  });

  it("flips y, so north on the map is up on the page", () => {
    const { container } = render(<GrowbotHouseMap soul={{ map: MAP }} name="Dave" />);
    const dots = [...container.querySelectorAll(".gb-map-thing")];
    const piano = Number(dots[0]?.getAttribute("cy"));
    const fridge = Number(dots[1]?.getAttribute("cy"));
    expect(piano).toBeLessThan(fridge);
  });

  it("says when there is no map yet", () => {
    render(<GrowbotHouseMap soul={{}} name="Dave" />);
    expect(screen.getByText(/Dave hasn't drawn a map yet/)).toBeTruthy();
  });

  it("drops what the phone's JSON got wrong", () => {
    const m = readMap({
      map: {
        v: 1,
        rooms: [
          { n: "hall", x: 0, y: 0, w: "4", d: 4 },
          { n: 5 },
          { n: "den", x: 0, y: 0, w: -1, d: 4 },
          { n: "den", x: 0, y: 0, w: 2, d: 2 },
          { n: "den", x: 5, y: 5, w: 2, d: 2 },
        ],
        doors: [{ x: null, y: 0 }],
        things: [{ n: "x".repeat(80), x: 1, y: 2 }],
        here: { x: "far" },
      },
    });
    expect(m?.rooms).toEqual([{ n: "den", x: 0, y: 0, w: 2, d: 2 }]);
    expect(m?.doors).toEqual([]);
    expect(m?.things[0]?.n).toHaveLength(32);
    expect(m?.here).toEqual({ x: 0, y: 0, h: 90, room: "" });
    expect(readMap({ map: { v: 2 } })).toBeNull();
  });

  it("keeps no more than the page's own caps from a hand-edited soul", () => {
    const many = (n: number, f: (i: number) => object) => Array.from({ length: n }, (_, i) => f(i));
    const big = {
      ...MAP,
      rooms: many(500, (i) => ({ n: `r${i}`, x: i, y: 0, w: 2, d: 2 })),
      doors: many(500, (i) => ({ x: i, y: 0, a: "", b: "" })),
      things: many(5000, (i) => ({ n: `t${i}`, x: i, y: 0, room: "", t: 1 })),
    };
    const got = readMap({ map: big } as never);
    expect(got?.rooms).toHaveLength(24);
    expect(got?.doors).toHaveLength(48);
    expect(got?.things).toHaveLength(120);
  });
});
