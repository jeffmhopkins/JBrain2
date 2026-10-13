// The Tricorder's live scan on the full map (§M25; DESIGN "The Tricorder's live scan on the
// Minecraft map"), read through a share link: it opens on its player, selected, and follows
// the Tricorder target's rule, so the same card shows on the owner's map and a shared one.
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mcMapInfo, mcMapPlayers, mcMapStructures, mcTimeline } from "../minecraftMapFixtures";
import { McShareApp } from "./McShareApp";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();
let texts: string[];

function fakeContext(): CanvasRenderingContext2D {
  const target: Record<string, unknown> = {
    measureText: (s: string) => ({ width: s.length * 6 }),
    fillText: (s: string) => texts.push(s),
  };
  return new Proxy(target, {
    get: (t, k: string) => (k in t ? t[k] : () => undefined),
    set: () => true,
  }) as unknown as CanvasRenderingContext2D;
}

type Scan = "" | "found" | "none" | "offline";

function share(scan: Scan, focus: "BlockyFox" | "Mira_P" = "BlockyFox") {
  const players = mcMapPlayers("slot2", true, scan);
  const fox = players.players.find((p) => p.gamertag === "BlockyFox");
  // Mira_P nearby, so the view opened on her has BlockyFox in it too.
  if (fox && focus === "Mira_P") {
    players.players = players.players.map((p) =>
      p.gamertag === "Mira_P" ? { ...p, dim: fox.dim, x: fox.x - 90, z: fox.z + 90 } : p,
    );
  }
  const who = players.players.find((p) => p.gamertag === focus);
  fetchMock.mockImplementation(async (input) => {
    const url = new URL(String(input), "http://box");
    const path = url.pathname;
    const dim = (url.searchParams.get("dim") ?? "overworld") as "overworld";
    if (path === "/api/mc-share/tok") {
      return json({
        slot: "slot2",
        name: "Castle Hill",
        active: true,
        last_played: null,
        focus: { xuid: who?.xuid, gamertag: focus },
      });
    }
    if (path === "/api/mc-share/tok/map/info") return json(mcMapInfo("slot2", dim));
    if (path === "/api/mc-share/tok/map/players") return json(players);
    if (path === "/api/mc-share/tok/map/structures") return json(mcMapStructures("slot2", dim));
    if (path === "/api/mc-share/tok/timeline") {
      return json(
        mcTimeline("slot2", 0, 10, {
          include: ["trails", "events", "sessions"],
          players: [],
          dim: "overworld",
        }),
      );
    }
    return new Response(null, { status: 404 });
  });
  window.history.replaceState(null, "", "/mc/s/tok");
  render(<McShareApp />);
}

const lines = async () => {
  const el = await waitFor(() => {
    const got = document.querySelector(".mc-map-det-scan");
    expect(got).not.toBeNull();
    return got as HTMLElement;
  });
  return (el.textContent ?? "").replace(/ /g, " ");
};

beforeEach(() => {
  texts = [];
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    () => fakeContext() as never,
  );
  // A phone-sized map; the floating controls take no room, so labels are placed by the
  // markers alone.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    const control = this.closest(".mc-map-top, .mc-map-fabs");
    const [w, h] = control ? [0, 0] : [390, 844];
    return { x: 0, y: 0, left: 0, top: 0, right: w, bottom: h, width: w, height: h } as DOMRect;
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  fetchMock.mockReset();
  window.history.replaceState(null, "", "/");
});

describe("the live scan on the map", () => {
  it("found: the held Tricorder's words, the depth, the reach, and the waypoint kept", async () => {
    share("found");
    const got = await lines();
    expect(got).toMatch(/^Tricorder · Scanning: diamond · 1[23] blocks north-east · y -?\d+/);
    expect(got).toContain("↓ 6 blocks below them");
    expect(got).toMatch(/Within 16 blocks, y -?\d+ to -?\d+\. Found \d+ s ago\./);
    expect(got).toContain("Their waypoint, Diamonds, comes back when the scan stops.");
    // The waypoint under the scan has no line of its own: the arrow points at the scan.
    expect(document.querySelectorAll(".mc-map-det-tri")).toHaveLength(1);
    // One plate: the tapped scanner's name first, never the scan's words alone.
    await waitFor(() => expect(texts).toContain("BlockyFox · Scanning: diamond"));
    expect(texts.some((t) => t.startsWith("Scanning:"))).toBe(false);
  });

  it("none nearby: the plate and the card say so, with the same reach", async () => {
    share("none");
    const got = await lines();
    expect(got).toMatch(/^Tricorder · Scanning: diamond · none nearby/);
    expect(got).toMatch(/Nothing within 16 blocks, y -?\d+ to -?\d+\. Last scan \d+ s ago\./);
    expect(got).not.toContain("below them");
    await waitFor(() => expect(texts).toContain("BlockyFox · Scanning: diamond · none nearby"));
  });

  it("offline: kept, and the card says it resumes; nothing scans on the map", async () => {
    share("offline");
    const got = await lines();
    expect(got).toBe("Tricorder · Scanning: diamond — resumes when BlockyFox is back on");
    await waitFor(() => expect(texts.length).toBeGreaterThan(0));
    expect(texts.some((t) => t.includes("Scanning"))).toBe(false);
  });

  it("overview: a scanner nobody tapped still wears their name and their scan", async () => {
    share("found", "Mira_P");
    expect(await screen.findByRole("button", { name: /Follow/ })).toBeInTheDocument();

    await waitFor(() => expect(texts).toContain("BlockyFox · Scanning: diamond"));
    // Mira_P's card is hers: no scan lines.
    expect(document.querySelector(".mc-map-det-scan")).toBeNull();
  });

  it("with no scan on, the waypoint line is the shipped one", async () => {
    share("");
    await screen.findByRole("button", { name: /Follow/ });
    await waitFor(() =>
      expect(document.querySelector(".mc-map-det-tri")).toHaveTextContent(
        /Tricorder · Diamonds · \d+ blocks north-east · y -58/,
      ),
    );
    expect(document.querySelector(".mc-map-det-scan")).toBeNull();
  });
});
