import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OpsStatus } from "../api/client";
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

describe("OpsScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the container list with state and health badges", async () => {
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/ops/status") {
        return new Response(JSON.stringify(STATUS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`Unexpected fetch: ${String(input)}`);
    });

    render(<OpsScreen />);

    // Service names also appear as log-viewer options, so scope to the list.
    const list = within(await screen.findByRole("list"));
    expect(list.getByText("api")).toBeInTheDocument();
    expect(list.getByText("worker")).toBeInTheDocument();
    expect(list.getByText("running")).toBeInTheDocument();
    expect(list.getByText("healthy")).toBeInTheDocument();
    expect(list.getByText("exited")).toBeInTheDocument();
    expect(list.getByText("jbrain/api:edge")).toBeInTheDocument();
    // Exited container has no health badge to render.
    expect(list.queryByText("unhealthy")).not.toBeInTheDocument();
  });

  it("shows an error when the status request fails", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));

    render(<OpsScreen />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Request failed: 500");
  });

  it("AI usage card: k/M token formatting; null cost shows tokens only", async () => {
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/ops/status") return json(STATUS);
      if (path === "/api/ops/llm-usage") {
        return json({
          today: { input_tokens: 41_200, output_tokens: 12_400, cost_usd: 0.08 },
          month: { input_tokens: 1_240_000, output_tokens: 338_000, cost_usd: 2.41 },
          by_task: [
            { task: "note.extract", input_tokens: 982_000, output_tokens: 241_000, cost_usd: 1.83 },
            // No price-table entry: the line must omit the cost cleanly.
            { task: "vision.ocr", input_tokens: 2_400_000, output_tokens: 990, cost_usd: null },
          ],
          days: [],
        });
      }
      return new Response(null, { status: 500 });
    });

    render(<OpsScreen />);

    expect(await screen.findByText("41k in · 12k out · ~$0.08")).toBeInTheDocument();
    expect(screen.getByText("1.2M in · 338k out · ~$2.41")).toBeInTheDocument();
    expect(screen.getByText("note.extract")).toBeInTheDocument();
    expect(screen.getByText("982k in · 241k out · ~$1.83")).toBeInTheDocument();
    expect(screen.getByText("2.4M in · 990 out")).toBeInTheDocument();
  });

  it("usage failures stay quiet — the card shows its empty line, not an error", async () => {
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/ops/status") return json(STATUS);
      return new Response(null, { status: 500 });
    });

    render(<OpsScreen />);
    expect(await screen.findByText("AI usage")).toBeInTheDocument();
    expect(screen.getByText("no usage data yet.")).toBeInTheDocument();
  });

  it("opens the Runs surface from the Ops header (Direction C, reachable from Ops)", async () => {
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/ops/status") return json(STATUS);
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

  function mockExportFlow() {
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/ops/export" && init?.method === "POST") return json({}, 202);
      if (path === "/api/ops/export/status") {
        return json({
          state: "exited",
          exit_code: 0,
          log_tail: "[export] complete",
          filename: "export-20260610-120000.jbrain.tar",
        });
      }
      return new Response(null, { status: 500 });
    });
  }

  it("export: starts the one-shot, then triggers the download via an anchor, not navigation", async () => {
    // The download must never navigate the SPA (that remounts the app and can
    // swallow the download in standalone PWAs), so capture anchor clicks.
    const clicked: Array<{ href: string | null; download: string | null }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push({ href: this.getAttribute("href"), download: this.getAttribute("download") });
    });
    mockExportFlow();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(screen.getByRole("button", { name: "Export backup" }));
      expect(await screen.findByText("Building export archive…")).toBeInTheDocument();

      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(clicked).toEqual([
        {
          href: "/api/ops/export/file/export-20260610-120000.jbrain.tar",
          download: "export-20260610-120000.jbrain.tar",
        },
      ]);
      expect(screen.getByText(/downloaded\./)).toBeInTheDocument();
      // The card survives the download: the action buttons are still mounted.
      expect(screen.getByRole("button", { name: "Export backup" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("export: done state keeps a tappable link to the latest archive", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mockExportFlow();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(screen.getByRole("button", { name: "Export backup" }));
      await screen.findByText("Building export archive…");
      await act(() => vi.advanceTimersByTimeAsync(3000));

      const link = screen.getByRole("link", {
        name: "download export-20260610-120000.jbrain.tar",
      });
      expect(link).toHaveAttribute(
        "href",
        "/api/ops/export/file/export-20260610-120000.jbrain.tar",
      );
      expect(link).toHaveAttribute("download", "export-20260610-120000.jbrain.tar");
    } finally {
      vi.useRealTimers();
    }
  });

  it("import: file pick arms a tap-again confirm, then uploads and polls to done", async () => {
    let importPolls = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/ops/import/upload" && init?.method === "POST") {
        return json({ archive: "import-20260610-134500.jbrain.tar" }, 201);
      }
      if (path === "/api/ops/import/start" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({
          archive: "import-20260610-134500.jbrain.tar",
        });
        return json({}, 202);
      }
      if (path === "/api/ops/import/status") {
        importPolls += 1;
        return importPolls < 2
          ? json({ state: "running", exit_code: null, log_tail: "[import] restoring" })
          : json({ state: "exited", exit_code: 0, log_tail: "[import] complete" });
      }
      return new Response(null, { status: 500 });
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      const input = document.querySelector<HTMLInputElement>('input[type="file"]');
      if (!input) throw new Error("file input missing");
      const file = new File(["bytes"], "mine.jbrain.tar", { type: "application/x-tar" });
      fireEvent.change(input, { target: { files: [file] } });

      const confirm = await screen.findByRole("button", {
        name: "Import — replaces ALL current data",
      });
      fireEvent.click(confirm);
      expect(fetchMock).not.toHaveBeenCalledWith("/api/ops/import/upload", expect.anything());
      fireEvent.click(
        screen.getByRole("button", { name: "Tap again — current data is overwritten" }),
      );

      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByText("Importing…")).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByText("Import complete.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Reload app" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reset: arms a tap-again confirm and auto-disarms after 3s without firing", async () => {
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/ops/status") return json(STATUS);
      return new Response(null, { status: 500 });
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(await screen.findByRole("button", { name: "Reset DB" }));
      expect(
        screen.getByRole("button", { name: "Tap again — erases ALL notes and data" }),
      ).toBeInTheDocument();
      expect(fetchMock).not.toHaveBeenCalledWith("/api/ops/reset", expect.anything());

      // Untouched for 3s, the confirm disarms itself.
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByRole("button", { name: "Reset DB" })).toBeInTheDocument();
      expect(fetchMock).not.toHaveBeenCalledWith("/api/ops/reset", expect.anything());
    } finally {
      vi.useRealTimers();
    }
  });

  it("reset: double-tap starts the one-shot and polls to done with Reload app", async () => {
    let polls = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/ops/status") return json(STATUS);
      if (path === "/api/ops/reset" && init?.method === "POST") {
        return json({ oneshot: "jbrain-reset-1" }, 202);
      }
      if (path === "/api/ops/reset/status") {
        polls += 1;
        return polls < 2
          ? json({ state: "running", exit_code: null, log_tail: "[reset] truncating" })
          : json({ state: "exited", exit_code: 0, log_tail: "[reset] complete" });
      }
      return new Response(null, { status: 500 });
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<OpsScreen />);
      fireEvent.click(await screen.findByRole("button", { name: "Reset DB" }));
      fireEvent.click(
        screen.getByRole("button", { name: "Tap again — erases ALL notes and data" }),
      );

      expect(await screen.findByText("Resetting…")).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByText("Resetting…")).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByText("Reset complete.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Reload app" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
