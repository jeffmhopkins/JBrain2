import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MinecraftPlayers, MinecraftStatus, MinecraftVersion } from "../api/client";
import {
  LATEST,
  RUNNING,
  mcBehind,
  mcPlayers,
  mcServer,
  mcStatus,
  mcStopped,
  mcUpdate,
  mcVersion,
} from "../minecraftFixtures";
import { MinecraftScreen } from "./MinecraftScreen";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Routes {
  status: MinecraftStatus | (() => MinecraftStatus);
  version?: MinecraftVersion;
  players?: MinecraftPlayers | (() => MinecraftPlayers);
  /** Overrides the answer to a POST; undefined falls through to the default 202. */
  post?: (path: string) => Promise<Response> | undefined;
}

describe("MinecraftScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function serve(routes: Routes) {
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/api/minecraft") {
        return json(typeof routes.status === "function" ? routes.status() : routes.status);
      }
      if (path.startsWith("/api/minecraft/version")) return json(routes.version ?? mcVersion());
      if (path === "/api/minecraft/players") {
        return json(
          typeof routes.players === "function" ? routes.players() : (routes.players ?? mcPlayers()),
        );
      }
      if (method === "POST") {
        const answer = routes.post?.(path);
        if (answer) return answer;
      }
      if (path === "/api/minecraft/update" && method === "POST") {
        return json(mcUpdate({ state: "backing_up", from: RUNNING, to: LATEST }), 202);
      }
      if (path.startsWith("/api/minecraft/") && method === "POST") {
        return json({ action: path.split("/").pop() }, 202);
      }
      if (path === "/api/minecraft/settings" && method === "PUT") {
        return json(JSON.parse(String(init?.body)));
      }
      return new Response(null, { status: 404 });
    });
  }

  const posted = () =>
    fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => String(url));

  it("running with two players: status, online timers, C's player table and server facts", async () => {
    serve({ status: mcStatus() });
    render(<MinecraftScreen />);

    expect(await screen.findByText("running")).toBeInTheDocument();
    expect(screen.getByText("up 3h 12m")).toBeInTheDocument();
    expect(screen.getByText("2 on now")).toBeInTheDocument();
    expect(screen.getByText("42 min")).toBeInTheDocument();
    expect(screen.getByText("7 min")).toBeInTheDocument();

    // Players: time played, sessions, first seen, last seen with the day.
    const table = await screen.findByRole("table", { name: "Time played" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(5);
    expect(within(rows[1] as HTMLElement).getByText("BlockyFox")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("on now")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("9h 40m")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("12 sessions")).toBeInTheDocument();
    expect(within(table).getAllByText(/^since /)).toHaveLength(4);
    expect(within(rows[3] as HTMLElement).getByText(/^(yesterday|today)$/)).toBeInTheDocument();

    // Server facts and how to join.
    expect(screen.getByText("Survival · Normal")).toBeInTheDocument();
    expect(screen.getByText("off — anyone on the network can join")).toBeInTheDocument();
    expect(screen.getByText("Friends → LAN Games → JBrain")).toBeInTheDocument();
    expect(screen.getByText("192.168.1.40")).toBeInTheDocument();
    expect(screen.getByText("19132")).toBeInTheDocument();

    // Lifetime stats are the honest placeholder, never numbers.
    expect(screen.getAllByText("Arrives with the companion add-on.").length).toBeGreaterThan(0);
    expect(screen.getByText("not available yet")).toBeInTheDocument();
  });

  it("Stop with players on confirms in the Dialog, naming who is disconnected", async () => {
    serve({ status: mcStatus() });
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));

    const dialog = await screen.findByRole("dialog", { name: "Stop the server?" });
    expect(dialog).toHaveTextContent(
      "2 players — BlockyFox and Mira_P — will be disconnected; the world is saved first.",
    );
    expect(posted()).toEqual([]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(posted()).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    const again = await screen.findByRole("dialog", { name: "Stop the server?" });
    fireEvent.click(within(again).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(posted()).toEqual(["/api/minecraft/stop"]));
  });

  it("Restart acts at once with nobody on, and reads as restarting through the server's states", async () => {
    let state: "running" | "stopping" | "stopped" | "starting" = "running";
    serve({ status: () => mcStatus(mcServer({ players: [], state })) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MinecraftScreen />);
    expect(await screen.findByText("Nobody on right now.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(posted()).toEqual(["/api/minecraft/restart"]));
    expect(screen.queryByRole("dialog")).toBeNull();

    // The server's own stopping → stopped → starting read as one restart, not a stop.
    for (const next of ["stopping", "stopped", "starting"] as const) {
      state = next;
      await act(() => vi.advanceTimersByTimeAsync(5000));
      expect(await screen.findByText("restarting")).toBeInTheDocument();
      expect(screen.getByText("saving the world, then starting again")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Restarting…" })).toBeDisabled();
    }
    state = "running";
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText("running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart" })).toBeEnabled();

    // A later plain stop is a stop, not a leftover restart.
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    state = "stopping";
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText("stopping")).toBeInTheDocument();
  });

  it("with auto-update on and an update waiting, Restart confirms even with nobody on", async () => {
    serve({
      status: mcStatus(mcServer({ players: [], auto_update: true })),
      version: mcBehind(),
    });
    render(<MinecraftScreen />);
    await screen.findByText(`${LATEST} is out.`);
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = await screen.findByRole("dialog", { name: `Restart and update to ${LATEST}?` });
    expect(dialog).toHaveTextContent(`after a backup (pre-update-${RUNNING})`);
    expect(posted()).toEqual([]);
  });

  it("stopped: Start only, facts still readable, and the online list says so", async () => {
    serve({ status: mcStatus(mcStopped({ auto_update: true })) });
    render(<MinecraftScreen />);
    expect(await screen.findByText("stopped")).toBeInTheDocument();
    // The container keeps running, so world, join details and auto-update stay readable.
    expect(screen.getByText("Survival · Normal")).toBeInTheDocument();
    expect(screen.getByText("192.168.1.40")).toBeInTheDocument();
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("The server is stopped.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start server" }));
    await waitFor(() => expect(posted()).toEqual(["/api/minecraft/start"]));
  });

  it("update available: banner, verbatim changelog, and Update Minecraft always confirms", async () => {
    let update = mcUpdate();
    serve({ status: () => mcStatus(mcServer({ update })), version: mcBehind() });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MinecraftScreen />);

    expect(await screen.findByText(`${LATEST} is out.`)).toBeInTheDocument();
    expect(
      screen.getByText("From Mojang's changelog · first 4 lines, verbatim"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Minecraft Bedrock Edition 26.60 Changelog/)).toHaveTextContent(
      "posted Oct 8",
    );
    expect(
      screen.getByText("Mobs can no longer push players through closed trapdoors"),
    ).toBeTruthy();
    expect(screen.getByRole("link", { name: /Release notes/ })).toHaveAttribute(
      "href",
      "https://feedback.minecraft.net/hc/en-us/articles/26-60",
    );

    fireEvent.click(screen.getByRole("button", { name: "Update Minecraft" }));
    const dialog = await screen.findByRole("dialog", { name: `Update Minecraft to ${LATEST}?` });
    expect(dialog).toHaveTextContent(`A backup (pre-update-${RUNNING}) is taken first`);
    expect(dialog).toHaveTextContent("players stay on while");
    fireEvent.click(within(dialog).getByRole("button", { name: "Update" }));
    await waitFor(() => expect(posted()).toContain("/api/minecraft/update"));

    // The steps advance as the wrapper reports them.
    update = mcUpdate({
      state: "downloading",
      from: RUNNING,
      to: LATEST,
      started_at: Date.now() / 1000,
    });
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText(`Downloading ${LATEST}`)).toBeInTheDocument();
    expect(screen.getByText(/Players stay on while it backs up and downloads/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart" })).toBeDisabled();
    expect(
      screen.getByText("Busy updating — Start, Stop and Restart come back when it's done."),
    ).toBeInTheDocument();

    update = { ...update, state: "restarting" };
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText(/Restarting — players are disconnected/)).toBeInTheDocument();

    update = { ...update, state: "done", finished_at: Date.now() / 1000 };
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText("Done — players can rejoin.")).toBeInTheDocument();
  });

  it("notes not published yet: says so and links Mojang's update page", async () => {
    serve({ status: mcStatus(), version: mcBehind({ notes: null }) });
    render(<MinecraftScreen />);
    expect(
      await screen.findByText(/Release notes not published yet — Mojang hasn't posted the 26.60/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Minecraft update page/ })).toHaveAttribute(
      "href",
      "https://aka.ms/MinecraftUpdate",
    );
    // The update still works without notes.
    expect(screen.getByRole("button", { name: "Update Minecraft" })).toBeEnabled();
  });

  it("update failed: still on the old version, backup kept, the lockout line, Try again", async () => {
    const failed = mcUpdate({
      state: "failed",
      from: RUNNING,
      to: LATEST,
      backup: `world-pre-update-${RUNNING}.mcworld`,
      error: `bedrock-server-${LATEST}.zip — checksum mismatch after download; nothing was installed`,
      started_at: 100,
      finished_at: 200,
    });
    serve({ status: mcStatus(mcServer({ update: failed })), version: mcBehind() });
    render(<MinecraftScreen />);

    expect(
      await screen.findByText(
        `Update failed — still on ${RUNNING}. Newer clients still can't join.`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/checksum mismatch after download/)).toBeInTheDocument();
    expect(screen.getByText(`world-pre-update-${RUNNING}.mcworld`)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`updated to ${LATEST} still can't join`))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("dialog", { name: `Update Minecraft to ${LATEST}?` }),
    ).toBeTruthy();
  });

  it("rolled back: its own headline, the backup kept, the lockout line, the failed step", async () => {
    const rolled = mcUpdate({
      state: "rolled_back",
      from: RUNNING,
      to: LATEST,
      backup: `world-pre-update-${RUNNING}.mcworld`,
      error: `bedrock_server ${LATEST} didn't start within 2 minutes`,
      started_at: 100,
      finished_at: 300,
    });
    serve({ status: mcStatus(mcServer({ update: rolled })), version: mcBehind() });
    render(<MinecraftScreen />);
    expect(
      await screen.findByText(
        `${LATEST} wouldn't start — rolled back to ${RUNNING}; world restored from the backup (kept).`,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        `${LATEST} wouldn't start — back on ${RUNNING}. Newer clients still can't join.`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`updated to ${LATEST} still can't join`))).toBeTruthy();
    expect(screen.getByText(`Restarting — ${LATEST} wouldn't start, rolled back`)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("a failed backup says nothing was installed", async () => {
    const failed = mcUpdate({
      state: "failed",
      from: RUNNING,
      to: LATEST,
      error: "backup failed: disk full",
      started_at: 100,
    });
    serve({ status: mcStatus(mcServer({ update: failed })), version: mcBehind() });
    render(<MinecraftScreen />);
    expect(
      await screen.findByText(/No backup was taken, so nothing was installed\./),
    ).toBeInTheDocument();
  });

  it("updating a stopped server keeps it stopped", async () => {
    let update = mcUpdate();
    serve({
      status: () => mcStatus(mcStopped({ update })),
      version: mcBehind(),
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Update Minecraft" }));
    const dialog = await screen.findByRole("dialog", { name: `Update Minecraft to ${LATEST}?` });
    expect(dialog).toHaveTextContent("the server stays stopped until you start it");
    fireEvent.click(within(dialog).getByRole("button", { name: "Update" }));

    update = mcUpdate({ state: "restarting", from: RUNNING, to: LATEST, started_at: 300 });
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText("Installing — server stays stopped")).toBeInTheDocument();
    expect(
      screen.getByText("Installing while stopped — the server stays stopped afterwards."),
    ).toBeInTheDocument();
  });

  it("installing claims nothing: no version, world, join details or update controls", async () => {
    serve({
      status: mcStatus(
        mcServer({ state: "installing", version: null, players: [], uptime_s: null }),
      ),
      players: { players: [], stats_available: false },
    });
    render(<MinecraftScreen />);
    expect(await screen.findByText("installing")).toBeInTheDocument();
    expect(screen.getByText("first boot — downloading the server")).toBeInTheDocument();
    expect(
      screen.getByText("Not installed yet — updates are checked once the server is installed."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Not installed yet — join details appear once the server is up."),
    ).toBeInTheDocument();
    expect(await screen.findByText("No one has played yet.")).toBeInTheDocument();
    expect(screen.queryByText("world")).toBeNull();
    expect(screen.queryByText(/192\.168/)).toBeNull();
    expect(screen.queryByText(/%/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Check for updates/ })).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("button", { name: /Start|Stop|Restart/ })).toBeNull();
  });

  it("install failed: the error verbatim and Retry install, nothing else claimed", async () => {
    const error = `bedrock-server-${RUNNING}.zip — download failed: HTTP 503 from www.minecraft.net`;
    serve({
      status: mcStatus(
        mcServer({ state: "install_failed", install_error: error, players: [], uptime_s: null }),
      ),
      players: { players: [], stats_available: false },
    });
    render(<MinecraftScreen />);
    expect(await screen.findByText("install failed")).toBeInTheDocument();
    expect(screen.getByText(error)).toBeInTheDocument();
    expect(screen.queryByText(/192\.168/)).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry install" }));
    await waitFor(() => expect(posted()).toEqual(["/api/minecraft/retry-install"]));
  });

  it("a container that is down says so and offers Start", async () => {
    serve({ status: mcStatus(null) });
    render(<MinecraftScreen />);
    expect(await screen.findByText("container down")).toBeInTheDocument();
    expect(screen.getByText("The container is down.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start server" })).toBeEnabled();
  });

  it("tapping a player opens a Sheet with their totals", async () => {
    serve({ status: mcStatus() });
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Steve42" }));
    const sheet = await screen.findByRole("dialog", { name: "Steve42" });
    expect(within(sheet).getByText("3h 15m")).toBeInTheDocument();
    expect(within(sheet).getByText("time on server")).toBeInTheDocument();
    expect(within(sheet).getByText("5")).toBeInTheDocument();
    expect(within(sheet).getByText("first seen")).toBeInTheDocument();
    expect(within(sheet).getByText(/Arrives with the companion add-on/)).toBeInTheDocument();
    // The grab handle and the explicit button both close it.
    const closers = within(sheet).getAllByRole("button", { name: "Close" });
    fireEvent.click(closers[closers.length - 1] as HTMLElement);
    expect(screen.queryByRole("dialog", { name: "Steve42" })).toBeNull();
  });

  it("Check for updates asks Mojang now and shows when it last checked", async () => {
    serve({ status: mcStatus(), version: mcVersion() });
    render(<MinecraftScreen />);
    expect(await screen.findByText("last checked 12 min ago")).toBeInTheDocument();
    expect(screen.getByText("up to date")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u) === "/api/minecraft/version?refresh=true"),
      ).toBe(true),
    );
  });

  it("the auto-update switch defaults off and saves when flipped", async () => {
    serve({ status: mcStatus() });
    render(<MinecraftScreen />);
    const sw = await screen.findByRole("switch", { name: /Update automatically on restart/ });
    expect(sw).toHaveAttribute("aria-checked", "false");
    fireEvent.click(sw);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, i]) =>
            String(u) === "/api/minecraft/settings" &&
            i?.method === "PUT" &&
            i.body === JSON.stringify({ auto_update: true }),
        ),
      ).toBe(true),
    );
  });

  it("polls the status about every 5 s", async () => {
    serve({ status: mcStatus() });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MinecraftScreen />);
    await screen.findByText("running");
    const polls = () => fetchMock.mock.calls.filter(([u]) => String(u) === "/api/minecraft").length;
    const before = polls();
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(polls()).toBe(before + 1);
  });

  it("after an update to X, a later release Y still shows as behind with Update Minecraft", async () => {
    const doneToX = mcUpdate({
      state: "done",
      from: RUNNING,
      to: LATEST,
      started_at: 100,
      finished_at: Date.now() / 1000 - 60,
    });
    serve({
      status: mcStatus(mcServer({ version: LATEST, update: doneToX })),
      version: mcBehind({ running: LATEST, latest: "1.26.70.1", notes: null }),
    });
    render(<MinecraftScreen />);
    expect(await screen.findByText("1.26.70.1 is out.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update Minecraft" })).toBeEnabled();
  });

  it("disables every act while a request is in flight, and says why", async () => {
    let answer: (r: Response) => void = () => {};
    serve({
      status: mcStatus(mcServer({ players: [] })),
      version: mcBehind(),
      post: (path) =>
        path === "/api/minecraft/stop"
          ? new Promise<Response>((resolve) => {
              answer = resolve;
            })
          : undefined,
    });
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Restart" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Update Minecraft" })).toBeDisabled();
    expect(screen.getAllByText("Waiting for the server to answer…").length).toBeGreaterThan(0);
    await act(async () => answer(json({ action: "stop" }, 202)));
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart" })).toBeEnabled());
  });

  it("a Start that ran the auto-update follows its steps, with no error", async () => {
    let update = mcUpdate();
    serve({
      status: () => mcStatus(mcStopped({ auto_update: true, update })),
      version: mcBehind(),
      post: (path) =>
        path === "/api/minecraft/start"
          ? Promise.resolve(
              json(
                {
                  action: "start",
                  update: mcUpdate({ state: "backing_up", from: RUNNING, to: LATEST }),
                },
                202,
              ),
            )
          : undefined,
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Start server" }));
    expect(
      await screen.findByText(`Auto-update is on — backing up, then installing ${LATEST}`),
    ).toBeInTheDocument();
    update = mcUpdate({ state: "downloading", from: RUNNING, to: LATEST, started_at: 500 });
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByText(`Downloading ${LATEST}`)).toBeInTheDocument();
    // A start that updates isn't an update of a stopped server: it starts afterwards.
    expect(screen.queryByText("Installing — server stays stopped")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a deferred Start says the container will start it, with no error", async () => {
    serve({
      status: mcStatus(mcStopped()),
      post: (path) =>
        path === "/api/minecraft/start"
          ? Promise.resolve(json({ action: "start", deferred: true }, 202))
          : undefined,
    });
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Start server" }));
    expect(
      await screen.findByText(
        "The container is still booting — the server starts on its own once it's up",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("re-reads the roster after a join settles, and on a slow beat", async () => {
    serve({ status: mcStatus() });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MinecraftScreen />);
    await screen.findByRole("table", { name: "Time played" });
    const reads = () =>
      fetchMock.mock.calls.filter(([u]) => String(u) === "/api/minecraft/players").length;
    const first = reads();
    await act(() => vi.advanceTimersByTimeAsync(6000));
    expect(reads()).toBe(first + 1);
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(reads()).toBeGreaterThanOrEqual(first + 2);
  });

  it("says when the server can't be reached", async () => {
    fetchMock.mockImplementation(async () =>
      json({ detail: "minecraft sidecar unreachable" }, 503),
    );
    render(<MinecraftScreen />);
    expect(
      await screen.findByText("Can't reach the Minecraft server — minecraft sidecar unreachable"),
    ).toBeInTheDocument();
  });
});
