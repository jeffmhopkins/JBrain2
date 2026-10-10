// Ops' Minecraft entry and its other stop paths (README finding 16): the Minecraft tile's glance,
// and the service row's Stop/Restart and the Services page's Restart all warning before they
// bounce players — with the same words the Minecraft screen uses.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftStatus, MinecraftVersion, OpsStatus } from "../api/client";
import { LATEST, RUNNING, mcBehind, mcServer, mcStatus, mcVersion } from "../minecraftFixtures";
import { OpsScreen } from "./OpsScreen";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const OPS_STATUS: OpsStatus = {
  containers: [
    {
      service: "api",
      state: "running",
      health: "healthy",
      started_at: "2026-10-10T08:00:00Z",
      image: "jbrain/api:edge",
    },
    {
      service: "minecraft",
      state: "running",
      health: "healthy",
      started_at: "2026-10-10T14:43:46Z",
      image: "jbrain2-minecraft:local",
    },
  ],
};

describe("OpsScreen · Minecraft", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function serve(status: MinecraftStatus, version: MinecraftVersion = mcVersion()) {
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/ops/status") return json(OPS_STATUS);
      if (path === "/api/minecraft") return json(status);
      if (path.startsWith("/api/minecraft/version")) return json(version);
      if (path.startsWith("/api/ops/logs/")) return new Response("log", { status: 200 });
      if (init?.method === "POST") return json({}, 202);
      return new Response(null, { status: 404 });
    });
  }

  const posts = () =>
    fetchMock.mock.calls
      .filter(([, init]) => init?.method === "POST")
      .map(([url, init]) => `${String(url)} ${init?.body ?? ""}`.trim());

  async function openServices() {
    fireEvent.click(await screen.findByRole("button", { name: /^Services:/ }));
  }

  it("the tile glances who's on, and opens the screen", async () => {
    serve(mcStatus());
    const open = vi.fn();
    render(<OpsScreen onOpenMinecraft={open} />);
    const tile = await screen.findByRole("button", {
      name: `Minecraft: running — ${RUNNING} · BlockyFox and Mira_P on`,
    });
    // The same green dot the launcher tile carries: a running server is news worth a glance.
    expect(tile.querySelector(".mc-tile-dot.ok")).not.toBeNull();
    fireEvent.click(tile);
    expect(open).toHaveBeenCalledOnce();
  });

  it("the tile flags that newer clients are locked out while an update waits", async () => {
    serve(mcStatus(), mcBehind());
    render(<OpsScreen onOpenMinecraft={() => {}} />);
    const tile = await screen.findByRole("button", {
      name: new RegExp(`^Minecraft: .* — update available · ${LATEST} — newer clients can't join`),
    });
    expect(tile.querySelector(".mc-tile-dot.warn")).not.toBeNull();
  });

  it("the tile is absent on a box with no Minecraft container", async () => {
    serve({ container: null, server: null, server_error: null });
    render(<OpsScreen onOpenMinecraft={() => {}} />);
    await screen.findByRole("button", { name: /^Services:/ });
    expect(screen.queryByRole("button", { name: /^Minecraft:/ })).toBeNull();
  });

  it("Restart all warns in the Dialog when Minecraft players are on", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    serve(mcStatus());
    render(<OpsScreen />);
    await openServices();
    const restartAll = await screen.findByRole("button", { name: "Restart all" });
    await waitFor(() => expect(restartAll).toBeEnabled());
    fireEvent.click(restartAll);

    const dialog = await screen.findByRole("dialog", { name: "Restart every service?" });
    expect(dialog).toHaveTextContent(
      "That includes Minecraft — 2 players (BlockyFox and Mira_P) will be disconnected for about a minute.",
    );
    expect(confirmSpy).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restart all" }));
    await waitFor(() => expect(posts()).toEqual(['/api/ops/restart {"service":"all"}']));
  });

  it("Restart all keeps its plain confirm when nobody is playing", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    serve(mcStatus(mcServer({ players: [] })));
    render(<OpsScreen />);
    await openServices();
    const restartAll = await screen.findByRole("button", { name: "Restart all" });
    await waitFor(() => expect(restartAll).toBeEnabled());
    fireEvent.click(restartAll);
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledWith("Restart ALL services?"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the minecraft service row's Stop routes through the Minecraft confirm and API", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    serve(mcStatus());
    render(<OpsScreen />);
    await openServices();
    fireEvent.click(await screen.findByRole("button", { name: /^Apps/ }));
    fireEvent.click(screen.getByText("minecraft"));
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));

    const dialog = await screen.findByRole("dialog", { name: "Stop the server?" });
    expect(dialog).toHaveTextContent(
      "2 players — BlockyFox and Mira_P — will be disconnected; the world is saved first.",
    );
    expect(confirmSpy).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(posts()).toEqual(["/api/minecraft/stop"]));
  });

  it("the minecraft service row's Restart warns too, and acts at once with nobody on", async () => {
    serve(mcStatus());
    const { unmount } = render(<OpsScreen />);
    await openServices();
    fireEvent.click(await screen.findByRole("button", { name: /^Apps/ }));
    fireEvent.click(screen.getByText("minecraft"));
    fireEvent.click(await screen.findByRole("button", { name: "Restart minecraft" }));
    expect(await screen.findByRole("dialog", { name: "Restart the server?" })).toBeTruthy();
    unmount();

    fetchMock.mockReset();
    serve(mcStatus(mcServer({ players: [] })));
    render(<OpsScreen />);
    await openServices();
    fireEvent.click(await screen.findByRole("button", { name: /^Apps/ }));
    fireEvent.click(screen.getByText("minecraft"));
    fireEvent.click(await screen.findByRole("button", { name: "Restart minecraft" }));
    await waitFor(() => expect(posts()).toEqual(["/api/minecraft/restart"]));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
