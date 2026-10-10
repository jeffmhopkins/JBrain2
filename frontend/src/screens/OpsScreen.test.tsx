import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MetricsHistory, OpsMetrics, OpsStatus } from "../api/client";
import { engineState } from "../components/engineFixtures";
import { openEngineCard, resetEngineStore, setEngineState } from "../engineState";
import { OpsScreen } from "./OpsScreen";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const STATUS: OpsStatus = {
  containers: [
    {
      service: "api",
      state: "running",
      health: "healthy",
      started_at: "2026-06-10T08:00:00Z",
      image: "jbrain/api:edge",
    },
    {
      service: "worker",
      state: "exited",
      health: null,
      started_at: null,
      image: "jbrain/worker:edge",
    },
  ],
};

const METRICS: OpsMetrics = {
  mem_total_bytes: 121 * 2 ** 30,
  // free (22) + reclaimable cache (61) ≈ available (83); used ≈ 38 GB.
  mem_available_bytes: 83 * 2 ** 30,
  swap_total_bytes: 0,
  swap_free_bytes: 0,
  disk_total_bytes: 1875 * 2 ** 30,
  disk_free_bytes: 1600 * 2 ** 30,
  load_1m: 0.55,
  load_5m: 0.64,
  load_15m: 0.62,
  uptime_seconds: 5 * 3600 + 40 * 60,
  gpu_busy_percent: 41,
  apu_power_w: 28.5,
  fan_rpm: { "CPU fan": 2100, "System fan": 1850 },
  // The 120B's weights live in GTT (33 GB), so its llama-server RSS is small.
  gpu_mem: {
    gtt_used_bytes: 33 * 2 ** 30,
    gtt_total_bytes: 120 * 2 ** 30,
    vram_used_bytes: 2 * 2 ** 30,
    vram_total_bytes: 4 * 2 ** 30,
  },
  mem_breakdown: {
    MemFree: 22 * 2 ** 30,
    Buffers: 1 * 2 ** 30,
    Cached: 60 * 2 ** 30,
  },
  containers: [{ service: "api", mem_bytes: 87 * 2 ** 20 }],
  processes: [
    {
      service: "local-llm",
      pid: 101,
      rss_bytes: 1.3 * 2 ** 30,
      command: "llama-server --model /models/gpt-oss-120b/x.gguf",
    },
    { service: "api", pid: 401, rss_bytes: 87 * 2 ** 20, command: "uvicorn jbrain.main:app" },
  ],
  db: {
    db_size_bytes: 23 * 2 ** 20,
    note_count: 2,
    attachment_count: 5,
    attachment_bytes: 5 * 2 ** 20,
  },
  blobs: { file_count: 5, total_bytes: 5 * 2 ** 20 },
};

/** Default handler: status + metrics resolve, everything else 404s quietly so
 * telemetry (usage) and on-demand fetches (logs) don't error the screen. */
const HISTORY: MetricsHistory = {
  resolution: "raw",
  step_seconds: 60,
  since: "2026-06-22T00:00:00Z",
  until: "2026-06-22T02:00:00Z",
  points: [
    {
      t: "2026-06-22T00:00:00Z",
      load_1m: 0.5,
      load_1m_max: 0.8,
      load_5m: 0.5,
      load_15m: 0.5,
      mem_used_bytes: 60 * 2 ** 30,
      mem_used_max_bytes: 64 * 2 ** 30,
      mem_total_bytes: 128 * 2 ** 30,
      swap_used_bytes: 0,
      disk_used_bytes: 500 * 2 ** 30,
      disk_used_max_bytes: 500 * 2 ** 30,
      disk_total_bytes: 2000 * 2 ** 30,
      gpu_busy_percent: 40,
      gpu_busy_max: 55,
      fan_rpm_max: 2100,
      power_w: 14.0,
      power_w_max: 20.0,
      net_rx_bps: 8 * 2 ** 20,
      net_tx_bps: 2 * 2 ** 20,
      disk_read_bps: 30 * 2 ** 20,
      disk_write_bps: 12 * 2 ** 20,
    },
    {
      t: "2026-06-22T01:00:00Z",
      load_1m: 1.5,
      load_1m_max: 1.9,
      load_5m: 1.2,
      load_15m: 1.0,
      mem_used_bytes: 72 * 2 ** 30,
      mem_used_max_bytes: 78 * 2 ** 30,
      mem_total_bytes: 128 * 2 ** 30,
      swap_used_bytes: 0,
      disk_used_bytes: 520 * 2 ** 30,
      disk_used_max_bytes: 520 * 2 ** 30,
      disk_total_bytes: 2000 * 2 ** 30,
      gpu_busy_percent: 70,
      gpu_busy_max: 88,
      fan_rpm_max: 2600,
      power_w: 31.0,
      power_w_max: 42.0,
      net_rx_bps: 14 * 2 ** 20,
      net_tx_bps: 3 * 2 ** 20,
      disk_read_bps: 50 * 2 ** 20,
      disk_write_bps: 20 * 2 ** 20,
    },
  ],
};

