import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OpsStatus } from "../api/client";
import { OpsScreen } from "./OpsScreen";

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
});
