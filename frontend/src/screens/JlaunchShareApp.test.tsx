import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { PublicJlaunchRun } from "../jlaunch/types";
import { JlaunchShareApp } from "./JlaunchShareApp";

const RUN: PublicJlaunchRun = {
  title: "Erdos-Straus census to 10^12",
  spec_name: "erdos_straus_1e12",
  repo: "https://github.com/jeffmhopkins/Erd-s-Straus-attack",
  state: "succeeded",
  verify_ok: true,
  headline: [
    "hard primes: 1170090020",
    "max minimal R: 107 at p = 8803369 (record R=107 stands)",
    "R >= 87 counts: {87: 3}",
  ],
  machine: { cpu: "AMD Ryzen AI Max+ 395", cores: 32, mem_bytes: 137438953472 },
  phase_times: [{ phase: "run", seconds: 27900 }],
  artifact_name: "es_1e12_artifacts.tar.gz",
  artifact_bytes: 432013312,
  has_artifact: true,
  created_at: "2026-08-03T00:00:00+00:00",
  finished_at: "2026-08-03T07:45:00+00:00",
};

afterEach(() => {
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

describe("JlaunchShareApp", () => {
  it("renders the headline, machine specs, and a download link", async () => {
    window.history.pushState({}, "", "/results/tok123");
    vi.spyOn(api, "jlaunchShareView").mockResolvedValue(RUN);

    render(<JlaunchShareApp />);

    await waitFor(() => expect(screen.getByText(/Erdos-Straus census/)).toBeInTheDocument());
    expect(screen.getByText(/record R=107 stands/)).toBeInTheDocument();
    expect(screen.getByText(/AMD Ryzen AI Max\+ 395/)).toBeInTheDocument();
    const dl = screen.getByRole("link", { name: /Download es_1e12_artifacts\.tar\.gz/ });
    expect(dl).toHaveAttribute("href", "/api/jlaunch-share/tok123/artifact");
  });

  it("shows a not-found message on an unknown/revoked token", async () => {
    window.history.pushState({}, "", "/results/dead");
    vi.spyOn(api, "jlaunchShareView").mockRejectedValue(new ApiError(404, "unknown"));

    render(<JlaunchShareApp />);

    await waitFor(() =>
      expect(screen.getByText(/unknown or has been revoked/)).toBeInTheDocument(),
    );
  });
});