/** The live misconfiguration this card exists for: `ttm.pages_limit` at 124 GiB on a
 *  121 GiB box, which DISABLES it — the GTT over-commit it refuses cannot occur above total
 *  RAM. It sat like that for weeks and the product said nothing. */
const HOST_SETTINGS_BAD = {
  settings: [
    {
      key: "ttm.pages_limit",
      current: "124 GiB",
      expected: "< 121 GiB (we set 105)",
      ok: false,
      impact:
        "DISABLED — the limit is at or above total RAM, so a GTT over-commit cannot be refused.",
      remedy: "Edit ttm.pages_limit in /etc/default/grub, run update-grub, reboot.",
      needs_host: true,
    },
    {
      key: "vm.swappiness",
      current: "10",
      expected: "<= 10",
      ok: true,
      impact: "Keeps the box out of swap.",
      remedy: "Ops -> Update applies this.",
      needs_host: false,
    },
  ],
  ok: false,
  needs_host: ["ttm.pages_limit"],
};

const HOST_SETTINGS_OK = {
  settings: [
    {
      key: "vm.swappiness",
      current: "10",
      expected: "<= 10",
      ok: true,
      impact: "Keeps the box out of swap.",
      remedy: "Ops -> Update applies this.",
      needs_host: false,
    },
  ],
  ok: true,
  needs_host: [],
};

/** Open a tile's page. A tile's name leads with its title, then its one-line state. */
async function openTile(title: string) {
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(`^${title}:`) }));
}

function container(service: string, state = "running") {
  return {
    service,
    state,
    health: null,
    started_at: state === "running" ? "2026-06-10T08:00:00Z" : null,
    image: `jbrain2-${service}:local`,
  };
}

function baseMock(input: RequestInfo | URL): Response | null {
  const path = String(input);
  if (path === "/api/ops/status") return json(STATUS);
  if (path === "/api/ops/metrics") return json(METRICS);
  if (path.startsWith("/api/ops/metrics/history")) return json(HISTORY);
  return null;
}

/** Two panels, and deliberately not both healthy: the half of this card that matters is the
 *  one that has stopped reporting. */
const PANELS = {
  panels: [
    {
      device_id: "panel-ellie",
      name: "Ellie",
      role: "jpet",
      reported_at: "2026-09-23T17:00:00Z",
      version: "0.2.94",
      age_s: 240,
      report: { screen: "dark", uptime_ms: 7_200_000, restart_why: "ota-park", crash_phase: -1 },
    },
    {
      device_id: "panel-mabel",
      name: "the other one",
      role: "jpet",
      reported_at: "2026-09-23T08:00:00Z",
      version: "0.2.89",
      age_s: 9 * 3600,
      report: { screen: "awake", blit_fail_total: 249, blit_recov: 1, mic_peak: 0 },
    },
  ],
};

