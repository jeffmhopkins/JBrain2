import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftMapShare } from "../api/client";
import {
  mcAllowlist,
  mcPlayers,
  mcServer,
  mcServerSettings,
  mcStatus,
  mcVersion,
  mcWorlds,
} from "../minecraftFixtures";
import {
  mcMapInfo,
  mcMapPlayers,
  mcMapPoint,
  mcMapStructures,
  mcTimeline,
} from "../minecraftMapFixtures";
import { mcShareUrl, parseMcSharePath, shareMap } from "../minecraftMapSource";
import { McShareApp } from "./McShareApp";
import { MinecraftScreen } from "./MinecraftScreen";

const log = () =>
  mcTimeline("slot2", 0, 10, {
    include: ["trails", "events", "sessions"],
    players: [],
    dim: "overworld",
  });

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();
const calls = (prefix: string) =>
  fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.startsWith(prefix));

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  // jsdom draws nothing; what's under test is what's asked for and shown.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  fetchMock.mockReset();
  window.history.replaceState(null, "", "/");
});

describe("the map share link", () => {
  it("parses only /mc/s/{token} and builds the link on this origin", () => {
    expect(parseMcSharePath("/mc/s/AbC_12-x")).toBe("AbC_12-x");
    expect(parseMcSharePath("/mc/s/AbC/")).toBe("AbC");
    expect(parseMcSharePath("/mc/s/")).toBeNull();
    expect(parseMcSharePath("/mc/s/a/b")).toBeNull();
    expect(parseMcSharePath("/share/abc")).toBeNull();
    expect(mcShareUrl("t0k")).toBe(`${window.location.origin}/mc/s/t0k`);
  });

  it("a shared source reads only the token's routes, whatever slot the page passes", async () => {
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://box").pathname;
      if (path.endsWith("/map/players")) return json(mcMapPlayers("slot2", true));
      if (path.endsWith("/timeline")) return json(log());
      return json({});
    });
    const src = shareMap("tok");
    await src.info("slot9", "nether");
    await src.players("slot9");
    await src.structures("slot9", "overworld");
    await src.point("slot9", "overworld", 3, -4);
    await src.timeline("slot9", 0, 10, { include: ["explored"], players: ["x1"] });
    const asked = fetchMock.mock.calls.map(([u]) => String(u));
    expect(asked).toEqual([
      "/api/mc-share/tok/map/info?dim=nether",
      "/api/mc-share/tok/map/players",
      "/api/mc-share/tok/map/structures?dim=overworld",
      "/api/mc-share/tok/map/point?dim=overworld&x=3&z=-4",
      "/api/mc-share/tok/timeline?from=0&to=10&include=explored&players=x1",
    ]);
    expect(asked.join()).not.toContain("slot");
    expect(src.tileUrl("slot9", "the_end", 2, -1, 5, "biomes")).toBe(
      "/api/mc-share/tok/map/tile/the_end/2/-1/5.png?layer=biomes",
    );
    expect(src.sweep).toBeNull();
  });
});

describe("sharing from the owner's map", () => {
  let links: (MinecraftMapShare & { token: string })[];
  let minted: unknown[];

  beforeEach(() => {
    links = [];
    minted = [];
    const server = mcServer();
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://box");
      const path = url.pathname;
      const q = url.searchParams;
      const slot = q.get("slot") ?? "slot1";
      const dim = (q.get("dim") ?? "overworld") as "overworld";
      if (path === "/api/minecraft") return json(mcStatus(server));
      if (path.startsWith("/api/minecraft/version")) return json(mcVersion());
      if (path === "/api/minecraft/players") return json(mcPlayers());
      if (path === "/api/minecraft/worlds") return json(mcWorlds());
      if (path === "/api/minecraft/allowlist") return json(mcAllowlist());
      if (path === "/api/minecraft/server-settings") return json(mcServerSettings());
      if (path === "/api/minecraft/backups") return json({ snapshots: [] });
      if (path === "/api/minecraft/map/info") return json(mcMapInfo(slot, dim));
      if (path === "/api/minecraft/map/players") return json(mcMapPlayers(slot, true));
      if (path === "/api/minecraft/map/structures") return json(mcMapStructures(slot, dim));
      if (path === "/api/minecraft/map/point") return json(mcMapPoint(slot, dim, 0, 0));
      if (path === "/api/minecraft/shares" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { slot: string; xuid: string };
        minted.push(body);
        const who = mcMapPlayers(body.slot, true).players.find((p) => p.xuid === body.xuid);
        const made = {
          id: "s1",
          slot: body.slot,
          xuid: body.xuid,
          gamertag: who?.gamertag ?? "?",
          created_at: 1,
          last_viewed_at: null,
          view_count: 0,
          token: "SeCrEt_tok",
        };
        links = [made];
        return json(made, 201);
      }
      if (path === "/api/minecraft/shares") return json(links);
      if (path === "/api/minecraft/shares/s1" && init?.method === "DELETE") {
        links = [];
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    });
  });

  async function openShare() {
    render(<MinecraftScreen />);
    const row = await screen.findByRole("button", { name: /^Map/ });
    await waitFor(() => expect(row).toHaveTextContent("Castle Hill"));
    fireEvent.click(row);
    await screen.findByText("Live · 2 on");
    fireEvent.click(screen.getByRole("button", { name: "Share this map" }));
    return screen.findByRole("dialog", { name: "Share the map" });
  }

  it("picks a gamertag, makes a link for this world, copies it, and revokes it", async () => {
    const sheet = await openShare();
    const pick = within(sheet).getByRole("radio", { name: /Mira_P/ });
    fireEvent.click(pick);
    fireEvent.click(within(sheet).getByRole("button", { name: "Create link" }));

    const url = await within(sheet).findByLabelText(/Send this link · opens on Mira_P/);
    expect(url).toHaveValue(`${window.location.origin}/mc/s/SeCrEt_tok`);
    const mira = mcMapPlayers("slot2", true).players.find((p) => p.gamertag === "Mira_P");
    expect(minted).toEqual([{ slot: "slot2", xuid: mira?.xuid }]);

    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Copy link" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(String((url as HTMLInputElement).value)),
    );
    expect(await within(sheet).findByRole("button", { name: "Copied" })).toBeInTheDocument();

    expect(within(sheet).getByText(/Castle Hill · not opened yet/)).toBeInTheDocument();
    fireEvent.click(within(sheet).getByRole("button", { name: "Revoke the link for Mira_P" }));
    await waitFor(() =>
      expect(within(sheet).queryByRole("button", { name: /Revoke the link/ })).toBeNull(),
    );
    expect(calls("/api/minecraft/shares/s1")).toHaveLength(1);
  });

  it("says what the link gives away before it is made", async () => {
    const sheet = await openShare();
    expect(sheet).toHaveTextContent(
      "every player's live position, travel history and timeline, opened on the player you pick",
    );
    expect(sheet).toHaveTextContent("It works until you revoke it.");
    // Everyone who has been in the world, not only who's on now.
    expect(within(sheet).getAllByRole("radio")).toHaveLength(
      mcMapPlayers("slot2", true).players.length,
    );
  });
});

