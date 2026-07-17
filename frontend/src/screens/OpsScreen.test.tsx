import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MetricsHistory, OpsMetrics, OpsStatus } from "../api/client";
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

function baseMock(input: RequestInfo | URL): Response | null {
  const path = String(input);
  if (path === "/api/ops/status") return json(STATUS);
  if (path === "/api/ops/metrics") return json(METRICS);
  if (path.startsWith("/api/ops/metrics/history")) return json(HISTORY);
  return null;
}

describe("OpsScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("groups services by role; a group expands to show state, health, and image", async () => {
    fetchMock.mockImplementation(
      async (input) => baseMock(input) ?? new Response(null, { status: 404 }),
    );

    render(<OpsScreen />);

    // Both api and worker land in the collapsed Core group.
    const core = await screen.findByRole("button", { name: /Core/ });
    fireEvent.click(core);

    expect(screen.getByText("api")).toBeInTheDocument();
    expect(screen.getByText("worker")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("healthy")).toBeInTheDocument();
    expect(screen.getByText("exited")).toBeInTheDocument();
    expect(screen.getByText("jbrain/api:edge", { exact: false })).toBeInTheDocument();
  });

  it("files wall under the Display group, not Other", async () => {
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/ops/status") {
        return json({
          containers: [
            {
              service: "api",
              state: "running",
              health: "healthy",
              started_at: "2026-06-10T08:00:00Z",
              image: "jbrain2-api:local",
            },
            {
              service: "wall",
              state: "running",
              health: null,
              started_at: "2026-06-10T08:00:00Z",
              image: "jbrain2-wall",
            },
          ],
        });
      }
      if (path === "/api/ops/metrics") return json(METRICS);
      if (path.startsWith("/api/ops/metrics/history")) return json(HISTORY);
      return new Response(null, { status: 404 });
    });

    render(<OpsScreen />);
    // The wall display gets its own "Display" group, not orphaned in "Other".
    fireEvent.click(await screen.findByRole("button", { name: /Display/ }));
    expect(screen.getByText("wall")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Other/ })).toBeNull();
  });

  it("the History card is expanded by default on the 6h window and switches range", async () => {
    fetchMock.mockImplementation(
      async (input) => baseMock(input) ?? new Response(null, { status: 404 }),
    );

    render(<OpsScreen />);

    // Open by default: charts render on mount, fetched over the 6h window.
    // "CPU load" and "Fan" are unique to the History card (System shows
    // "Load"/"Fans").
    expect(await screen.findByText("CPU load")).toBeInTheDocument();
    expect(screen.getByText("Fan")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("metrics/history?range=6h"))).toBe(
      true,
    );
    // Peak label reflects the bucket MAX band (load_1m_max 1.9), not the avg line
    // (1.5) — so a spike shorter than a bucket still shows as the peak.
    expect(screen.getByText("1.90 peak")).toBeInTheDocument();
    expect(screen.getByText("2 30s buckets")).toBeInTheDocument();

    // Picking a range refetches with that window.
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u).includes("metrics/history?range=7d")),
      ).toBe(true),
    );

    // The top Refresh button refreshes the History graphs too (not just status +
    // metrics): a fresh fetch for the current window fires.
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

  it("the System card is expanded by default with the embedded Server update", async () => {
    fetchMock.mockImplementation(
      async (input) => baseMock(input) ?? new Response(null, { status: 404 }),
    );

    render(<OpsScreen />);

    expect(await screen.findByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Database")).toBeInTheDocument();
    expect(screen.getByText("Load")).toBeInTheDocument();
    // Fan telemetry renders one labelled row, RPM per fan.
    expect(screen.getByText("Fans")).toBeInTheDocument();
    expect(screen.getByText("CPU fan 2100rpm · System fan 1850rpm")).toBeInTheDocument();
    // APU package power renders its own row.
    expect(screen.getByText("Power")).toBeInTheDocument();
    expect(screen.getByText("28.5 W", { exact: false })).toBeInTheDocument();
    // Server update is folded into the Load row, not a separate footer card.
    expect(screen.getByRole("button", { name: "Update server" })).toBeInTheDocument();
  });

  it("counts reclaimable cache as available: memory 'used' is non-reclaimable only", async () => {
    fetchMock.mockImplementation(
      async (input) => baseMock(input) ?? new Response(null, { status: 404 }),
    );

    render(<OpsScreen />);

    // used = total - free - cache = 121 - 22 - 61 = 38 GB (~31%), NOT the
    // total-available figure (which would count reclaimable cache as used).
    expect(await screen.findByText(/31% · 38\.0 GB \/ 121\.0 GB/)).toBeInTheDocument();

    // Expand the breakdown: cache is grouped as available, not used.
    fireEvent.click(screen.getByRole("button", { name: /System memory/ }));
    // The footer reports available = cache + free with the reclaimable cache called
    // out (lowercase — the note's "Reclaimable" is a separate element).
    expect(await screen.findByText(/reclaimable cache/)).toBeInTheDocument();
    expect(screen.getAllByText(/available/).length).toBeGreaterThan(0);
  });

  it("server update: tap-again confirm, then polls running → done with Reload app", async () => {
    let updatePolls = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      const base = baseMock(input);
      if (base) return base;
      if (path === "/api/ops/update" && init?.method === "POST")
        return json({ updater: "u1" }, 202);
      if (path === "/api/ops/update/status") {
        updatePolls += 1;
        return updatePolls < 2
          ? json({ state: "running", exit_code: null, log_tail: "[update] building" })
          : json({ state: "exited", exit_code: 0, log_tail: "[update] done" });
      }
      return new Response(null, { status: 404 });
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(await screen.findByRole("button", { name: "Update server" }));
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
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      const base = baseMock(input);
      if (base) return base;
      if (path.startsWith("/api/ops/logs/api")) {
        return new Response("api line one\napi line two", { status: 200 });
      }
      return new Response(null, { status: 404 });
    });

    render(<OpsScreen />);
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

    // With everything down the now-open History card also surfaces its own
    // alert, so assert the failure is reported rather than that it's the lone one.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((el) => el.textContent?.includes("Request failed: 500"))).toBe(true);
  });

  it("rebuilds a single service and shows the one-shot's progress to completion", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let rebuildPolls = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      const base = baseMock(input);
      if (base) return base;
      if (path.startsWith("/api/ops/logs/api")) return new Response("api log", { status: 200 });
      if (path === "/api/ops/rebuild" && init?.method === "POST") {
        return json({ oneshot: "jbrain-rebuild-1" }, 202);
      }
      if (path === "/api/ops/rebuild/status") {
        rebuildPolls += 1;
        return rebuildPolls < 2
          ? json({ state: "running", exit_code: null, log_tail: "[rebuild] api building" })
          : json({ state: "exited", exit_code: 0, log_tail: "[rebuild] api done" });
      }
      return new Response(null, { status: 404 });
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(await screen.findByRole("button", { name: /Core/ }));
      fireEvent.click(screen.getByText("api"));
      // The service row offers Restart AND Rebuild.
      fireEvent.click(await screen.findByRole("button", { name: "Rebuild" }));
      // The button flips to a disabled "Rebuilding…" while the one-shot runs.
      expect(await screen.findByRole("button", { name: "Rebuilding…" })).toBeDisabled();
      await act(() => vi.advanceTimersByTimeAsync(2000));
      await act(() => vi.advanceTimersByTimeAsync(2000));
      expect(screen.getByText("Rebuild complete.")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers Stop for a running service and Start for a stopped one, and stops it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      const base = baseMock(input);
      if (base) return base;
      if (path.startsWith("/api/ops/logs/")) return new Response("log", { status: 200 });
      if (path === "/api/ops/stop" && init?.method === "POST")
        return new Response(null, { status: 202 });
      return new Response(null, { status: 404 });
    });

    render(<OpsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /Core/ }));
    // api is running -> Stop; worker is exited -> Start.
    fireEvent.click(screen.getByText("api"));
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("worker"));
    expect(await screen.findByRole("button", { name: "Start" })).toBeInTheDocument();

    // Stopping api hits the stop endpoint.
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, i]) => String(u) === "/api/ops/stop" && (i?.method ?? "") === "POST",
        ),
      ).toBe(true),
    );
  });

  it("opens the Runs surface from the Ops header (Direction C, reachable from Ops)", async () => {
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      const base = baseMock(input);
      if (base) return base;
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
      // The sweep-trigger list is sibling Track B's; absent here.
      if (path === "/api/ops/triggers") return new Response(null, { status: 404 });
      return new Response(null, { status: 500 });
    });

    render(<OpsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Runs" }));

    // The Runs sub-screen mounts and loads its log.
    expect(await screen.findByText("Recent runs")).toBeInTheDocument();
    expect(await screen.findByText("integrate_note")).toBeInTheDocument();
  });
});