describe("OpsScreen panels", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("flags a panel that stopped reporting on its tile, and lists the fleet on its page", async () => {
    /* THE TILE HAS TO INTERRUPT OR IT IS NOT AN INSTRUMENT. Everything the page shows arrived
       in a telemetry body the owner could not read without a terminal (CLAUDE.md #10) — a tile
       that had to be opened to find a dead panel would be the same blind spot with more steps. */
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") return json(PANELS);
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    const { unmount } = render(<OpsScreen />);
    expect(
      await screen.findByRole("button", { name: /^Panels: 1 not reporting/ }),
    ).toBeInTheDocument();
    await openTile("Panels");
    expect(await screen.findByText("Ellie")).toBeInTheDocument();
    expect(screen.getByText("the other one")).toBeInTheDocument();
    expect(screen.getByText("9 hours ago")).toBeInTheDocument();
    unmount();

    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") return json({ panels: [PANELS.panels[0]] });
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    render(<OpsScreen />);
    expect(await screen.findByRole("button", { name: /^Panels: 1 reporting/ })).toBeInTheDocument();
  });

  it("shows the version, the screen stage and what is wrong", async () => {
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") return json(PANELS);
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    render(<OpsScreen />);
    await openTile("Panels");

    // "Did the update land" is the question this was built for.
    expect(await screen.findByText("0.2.94")).toBeInTheDocument();
    // And the reading that stops a sleeping panel being read as a stalled render task.
    expect(screen.getByText(/screen dark/)).toBeInTheDocument();
    expect(screen.getByText(/249 failed frames/)).toBeInTheDocument();
    expect(screen.getByText(/heard nothing since the last report/)).toBeInTheDocument();
  });

  it("says a panel has never reported rather than showing it as merely old", async () => {
    /* A different fault from having gone quiet, with a different first move. */
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") {
        return json({
          panels: [
            {
              device_id: "fresh",
              name: "Rae",
              reported_at: "",
              version: "",
              age_s: -1,
              report: {},
            },
          ],
        });
      }
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    render(<OpsScreen />);
    await openTile("Panels");
    expect(await screen.findByText(/has never reported/)).toBeInTheDocument();
  });

  it("refetches the fleet when the owner presses Refresh", async () => {
    /* The press right after an update is the owner asking whether the new version landed. A
       page that answered with the pre-update reading would be worse than one that made him
       reload the app. */
    let version = "0.2.93";
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") {
        return json({
          panels: [{ ...PANELS.panels[0], version, report: { screen: "awake" } }],
        });
      }
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    render(<OpsScreen />);
    await openTile("Panels");
    expect(await screen.findByText("0.2.93")).toBeInTheDocument();

    version = "0.2.94";
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("0.2.94")).toBeInTheDocument();
  });

  it("badges a display, and leaves a pet unmarked", async () => {
    /* The exception is what earns a word. Every panel in the house is a pet, so badging both
       would put a label on every row answering a question nobody asked. */
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") {
        return json({ panels: [{ ...PANELS.panels[0], name: "Jeff", role: "display" }] });
      }
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    render(<OpsScreen />);
    await openTile("Panels");
    expect(await screen.findByText("display")).toBeInTheDocument();
  });

  it("does not take the whole screen down when the fleet cannot be read", async () => {
    /* One question among many on Ops; a 500 here must not cost the owner the service list
       he came for. */
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/endpoint/status") return new Response(null, { status: 500 });
      return baseMock(input) ?? new Response(null, { status: 404 });
    });
    render(<OpsScreen />);
    expect(await screen.findByRole("button", { name: /^Panels: unavailable/ })).toBeInTheDocument();
    await openTile("Services");
    expect(await screen.findByRole("button", { name: /Core/ })).toBeInTheDocument();
  });
});