describe("the shared map, as the visitor sees it", () => {
  function serveShare(view: Response | (() => Response)) {
    fetchMock.mockImplementation(async (input) => {
      const url = new URL(String(input), "http://box");
      const path = url.pathname;
      const dim = (url.searchParams.get("dim") ?? "overworld") as "overworld";
      if (path === "/api/mc-share/tok") return typeof view === "function" ? view() : view;
      if (path === "/api/mc-share/tok/map/info") return json(mcMapInfo("slot2", dim));
      if (path === "/api/mc-share/tok/map/players") return json(mcMapPlayers("slot2", true));
      if (path === "/api/mc-share/tok/map/structures") {
        return json(mcMapStructures("slot2", dim));
      }
      if (path === "/api/mc-share/tok/timeline") return json(log());
      return new Response(null, { status: 404 });
    });
  }

  it("opens view-only on the shared player, with no way back or out", async () => {
    window.history.replaceState(null, "", "/mc/s/tok");
    const fox = mcMapPlayers("slot2", true).players.find((p) => p.gamertag === "BlockyFox");
    serveShare(
      json({
        slot: "slot2",
        name: "Castle Hill",
        active: true,
        last_played: null,
        focus: { xuid: fox?.xuid, gamertag: "BlockyFox" },
      }),
    );
    render(<McShareApp />);
    expect(await screen.findByRole("application")).toBeInTheDocument();
    expect(screen.getByText("View only")).toBeInTheDocument();
    // The shared player is selected: their card is open, with Follow.
    expect(await screen.findByRole("button", { name: /Follow/ })).toBeInTheDocument();
    expect(document.querySelector(".mc-map-sel")).toHaveTextContent("BlockyFox");
    // Dave has their Tricorder on something: the card says how far and which way.
    expect(document.querySelector(".mc-map-det-tri")).toHaveTextContent(
      /Tricorder · Diamonds · \d+ blocks north-east · y -58/,
    );
    // Beds are the owner's (§M8): no bed line, no Show the bed, and the row is the spawn's.
    expect(document.querySelector(".mc-map-sel")).not.toHaveTextContent(/Bed ·|No bed set/);
    expect(screen.queryByRole("button", { name: /^Show the / })).toBeNull();
    expect(screen.getByRole("switch", { name: "World spawn" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /beds/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Share this map" })).toBeNull();
    expect(screen.queryByRole("button", { name: /World: .* change/ })).toBeNull();
    expect(screen.getByText("Castle Hill")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Timeline" })).toBeInTheDocument();
    // Every read went through the token; nothing reached an owner route.
    const asked = fetchMock.mock.calls.map(([u]) => String(u));
    expect(asked.length).toBeGreaterThan(2);
    expect(asked.every((u) => u.startsWith("/api/mc-share/tok"))).toBe(true);
  });

  it("an ended link says so and offers nothing else", async () => {
    window.history.replaceState(null, "", "/mc/s/tok");
    serveShare(json({ detail: "unknown or revoked share link" }, 404));
    render(<McShareApp />);
    expect(
      await screen.findByRole("heading", { name: "This map link was turned off" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("application")).toBeNull();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("an unreachable box is not called an ended link", async () => {
    window.history.replaceState(null, "", "/mc/s/tok");
    serveShare(json({ detail: "Minecraft sidecar unreachable" }, 502));
    render(<McShareApp />);
    expect(
      await screen.findByRole("heading", { name: "The map isn't answering" }),
    ).toBeInTheDocument();
  });
});
