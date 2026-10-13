import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftMapPoint, MinecraftServer } from "../api/client";
import {
  mcAllowlist,
  mcGoalProgress,
  mcPlayers,
  mcServer,
  mcServerSettings,
  mcStatus,
  mcStopped,
  mcVersion,
  mcWorlds,
} from "../minecraftFixtures";
import {
  GOAL_KEYS,
  mcMapEntities,
  mcMapInfo,
  mcMapMarkers,
  mcMapPlaces,
  mcMapPlayers,
  mcMapPoint,
  mcMapStructures,
  mcMapVillager,
} from "../minecraftMapFixtures";
import { shareMap } from "../minecraftMapSource";
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

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    // jsdom draws nothing; the map's geometry is what's under test.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    server = mcServer();
    down = false;
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
      if (path === "/api/minecraft/map/places") return json(mcMapPlaces(slot));
      if (path === "/api/minecraft/map/entities") return json(mcMapEntities(slot, dim));
      if (path === "/api/minecraft/map/villager") {
        return json(mcMapVillager(slot, q.get("id") ?? ""));
      }
      if (path === "/api/minecraft/goals/progress") {
        const who = q.get("xuid") ?? "";
        return json(mcGoalProgress(GOAL_KEYS[who] ?? who));
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
    for (const name of ["Trails", "Deaths", "Beds & spawn", "Explored by…"]) {
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
    // Saved places arrived (minecraft-map-layers): nothing on the map is "later" now.
    expect(screen.queryByText("later")).toBeNull();
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
    expect(screen.getByText(/in the Nether · X \u221220 · Z 40$/)).toBeInTheDocument();
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

  it("saved places: on, listed by player, and a card with no Tricorder button", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    const row = screen.getByRole("switch", { name: "Saved places" });
    expect(row).toHaveAttribute("aria-checked", "true");
    expect(
      await screen.findByText(/^7 in this world · BlockyFox.3 · Mira_P.2 · Steve42.2/),
    ).toBeInTheDocument();
    expect(screen.getByText("BlockyFox's places")).toBeInTheDocument();
    expect(screen.getByText(/^Lookout$/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Home X.70 · Z.12 · protected/ }));
    expect(
      screen.getByText(/^BlockyFox's saved place · saved .* · in the Overworld$/),
    ).toBeInTheDocument();
    expect(screen.getByText("Protected")).toBeInTheDocument();
    expect(screen.getByText(/by their No-Spawn Beacon at X.90 · Z.\u221210/)).toBeInTheDocument();
    // This place's share of the goal, never the goal's total (the review's S2).
    expect(
      await screen.findByText(/5 of the 23 iron blocks are in chests here/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/toward its nearest saved place, or toward BlockyFox/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Tricorder/ })).toBeNull();
    const asked = calls("/api/minecraft/goals/progress");
    expect(asked).toEqual(["/api/minecraft/goals/progress?xuid=2535411"]);

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(screen.getByRole("button", { name: /^Tree farm/ }));
    expect(screen.getByText(/^Wormhole gate.1 leads here, from X.30 · Z.14$/)).toBeInTheDocument();
    expect(screen.getByText("Not protected · no No-Spawn Beacon covers it")).toBeInTheDocument();
    // Back to Home: its count is kept for the open map, not read again.
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(screen.getByRole("button", { name: /^Home/ }));
    expect(await screen.findByText(/5 of the 23 iron blocks/)).toBeInTheDocument();
    expect(calls("/api/minecraft/goals/progress")).toHaveLength(1);

    // A place in the Nether switches the map there and keeps its card.
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(screen.getByRole("button", { name: /^Nether base/ }));
    expect(screen.getByRole("radio", { name: "Nether" })).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByText(/^BlockyFox's saved place · moved .* · in the Nether$/),
    ).toBeInTheDocument();
    expect(calls("/api/minecraft/map/places")[0]).toBe("/api/minecraft/map/places?slot=slot2");
  });

  it("villagers: off by default; on, a Mending chip ranks them and a card lists its trades", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    const villagers = screen.getByRole("switch", { name: "Villagers" });
    expect(villagers).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "Pets & named mobs" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(calls("/api/minecraft/map/entities")).toEqual([]);
    fireEvent.click(villagers);
    // A live world: no minutes, only that it's re-read (the review's S3).
    expect(
      await screen.findByText(
        "31 in 2 villages · as of the world's last save, re-read every minute · a village is one tile",
      ),
    ).toBeInTheDocument();
    expect(calls("/api/minecraft/map/entities")[0]).toBe(
      "/api/minecraft/map/entities?slot=slot2&dim=overworld",
    );
    expect(screen.getByRole("radio", { name: "All jobs" })).toHaveAttribute("aria-checked", "true");
    // The owner asked for no "Read the world again": the automatic re-read only.
    expect(screen.queryByRole("button", { name: /Read the world again/ })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Sells Mending" }));
    const ranked = screen.getByLabelText("Nearest first");
    const rows = within(ranked).getAllByRole("button");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(/^1Librarian · Master.*sells Mending · job locked in/);
    expect(rows[1]).toHaveTextContent(/^2Librarian · Expert\d+.blocks north-east of BlockyFox/);
    fireEvent.click(rows[1] as HTMLElement);
    const card = document.querySelector(".mc-map-sel") as HTMLElement;
    expect(within(card).getByText("Librarian · Expert")).toBeInTheDocument();
    expect(within(card).getByText("job locked in")).toBeInTheDocument();
    expect(screen.getByText(/in the village of.23/)).toBeInTheDocument();
    expect(await screen.findByText("Apprentice")).toBeInTheDocument();
    expect(screen.getByText(/^Mending.I$/)).toBeInTheDocument();
    expect(screen.getByText("used up · restocks")).toBeInTheDocument();
    expect(
      screen.getByText(/^Its trades now, levels Novice to Expert\. Master trades/),
    ).toBeInTheDocument();
    expect(calls("/api/minecraft/map/villager")[0]).toMatch(/\?slot=slot2&id=-8589934000$/);
  });

  it("pets: a player's chip lists theirs nearest first; a card says whose and how old", async () => {
    await openMap();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(screen.getByRole("switch", { name: "Pets & named mobs" }));
    expect(
      await screen.findByText(
        "6 pets · 2 named · as of the world's last save, re-read every minute",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Steve42's" }));
    const list = screen.getByLabelText("Their pets, nearest first");
    const rows = within(list).getAllByRole("button");
    expect(rows.map((r) => r.querySelector("b")?.textContent)).toEqual(["Biscuit", "Luna"]);
    fireEvent.click(rows[1] as HTMLElement);
    expect(screen.getByText("Steve42's dog · black · sitting")).toBeInTheDocument();
    expect(
      screen.getByText(
        /^Where Luna was as of the world's last save, re-read every minute\. A sitting pet stays put/,
      ),
    ).toBeInTheDocument();
  });

  it("a villager or pet card from the chat opens the map with that layer on", async () => {
    render(
      <MinecraftScreen
        openMap={{
          slot: "slot2",
          dim: "overworld",
          x: 52,
          z: 52,
          zoom: -1,
          mark: true,
          show: "pets",
        }}
      />,
    );
    await screen.findByRole("application");
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("switch", { name: "Pets & named mobs" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("switch", { name: "Villagers" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("a share link has none of them: places, villagers and pets are owner-only", () => {
    const src = shareMap("tok");
    expect([src.places, src.entities, src.villager, src.goals]).toEqual([null, null, null, null]);
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
