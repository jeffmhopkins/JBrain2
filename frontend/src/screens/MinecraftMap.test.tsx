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
import { mcMapInfo, mcMapPlayers, mcMapPoint, mcMapStructures } from "../minecraftMapFixtures";
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

  it("back returns to the Minecraft screen", async () => {
    await openMap();
    fireEvent.click(screen.getAllByRole("button", { name: "Back" }).at(-1) as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("application")).toBeNull());
  });
});
