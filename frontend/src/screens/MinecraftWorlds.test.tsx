import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MinecraftBackup,
  MinecraftJob,
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
  /** Overrides a write's answer; undefined falls through to a plain 200. */
  write?: (method: string, path: string, body: unknown) => Response | undefined;
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
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
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
    };
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://box");
      const path = url.pathname;
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      if (method !== "GET") {
        const answer = world.write?.(method, path, body);
        if (answer) return answer;
      }
      if (path === "/api/minecraft") return json(mcStatus({ ...world.server, job: world.job }));
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
      "Creative test is backed up first, then replaced by Hilltop Village with the file's own settings.",
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
  });
});