describe("OpsScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    resetEngineStore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function serve(extra?: (path: string, init?: RequestInit) => Response | null) {
    fetchMock.mockImplementation(async (input, init) => {
      const hit = extra?.(String(input), init) ?? baseMock(input);
      return hit ?? new Response(null, { status: 404 });
    });
  }

  function serveContainers(containers: ReturnType<typeof container>[]) {
    serve((path) => (path === "/api/ops/status" ? json({ containers }) : null));
  }

  it("opens on the live vitals, one Update, and no developer switches", async () => {
    serve();
    render(<OpsScreen />);

    const vitals = await screen.findByRole("region", { name: "Vitals" });
    await waitFor(() => expect(vitals).toHaveTextContent("41%"));
    // used = total - free - cache = 121 - 22 - 61 = 38 GB (~31%): reclaimable cache is
    // available, not used.
    expect(vitals).toHaveTextContent(/31%\s*38\.0 GB \/ 121\.0 GB/);
    expect(vitals).toHaveTextContent("28.5");
    expect(vitals).toHaveTextContent("load 0.55 · 0.64 · 0.62 · up 5h 40m");
    expect(screen.getAllByRole("button", { name: "Update" })).toHaveLength(1);
    // The owner settled on Flash-Next: the update's engine switches are gone from the PWA.
    expect(screen.queryByRole("switch", { name: /Track newest llama.cpp/ })).toBeNull();
    expect(screen.queryByRole("switch", { name: /Fast Qwen loads/ })).toBeNull();
    expect(screen.queryByRole("group", { name: "Local engine" })).toBeNull();
  });

  it("names a down service in a banner that opens Services", async () => {
    // STATUS has worker exited: a stock service, so it is down, not off.
    serve();
    render(<OpsScreen />);
    const banner = await screen.findByRole("button", { name: /worker down/ });
    expect(screen.getByRole("button", { name: /^Services: 1 down/ })).toBeInTheDocument();
    fireEvent.click(banner);
    expect(await screen.findByRole("button", { name: /Core/ })).toHaveTextContent("down");
  });

  it("groups services by what they are for; a group expands to show state, health, and image", async () => {
    serve();
    render(<OpsScreen />);
    await openTile("Services");

    // Both api and worker land in Core.
    fireEvent.click(await screen.findByRole("button", { name: /Core/ }));
    expect(screen.getByText("api")).toBeInTheDocument();
    expect(screen.getByText("worker")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("healthy")).toBeInTheDocument();
    expect(screen.getByText("exited")).toBeInTheDocument();
    // Each row says what the service is, in plain words.
    expect(screen.getByText(/app server/)).toBeInTheDocument();
  });

  it("files every running service under its purpose, with nothing left in Other", async () => {
    serveContainers([
      container("proxy"),
      container("flash-next"),
      container("rapidocr"),
      container("byparr"),
      container("browser"),
      container("pysandbox"),
      container("wall"),
      container("endpoint"),
      container("minecraft"),
      container("jlaunch"),
    ]);
    render(<OpsScreen />);
    await openTile("Services");

    const expected: Record<string, string[]> = {
      Core: ["proxy"],
      Models: ["flash-next", "rapidocr"],
      "Assistant tools": ["byparr", "browser", "pysandbox"],
      Devices: ["wall", "endpoint"],
      Apps: ["minecraft", "jlaunch"],
    };
    for (const [group, services] of Object.entries(expected)) {
      const head = await screen.findByRole("button", { name: new RegExp(`^${group}`) });
      fireEvent.click(head);
      const card = head.closest("section") as HTMLElement;
      for (const svc of services) expect(within(card).getByText(svc)).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: /^Other/ })).toBeNull();
  });

  it("shows a service switched off on purpose as grey 'off', never as down", async () => {
    // comfyui and jcode are opt-in; local-llm is the engine the owner did not choose.
    setEngineState(
      engineState({
        desired: "flash-next",
        effective: "flash-next",
        running: ["flash-next"],
        services: {
          standard: { service: "local-llm", state: "exited" },
          "flash-next": { service: "flash-next", state: "running" },
        },
      }),
    );
    serveContainers([
      container("api"),
      container("flash-next"),
      container("local-llm", "exited"),
      container("comfyui", "exited"),
      container("jcode", "exited"),
    ]);
    render(<OpsScreen />);

    expect(
      await screen.findByRole("button", { name: /^Services: 2 up · 3 off/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /down/ })).toBeNull();
    await openTile("Services");
    const models = await screen.findByRole("button", { name: /^Models/ });
    expect(models).toHaveTextContent("2 off");
    expect(models).toHaveTextContent("all up");
    // A group of nothing but switched-off extras reads "off", not "all up".
    expect(screen.getByRole("button", { name: /^Assistant tools/ })).toHaveTextContent("off");
    fireEvent.click(models);
    const card = models.closest("section") as HTMLElement;
    expect(within(card).getAllByText("off")).toHaveLength(2);
    expect(card.querySelectorAll(".ops-sdot-off")).toHaveLength(2);
  });

  it("treats the chosen engine stopping as a failure even though it is opt-in", async () => {
    setEngineState(
      engineState({
        desired: "flash-next",
        effective: "flash-next",
        running: [],
        services: {
          standard: { service: "local-llm", state: "exited" },
          "flash-next": { service: "flash-next", state: "exited" },
        },
      }),
    );
    serveContainers([container("api"), container("flash-next", "exited")]);
    render(<OpsScreen />);
    expect(await screen.findByRole("button", { name: /flash-next down/ })).toBeInTheDocument();
  });

  it("the History graphs are open on the 6h window and switch range", async () => {
    serve();
    render(<OpsScreen />);

    // Open from the start: charts render on mount, fetched over the 6h window.
    expect(await screen.findByText("CPU load")).toBeInTheDocument();
    expect(screen.getByText("Fan")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("metrics/history?range=6h"))).toBe(
      true,
    );
    // Peak label reflects the bucket MAX band (load_1m_max 1.9), not the avg line (1.5) — so a
    // spike shorter than a bucket still shows as the peak.
    expect(screen.getByText("1.90 peak")).toBeInTheDocument();
    expect(screen.getByText("2 30s buckets")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u).includes("metrics/history?range=7d")),
      ).toBe(true),
    );

    // Refresh means everything, the graphs included.
    const before = fetchMock.mock.calls.filter(([u]) =>
      String(u).includes("metrics/history?range=7d"),
    ).length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([u]) => String(u).includes("metrics/history?range=7d")).length,
      ).toBeGreaterThan(before),
    );
  });

  it("the Memory page splits used from reclaimable cache", async () => {
    serve();
    render(<OpsScreen />);
    expect(await screen.findByRole("button", { name: /^Memory: 31% used/ })).toBeInTheDocument();
    await openTile("Memory");
    expect(await screen.findByText(/reclaimable cache/)).toBeInTheDocument();
    expect(screen.getAllByText(/available/).length).toBeGreaterThan(0);
  });

  it("the Storage page holds the database, disk and fans that left the vitals", async () => {
    serve();
    render(<OpsScreen />);
    await openTile("Storage");
    expect(await screen.findByText("Database")).toBeInTheDocument();
    expect(screen.getByText(/2 notes · 5 files/)).toBeInTheDocument();
    expect(screen.getByText("CPU fan 2100rpm · System fan 1850rpm")).toBeInTheDocument();
  });

  it("server update: tap-again confirm, then polls running → done with Reload app", async () => {
    let updatePolls = 0;
    serve((path, init) => {
      if (path === "/api/ops/update" && init?.method === "POST")
        return json({ updater: "u1" }, 202);
      if (path === "/api/ops/update/status") {
        updatePolls += 1;
        return updatePolls < 2
          ? json({ state: "running", exit_code: null, log_tail: "[update] building" })
          : json({ state: "exited", exit_code: 0, log_tail: "[update] done" });
      }
      return null;
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(await screen.findByRole("button", { name: "Update" }));
      fireEvent.click(screen.getByRole("button", { name: "Tap again to update" }));

      expect(await screen.findByText("Updating…")).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(3000));
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByText("Update complete.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Reload app" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a service row pulls its own log tail and copies it with one button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    serve((path) =>
      path.startsWith("/api/ops/logs/api")
        ? new Response("api line one\napi line two", { status: 200 })
        : null,
    );

    render(<OpsScreen />);
    await openTile("Services");
    fireEvent.click(await screen.findByRole("button", { name: /Core/ }));
    fireEvent.click(screen.getByText("api"));

    expect(await screen.findByText("api line one", { exact: false })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy logs" }));

    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith("api line one\napi line two");
  });

  it("shows an error when the status request fails", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));
    render(<OpsScreen />);
    // History surfaces its own alert too, so assert the failure is reported, not that it's alone.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((el) => el.textContent?.includes("Request failed: 500"))).toBe(true);
  });

  it("offers Stop and Start but no per-service Rebuild, and stops a service", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    serve((path, init) => {
      if (path.startsWith("/api/ops/logs/")) return new Response("log", { status: 200 });
      if (path === "/api/ops/stop" && init?.method === "POST")
        return new Response(null, { status: 202 });
      return null;
    });

    render(<OpsScreen />);
    await openTile("Services");
    fireEvent.click(await screen.findByRole("button", { name: /Core/ }));
    // api is running -> Stop; worker is exited -> Start.
    fireEvent.click(screen.getByText("api"));
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("worker"));
    expect(await screen.findByRole("button", { name: "Start" })).toBeInTheDocument();
    // Update rebuilds everything; a per-service rebuild is no longer offered.
    expect(screen.queryByRole("button", { name: "Rebuild" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, i]) => String(u) === "/api/ops/stop" && (i?.method ?? "") === "POST",
        ),
      ).toBe(true),
    );
  });

  it("opens the Runs surface from its tile", async () => {
    serve((path) => {
      // The list is filtered server-side, so the client sends a query string.
      if (path === "/api/runs" || path.startsWith("/api/runs?"))
        return json([
          {
            id: "r1",
            kind: "integration",
            status: "running",
            name: "integrate_note",
            started_at: new Date().toISOString(),
            duration_ms: null,
            step_count: 3,
            cost_tokens: 4100,
            last_error: null,
            progress_note: null,
          },
        ]);
      if (path.startsWith("/api/runs/stats"))
        return json({
          active: 1,
          failed_today: 0,
          tokens_today: 4100,
          by_kind: { agent: 0, integration: 1, pipeline: 0 },
        });
      return null;
    });

    render(<OpsScreen />);
    await openTile("Runs");
    expect(await screen.findByText("Recent runs")).toBeInTheDocument();
    expect(await screen.findByText("integrate_note")).toBeInTheDocument();
  });

  it("the Engine page lets the owner turn the chat cache off and set its disk budget", async () => {
    // FLASH_NEXT_ENGINE_PLAN F4: both knobs live in the PWA, never in a host file.
    const puts: unknown[] = [];
    serve((path, init) => {
      if (path.endsWith("/api/settings") && init?.method === "PUT") {
        puts.push(JSON.parse(String(init.body)));
        return json({});
      }
      if (path.endsWith("/api/settings")) {
        return json({
          llm_kv_conversation_cache: true,
          llm_kv_prefix_budget_gb: 40,
          llm_kv_restore_gate: "failed",
        });
      }
      return null;
    });

    render(<OpsScreen />);
    await openTile("Engine");

    const toggle = await screen.findByRole("switch", { name: /Keep chats on disk/ });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    // The privacy scope and the restore gate are said where the switch is.
    expect(screen.getByText(/Brain chats never leave the database/)).toBeInTheDocument();
    expect(screen.getAllByText(/slot check failed/).length).toBeGreaterThan(0);
    fireEvent.click(toggle);
    await waitFor(() => expect(puts).toEqual([{ llm_kv_conversation_cache: false }]));

    const budget = screen.getByRole("combobox", { name: "Prompt cache disk budget" });
    await waitFor(() => expect(budget).toHaveValue("40"));
    fireEvent.change(budget, { target: { value: "80" } });
    await waitFor(() =>
      expect(puts).toEqual([{ llm_kv_conversation_cache: false }, { llm_kv_prefix_budget_gb: 80 }]),
    );
    expect(budget).toHaveValue("80");
  });

  it("shows a stored budget outside the presets and puts it back on a refusal", async () => {
    serve((path, init) => {
      if (path.endsWith("/api/settings") && init?.method === "PUT") {
        return new Response(null, { status: 500 });
      }
      if (path.endsWith("/api/settings")) {
        return json({ llm_kv_conversation_cache: true, llm_kv_prefix_budget_gb: 33 });
      }
      return null;
    });

    render(<OpsScreen />);
    await openTile("Engine");

    const budget = await screen.findByRole("combobox", { name: "Prompt cache disk budget" });
    await waitFor(() => expect(budget).toHaveValue("33"));
    fireEvent.change(budget, { target: { value: "120" } });
    await waitFor(() => expect(budget).toHaveValue("33"));
  });

  it("turns the Host tile red when a setting is wrong, and explains it on its page", async () => {
    // A health panel nobody opens is not a health panel. The setting that cost a freeze was
    // invisible for weeks; a quiet tile would have kept it that way.
    serve((path) => (path === "/api/ops/host-settings" ? json(HOST_SETTINGS_BAD) : null));
    render(<OpsScreen />);

    const tile = await screen.findByRole("button", { name: /^Host: 1 issue/ });
    expect(tile.querySelector(".ops-tile-dot.bad")).not.toBeNull();
    fireEvent.click(tile);
    expect(await screen.findByText("ttm.pages_limit")).toBeInTheDocument();
    expect(screen.getByText(/124 GiB/)).toBeInTheDocument();
    expect(screen.getByText(/DISABLED/)).toBeInTheDocument();
    // And it says plainly that no button here will fix it.
    expect(screen.getByText(/Needs host access/)).toBeInTheDocument();
  });

  it("keeps the Host tile quiet when every host setting holds", async () => {
    serve((path) => (path === "/api/ops/host-settings" ? json(HOST_SETTINGS_OK) : null));
    render(<OpsScreen />);
    const tile = await screen.findByRole("button", { name: /^Host: all good/ });
    expect(tile.querySelector(".ops-tile-dot")).toBeNull();
  });

  it("survives the host-settings check being unavailable", async () => {
    // Best-effort: an older box without the endpoint must not break the Ops screen.
    serve();
    render(<OpsScreen />);
    expect(await screen.findByRole("button", { name: /^Host: unavailable/ })).toBeInTheDocument();
    await openTile("Services");
    expect(await screen.findByRole("button", { name: /Core/ })).toBeInTheDocument();
  });

  it("a page climbs back to the grid", async () => {
    serve();
    render(<OpsScreen />);
    await openTile("Storage");
    expect(await screen.findByText("Database")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.queryByText("Database")).toBeNull();
  });
});

