import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mcMapMarkers } from "../../minecraftMapFixtures";
import { type MapIntent, setMapOpener } from "../../minecraftMapNav";
import { focusedMarker, parseMinecraftMap } from "./minecraftMapCard";
import { ToolView } from "./registry";

// The GUI gate's chat fixture: BlockyFox's lava death in the Nether, beside the fortress.
// "Now" is pinned the next day, so the card's day words are fixed.
const NOW = new Date(2026, 9, 10, 14, 40).getTime();
const DIED = Math.round(new Date(2026, 9, 9, 17, 12).getTime() / 1000);

function deathCard(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    slot: "slot2",
    world: "Castle Hill",
    loaded: true,
    dim: "nether",
    center: { x: 41, z: -58 },
    zoom: -1,
    overlays: { trails: true, deaths: true, structures: true, players: false },
    focus: {
      kind: "death",
      label: "BlockyFox died here",
      x: 41,
      y: 33,
      z: -58,
      at: DIED,
      cause: "lava",
      killer: null,
    },
    overworld: { x: 328, z: -464 },
    trail: { player: "2535411", gamertag: "BlockyFox", from: DIED - 7200, to: DIED + 120 },
    ...over,
  };
}

function show(data: Record<string, unknown>) {
  return render(
    <ToolView payload={{ view: "minecraft_map", surface: "inline", data, refs: [] }} />,
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A 2D context that draws nothing but remembers its words, so the scale bar and labels
 *  can be read back. */
function fakeContext(texts: string[]): CanvasRenderingContext2D {
  const target: Record<string, unknown> = {
    measureText: (s: string) => ({ width: s.length * 6 }),
    fillText: (s: string) => texts.push(s),
  };
  return new Proxy(target, {
    get: (t, k: string) => (k in t ? t[k] : () => undefined),
    set: () => true,
  }) as unknown as CanvasRenderingContext2D;
}

describe("the minecraft_map chat card", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let tiles: string[];
  let texts: string[];
  let opened: MapIntent[];

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    tiles = [];
    texts = [];
    opened = [];
    setMapOpener((i) => opened.push(i));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal(
      "Image",
      class {
        decoding = "";
        onload: (() => void) | null = null;
        onerror: (() => void) | null = null;
        set src(v: string) {
          tiles.push(v);
        }
      },
    );
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      () => fakeContext(texts) as never,
    );
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://box").pathname;
      if (path === "/api/minecraft/map/players") {
        return json({ slot: "slot2", world: "Castle Hill", live: true, players: [] });
      }
      if (path === "/api/minecraft/map/structures") {
        return json({
          slot: "slot2",
          dimension: "nether",
          structures: [{ structure: "fortress", x: 120, z: -90 }],
          swept_at: 1,
          stale: false,
          sweep: { state: "idle" },
        });
      }
      if (path === "/api/minecraft/timeline") {
        return json({ from: DIED - 7200, to: DIED + 120, trails: [], events: [] });
      }
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    setMapOpener(null);
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const asked = () => fetchMock.mock.calls.map(([u]) => String(u));

  it("shows the death, its Overworld side, the legend and the scale bar", async () => {
    show(deathCard());
    expect(screen.getByText("Castle Hill · Nether")).toBeInTheDocument();
    expect(screen.getByText("BlockyFox burned in lava")).toBeInTheDocument();
    expect(screen.getByText(/^Yesterday, 5:12/)).toBeInTheDocument();
    expect(screen.getByText("X 328 · Z -464")).toBeInTheDocument();
    expect(screen.getByText("Explored")).toBeInTheDocument();
    expect(screen.getByText("Satellite survey")).toBeInTheDocument();
    expect(screen.getByText("Death")).toBeInTheDocument();
    expect(screen.getByText("Structure")).toBeInTheDocument();
    // Honest status: nothing claimed until the box says, then live and how far behind.
    expect(screen.getByText("Loading the map…")).toBeInTheDocument();
    expect(await screen.findByText("Live · 0 on")).toBeInTheDocument();
    // The canvas draws the scale bar (north up) and the focus label.
    await waitFor(() => expect(texts).toContain("N↑"));
    expect(texts.some((t) => /blocks?$/.test(t) || / block$/.test(t))).toBe(true);
    expect(texts).toContain("BlockyFox died here");
  });

  it("builds every tile request itself and never follows a URL in the payload", async () => {
    show(
      deathCard({
        tile_url: "https://evil.example/{z}/{x}.png",
        url: "https://evil.example/map",
        src: "data:image/png;base64,AAAA",
        world: "Castle Hill",
      }),
    );
    await waitFor(() => expect(tiles.length).toBeGreaterThan(0));
    for (const t of tiles) {
      expect(t).toMatch(
        /^\/api\/minecraft\/map\/tile\/nether\/0\/-?\d+\/-?\d+\.png\?slot=slot2&layer=(surface|biomes)$/,
      );
    }
    await waitFor(() =>
      expect(asked().some((u) => u.includes("/api/minecraft/timeline"))).toBe(true),
    );
    for (const u of [...asked(), ...tiles]) {
      expect(u).not.toContain("evil");
      expect(u.startsWith("/api/minecraft/")).toBe(true);
    }
    // The trail and deaths are this player's own, over the payload's window.
    const tl = new URL(asked().find((u) => u.includes("/timeline")) ?? "", "http://box");
    expect(tl.searchParams.get("players")).toBe("2535411");
    expect(tl.searchParams.get("from")).toBe(String(DIED - 7200));
  });

  it("a tap opens the full map on the card's spot; a drag or a scroll does not", async () => {
    show(deathCard());
    const map = screen.getByRole("img", { name: /^Map of Castle Hill · Nether/ });
    // One finger dragging is the chat's scroll: the browser cancels, or it simply moved.
    fireEvent.pointerDown(map, { pointerId: 1, clientX: 10, clientY: 10, pointerType: "touch" });
    fireEvent.pointerCancel(map, { pointerId: 1, clientX: 10, clientY: 60 });
    fireEvent.pointerDown(map, { pointerId: 2, clientX: 10, clientY: 10, pointerType: "touch" });
    fireEvent.pointerMove(map, { pointerId: 2, clientX: 40, clientY: 10, pointerType: "touch" });
    fireEvent.pointerUp(map, { pointerId: 2, clientX: 40, clientY: 10 });
    expect(opened).toEqual([]);
    fireEvent.pointerDown(map, { pointerId: 3, clientX: 300, clientY: 200 });
    fireEvent.pointerUp(map, { pointerId: 3, clientX: 300, clientY: 200 });
    expect(opened).toEqual([{ slot: "slot2", dim: "nether", x: 41, z: -58, zoom: -1, mark: true }]);
    fireEvent.click(screen.getByRole("button", { name: "Open the full map" }));
    expect(opened).toHaveLength(2);
  });

  it("two fingers pinch the card, and the full map opens at the new zoom", async () => {
    show(deathCard());
    const map = screen.getByRole("img", { name: /^Map of/ });
    fireEvent.pointerDown(map, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerDown(map, { pointerId: 2, clientX: 140, clientY: 100 });
    fireEvent.pointerMove(map, { pointerId: 2, clientX: 420, clientY: 100 });
    fireEvent.pointerUp(map, { pointerId: 2, clientX: 420, clientY: 100 });
    fireEvent.pointerUp(map, { pointerId: 1, clientX: 100, clientY: 100 });
    expect(opened).toEqual([]); // a pinch is never a tap
    fireEvent.click(screen.getByRole("button", { name: "Open the full map" }));
    expect(opened[0]?.zoom).toBe(-3);
  });

  it("copies X Z as two numbers, and the overlay chips switch", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    show(deathCard());
    fireEvent.click(screen.getByRole("button", { name: "Copy X Z" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("41 -58"));
    expect(await screen.findByText("Copied X Z: 41 -58")).toBeInTheDocument();
    const chip = screen.getByRole("button", { name: "Structures" });
    expect(chip).toHaveAttribute("aria-pressed", "true");
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Players" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("Dave's answer about a gate shows that gate, open, with the Beacons and Gates chips", async () => {
    const marks = mcMapMarkers("slot2");
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://box").pathname;
      if (path === "/api/minecraft/map/markers") return json(marks);
      if (path === "/api/minecraft/map/players") {
        return json({ slot: "slot2", world: "Castle Hill", live: true, players: [] });
      }
      return new Response(null, { status: 404 });
    });
    // A card's real size, so the gate, its line and its place all land on it.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 360,
      bottom: 220,
      width: 360,
      height: 220,
      toJSON: () => ({}),
    });
    show({
      slot: "slot2",
      world: "Castle Hill",
      loaded: true,
      dim: "overworld",
      center: { x: -60, z: -60 },
      zoom: 1,
      overlays: { beacons: true, gates: true },
      focus: { kind: "gate", label: "Wormhole gate 1", x: 30, y: 71, z: 14 },
    });
    // From the box's own record, not the payload: whose it is and where it leads.
    expect(await screen.findByText("Wormhole gate 1")).toBeInTheDocument();
    expect(await screen.findByText(/BlockyFox's, to Tree farm/)).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("Copy X Z")).toBeInTheDocument();
    expect(asked().filter((u) => u.includes("/map/markers"))).toEqual([
      "/api/minecraft/map/markers?slot=slot2",
    ]);
    for (const name of ["Beacons", "Gates"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-pressed", "true");
    }
    // The gate's line ends at its place, labelled on the canvas.
    await waitFor(() => expect(texts).toContain("Tree farm"));
  });

  it("Dave's answer about a scan draws the scanner live: one plate, the square's words", async () => {
    const now = NOW / 1000;
    const fox = {
      xuid: "2535411",
      gamertag: "BlockyFox",
      dim: "overworld",
      x: 32,
      y: -52,
      z: -70,
      at: now,
      online: true,
      target: { dim: "overworld", x: 300, y: null, z: 0, label: "Tree farm", set_at: 0 },
      scan: {
        label: "diamond",
        state: "found",
        found: { dim: "overworld", x: 41, y: -58, z: -79 },
        at: now - 40,
        set_at: now - 900,
      },
    };
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://box").pathname;
      if (path === "/api/minecraft/map/players") {
        return json({ slot: "slot2", world: "Castle Hill", live: true, players: [fox] });
      }
      return new Response(null, { status: 404 });
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 360,
      bottom: 220,
      width: 360,
      height: 220,
      toJSON: () => ({}),
    });
    show({
      slot: "slot2",
      world: "Castle Hill",
      loaded: true,
      dim: "overworld",
      center: { x: 32, z: -70 },
      zoom: -2,
      overlays: { players: true },
      focus: { kind: "scan", label: "blockyfox", x: 30, y: -50, z: -70, at: null },
      trail: { player: "2535411", gamertag: "BlockyFox", from: null, to: null },
    });
    const lines = await waitFor(() => {
      const el = document.querySelector(".mc-map-det-scan");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    const words = (lines.textContent ?? "").replace(/\u00a0/g, " ");
    expect(words).toContain("Tricorder · Scanning: diamond · 13 blocks north-east · y -58");
    expect(words).toContain("↓ 6 blocks below them · y -58");
    expect(words).toContain("Within 16 blocks, y -64 to -36. Found 40 s ago.");
    // The scanner where they are now, not where Dave saw them.
    expect(screen.getByText("BlockyFox")).toBeInTheDocument();
    expect(document.querySelector(".tv-mc-xyz")).toHaveTextContent("X 32 · Y -52 · Z -70");
    // One plate carries the name and the scan; the name is never on its own beside it.
    await waitFor(() => expect(texts).toContain("BlockyFox · Scanning: diamond"));
    expect(texts).not.toContain("BlockyFox");
  });

  it("a scan card for a player who's offline says it resumes, and draws no square", async () => {
    const now = NOW / 1000;
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://box").pathname;
      if (path === "/api/minecraft/map/players") {
        return json({
          slot: "slot2",
          world: "Castle Hill",
          live: true,
          players: [
            {
              xuid: "2535411",
              gamertag: "BlockyFox",
              dim: "overworld",
              x: 32,
              y: -52,
              z: -70,
              at: now - 600,
              online: false,
              scan: { label: "diamond", state: "none", found: null, at: now - 700, set_at: 0 },
            },
          ],
        });
      }
      return new Response(null, { status: 404 });
    });
    show({
      slot: "slot2",
      world: "Castle Hill",
      loaded: true,
      dim: "overworld",
      center: { x: 32, z: -70 },
      zoom: -2,
      overlays: {},
      focus: { kind: "scan", label: "BlockyFox", x: 32, y: -52, z: -70, at: null },
      trail: null,
    });
    expect(
      await screen.findByText(/Scanning: diamond — resumes when BlockyFox is back on/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Offline · Overworld/)).toBeInTheDocument();
    await waitFor(() => expect(texts).toContain("BlockyFox"));
    expect(texts.some((t) => t.includes("Scanning"))).toBe(false);
  });

  it("a villager or pet a finder found: marked as of the last save, opening with its layer", async () => {
    show(
      deathCard({
        dim: "overworld",
        overworld: null,
        focus: { kind: "villager", label: "Librarian · Expert", x: -122, y: 66, z: -104 },
      }),
    );
    expect(await screen.findByText("Librarian · Expert")).toBeInTheDocument();
    expect(
      screen.getByText("Villager · as of the world's last save · Overworld"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open the full map" }));
    expect(opened[0]).toMatchObject({ x: -122, z: -104, mark: true, show: "villagers" });
    cleanup();
    show(
      deathCard({
        dim: "overworld",
        overworld: null,
        focus: { kind: "pet", label: "Rex", x: 52, y: 70, z: 52 },
      }),
    );
    expect(
      await screen.findByText("Pet · as of the world's last save · Overworld"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open the full map" }));
    expect(opened[1]).toMatchObject({ x: 52, z: 52, show: "pets" });
  });

  it("a malformed payload draws nothing it shouldn't", () => {
    const { container } = show({ slot: "../../etc", dim: "<img src=x onerror=alert(1)>" });
    expect(screen.getByText("This map can't be shown.")).toBeInTheDocument();
    expect(container.querySelector("img, iframe, script, canvas")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("parseMinecraftMap", () => {
  it("clamps every slot and reads nothing else", () => {
    const card = parseMinecraftMap({
      slot: "slot3",
      dim: "aether",
      world: `  ${"W".repeat(90)}  `,
      center: { x: 9e12, z: "far" },
      zoom: 77,
      overlays: { trails: "yes", players: true, iframe: true },
      focus: { kind: "treasure", x: 1, z: 2 },
      trail: { player: "", gamertag: "x" },
      html: "<b>hi</b>",
    });
    expect(card).toEqual({
      slot: "slot3",
      world: "W".repeat(60),
      loaded: false,
      dim: "overworld",
      x: 30_000_000,
      z: 0,
      zoom: 4,
      overlays: {
        trails: false,
        deaths: false,
        structures: false,
        players: true,
        beacons: false,
        gates: false,
      },
      focus: null,
      trail: null,
    });
  });

  it("defaults the overlays and centres on the focus", () => {
    const card = parseMinecraftMap({
      slot: "slot1",
      focus: { kind: "spot", label: 42, x: 10.6, z: -3.2, y: "high" },
    });
    expect(card?.overlays).toEqual({
      trails: true,
      deaths: true,
      structures: true,
      players: false,
      beacons: true,
      gates: true,
    });
    expect(card?.focus).toMatchObject({ kind: "spot", label: "", x: 11, z: -3, y: null });
    expect([card?.x, card?.z, card?.zoom]).toEqual([11, -3, 1]);
    expect(
      parseMinecraftMap({ slot: "slot1", focus: { kind: "gate", x: 30, z: 14 } })?.focus,
    ).toMatchObject({ kind: "gate", x: 30, z: 14 });
    expect(parseMinecraftMap({ slot: "slot0" })).toBeNull();
    expect(parseMinecraftMap({})).toBeNull();
  });
});

describe("focusedMarker", () => {
  const marks = mcMapMarkers("slot2");
  it("finds the gate round the focus spot and the beacon on it, in that dimension", () => {
    const focus = (kind: "gate" | "beacon", x: number, z: number) =>
      ({ kind, label: "", x, y: null, z, at: null, cause: null, killer: null }) as const;
    expect(focusedMarker(marks, "overworld", focus("gate", 30, 14)).gate?.id).toBe(1);
    expect(focusedMarker(marks, "overworld", focus("gate", 29, 15)).gate?.id).toBe(1);
    expect(focusedMarker(marks, "overworld", focus("gate", 40, 14)).gate).toBeUndefined();
    expect(focusedMarker(marks, "nether", focus("gate", 30, 14)).gate).toBeUndefined();
    expect(focusedMarker(marks, "overworld", focus("beacon", 60, -40)).beacon?.placed_by).toBe(
      "Mira_P",
    );
    expect(focusedMarker(null, "overworld", focus("beacon", 60, -40))).toEqual({});
  });
});
