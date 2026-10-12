import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MinecraftBackup,
  MinecraftJob,
  MinecraftLastJob,
  MinecraftRules,
  MinecraftServer,
  MinecraftWorlds,
} from "../api/client";
import {
  mcAllowlist,
  mcBackups,
  mcJob,
  mcPlayers,
  mcRules,
  mcServer,
  mcServerSettings,
  mcSlots,
  mcStatus,
  mcStopped,
  mcVersion,
  mcWorlds,
} from "../minecraftFixtures";
import { MinecraftScreen } from "./MinecraftScreen";

const MB = 1024 * 1024;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface World {
  server: MinecraftServer;
  job: MinecraftJob | null;
  worlds: MinecraftWorlds;
  backups: MinecraftBackup[];
  rules: (slot: string) => MinecraftRules;
  lastJob: MinecraftLastJob | null;
  pendingRestart: string[];
  /** Overrides a write's answer; undefined falls through to a plain 200. */
  write?: (method: string, path: string, body: unknown) => Response | Promise<Response> | undefined;
  /** Overrides a read's answer, e.g. to fail it. */
  read?: (path: string) => Response | undefined;
}

/** A stand-in for the browser's XHR: the test drives its progress and its answer. */
class FakeXHR {
  static last: FakeXHR | null = null;
  url = "";
  body: unknown = null;
  status = 0;
  responseText = "";
  withCredentials = false;
  upload: { onprogress: ((e: Partial<ProgressEvent>) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader() {}
  send(body: unknown) {
    this.body = body;
    FakeXHR.last = this;
  }
  progress(loaded: number, total: number) {
    this.upload.onprogress?.({ loaded, total, lengthComputable: true });
  }
  onabort: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onloadend: (() => void) | null = null;
  aborted = false;
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
    this.onloadend?.();
  }
  drop() {
    this.onerror?.();
    this.onloadend?.();
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
    this.onloadend?.();
  }
}

function sized(name: string, bytes: number): File {
  const f = new File(["x"], name);
  Object.defineProperty(f, "size", { value: bytes });
  return f;
}

describe("Minecraft worlds and backups", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let world: World;

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    FakeXHR.last = null;
    world = {
      server: mcServer(),
      job: null,
      worlds: mcWorlds(),
      backups: mcBackups(),
      rules: (slot) => mcRules(slot, slot === "slot2"),
      lastJob: null,
      pendingRestart: [],
    };
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://box");
      const path = url.pathname;
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      if (method !== "GET") {
        const answer = world.write?.(method, path, body);
        if (answer) return answer;
      } else {
        const answer = world.read?.(path);
        if (answer) return answer;
      }
      if (path === "/api/minecraft") {
        return json(
          mcStatus({
            ...world.server,
            job: world.job,
            last_job: world.lastJob,
            pending_restart: world.pendingRestart,
          }),
        );
      }
      if (path.startsWith("/api/minecraft/version")) return json(mcVersion());
      if (path === "/api/minecraft/players") return json(mcPlayers());
      if (path === "/api/minecraft/worlds") return json(world.worlds);
      if (path === "/api/minecraft/worlds/new-seed") return json({ seed: "424242" });
      const rules = path.match(/^\/api\/minecraft\/worlds\/(slot\d)\/rules$/);
      if (rules?.[1]) {
        const view = world.rules(rules[1]);
        if (method === "PUT") {
          const rulesNow = { ...view.rules, ...body.set };
          return json({
            ...view,
            rules: rulesNow,
            pending: view.live ? [] : Object.keys(body.set),
          });
        }
        return json(view);
      }
      if (path === "/api/minecraft/backups") {
        if (method === "POST") return json({ name: "new.mcworld", bytes: 1, files: 1 });
        const slot = world.worlds.slots.find((s) => s.id === url.searchParams.get("slot"));
        return json({ snapshots: world.backups.filter((b) => !slot || b.folder === slot.folder) });
      }
      if (path === "/api/minecraft/allowlist") return json(mcAllowlist());
      if (path === "/api/minecraft/server-settings") return json(mcServerSettings());
      if (method !== "GET") return json({ ok: true, loaded: true });
      return new Response(null, { status: 404 });
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const writes = () =>
    fetchMock.mock.calls
      .filter(([, init]) => (init?.method ?? "GET") !== "GET")
      .map(
        ([u, init]) => `${init?.method} ${String(u)}${init?.body ? ` ${String(init.body)}` : ""}`,
      );

  async function openWorlds() {
    render(<MinecraftScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /Worlds & backups/ }));
    return screen.findByRole("button", { name: "Back" });
  }

  async function openWorld(name: RegExp) {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name }));
    return screen.findByText("Origin");
  }

  it("the main screen's row: the loaded world, slots used and the last copy off the box", async () => {
    render(<MinecraftScreen />);
    const row = await screen.findByRole("button", { name: /Worlds & backups/ });
    await waitFor(() => expect(row).toHaveTextContent("Castle Hill loaded · 4 of 5 slots used"));
    expect(row).toHaveTextContent(/last copy off the box \w+ \d+/);
    // The status block heads its facts with the loaded world.
    expect(screen.getByText("world loaded")).toBeInTheDocument();
  });

  it("warns amber when no world has a copy off the box, and shows a running job", async () => {
    world.worlds = mcWorlds(mcSlots().map((s) => ({ ...s, last_download: null })));
    world.job = mcJob();
    render(<MinecraftScreen />);
    const row = await screen.findByRole("button", { name: /Worlds & backups/ });
    await waitFor(() => expect(row).toHaveTextContent("Importing a world — writing…"));
    expect(within(row).getByText("no world has a copy off the box yet")).toHaveClass("warn");
    // A job owns the server: Start, Stop and Restart wait for it.
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(
      screen.getByText("Busy importing a world — the server is handled for you."),
    ).toBeVisible();
  });

  it("Worlds groups the slots; empty ones offer New world and Import", async () => {
    await openWorlds();
    expect(await screen.findByText("Loaded")).toBeInTheDocument();
    expect(screen.getByText("Other worlds")).toBeInTheDocument();
    expect(screen.getByText("Slot 5 — empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New world" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
    expect(screen.getByText(/^5 slots\./)).toBeInTheDocument();
  });

  it("with every slot in use it says to reset or empty one first", async () => {
    world.worlds = mcWorlds(
      mcSlots().map((s) => (s.id === "slot5" ? { ...s, name: "Fifth", exists: true } : s)),
    );
    await openWorlds();
    expect(
      await screen.findByText(/^All 5 slots in use — reset or empty one first\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import .mcworld over a world" })).toBeEnabled();
  });

  it("a world's page: seed with Copy, or unknown when level.dat couldn't be read", async () => {
    world.worlds = mcWorlds(mcSlots().map((s) => (s.id === "slot2" ? { ...s, seed: null } : s)));
    await openWorld(/^Creative test/);
    expect(screen.getByText("8675309")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy seed of Creative test" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Castle Hill/ }));
    expect(await screen.findByText(/not recorded/)).toBeInTheDocument();
  });

  it("Load with players on names the 10-second chat warning, then loads", async () => {
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Load Creative test" }));
    const dialog = await screen.findByRole("dialog", { name: "Load Creative test?" });
    expect(dialog).toHaveTextContent(
      "BlockyFox and Mira_P get a 10-second warning in chat and are disconnected; Castle Hill is backed up first, then Creative test starts.",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Load" }));
    await waitFor(() => expect(writes()).toContain("POST /api/minecraft/worlds/slot3/load"));
  });

  it("Load while the server is stopped takes no backup and says so", async () => {
    world.server = mcStopped();
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Load Creative test" }));
    const dialog = await screen.findByRole("dialog", { name: "Load Creative test?" });
    expect(dialog).toHaveTextContent(
      "Creative test becomes the loaded world — the server stays stopped until you start it.",
    );
    expect(dialog).not.toHaveTextContent(/backed up/);
  });

  it("a 409 refusal shows its detail and that nothing changed", async () => {
    world.write = (_method, path) =>
      path.endsWith("/load") ? json({ detail: "busy: updating" }, 409) : undefined;
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Load Creative test" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Load" }),
    );
    // Only the page in front is in reach; the alert under it is hidden with its page.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't load Creative test — busy: updating. Nothing changed; Castle Hill is still loaded.",
    );
  });

  it("difficulty is live on the loaded, running world; game mode waits for a restart", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(screen.getByRole("radio", { name: "Hard" }));
    expect(await screen.findByText("Applied live — difficulty is hard now")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Creative" }));
    expect(
      await screen.findByText(
        "Saved — creative is the default for new players at the next restart; existing players keep theirs",
      ),
    ).toBeInTheDocument();
    expect(writes()).toEqual([
      'PATCH /api/minecraft/worlds/slot2 {"difficulty":"hard"}',
      'PATCH /api/minecraft/worlds/slot2 {"gamemode":"creative"}',
    ]);
  });

  it("Game rules on the loaded, running world are live; four stepper taps make one save", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    expect(
      await screen.findByText("Loaded and running — changes apply live, at once."),
    ).toBeInTheDocument();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByText("World", { selector: ".mc-gt" }));
    const raise = screen.getByRole("button", { name: "Raise — Random tick speed" });
    for (let i = 0; i < 4; i++) fireEvent.click(raise);
    expect(screen.getByRole("textbox", { name: "Random tick speed" })).toHaveValue("5");
    expect(writes()).toEqual([]);
    await act(() => vi.advanceTimersByTimeAsync(800));
    expect(writes()).toEqual([
      'PUT /api/minecraft/worlds/slot2/rules {"set":{"randomTickSpeed":5}}',
    ]);
    expect(await screen.findByText("Applied live — random tick speed: 5")).toBeInTheDocument();
  });

  it("Game rules on a world that isn't loaded are pending until it loads", async () => {
    world.rules = (slot) => mcRules(slot, false, { keepInventory: true }, ["keepInventory"]);
    await openWorld(/^Skyblock run/);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    expect(
      await screen.findByText(
        "Not loaded — changes are saved and applied when Skyblock run next loads.",
      ),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a game rule" }), {
      target: { value: "inventory" },
    });
    const keep = screen.getByRole("radiogroup", { name: "Keep inventory on death" });
    expect(within(keep).getByRole("radio", { name: "On" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByText("changed · default off")).toBeInTheDocument();
    fireEvent.click(within(keep).getByRole("radio", { name: "Off" }));
    expect(
      await screen.findByText("Saved — keep inventory Off from Skyblock run's next load"),
    ).toBeInTheDocument();
    expect(writes()).toContain(
      'PUT /api/minecraft/worlds/slot4/rules {"set":{"keepInventory":false}}',
    );
  });

  it("the playerWaypoints choice is the reported value plus everyone", async () => {
    world.rules = (slot) => mcRules(slot, true, { playerWaypoints: "none" });
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    fireEvent.change(await screen.findByRole("searchbox", { name: "Find a game rule" }), {
      target: { value: "waypoints" },
    });
    const select = screen.getByRole("combobox", { name: "Players shown on the locator bar" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["none", "everyone"]);
  });

  it("New world: Random shows the box's seed with Re-roll; Enter counts to 64", async () => {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "New world" }));
    const sheet = await screen.findByRole("dialog", { name: "New world" });
    expect(await within(sheet).findByText("424242")).toBeInTheDocument();
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "Skyblock 2" } });
    fireEvent.click(within(sheet).getByRole("radio", { name: "Enter a seed" }));
    const create = within(sheet).getByRole("button", { name: "Create world" });
    expect(create).toBeDisabled();
    fireEvent.change(within(sheet).getByRole("textbox", { name: "Seed" }), {
      target: { value: "kids castle" },
    });
    expect(within(sheet).getByText(/of 64/)).toHaveTextContent("11 of 64");
    fireEvent.click(within(sheet).getByRole("radio", { name: "Random" }));
    fireEvent.click(create);
    await waitFor(() =>
      expect(writes()).toContain(
        'POST /api/minecraft/worlds/slot5/create {"name":"Skyblock 2","seed":"424242","gamemode":"survival","difficulty":"normal","cheats":false,"wreck":true,"power_pack_transport":true}',
      ),
    );
  });

  it("Import refuses over 1 GB before uploading, and warns over 100 MB", async () => {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const sheet = await screen.findByRole("dialog", { name: "Import a world" });
    expect(within(sheet).getByText(/Settings come from the world file/)).toBeInTheDocument();
    const input = sheet.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [sized("Skyblock XL.mcworld", 1331 * MB)] } });
    expect(within(sheet).getByText("Too big to import.")).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Import into slot 5" })).toBeDisabled();
    fireEvent.change(input, { target: { files: [sized("Hilltop City.mcworld", 300 * MB)] } });
    expect(
      within(sheet).getByText("Over 100 MB only uploads at home on the Wi-Fi."),
    ).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Import into slot 5" })).toBeEnabled();
    expect(FakeXHR.last).toBeNull();
  });

  it("Import uploads the raw file with real progress", async () => {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const sheet = await screen.findByRole("dialog", { name: "Import a world" });
    const input = sheet.querySelector('input[type="file"]') as HTMLInputElement;
    const file = sized("Hilltop Village.mcworld", 9.4 * MB);
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Import into slot 5" }));
    await waitFor(() => expect(FakeXHR.last).not.toBeNull());
    const xhr = FakeXHR.last as FakeXHR;
    expect(xhr.url).toBe("/api/minecraft/worlds/slot5/import?name=");
    expect(xhr.body).toBe(file);
    act(() => xhr.progress(4.7 * MB, 9.4 * MB));
    expect(await screen.findByText("4.7 MB of 9.4 MB")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Upload" })).toHaveAttribute(
      "aria-valuenow",
      "50",
    );
    act(() => xhr.respond(200, { ...mcSlots()[4], name: "Hilltop Village", exists: true }));
    expect(await screen.findByText("Hilltop Village imported into slot 5")).toBeInTheDocument();
  });

  it("Import over an occupied world confirms in the Dialog first", async () => {
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Import over" }));
    const sheet = await screen.findByRole("dialog", { name: "Import a world" });
    const input = sheet.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [sized("Hilltop Village.mcworld", 9 * MB)] } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Import into Creative test" }));
    const dialog = await screen.findByRole("dialog", { name: "Import over Creative test?" });
    expect(dialog).toHaveTextContent(
      "Creative test is backed up first, then replaced by the world in Hilltop Village.mcworld, with the file's own name and settings.",
    );
    expect(FakeXHR.last).toBeNull();
  });

  it("Reset offers Same seed only for a known seed, and confirms by typing the name", async () => {
    world.worlds = mcWorlds(mcSlots().map((s) => (s.id === "slot3" ? { ...s, seed: null } : s)));
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Reset…" }));
    const sheet = await screen.findByRole("dialog", { name: "Reset Creative test" });
    expect(within(sheet).getByRole("radio", { name: /^Same seed/ })).toBeDisabled();
    expect(within(sheet).getByRole("radio", { name: /^New seed/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(within(sheet).getByRole("radio", { name: /^Empty/ })).toBeEnabled();
    fireEvent.click(within(sheet).getByRole("button", { name: "Continue" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset Creative test?" });
    const go = within(dialog).getByRole("button", { name: "Reset" });
    expect(go).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), {
      target: { value: "Creative test" },
    });
    expect(go).toBeEnabled();
    fireEvent.click(go);
    await waitFor(() =>
      expect(writes()).toContain(
        'POST /api/minecraft/worlds/slot3/reset {"mode":"new_seed","seed":"424242","wreck":true}',
      ),
    );
  });

  it("each world says where its crash site stands, on its row and its page", async () => {
    await openWorlds();
    expect(await screen.findByRole("button", { name: /^Creative test/ })).toHaveTextContent(
      "Crash site: built",
    );
    expect(screen.getByRole("button", { name: /^Skyblock run/ })).toHaveTextContent(
      "Crash site: on its way",
    );
    expect(screen.getByRole("button", { name: /^Castle Hill/ })).toHaveTextContent(
      "Crash site: off",
    );
    fireEvent.click(screen.getByRole("button", { name: /^Castle Hill/ }));
    expect(await screen.findByText("none — imported worlds are never touched")).toBeVisible();
    // A played world can't get one now: the page says why and how to start one fresh.
    expect(screen.getByText(/can't be added to a world that's been played/)).toBeVisible();
  });

  it("an armed world's build shows its phase while it runs, and a failed try", async () => {
    const armed = (phase: string) =>
      mcWorlds(
        mcSlots().map((s) =>
          s.id === "slot4"
            ? { ...s, active: true, exists: true, wreck_phase: phase }
            : { ...s, active: false },
        ),
      );
    world.worlds = armed("pod");
    await openWorlds();
    expect(await screen.findByRole("button", { name: /^Skyblock run/ })).toHaveTextContent(
      "Crash site: building — the pod",
    );
    fireEvent.click(screen.getByRole("button", { name: /^Skyblock run/ }));
    expect(await screen.findByText("building — building the pod")).toBeVisible();
    cleanup();
    world.worlds = armed("failed");
    await openWorlds();
    expect(await screen.findByRole("button", { name: /^Skyblock run/ })).toHaveTextContent(
      "Crash site: didn't finish — trying again",
    );
  });

  it("a stopped server's armed world is on its way, whatever phase it stopped at", async () => {
    world.server = mcStopped();
    world.worlds = mcWorlds(
      mcSlots().map((s) =>
        s.id === "slot4"
          ? { ...s, active: true, exists: true, wreck_phase: "ship" }
          : { ...s, active: false },
      ),
    );
    await openWorlds();
    expect(await screen.findByRole("button", { name: /^Skyblock run/ })).toHaveTextContent(
      "Crash site: on its way",
    );
  });

  it("New world: the crash-site switch starts at the server default and is sent", async () => {
    world.server = mcServer({ spawn_wreck: false });
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "New world" }));
    const sheet = await screen.findByRole("dialog", { name: "New world" });
    const sw = within(sheet).getByRole("switch", { name: "Crash site at spawn" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(sheet).toHaveTextContent("off — the world's own spawn, nothing built");
    expect(sheet).not.toHaveTextContent("not the server default");
    fireEvent.click(sw);
    expect(sheet).toHaveTextContent(
      "on — an escape pod crashed at spawn, its mothership overhead · not the server default",
    );
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "Pod world" } });
    await within(sheet).findByText("424242");
    fireEvent.click(within(sheet).getByRole("button", { name: "Create world" }));
    await waitFor(() => expect(writes().join("\n")).toMatch(/slot5\/create .*"wreck":true/));
    expect(await screen.findByText(/generated, crash site and all, the first time/)).toBeVisible();
  });

  it("Reset: the crash-site switch shows for a seed, not for Empty, and is sent", async () => {
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Reset…" }));
    const sheet = await screen.findByRole("dialog", { name: "Reset Creative test" });
    fireEvent.click(within(sheet).getByRole("radio", { name: /^Empty/ }));
    expect(within(sheet).queryByRole("switch", { name: "Crash site at spawn" })).toBeNull();
    fireEvent.click(within(sheet).getByRole("radio", { name: /^Same seed/ }));
    const sw = within(sheet).getByRole("switch", { name: "Crash site at spawn" });
    expect(sw).toHaveAttribute("aria-checked", "true");
    fireEvent.click(sw);
    expect(sheet).toHaveTextContent("· not the server default");
    fireEvent.click(within(sheet).getByRole("button", { name: "Continue" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset Creative test?" });
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), {
      target: { value: "Creative test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reset" }));
    await waitFor(() =>
      expect(writes()).toContain(
        'POST /api/minecraft/worlds/slot3/reset {"mode":"same_seed","seed":null,"wreck":false}',
      ),
    );
  });

  it("Server settings' crash-site default saves at once and names a world still armed", async () => {
    render(<MinecraftScreen />);
    const sw = await screen.findByRole("switch", { name: "Crash site by default" });
    const row = await screen.findByRole("button", { name: /Worlds & backups/ });
    await waitFor(() => expect(row).toHaveTextContent("4 of 5 slots used"));
    expect(sw).toHaveAttribute("aria-checked", "true");
    fireEvent.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "false");
    await waitFor(() =>
      expect(writes()).toContain('PUT /api/minecraft/settings {"spawn_wreck":false}'),
    );
    expect(
      await screen.findByText(
        "Saved — new worlds start at a plain spawn unless you turn it on for one. Skyblock run still gets its crash site — set when made.",
      ),
    ).toBeVisible();
  });

  it("a refused crash-site default flips back and says so", async () => {
    world.write = (method, path) =>
      method === "PUT" && path === "/api/minecraft/settings"
        ? json({ detail: "the box said no" }, 500)
        : undefined;
    render(<MinecraftScreen />);
    const sw = await screen.findByRole("switch", { name: "Crash site by default" });
    fireEvent.click(sw);
    expect(await screen.findByText(/Couldn't change the crash-site default/)).toBeVisible();
    expect(sw).toHaveAttribute("aria-checked", "true");
  });

  it("Reset never offers Empty for the loaded world", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(screen.getByRole("button", { name: "Reset…" }));
    const sheet = await screen.findByRole("dialog", { name: "Reset Castle Hill" });
    expect(within(sheet).getByRole("radio", { name: /^Same seed/ })).toBeEnabled();
    expect(within(sheet).getByRole("radio", { name: /^Empty/ })).toBeDisabled();
  });

  it("a pinned backup can't be deleted; an unpinned one confirms first", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^before the castle roof/ }));
    let sheet = await screen.findByRole("dialog", { name: "before the castle roof" });
    expect(within(sheet).getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(
      within(sheet).getByText("Unpin it first — a pinned backup can't be deleted."),
    ).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "/api/minecraft/backups/slot2-20261009-191200-before-the-castle-roof.mcworld/file",
    );
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(await screen.findByRole("button", { name: /^first night/ }));
    sheet = await screen.findByRole("dialog", { name: "first night" });
    expect(within(sheet).getByText(/^downloaded /)).toBeInTheDocument();
    fireEvent.click(within(sheet).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete “first night”?" });
    expect(dialog).toHaveTextContent("your downloaded copy is unaffected");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(writes()).toContain(
        "DELETE /api/minecraft/backups/slot2-20261004-201500-first-night.mcworld",
      ),
    );
  });

  it("explains retention, and a restore into another slot carries the seed and settings", async () => {
    await openWorld(/^Castle Hill/);
    expect(await screen.findByText(/Each world keeps its newest 20\./)).toBeInTheDocument();
    expect(screen.getByText("3 of 20")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /^first night/ }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Restore…" }),
    );
    const sheet = await screen.findByRole("dialog", { name: "Restore a backup" });
    fireEvent.click(within(sheet).getByRole("radio", { name: /^Creative test/ }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Continue" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore into Creative test?" });
    expect(dialog).toHaveTextContent(/its seed and settings included\./);
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(writes()).toContain(
        'POST /api/minecraft/backups/slot2-20261004-201500-first-night.mcworld/restore {"slot":"slot3"}',
      ),
    );
  });

  it("the job card shows what, phase and time since it started", async () => {
    world.job = mcJob({ what: "loading a world", phase: "warning players" });
    await openWorlds();
    const card = await screen.findByRole("status");
    expect(card).toHaveTextContent("Loading a world");
    expect(card).toHaveTextContent("Warning players — players got a 10-second warning in chat");
    expect(card).toHaveTextContent(/0:4\d/);
    expect(card).toHaveTextContent("any device opening this screen sees it");
    // Every world action waits, and says why.
    expect(screen.getByRole("button", { name: "New world" })).toBeDisabled();
    expect(screen.getAllByText("Wait — the server is loading a world.").length).toBeGreaterThan(0);
  });

  it("server settings save for the next restart; the allowlist adds names live", async () => {
    render(<MinecraftScreen />);
    const more = await screen.findByRole("button", { name: "More — Max players" });
    fireEvent.click(more);
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText("Saved — applies at the next restart")).toBeInTheDocument();
    expect(writes()).toContain(
      'PUT /api/minecraft/server-settings {"server_name":"JBrain","max_players":11,"view_distance":32}',
    );
    fireEvent.change(screen.getByLabelText("Add a gamertag"), { target: { value: "Newbie" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("Newbie can join now — no restart needed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "Allowlist" }));
    const dialog = await screen.findByRole("dialog", { name: "Turn the allowlist off?" });
    expect(dialog).toHaveTextContent(/From the next restart, anyone who can reach the server/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() =>
      expect(writes()).toContain('POST /api/minecraft/allowlist {"enabled":false}'),
    );
    expect(await screen.findByText("Allowlist off from the next restart")).toBeInTheDocument();
  });

  // ---- long operations as jobs ----

  async function confirmLoad() {
    fireEvent.click(screen.getByRole("button", { name: "Load Creative test" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Load" }),
    );
  }

  const statusReads = () =>
    fetchMock.mock.calls.filter(([u, init]) => String(u) === "/api/minecraft" && !init?.method)
      .length;

  it("a load the box accepts is followed through its job to the outcome", async () => {
    world.lastJob = { what: "resetting a world", ok: true, detail: null, finished_at: 100 };
    world.write = (_m, path) => {
      if (!path.endsWith("/load")) return undefined;
      world.job = mcJob({ what: "loading a world", phase: "starting" });
      return json({ accepted: true, what: "loading a world" }, 202);
    };
    await openWorld(/^Creative test/);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await confirmLoad();
    expect((await screen.findAllByText("Starting")).length).toBeGreaterThan(0);
    // An older outcome isn't this one: still waiting while the job runs.
    expect(screen.getByRole("button", { name: "Reset…" })).toBeDisabled();
    world.job = null;
    world.lastJob = { what: "loading a world", ok: true, detail: null, finished_at: 200 };
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(await screen.findByText("Creative test is loaded — players can join")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reset…" })).toBeEnabled());
  });

  it("an accepted job that fails says why", async () => {
    world.write = (_m, path) => {
      if (!path.endsWith("/load")) return undefined;
      world.job = mcJob({ what: "loading a world", phase: "writing" });
      return json({ accepted: true, what: "loading a world" }, 202);
    };
    await openWorld(/^Creative test/);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await confirmLoad();
    await screen.findAllByText("Writing");
    world.job = null;
    world.lastJob = {
      what: "loading a world",
      ok: false,
      detail: "the server didn't start within 2 minutes",
      finished_at: 300,
    };
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't load Creative test — the server didn't start within 2 minutes.",
    );
  });

  it("while a load is being sent, the status is read at once and other world actions wait", async () => {
    world.write = (_m, path) => (path.endsWith("/load") ? new Promise(() => {}) : undefined);
    await openWorld(/^Creative test/);
    const before = statusReads();
    await confirmLoad();
    await waitFor(() => expect(statusReads()).toBeGreaterThan(before));
    expect(screen.getByRole("button", { name: "Reset…" })).toBeDisabled();
    expect(screen.getAllByText("Wait — the server is loading a world.").length).toBeGreaterThan(0);
  });

  it("a lost answer (503) doesn't claim nothing changed; it points at the job", async () => {
    world.write = (_m, path) =>
      path.endsWith("/load") ? json({ detail: "gateway timeout" }, 524) : undefined;
    await openWorld(/^Creative test/);
    await confirmLoad();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Couldn't confirm that — gateway timeout. The server may still be loading a world",
    );
    expect(alert).not.toHaveTextContent("Nothing changed");
  });

  it("a reset refused as busy says nothing changed", async () => {
    world.write = (_m, path) =>
      path.endsWith("/reset") ? json({ detail: "busy: restoring a backup" }, 409) : undefined;
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("button", { name: "Reset…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Continue" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Reset Creative test?" });
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), {
      target: { value: "Creative test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reset" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't reset Creative test — busy: restoring a backup. Nothing changed.",
    );
  });

  it("Load of the world already loaded says so", async () => {
    world.write = (_m, path) =>
      path.endsWith("/load") ? json({ loaded: false, detail: "already loaded" }) : undefined;
    await openWorld(/^Creative test/);
    await confirmLoad();
    expect(await screen.findByText("Creative test is already loaded")).toBeInTheDocument();
  });

  // ---- pending, from the box and from this device ----

  it("the loaded world's game mode and cheats, the allowlist and server settings read pending_restart", async () => {
    world.pendingRestart = ["allow-cheats", "allow-list", "max-players"];
    render(<MinecraftScreen />);
    const allow = await screen.findByText("Allowlist on");
    expect(allow.parentElement).toHaveTextContent("pending");
    expect(screen.getByText("Max players").parentElement).toHaveTextContent("pending");
    expect(screen.getByText("View distance").parentElement).not.toHaveTextContent("pending");
    fireEvent.click(await screen.findByRole("button", { name: /Worlds & backups/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^Castle Hill/ }));
    await screen.findByText("Origin");
    expect(screen.getByText("Cheats").parentElement).toHaveTextContent("pending");
    expect(screen.getByText(/^Game mode/)).not.toHaveTextContent("pending");
  });

  it("a change to a world that isn't loaded is pending until it loads, on this device", async () => {
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("radio", { name: "Hard" }));
    expect(
      await screen.findByText("Saved — difficulty changes when Creative test next loads"),
    ).toBeInTheDocument();
    expect(screen.getByText(/^Difficulty/)).toHaveTextContent("pending");
  });

  it("a setting refused while a job runs goes back, with the reason", async () => {
    world.write = (method) =>
      method === "PATCH" ? json({ detail: "busy: loading a world" }, 409) : undefined;
    await openWorld(/^Creative test/);
    fireEvent.click(screen.getByRole("radio", { name: "Hard" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save the setting — busy: loading a world.",
    );
    expect(screen.getByRole("radio", { name: "Peaceful" })).toHaveAttribute("aria-checked", "true");
  });

  it("a rule refused while a job runs switches back", async () => {
    world.write = (method) =>
      method === "PUT" ? json({ detail: "busy: importing a world" }, 409) : undefined;
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    fireEvent.change(await screen.findByRole("searchbox", { name: "Find a game rule" }), {
      target: { value: "inventory" },
    });
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a game rule" }), {
      target: { value: "hurt each other" },
    });
    const sw = screen.getByRole("switch", { name: "Players can hurt each other" });
    fireEvent.click(sw);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save the rule — busy: importing a world.",
    );
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "true"));
  });

  // ---- keep inventory: Off · On · Power Pack (§M10) ----

  /** A sidecar stand-in for one world's switch and saved choice: the rules view reports the
   *  held rule as managed, refuses a write to it, and the slot carries both. */
  function keepWorld(slot: string, start: { pack: boolean; keep: boolean; live: boolean }) {
    const k = { ...start };
    const base = world.rules;
    world.rules = (id) => {
      const view = base(id);
      if (id !== slot) return view;
      return {
        ...view,
        live: k.live,
        rules: { ...view.rules, keepInventory: k.pack || k.keep },
        managed: k.pack ? { keepInventory: true } : {},
      };
    };
    const slots = () =>
      mcWorlds(
        mcSlots().map((s) =>
          s.id === slot ? { ...s, power_pack_keep: k.pack, rules: { keepInventory: k.keep } } : s,
        ),
      );
    world.worlds = slots();
    world.read = (path) => (path === "/api/minecraft/worlds" ? json(slots()) : undefined);
    return k;
  }

  async function openKeep(name: RegExp) {
    await openWorld(name);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    fireEvent.change(await screen.findByRole("searchbox", { name: "Find a game rule" }), {
      target: { value: "inventory" },
    });
    return screen.getByRole("radiogroup", { name: "Keep inventory on death" });
  }

  it("keep inventory is Off · On · Power Pack, explained before a pick", async () => {
    keepWorld("slot2", { pack: false, keep: false, live: true });
    const keep = await openKeep(/^Castle Hill/);
    expect(
      within(keep)
        .getAllByRole("radio")
        .map((r) => r.textContent),
    ).toEqual(["Off", "On", "Power Pack"]);
    expect(within(keep).getByRole("radio", { name: "Off" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByText("Everyone drops their things where they die.")).toBeInTheDocument();
    expect(
      screen.getByText(/^Power Pack: players keep their things only while carrying/),
    ).toHaveTextContent("their own Tricorder; one pack burns each time");
  });

  it("Power Pack on the loaded, running world is the switch, applied live", async () => {
    const k = keepWorld("slot2", { pack: false, keep: false, live: true });
    world.write = (_method, path, body) => {
      if (path.endsWith("/power-pack")) {
        k.pack = (body as { on: boolean }).on;
        return json({ ...mcSlots()[1], power_pack_keep: k.pack, applies: "now" });
      }
      return undefined;
    };
    const keep = await openKeep(/^Castle Hill/);
    fireEvent.click(within(keep).getByRole("radio", { name: "Power Pack" }));
    expect(await screen.findByText("Applied live — keep inventory Power Pack")).toBeInTheDocument();
    expect(writes()).toEqual(['PUT /api/minecraft/worlds/slot2/power-pack {"on":true}']);
    expect(within(keep).getByRole("radio", { name: "Power Pack" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByText("held on by the Power Pack")).toBeInTheDocument();
    expect(
      screen.getByText("Underneath it's Off — that comes back if Power Pack is turned off."),
    ).toBeInTheDocument();
  });

  it("leaving Power Pack is the switch off, then the rule; each step shows while it runs", async () => {
    const k = keepWorld("slot4", { pack: true, keep: false, live: false });
    let release: () => void = () => {};
    world.write = (_method, path, body) => {
      if (path.endsWith("/power-pack")) {
        return new Promise<Response>((done) => {
          release = () => {
            k.pack = (body as { on: boolean }).on;
            done(json({ ...mcSlots()[3], power_pack_keep: k.pack, applies: "next load" }));
          };
        });
      }
      if (path.endsWith("/rules"))
        k.keep = (body as { set: { keepInventory: boolean } }).set.keepInventory;
      return undefined;
    };
    const keep = await openKeep(/^Skyblock run/);
    fireEvent.click(within(keep).getByRole("radio", { name: "On" }));
    expect(
      await screen.findByText(
        "Turning Power Pack off — keep inventory goes back to your saved Off…",
      ),
    ).toBeInTheDocument();
    expect(within(keep).getByRole("radio", { name: "On" })).toHaveAttribute("aria-busy", "true");
    expect(within(keep).getByRole("radio", { name: "Off" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    act(() => release());
    expect(
      await screen.findByText(
        "Saved — Power Pack off, keep inventory On from Skyblock run's next load",
      ),
    ).toBeInTheDocument();
    expect(writes()).toEqual([
      'PUT /api/minecraft/worlds/slot4/power-pack {"on":false}',
      'PUT /api/minecraft/worlds/slot4/rules {"set":{"keepInventory":true}}',
    ]);
  });

  it("a refused pick names the real reason and says nothing changed only once re-read", async () => {
    keepWorld("slot2", { pack: false, keep: false, live: true });
    world.write = (_method, path) =>
      path.endsWith("/power-pack") ? json({ detail: "busy: restoring a backup" }, 409) : undefined;
    const keep = await openKeep(/^Castle Hill/);
    fireEvent.click(within(keep).getByRole("radio", { name: "Power Pack" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't set keep inventory to Power Pack — the server is restoring a backup. Nothing changed.",
    );
    expect(within(keep).getByRole("radio", { name: "Off" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("a write saved before the console failed reads back as saved, not as nothing changed", async () => {
    const k = keepWorld("slot2", { pack: false, keep: false, live: true });
    world.write = (_method, path) => {
      if (!path.endsWith("/power-pack")) return undefined;
      k.pack = true; // the sidecar saves the slot first, then the console refuses
      return json({ detail: "server is not running" }, 409);
    };
    const keep = await openKeep(/^Castle Hill/);
    fireEvent.click(within(keep).getByRole("radio", { name: "Power Pack" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Keep inventory reads Power Pack now, but the server reported a problem — the server isn't running.",
    );
    expect(within(keep).getByRole("radio", { name: "Power Pack" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("a dropped connection never says nothing changed", async () => {
    keepWorld("slot2", { pack: false, keep: false, live: true });
    world.write = (_method, path) =>
      path.endsWith("/rules") ? json({ detail: "upstream unavailable" }, 503) : undefined;
    const keep = await openKeep(/^Castle Hill/);
    fireEvent.click(within(keep).getByRole("radio", { name: "On" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Couldn't set keep inventory to On — the server didn't answer. It still reads Off. The server may still be working on it.",
    );
    expect(alert).not.toHaveTextContent("Nothing changed");
  });

  it("the rule refused after the switch went off says where it now stands", async () => {
    const k = keepWorld("slot2", { pack: true, keep: true, live: true });
    world.write = (_method, path) => {
      if (path.endsWith("/power-pack")) {
        k.pack = false;
        return json({ ...mcSlots()[1], power_pack_keep: false, applies: "now" });
      }
      return json({ detail: "busy: restart" }, 409);
    };
    const keep = await openKeep(/^Castle Hill/);
    fireEvent.click(within(keep).getByRole("radio", { name: "Off" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't set keep inventory to Off — the server is restarting. Power Pack is off and it reads On for now.",
    );
  });

  it("a stale device's pick is refused as managed, and the row shows Power Pack", async () => {
    const k = keepWorld("slot2", { pack: false, keep: false, live: true });
    world.write = (_method, path) =>
      path.endsWith("/rules")
        ? json({ detail: "keepInventory is managed by the Power Pack charm while it is on" }, 400)
        : undefined;
    const keep = await openKeep(/^Castle Hill/);
    k.pack = true; // turned on elsewhere (the debug API) since this page read the rules
    fireEvent.click(within(keep).getByRole("radio", { name: "On" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't set keep inventory to On — Power Pack is holding it on. It reads Power Pack for now.",
    );
    expect(within(keep).getByRole("radio", { name: "Power Pack" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("the held rule is never counted as changed nor sent by a reset", async () => {
    keepWorld("slot2", { pack: true, keep: false, live: true });
    const base = world.rules;
    world.rules = (id) => {
      const v = base(id);
      return id === "slot2" ? { ...v, rules: { ...v.rules, pvp: false } } : v;
    };
    // The reset answers with pvp still off, so both resets stay on screen to be tried.
    world.write = (_method, path) =>
      path.endsWith("/slot2/rules") ? json(world.rules("slot2")) : undefined;
    await openWorld(/^Castle Hill/);
    expect(await screen.findByText(/1 changed from default/)).toHaveTextContent(
      "keep inventory Power Pack",
    );
    fireEvent.click(screen.getByRole("button", { name: /^Game rules/ }));
    const players = (await screen.findByText("Players", { selector: ".mc-gt" })).closest(
      "summary",
    ) as HTMLElement;
    expect(players).toHaveTextContent("1 changed · Power Pack");
    fireEvent.click(players);
    fireEvent.click(screen.getByRole("button", { name: "Reset players to defaults" }));
    await waitFor(() =>
      expect(writes()).toEqual(['PUT /api/minecraft/worlds/slot2/rules {"set":{"pvp":true}}']),
    );
    expect(screen.getByText(/Reset all leaves alone/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reset all" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()[1]).toBe('PUT /api/minecraft/worlds/slot2/rules {"set":{"pvp":true}}');
  });

  it("the segments are one tab stop; arrows move between them", async () => {
    keepWorld("slot2", { pack: false, keep: false, live: true });
    const keep = await openKeep(/^Castle Hill/);
    const [off, on, pack] = within(keep).getAllByRole("radio") as HTMLElement[];
    expect([off, on, pack].map((b) => b?.tabIndex)).toEqual([0, -1, -1]);
    off?.focus();
    fireEvent.keyDown(off as HTMLElement, { key: "ArrowLeft" });
    expect(pack).toHaveFocus();
    fireEvent.keyDown(pack as HTMLElement, { key: "Home" });
    expect(off).toHaveFocus();
    fireEvent.keyDown(off as HTMLElement, { key: "ArrowRight" });
    expect(on).toHaveFocus();
    expect(writes()).toEqual([]);
  });

  // ---- Power Pack teleport (plan §M10; binding mock docs/mocks/minecraft-transport-switch/ B)

  /** The box's side of the switch: the route saves it on the slot, and /worlds reports it. */
  function transportWorld(slot: string, on: boolean, applies: "now" | "next load") {
    const set = (v: boolean) => {
      world.worlds = {
        ...world.worlds,
        slots: world.worlds.slots.map((x) =>
          x.id === slot ? { ...x, power_pack_transport: v } : x,
        ),
      };
    };
    set(on);
    world.write = (_method, path, body) => {
      if (!path.endsWith(`/${slot}/power-pack-transport`)) return undefined;
      set((body as { on: boolean }).on);
      const rec = world.worlds.slots.find((x) => x.id === slot);
      return json({ ...rec, applies });
    };
    return set;
  }

  const tpSwitch = (world: string) =>
    screen.getByRole("switch", { name: `Power Pack teleport in ${world}` });

  it("Power Pack teleport sits under Cheats, on, explained before a flip, and live", async () => {
    transportWorld("slot2", true, "now");
    await openWorld(/^Castle Hill/);
    const sw = tpSwitch("Castle Hill");
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByText(/^Loaded and running — difficulty and Power Pack teleport change live;/),
    ).toBeInTheDocument();
    const row = sw.closest(".mc-sw-row") as HTMLElement;
    expect(row).toHaveTextContent(
      "Power Pack teleport on — players can ask Dave to take them to their own saved places; each trip burns a Power Pack.",
    );
    expect(row).not.toHaveTextContent("pending");
    expect(screen.getByText(/^Power Pack teleport:/).closest("p")).toHaveTextContent(
      "Power Pack teleport: a player asks Dave for one of their own saved places, then says Yes on a form. One Power Pack burns only if they arrive, and they need their own Tricorder. Wormhole gates aren't affected by this switch.",
    );
    fireEvent.click(sw);
    expect(await screen.findByText("Applied live — teleport off")).toBeInTheDocument();
    expect(writes()).toEqual(['PUT /api/minecraft/worlds/slot2/power-pack-transport {"on":false}']);
    await waitFor(() => expect(tpSwitch("Castle Hill")).toHaveAttribute("aria-checked", "false"));
    const off = tpSwitch("Castle Hill").closest(".mc-sw-row") as HTMLElement;
    expect(off).toHaveTextContent(
      "off — Dave won't teleport anyone in this world. Saved places still work with the Tricorder.",
    );
    expect(off).not.toHaveTextContent("pending");
  });

  it("the switch shows the new value the moment the flip lands, even if the re-read fails", async () => {
    transportWorld("slot2", true, "now");
    await openWorld(/^Castle Hill/);
    // After the PUT, the worlds list can't be read: the PUT's own answer has to carry it.
    const write = world.write;
    world.write = (method, path, body) => {
      const res = write?.(method, path, body);
      world.read = (p) =>
        p === "/api/minecraft/worlds" ? json({ detail: "sidecar unreachable" }, 503) : undefined;
      return res;
    };
    fireEvent.click(tpSwitch("Castle Hill"));
    expect(await screen.findByText("Applied live — teleport off")).toBeInTheDocument();
    expect(tpSwitch("Castle Hill")).toHaveAttribute("aria-checked", "false");
  });

  it("turned off on a world that isn't loaded, it's pending until that world loads", async () => {
    transportWorld("slot4", true, "next load");
    await openWorld(/^Skyblock run/);
    fireEvent.click(tpSwitch("Skyblock run"));
    expect(
      await screen.findByText("Saved — teleport off from Skyblock run's next load"),
    ).toBeInTheDocument();
    await waitFor(() => expect(tpSwitch("Skyblock run")).toHaveAttribute("aria-checked", "false"));
    expect(tpSwitch("Skyblock run").closest(".mc-sw-row")).toHaveTextContent(
      "Power Pack teleport pending",
    );
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    const listRow = await screen.findByRole("button", { name: /^Skyblock run/ });
    expect(listRow).toHaveTextContent("1 pending");
    expect(listRow).toHaveTextContent("teleport off");
  });

  it("on the loaded world with the server stopped, it waits for the start", async () => {
    world.server = mcStopped();
    transportWorld("slot2", false, "next load");
    await openWorld(/^Castle Hill/);
    fireEvent.click(tpSwitch("Castle Hill"));
    expect(await screen.findByText("Saved — teleport on when the server starts")).toBeVisible();
    await waitFor(() =>
      expect(tpSwitch("Castle Hill").closest(".mc-sw-row")).toHaveTextContent("pending"),
    );
  });

  it("while it applies, the knob is the spinner and the line names the step", async () => {
    transportWorld("slot2", true, "now");
    let release: () => void = () => {};
    const box = world.write;
    world.write = (method, path, body) =>
      path.endsWith("/power-pack-transport")
        ? new Promise<Response>((done) => {
            release = () => done(box?.(method, path, body) as Response);
          })
        : undefined;
    await openWorld(/^Castle Hill/);
    fireEvent.click(tpSwitch("Castle Hill"));
    expect(await screen.findByText("Turning Power Pack teleport off…")).toBeInTheDocument();
    const sw = tpSwitch("Castle Hill");
    expect(sw).toHaveAttribute("aria-busy", "true");
    expect(sw).toHaveClass("busy");
    // One write at a time: a second tap while it applies sends nothing.
    fireEvent.click(sw);
    expect(writes()).toHaveLength(1);
    act(() => release());
    expect(await screen.findByText("Applied live — teleport off")).toBeInTheDocument();
    expect(screen.queryByText("Turning Power Pack teleport off…")).toBeNull();
    expect(tpSwitch("Castle Hill")).toHaveAttribute("aria-busy", "false");
  });

  it("a refusal names the reason and says nothing changed once re-read", async () => {
    transportWorld("slot2", true, "now");
    world.write = (_method, path) =>
      path.endsWith("/power-pack-transport")
        ? json({ detail: "busy: restoring a backup" }, 409)
        : undefined;
    await openWorld(/^Castle Hill/);
    fireEvent.click(tpSwitch("Castle Hill"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Couldn't turn Power Pack teleport off — the server is restoring a backup. Nothing changed; it's still on.",
    );
    expect(tpSwitch("Castle Hill")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(within(alert).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a dropped connection never says nothing changed", async () => {
    transportWorld("slot2", true, "now");
    world.write = (_method, path) =>
      path.endsWith("/power-pack-transport")
        ? json({ detail: "upstream unavailable" }, 503)
        : undefined;
    await openWorld(/^Castle Hill/);
    fireEvent.click(tpSwitch("Castle Hill"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Couldn't turn Power Pack teleport off — the server didn't answer. It still reads on. The server may still be working on it.",
    );
    expect(alert).not.toHaveTextContent("Nothing changed");
  });

  it("New world: Power Pack teleport starts on, says gates aren't affected, and is sent", async () => {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "New world" }));
    const sheet = await screen.findByRole("dialog", { name: "New world" });
    const sw = within(sheet).getByRole("switch", { name: "Power Pack teleport" });
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(sheet).toHaveTextContent(
      "on — players can ask Dave to take them to their own saved places; each trip burns a Power Pack",
    );
    expect(sheet).toHaveTextContent("Wormhole gates aren't affected by this switch.");
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "Quiet" } });
    await within(sheet).findByText("424242");
    fireEvent.click(within(sheet).getByRole("button", { name: "Create world" }));
    await waitFor(() =>
      expect(writes().join("\n")).toMatch(/slot5\/create .*"power_pack_transport":true/),
    );
  });

  it("New world: turned off, the create says so", async () => {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "New world" }));
    const sheet = await screen.findByRole("dialog", { name: "New world" });
    fireEvent.click(within(sheet).getByRole("switch", { name: "Power Pack teleport" }));
    expect(within(sheet).getByRole("switch", { name: "Power Pack teleport" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(sheet).toHaveTextContent("off — Dave won't teleport anyone in this world");
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "Quiet" } });
    await within(sheet).findByText("424242");
    fireEvent.click(within(sheet).getByRole("button", { name: "Create world" }));
    await waitFor(() =>
      expect(writes().join("\n")).toMatch(/slot5\/create .*"power_pack_transport":false/),
    );
  });

  it("the Worlds list says teleport off only on a world that has it off", async () => {
    transportWorld("slot2", false, "now");
    await openWorlds();
    expect(await screen.findByRole("button", { name: /^Castle Hill/ })).toHaveTextContent(
      "teleport off",
    );
    for (const name of [/^Creative test/, /^Skyblock run/]) {
      expect(screen.getByRole("button", { name })).not.toHaveTextContent("teleport");
    }
  });

  it("it isn't a game rule: Reset all never sends it", async () => {
    transportWorld("slot2", false, "now");
    const base = world.rules;
    world.rules = (id) => {
      const v = base(id);
      return id === "slot2" ? { ...v, rules: { ...v.rules, pvp: false } } : v;
    };
    const box = world.write;
    world.write = (method, path, body) =>
      path.endsWith("/slot2/rules") ? json(world.rules("slot2")) : box?.(method, path, body);
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Reset all" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toBe('PUT /api/minecraft/worlds/slot2/rules {"set":{"pvp":true}}');
  });

  it("New world's rules offer keep inventory Off · On only — no Power Pack", async () => {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "New world" }));
    const sheet = await screen.findByRole("dialog", { name: "New world" });
    fireEvent.click(within(sheet).getByText("World rules"));
    fireEvent.change(within(sheet).getByRole("searchbox", { name: "Find a game rule" }), {
      target: { value: "inventory" },
    });
    const keep = within(sheet).getByRole("radiogroup", { name: "Keep inventory on death" });
    expect(
      within(keep)
        .getAllByRole("radio")
        .map((r) => r.textContent),
    ).toEqual(["Off", "On"]);
    // Keep's Power Pack mode is a saved world's; the sheet's teleport switch is outside the rules.
    const rulesBox = sheet.querySelector(".mc-rules-new") as HTMLElement;
    expect(within(rulesBox).queryByText(/Power Pack/)).toBeNull();
    fireEvent.click(within(keep).getByRole("radio", { name: "On" }));
    expect(within(keep).getByRole("radio", { name: "On" })).toHaveAttribute("aria-checked", "true");
    expect(within(sheet).getByText("1 rule changed")).toBeInTheDocument();
  });

  it("closing Game rules mid-tap still sends one save with the final value", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^Game rules/ }));
    await screen.findByText("Loaded and running — changes apply live, at once.");
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByText("World", { selector: ".mc-gt" }));
    const raise = screen.getByRole("button", { name: "Raise — Random tick speed" });
    fireEvent.click(raise);
    fireEvent.click(raise);
    await act(() => vi.advanceTimersByTimeAsync(200));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(writes().filter((w) => w.startsWith("PUT"))).toEqual([
      'PUT /api/minecraft/worlds/slot2/rules {"set":{"randomTickSpeed":3}}',
    ]);
  });

  // ---- import failures ----

  async function startImport(bytes = 9.4 * MB) {
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const sheet = await screen.findByRole("dialog", { name: "Import a world" });
    const input = sheet.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [sized("Hilltop Village.mcworld", bytes)] } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Import into slot 5" }));
    await waitFor(() => expect(FakeXHR.last).not.toBeNull());
    return FakeXHR.last as FakeXHR;
  }

  it("a file the box refuses after the upload shows the reason, and the upload ends", async () => {
    const xhr = await startImport();
    act(() => xhr.respond(400, { detail: "no level.dat inside — not a Bedrock world" }));
    expect(await screen.findByText("Nothing was imported.")).toBeInTheDocument();
    expect(screen.getByText("no level.dat inside — not a Bedrock world")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose another file" })).toBeEnabled();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("a busy server refuses at once: nothing changed", async () => {
    const xhr = await startImport();
    act(() => xhr.respond(409, { detail: "busy: loading a world" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't import Hilltop Village.mcworld — busy: loading a world. Nothing changed.",
    );
    expect(screen.getByRole("button", { name: "New world" })).toBeEnabled();
  });

  it("the tunnel's 413 says it only uploads at home", async () => {
    const xhr = await startImport(300 * MB);
    act(() => xhr.respond(413, "<html>Payload Too Large</html>"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Over 100 MB only uploads at home on the Wi-Fi. Nothing changed.",
    );
  });

  it("a dropped connection says the server may still be working", async () => {
    const xhr = await startImport();
    act(() => xhr.drop());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't confirm the import — the connection dropped during the upload.",
    );
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("Cancel upload aborts it and the screen settles", async () => {
    const xhr = await startImport();
    act(() => xhr.progress(2 * MB, 9.4 * MB));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel upload" }));
    expect(xhr.aborted).toBe(true);
    expect(await screen.findByText("Upload cancelled — nothing was imported")).toBeVisible();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByRole("button", { name: "New world" })).toBeEnabled();
  });

  it("this device's upload also holds Start, Stop and Restart", async () => {
    await startImport();
    fireEvent.click(screen.getAllByRole("button", { name: "Back" }).at(-1) as HTMLElement);
    expect(await screen.findByRole("button", { name: "Stop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Restart" })).toBeDisabled();
  });

  // ---- the rest of the backups and manage paths ----

  it("an automatic backup shows the box's note", async () => {
    await openWorld(/^Castle Hill/);
    expect(
      await screen.findByRole("button", { name: /^Before loading Creative test/ }),
    ).toBeInTheDocument();
  });

  it("pin and unpin", async () => {
    world.write = (_m, path, body) => {
      if (!path.endsWith("/pin")) return undefined;
      const pinned = (body as { pinned: boolean }).pinned;
      world.backups = world.backups.map((b) => (path.includes(b.name) ? { ...b, pinned } : b));
      return json({ pinned });
    };
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^first night/ }));
    const sheet = await screen.findByRole("dialog", { name: "first night" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Pin" }));
    expect(await screen.findByText("Pinned — kept for good, outside the 20")).toBeVisible();
    fireEvent.click(within(sheet).getByRole("button", { name: "Unpin" }));
    expect(await screen.findByText("Unpinned — it counts toward the 20 again")).toBeVisible();
    expect(writes()).toEqual([
      'POST /api/minecraft/backups/slot2-20261004-201500-first-night.mcworld/pin {"pinned":true}',
      'POST /api/minecraft/backups/slot2-20261004-201500-first-night.mcworld/pin {"pinned":false}',
    ]);
  });

  it("Back up now takes an optional label", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(screen.getByRole("button", { name: "Back up Castle Hill now" }));
    const sheet = await screen.findByRole("dialog", { name: "Back up Castle Hill" });
    fireEvent.change(within(sheet).getByRole("textbox"), { target: { value: "roof done" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Back up now" }));
    expect(await screen.findByText("Backed up Castle Hill — “roof done”")).toBeVisible();
    expect(writes()).toContain('POST /api/minecraft/backups {"label":"roof done","slot":"slot2"}');
  });

  it("Rename", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    const sheet = await screen.findByRole("dialog", { name: "Rename world" });
    fireEvent.change(within(sheet).getByRole("textbox"), { target: { value: "Castle Hill 2" } });
    expect(within(sheet).getByText(/of 40/)).toHaveTextContent("13 of 40");
    fireEvent.click(within(sheet).getByRole("button", { name: "Save name" }));
    expect(await screen.findByText("Renamed Castle Hill to Castle Hill 2")).toBeVisible();
    expect(writes()).toContain('PATCH /api/minecraft/worlds/slot2 {"name":"Castle Hill 2"}');
  });

  it("restore into an empty slot names it as the box will", async () => {
    await openWorld(/^Castle Hill/);
    fireEvent.click(await screen.findByRole("button", { name: /^first night/ }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Restore…" }),
    );
    const sheet = await screen.findByRole("dialog", { name: "Restore a backup" });
    const empty = within(sheet).getByRole("radio", { name: /^Slot 5/ });
    expect(empty).toHaveTextContent(/becomes “Castle Hill \(\w+ \d+\)”/);
    fireEvent.click(empty);
    fireEvent.click(within(sheet).getByRole("button", { name: "Continue" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore into slot 5?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore" }));
    expect(await screen.findByText(/^Slot 5 now holds Castle Hill from /)).toBeVisible();
    expect(writes()).toContain(
      'POST /api/minecraft/backups/slot2-20261004-201500-first-night.mcworld/restore {"slot":"slot5"}',
    );
  });

  // ---- failures to read, and focus ----

  it("a worlds list that can't be read says so, with Retry", async () => {
    let fail = true;
    world.read = (path) =>
      fail && path === "/api/minecraft/worlds"
        ? json({ detail: "sidecar unreachable" }, 503)
        : undefined;
    render(<MinecraftScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't read the worlds — sidecar unreachable.",
    );
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /Worlds & backups/ })).toBeInTheDocument();
  });

  it("when the box can't roll a seed, New world says the box will pick one", async () => {
    world.read = (path) =>
      path === "/api/minecraft/worlds/new-seed" ? json({ detail: "down" }, 503) : undefined;
    await openWorlds();
    fireEvent.click(await screen.findByRole("button", { name: "New world" }));
    const sheet = await screen.findByRole("dialog", { name: "New world" });
    expect(await within(sheet).findByText("The box will pick one")).toBeInTheDocument();
  });

  it("a pushed page takes focus at Back; popping it returns focus to the row", async () => {
    render(<MinecraftScreen />);
    const row = await screen.findByRole("button", { name: /Worlds & backups/ });
    row.focus();
    fireEvent.click(row);
    const back = await screen.findByRole("button", { name: "Back" });
    expect(back).toHaveFocus();
    fireEvent.click(back);
    await waitFor(() => expect(row).toHaveFocus());
  });
});
