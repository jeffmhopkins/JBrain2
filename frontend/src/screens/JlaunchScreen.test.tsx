import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { JlaunchJob, JlaunchSpec } from "../jlaunch/types";
import { JlaunchScreen } from "./JlaunchScreen";

const SPEC = {
  name: "growbot_look",
  title: "GrowBot look",
  est_hours: "2-4 h",
  all_cores: true,
  disk_gb: 3,
  notes: "Stand in place and turn.",
} as unknown as JlaunchSpec;

function job(id: string, state: string): JlaunchJob {
  return {
    id,
    state,
    title: `Run ${id}`,
    spec_name: "growbot_look",
    phase: "train",
    started_at: "2026-11-14T08:00:00Z",
    finished_at: state === "running" ? null : "2026-11-14T09:00:00Z",
  } as unknown as JlaunchJob;
}

afterEach(() => vi.restoreAllMocks());

describe("JlaunchScreen", () => {
  it("lists jobs compactly and starts a run only from the expanded Start button", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([]);
    const start = vi.spyOn(api, "jlaunchStart").mockRejectedValue(new Error("stop here"));
    vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<JlaunchScreen onClose={() => {}} />);
    expect(screen.getByRole("heading", { name: /Jobs/ })).toBeTruthy();
    const title = await screen.findByText("GrowBot look");
    // Collapsed: the long notes stay hidden until the row is opened.
    expect(screen.queryByText("Stand in place and turn.")).toBeNull();
    fireEvent.click(title);
    fireEvent.click(screen.getByText("Stand in place and turn."));
    expect(start).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Start GrowBot look" }));
    expect(start).toHaveBeenCalledWith("growbot_look");
  });

  it("shows runs on their own tab and deletes only a finished one", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([job("a1", "running"), job("b2", "failed")]);
    const del = vi.spyOn(api, "jlaunchDelete").mockResolvedValue();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Runs/ }));
    expect(screen.getByText("Run a1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete run Run a1" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Delete run Run b2" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("b2"));
  });
});
