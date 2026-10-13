import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftMapPoint, MinecraftServer } from "../api/client";
import {
  mcAllowlist,
  mcPlayers,
  mcServer,
  mcServerSettings,
  mcStatus,
  mcStopped,
  mcVersion,
  mcWorlds,
} from "../minecraftFixtures";
import {
  mcMapInfo,
  mcMapMarkers,
  mcMapPlayers,
  mcMapPoint,
  mcMapStructures,
  mcTimeline,
} from "../minecraftMapFixtures";
import { MinecraftScreen } from "./MinecraftScreen";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("the Minecraft map", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let server: MinecraftServer;
  let point: (x: number, z: number, dim: string) => MinecraftMapPoint;
  let down = false;
  // The travel log behind deaths and wakings, served only to the tests that read it.
  let withLog = false;

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    // jsdom draws nothing; the map's geometry is what's under test.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    server = mcServer();
    down = false;
    withLog = false;
    point = (x, z, dim) => mcMapPoint("slot2", dim as "overworld", x, z);
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://box");
      const path = url.pathname;
      const q = url.searchParams;
      const slot = q.get("slot") ?? "slot1";
      const dim = (q.get("dim") ?? "overworld") as "overworld";
      if (path.startsWith("/api/minecraft/map/") && down) {
        return json({ detail: "Minecraft sidecar unreachable" }, 503);
      }
      if (path === "/api/minecraft") return json(mcStatus(server));
      if (path.startsWith("/api/minecraft/version")) return json(mcVersion());
      if (path === "/api/minecraft/players") return json(mcPlayers());
      if (path === "/api/minecraft/worlds") return json(mcWorlds());
      if (path === "/api/minecraft/allowlist") return json(mcAllowlist());
      if (path === "/api/minecraft/server-settings") return json(mcServerSettings());
      if (path === "/api/minecraft/backups") return json({ snapshots: [] });
      if (path === "/api/minecraft/map/info") return json(mcMapInfo(slot, dim));
      if (path === "/api/minecraft/map/players") {
        return json(mcMapPlayers(slot, server.state === "running" && slot === "slot2"));
      }
      if (path === "/api/minecraft/map/structures") return json(mcMapStructures(slot, dim));
      if (path === "/api/minecraft/map/markers") return json(mcMapMarkers(slot));
      if (path === "/api/minecraft/timeline" && withLog) {
        const parts = (q.get("include") ?? "trails,events,sessions").split(",");
        return json(
          mcTimeline(slot, Number(q.get("from")), Number(q.get("to")), {
            include: parts as never,
            players: [],
            dim,
          }),
        );
      }
      if (path === "/api/minecraft/map/point") {
        return json(point(Number(q.get("x")), Number(q.get("z")), dim));
      }
      if (path === "/api/minecraft/map/structures/sweep" && init?.method === "POST") {
        return json({ state: "running", done: 0, total: 900 }, 202);
      }
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const calls = (prefix: string) =>
    fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.startsWith(prefix));

  async function openMap() {
    render(<MinecraftScreen />);
    const row = await screen.findByRole("button", { name: /^Map/ });
    await waitFor(() =>
      expect(row).toHaveTextContent("Castle Hill · BlockyFox and Mira_P on the map"),
    );
    fireEvent.click(row);
    return screen.findByRole("application");
  }

  function tapAt(el: Element, x: number, y: number) {
    fireEvent.pointerDown(el, { pointerId: 1, clientX: x, clientY: y });
    fireEvent.pointerUp(el, { pointerId: 1, clientX: x, clientY: y });
  }

  it("opens from the Minecraft screen with the honest live status", async () => {
    await openMap();
    expect(await screen.findByText("Live · 2 on")).toBeInTheDocument();
    expect(
      screen.getByText("Up to a minute behind the world · positions live"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "World: Castle Hill — change" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Overworld" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(calls("/api/minecraft/map/info")[0]).toContain("slot=slot2");
    expect(calls("/api/minecraft/map/players")[0]).toContain("slot=slot2");
    expect(calls("/api/minecraft/map/structures")[0]).toContain("dim=overworld");
  });

  it("a tap asks the box what's there: biome, y, explored, and Copy X Z", async () => {
    const map = await openMap();
    await screen.findByText("Live · 2 on");
    tapAt(map, 0, 0);
    expect(await screen.findByText(/^Explored · ground at Y/)).toBeInTheDocument();
    const asked = new URL(calls("/api/minecraft/map/point")[0] ?? "", "http://box").searchParams;
    expect(asked.get("slot")).toBe("slot2");
    expect(asked.get("dim")).toBe("overworld");
    expect(Number.isInteger(Number(asked.get("x")))).toBe(true);
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    fireEvent.click(screen.getByRole("button", { name: /Copy X Z/ }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${asked.get("x")} ${asked.get("z")}`),
    );
    expect(await screen.findByText(/^Copied X Z:/)).toBeInTheDocument();
  });

  it("a chat's map card opens it on the card's spot, marked, in the card's dimension", async () => {
    const consumed = vi.fn();
    render(
      <MinecraftScreen
        openMap={{ slot: "slot2", dim: "nether", x: 41, z: -58, zoom: -1, mark: true }}
        onOpenMapConsumed={consumed}
      />,
    );
    await screen.findByRole("application");
    expect(consumed).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("radio", { name: "Nether" })).toHaveAttribute("aria-checked", "true");
    // The marked spot asks the box what's there, like a tap would.
    await waitFor(() => expect(calls("/api/minecraft/map/point").length).toBeGreaterThan(0));
    const asked = new URL(calls("/api/minecraft/map/point")[0] ?? "", "http://box").searchParams;
    expect([asked.get("slot"), asked.get("dim"), asked.get("x"), asked.get("z")]).toEqual([
      "slot2",
      "nether",
      "41",
      "-58",
    ]);
    expect(await screen.findByText("X 328 · Z -464")).toBeInTheDocument();
  });

  it("the survey's y is an estimate: ~Y in the line, none in the coordinates", async () => {
    point = (x, z) => ({
      x,
      z,
      dimension: "overworld",
      source: "survey",
      biome: "Cherry Grove",
      y: 129,
    });
    const map = await openMap();
    tapAt(map, 10, 10);
    expect(
      await screen.findByText(/^Satellite survey \(predicted\) · ground at Y.~129$/),
    ).toBeInTheDocument();
    const coords = screen.getByText("Copy X Z").closest(".mc-map-coord");
    expect(coords).not.toHaveTextContent(/Y/);
  });

  it("a long press drops a pin that nudges one block at a time", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const map = await openMap();
      fireEvent.pointerDown(map, { pointerId: 1, clientX: 0, clientY: 0 });
      await act(async () => {
        vi.advanceTimersByTime(500);
      });
      fireEvent.pointerUp(map, { pointerId: 1, clientX: 0, clientY: 0 });
      expect(await screen.findByText("Pin dropped — nudge it block by block")).toBeInTheDocument();
      const before = new URL(calls("/api/minecraft/map/point").at(-1) ?? "", "http://box")
        .searchParams;
      fireEvent.click(screen.getByRole("button", { name: "East" }));
      await waitFor(() => {
        const after = new URL(calls("/api/minecraft/map/point").at(-1) ?? "", "http://box")
          .searchParams;
        expect(Number(after.get("x"))).toBe(Number(before.get("x")) + 1);
        expect(after.get("z")).toBe(before.get("z"));
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("switches dimension and world, and lists all five slots with the empty one disabled", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("radio", { name: "Nether" }));
    await waitFor(() => expect(calls("/api/minecraft/map/info").at(-1)).toContain("dim=nether"));
    fireEvent.click(screen.getByRole("button", { name: /^World:/ }));
    const sheet = await screen.findByRole("dialog");
    const rows = within(sheet)
      .getAllByRole("button")
      .filter((b) => /Slot \d/.test(b.textContent ?? ""));
    expect(rows).toHaveLength(5);
    expect(rows.filter((b) => (b as HTMLButtonElement).disabled).length).toBeGreaterThan(0);
    fireEvent.click(within(sheet).getByRole("button", { name: /World\s*Slot 1/ }));
    await waitFor(() => expect(calls("/api/minecraft/map/players").at(-1)).toContain("slot=slot1"));
    expect(await screen.findByText("World isn't loaded")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Overworld" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("a stopped server is never called live, and its players are where they logged off", async () => {
    server = mcStopped();
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /^Map/ }));
    expect(await screen.findByText("Server stopped")).toBeInTheDocument();
    expect(screen.queryByText(/^Live/)).toBeNull();
    expect(screen.getByText(/Nobody can be on while it.s stopped/)).toBeInTheDocument();
  });

  it("an unreachable box says so and keeps the map", async () => {
    down = true;
    await openMap();
    expect(await screen.findByText("Box unreachable — retrying…")).toBeInTheDocument();
    expect(screen.getByRole("application")).toBeInTheDocument();
  });

  it("the Layers tab lists the live overlays, a fresh search, and what comes later", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("switch", { name: "Players" })).toHaveAttribute("aria-checked", "true");
    const structures = screen.getByRole("switch", { name: "Structures" });
    expect(structures).toHaveAttribute("aria-checked", "true");
    expect(await screen.findByText("from the server’s locate · exact")).toBeInTheDocument();
    fireEvent.click(structures);
    expect(structures).toHaveAttribute("aria-checked", "false");
    fireEvent.click(screen.getByRole("button", { name: "Search again" }));
    await waitFor(() => expect(screen.getByText(/searching the world · 0%/)).toBeInTheDocument());
    expect(screen.getByText("Explored")).toBeInTheDocument();
    expect(screen.getByText("Satellite survey")).toBeInTheDocument();
    expect(screen.getByText("Wormhole gates")).toBeInTheDocument();
    for (const name of ["Trails", "Deaths", "Spawn · beds on tap", "Explored by…"]) {
      expect(screen.getByRole("switch", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("Saved places")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Timeline" })).toBeEnabled();
  });

  it("beacons and gates: two switches, no 'later', and every one listed in this world", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    const beacons = screen.getByRole("switch", { name: "No-Spawn Beacons" });
    const gates = screen.getByRole("switch", { name: "Wormhole gates" });
    expect(beacons).toHaveAttribute("aria-checked", "true");
    expect(gates).toHaveAttribute("aria-checked", "true");
    expect(
      await screen.findByText("3 in this world · each with its 48 × 48 protected area"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("2 open · 1 dormant · a dotted line to where each leads"),
    ).toBeInTheDocument();
    // Only saved places are still to come.
    expect(screen.getAllByText("later")).toHaveLength(1);
    fireEvent.click(gates);
    expect(gates).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("heading", { name: "In this world" })).toBeInTheDocument();
    for (const name of [
      /^No-Spawn Beacon · Mira_P/,
      /^Gate 1 · BlockyFox/,
      /^Gate 3 · BlockyFox/,
    ]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByText(/up at Y 210/)).toBeInTheDocument();
    expect(screen.getByText("Gate · dormant")).toBeInTheDocument();
    expect(screen.getByText(/the pack's own records/)).toBeInTheDocument();
    expect(calls("/api/minecraft/map/markers")[0]).toBe("/api/minecraft/map/markers?slot=slot2");
  });

  it("a gate from the list: where it leads, Show Tree farm; dormant; into the Nether", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Gate 1 · BlockyFox/ }));
    expect(screen.getByText("Wormhole gate 1")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText(/BlockyFox's · planted/)).toBeInTheDocument();
    expect(screen.getByText(/244 blocks north-west · X \u2212160/)).toBeInTheDocument();
    // The middle of the ring, where a player stands.
    expect(screen.getByRole("button", { name: "Copy X Z" })).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show Tree farm" }));

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Gate 2 · Mira_P/ }));
    expect(screen.getByText("dormant")).toBeInTheDocument();
    expect(screen.getByText(/^Not open yet\. Mira_P can ask Dave/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Show (Tree|it)/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Gate 3 · BlockyFox/ }));
    expect(screen.getByText(/in the Nether · X \u221220 · Z 40/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show it in the Nether" }));
    expect(screen.getByRole("radio", { name: "Nether" })).toHaveAttribute("aria-checked", "true");
  });

  it("a beacon from the list: protecting, its square to the block, and its neighbour", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^No-Spawn Beacon · Mira_P/ }));
    expect(screen.getByText("protecting")).toBeInTheDocument();
    expect(screen.getByText(/placed by Mira_P/)).toBeInTheDocument();
    expect(screen.getByText("48 × 48 blocks, floor to sky")).toBeInTheDocument();
    expect(
      screen.getByText(/overlaps 1 other beacon\. Wild and hostile mobs vanish/),
    ).toBeInTheDocument();
  });

  it("draws every player above every beacon and gate, and above the selection ring", async () => {
    vi.stubGlobal("Path2D", class {});
    const log: [string, unknown[]][] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (t, k: string) =>
        k in t
          ? t[k]
          : k === "measureText"
            ? (x: string) => ({ width: x.length * 6 })
            : (...a: unknown[]) => {
                log.push([k, a]);
              },
      set: (t, k: string, v) => {
        t[k] = v;
        return true;
      },
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as never);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 390,
      bottom: 844,
      width: 390,
      height: 844,
      toJSON: () => ({}),
    });
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Gate 1 · BlockyFox/ }));
    const lastFrame = () => log.slice(log.map(([k]) => k).lastIndexOf("setTransform"));
    const at = (f: [string, unknown[]][], pick: (k: string, a: unknown[]) => boolean) =>
      f.flatMap(([k, a], i) => (pick(k, a) ? [i] : []));
    await waitFor(() => {
      const f = lastFrame();
      // A player's disc: the 9.5-px fill or outline on its halo.
      expect(at(f, (k, a) => k === "arc" && a[2] === 9.5).length).toBeGreaterThan(0);
      // A violet-ringed gate tile and the tapped gate's ring are both drawn.
      expect(at(f, (k, a) => k === "roundRect" && a[2] === 21).length).toBeGreaterThan(0);
      expect(at(f, (k, a) => k === "arc" && a[2] === 18).length).toBeGreaterThan(0);
    });
    const f = lastFrame();
    const discs = at(f, (k, a) => k === "arc" && a[2] === 9.5);
    const tiles = at(f, (k, a) => k === "roundRect" && a[2] === 21);
    const rings = at(f, (k, a) => k === "arc" && a[2] === 18);
    expect(Math.max(...tiles, ...rings)).toBeLessThan(Math.min(...discs));
  });

  it("beds: the row says they come on a tap, and every respawn point is listed", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    const row = screen.getByRole("switch", { name: "Spawn · beds on tap" });
    expect(row).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByText(
        "the world spawn, in the Overworld · a player's bed shows when you tap them or one of their deaths",
      ),
    ).toBeInTheDocument();
    for (const name of [
      /^BlockyFox's bed/,
      /^Mira_P's respawn anchor/,
      /^Pebble_J's spawn point/,
    ]) {
      expect(await screen.findByRole("button", { name })).toBeInTheDocument();
    }
    // Known to have none: said, but nothing to fly to.
    expect(screen.getByText("Steve42 · no bed set")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Steve42 · no bed set/ })).toBeNull();
    expect(screen.getByText(/respawns at the world spawn · X 32 · Z \u22128/)).toBeInTheDocument();
    expect(screen.getByText("No bed seen there")).toBeInTheDocument();
    expect(screen.getByText("Respawned here")).toBeInTheDocument();
  });

  it("a bed from the list: its card, the deaths its player woke at it, as the pack sent it", async () => {
    withLog = true;
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^BlockyFox's bed/ }));
    const card = document.querySelector(".mc-map-sel") as HTMLElement;
    await waitFor(() => expect(card).toHaveTextContent(/Respawned here after/));
    expect(card).toHaveTextContent("BlockyFox's bed");
    expect(within(card).getByText("respawn point")).toBeInTheDocument();
    expect(card).toHaveTextContent(
      /Respawned here after 3 deaths since it was set · burned in lava/,
    );
    expect(card).toHaveTextContent("As the pack last sent it.");
    expect(card).toHaveTextContent(/BlockyFox is \d+ blocks [a-z-]+ of it · online/);

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Pebble_J's spawn point/ }));
    const unseen = document.querySelector(".mc-map-sel") as HTMLElement;
    expect(within(unseen).getByText("not seen there")).toBeInTheDocument();
    expect(unseen).toHaveTextContent(
      /No bed or respawn anchor was seen at this point when the pack last looked/,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Mira_P's respawn anchor/ }));
    expect(screen.getByRole("radio", { name: "Nether" })).toHaveAttribute("aria-checked", "true");
  });

  it("tapping a player adds their bed line and Show the bed; a death says where they woke", async () => {
    withLog = true;
    vi.stubGlobal("Path2D", class {});
    const log: [string, unknown[]][] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get: (t, k: string) =>
        k in t
          ? t[k]
          : k === "measureText"
            ? (x: string) => ({ width: x.length * 6 })
            : (...a: unknown[]) => {
                log.push([k, a]);
              },
      set: (t, k: string, v) => {
        t[k] = v;
        return true;
      },
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as never);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 390,
      bottom: 844,
      width: 390,
      height: 844,
      toJSON: () => ({}),
    });
    const map = await openMap();
    const lastFrame = () => log.slice(log.map(([k]) => k).lastIndexOf("setTransform"));
    // A player's disc carries their initial: BlockyFox's "B".
    const disc = async (initial: string) => {
      let at: [number, number] | null = null;
      await waitFor(() => {
        const hit = lastFrame().find(([k, a]) => k === "fillText" && a[0] === initial);
        expect(hit).toBeTruthy();
        at = [Number(hit?.[1][1]), Number(hit?.[1][2]) - 0.5];
      });
      return at as unknown as [number, number];
    };
    const [bx, by] = await disc("B");
    tapAt(map, bx, by);
    const card = () => document.querySelector(".mc-map-sel") as HTMLElement;
    await waitFor(() => expect(card()).toHaveTextContent("BlockyFox"));
    expect(card()).toHaveTextContent(/Bed · \d+ blocks [a-z-]+ · X \u2212136 · Z 94/);
    // The bed is drawn now: a tile with Lucide's bed in the frame.
    await waitFor(() =>
      expect(lastFrame().some(([k, a]) => k === "roundRect" && a[2] === 21)).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Show the bed" }));
    await waitFor(() => expect(card()).toHaveTextContent("BlockyFox's bed"));

    // A death: its skull (the 9-px rose disc), tapped, says only where its player woke,
    // and draws the curve there.
    fireEvent.click(screen.getByRole("button", { name: "Show everyone" }));
    let skull: [number, number] | null = null;
    await waitFor(() => {
      const hit = lastFrame().find(([k, a]) => k === "arc" && a[2] === 9);
      expect(hit).toBeTruthy();
      skull = [Number(hit?.[1][0]), Number(hit?.[1][1])];
    });
    const [kx, ky] = skull as unknown as [number, number];
    tapAt(map, kx, ky);
    await waitFor(() =>
      expect(card()).toHaveTextContent(
        /Respawned (at their bed|at their respawn anchor|at their spawn point|at the world spawn| · )/,
      ),
    );
    expect(card()).not.toHaveTextContent(/missing|blocked/);
  });

  it("picks the base layer: Surface (true colour) by default, Biomes on a tap, remembered", async () => {
    localStorage.removeItem("jbrain.minecraftMap.layer");
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    const surface = screen.getByRole("radio", { name: /^Surface/ });
    const biomes = screen.getByRole("radio", { name: /^Biomes/ });
    expect(surface).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/in the colour of its top block/)).toBeInTheDocument();
    fireEvent.click(biomes);
    expect(biomes).toHaveAttribute("aria-checked", "true");
    expect(surface).toHaveAttribute("aria-checked", "false");
    expect(localStorage.getItem("jbrain.minecraftMap.layer")).toBe("biomes");
    expect(screen.getByRole("radio", { name: /^Height/ })).toBeDisabled();
    localStorage.removeItem("jbrain.minecraftMap.layer");
  });

  it("back returns to the Minecraft screen", async () => {
    await openMap();
    fireEvent.click(screen.getAllByRole("button", { name: "Back" }).at(-1) as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("application")).toBeNull());
  });
});
