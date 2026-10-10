import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    const sw = screen.getByRole("switch", { name: "Keep inventory on death" });
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByText("changed · default off")).toBeInTheDocument();
    fireEvent.click(sw);
    expect(
      await screen.findByText(
        "Saved — keep inventory on death changes when Skyblock run next loads",
      ),
    ).toBeInTheDocument();
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
        'POST /api/minecraft/worlds/slot5/create {"name":"Skyblock 2","seed":"424242","gamemode":"survival","difficulty":"normal","cheats":false}',
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
        'POST /api/minecraft/worlds/slot3/reset {"mode":"new_seed","seed":"424242"}',
      ),
    );
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
    const sw = screen.getByRole("switch", { name: "Keep inventory on death" });
    fireEvent.click(sw);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save the rule — busy: importing a world.",
    );
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "false"));
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
