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
  mem_available_bytes: 55 * 2 ** 30,
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
  containers: [{ service: "api", mem_bytes: 87 * 2 ** 20 }],
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
      load_5m: 0.5,
      load_15m: 0.5,
      mem_used_bytes: 60 * 2 ** 30,
      mem_total_bytes: 128 * 2 ** 30,
      swap_used_bytes: 0,
      disk_used_bytes: 500 * 2 ** 30,
      disk_total_bytes: 2000 * 2 ** 30,
      gpu_busy_percent: 40,
      fan_rpm_max: 2100,
      power_w: 14.0,
    },
    {
      t: "2026-06-22T01:00:00Z",
      load_1m: 1.5,
      load_5m: 1.2,
      load_15m: 1.0,
      mem_used_bytes: 72 * 2 ** 30,
      mem_total_bytes: 128 * 2 ** 30,
      swap_used_bytes: 0,
      disk_used_bytes: 520 * 2 ** 30,
      disk_total_bytes: 2000 * 2 ** 30,
      gpu_busy_percent: 70,
      fan_rpm_max: 2600,
      power_w: 31.0,
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
    // Peak label reflects the higher of the two buckets (load 1.5).
    expect(screen.getByText("1.50 peak")).toBeInTheDocument();
    expect(screen.getByText("2 30s buckets")).toBeInTheDocument();

    // Picking a range refetches with that window.
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u).includes("metrics/history?range=7d")),
      ).toBe(true),
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

  it("opens the Runs surface from the Ops header (Direction C, reachable from Ops)", async () => {
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      const base = baseMock(input);
      if (base) return base;
      if (path === "/api/runs")
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
          },
        ]);
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