describe("OpsScreen engine", () => {
  afterEach(() => {
    resetEngineStore();
    vi.unstubAllGlobals();
  });

  function serveEngine(state = engineState()) {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input) => {
        const hit = baseMock(input);
        if (hit) return hit;
        if (String(input) === "/api/settings/llm/engine") return json(state);
        return json({ detail: "nope" }, 404);
      }),
    );
  }

  it("names the serving engine on its tile, and its page is a status, not a switch", async () => {
    resetEngineStore();
    serveEngine(
      engineState({ desired: "flash-next", effective: "flash-next", running: ["flash-next"] }),
    );
    setEngineState(
      engineState({ desired: "flash-next", effective: "flash-next", running: ["flash-next"] }),
    );
    render(<OpsScreen />);
    expect(await screen.findByRole("button", { name: /^Engine: Flash-Next/ })).toBeInTheDocument();
    await openTile("Engine");
    expect(await screen.findByText("Flash-Next serving")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Local engine" })).toBeNull();
  });

  it("the engine banner's Details lands on the Engine page", async () => {
    resetEngineStore();
    serveEngine();
    render(<OpsScreen />);
    await screen.findByRole("region", { name: "Vitals" });
    act(() => openEngineCard());
    expect(await screen.findByText("Standard serving")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back" })).toHaveTextContent("Engine");
  });
});
