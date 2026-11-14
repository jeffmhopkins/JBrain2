import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
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
    revision: id === "a1" ? 1 : 2,
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
    expect(screen.getByRole("button", { name: "Open v1 of Run a1" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete v1 of Run a1" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Delete v2 of Run b2" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("b2"));
  });

  it("tells the owner to stop a run the server says is still live", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([job("b2", "failed")]);
    vi.spyOn(api, "jlaunchDelete").mockRejectedValue(new ApiError(409, "still running"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Runs/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete v2 of Run b2" }));
    await waitFor(() => expect(alert).toHaveBeenCalledWith(expect.stringMatching(/stop it first/)));
  });

  it("groups jobs by category and jumps from a job to just its runs", async () => {
    const MATH = { ...SPEC, name: "erdos", title: "Census", category: "Math" } as JlaunchSpec;
    const ROBOT = { ...SPEC, category: "Robot" } as JlaunchSpec;
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([MATH, ROBOT]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([
      { ...job("b2", "succeeded"), note: "lower lr" } as JlaunchJob,
      { ...job("c3", "failed"), spec_name: "erdos", title: "Census" } as JlaunchJob,
    ]);
    render(<JlaunchScreen onClose={() => {}} />);
    const robot = await screen.findByRole("button", { name: /Robot/ });
    expect(screen.getByRole("button", { name: /Math/ })).toBeTruthy();
    // A category folds shut, and opens again.
    fireEvent.click(robot);
    expect(screen.queryByText("GrowBot look")).toBeNull();
    fireEvent.click(robot);
    fireEvent.click(screen.getByText("GrowBot look"));
    fireEvent.click(screen.getByRole("button", { name: "View runs of GrowBot look" }));
    expect(screen.getByRole("tab", { name: /Runs/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText(/lower lr/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /of Census/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Show all/ }));
    expect(screen.getByRole("button", { name: "Open v2 of Census" })).toBeTruthy();
  });
});
