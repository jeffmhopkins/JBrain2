import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { closeTopModalLayer } from "../backLayers";
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

/** Swipe a row left, as a thumb does, to open its archive / delete menu. */
function swipe(row: HTMLElement) {
  const slide = row.closest(".swipe-slide") as HTMLElement;
  fireEvent.touchStart(slide, { touches: [{ clientX: 300, clientY: 10 }] });
  fireEvent.touchMove(slide, { touches: [{ clientX: 120, clientY: 12 }] });
  fireEvent.touchEnd(slide, { touches: [] });
}

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

  it("shows runs on their own tab and deletes only a finished one, on a second tap", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([job("a1", "running"), job("b2", "failed")]);
    const del = vi.spyOn(api, "jlaunchDelete").mockResolvedValue();
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Runs/ }));
    // A live run's swipe opens no menu: stop it first.
    swipe(screen.getByRole("button", { name: "Open v1 of Run a1" }));
    expect(screen.queryByRole("button", { name: "delete" })).toBeNull();
    swipe(screen.getByRole("button", { name: "Open v2 of Run b2" }));
    fireEvent.click(screen.getByRole("button", { name: "delete" }));
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "tap again" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("b2"));
  });

  it("tells the owner to stop a run the server says is still live", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([job("b2", "failed")]);
    vi.spyOn(api, "jlaunchDelete").mockRejectedValue(new ApiError(409, "still running"));
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Runs/ }));
    swipe(screen.getByRole("button", { name: "Open v2 of Run b2" }));
    fireEvent.click(screen.getByRole("button", { name: "delete" }));
    fireEvent.click(screen.getByRole("button", { name: "tap again" }));
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

  it("saves a trimmed note on a run", async () => {
    const run = { ...job("b2", "succeeded"), repo: "r", phase_times: [] } as unknown as JlaunchJob;
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([run]);
    vi.spyOn(api, "jlaunchGetJob").mockResolvedValue(run);
    vi.spyOn(api, "jlaunchLiveFiles").mockRejectedValue(new ApiError(404, "none"));
    const save = vi
      .spyOn(api, "jlaunchSetNote")
      .mockResolvedValue({ ...run, note: "lower lr" } as JlaunchJob);
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Runs/ }));
    fireEvent.click(screen.getByRole("button", { name: "Open v2 of Run b2" }));
    const input = await screen.findByRole("textbox", { name: "Run note" });
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    fireEvent.change(input, { target: { value: "lower lr" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("b2", "lower lr"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).toBeNull());
  });

  it("archives a job type to the Archived tab, hides its runs, and restores it", async () => {
    const OLD = { ...SPEC, name: "erdos", title: "Census", archived: true } as JlaunchSpec;
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC, OLD]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([
      { ...job("c3", "succeeded"), spec_name: "erdos", title: "Census" } as JlaunchJob,
      job("b2", "succeeded"),
    ]);
    const archive = vi.spyOn(api, "jlaunchArchive").mockResolvedValue();
    render(<JlaunchScreen onClose={() => {}} />);
    await screen.findByText("GrowBot look");
    expect(screen.queryByText("Census")).toBeNull();
    swipe(screen.getByText("GrowBot look"));
    fireEvent.click(screen.getByRole("button", { name: "archive" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("growbot_look", true));
    // The archived job type's finished runs leave the Runs list.
    fireEvent.click(screen.getByRole("tab", { name: /Runs/ }));
    expect(screen.queryByRole("button", { name: /of Census/ })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /Archived/ }));
    fireEvent.click(screen.getByText("Census"));
    expect(screen.queryByRole("button", { name: "Start Census" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View runs of Census" }));
    expect(screen.getByRole("button", { name: "Open v2 of Census" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Archived/ }));
    swipe(screen.getByText("Census"));
    fireEvent.click(screen.getByRole("button", { name: "unarchive" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("erdos", false));
  });

  it("archives one run to the Archived tab and unarchives it from there", async () => {
    let runs = [job("b2", "succeeded"), job("a1", "failed")];
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockImplementation(async () => runs);
    const archive = vi.spyOn(api, "jlaunchArchiveRun").mockImplementation(async (id, on) => {
      runs = runs.map((r) => (r.id === id ? { ...r, archived: on } : r));
      return runs.find((r) => r.id === id) as JlaunchJob;
    });
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Runs/ }));
    swipe(screen.getByRole("button", { name: "Open v2 of Run b2" }));
    fireEvent.click(screen.getByRole("button", { name: "archive" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("b2", true));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Open v2 of Run b2" })).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Open v1 of Run a1" })).toBeTruthy();
    const tab = screen.getByRole("tab", { name: /Archived/ });
    expect(tab.textContent).toContain("1");
    fireEvent.click(tab);
    expect(screen.getByText("Archived runs")).toBeTruthy();
    swipe(screen.getByRole("button", { name: "Open v2 of Run b2" }));
    fireEvent.click(screen.getByRole("button", { name: "unarchive" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("b2", false));
    await waitFor(() => expect(screen.queryByText("Archived runs")).toBeNull());
  });

  it("offers no archive for a job while one of its runs is going", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([job("a1", "running")]);
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /^Jobs/ }));
    swipe(screen.getByText("GrowBot look"));
    expect(screen.queryByRole("button", { name: "archive" })).toBeNull();
  });
  it("re-reads the training repo's jobs and says what changed", async () => {
    const repoSpec = {
      ...SPEC,
      source: "repo",
      commit: "abcdef0123456789abcdef0123456789abcdef01",
      config: "configs/look.toml",
    } as JlaunchSpec;
    const specs = vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([repoSpec]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([]);
    const pull = vi.spyOn(api, "jlaunchRefreshSpecs").mockResolvedValue({
      commit: "1234567890abcdef1234567890abcdef12345678",
      jobs: ["growbot_look", "growbot_new"],
      added: ["growbot_new"],
      removed: [],
    });
    render(<JlaunchScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("GrowBot look"));
    expect(screen.getByText("configs/look.toml · training repo at abcdef0")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check the training repo for new jobs" }));
    expect(await screen.findByText("New: growbot_new (training repo at 1234567).")).toBeTruthy();
    expect(pull).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(specs).toHaveBeenCalledTimes(2));

    pull.mockResolvedValueOnce({ commit: "f".repeat(40), jobs: [], added: [], removed: [] });
    fireEvent.click(screen.getByRole("button", { name: "Check the training repo for new jobs" }));
    expect(await screen.findByText("Up to date with the training repo at fffffff.")).toBeTruthy();

    pull.mockResolvedValueOnce({
      commit: "e".repeat(40),
      jobs: [],
      added: [],
      removed: ["growbot_look"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Check the training repo for new jobs" }));
    expect(
      await screen.findByText("Gone from jobs.toml: growbot_look (training repo at eeeeeee)."),
    ).toBeTruthy();
  });

  it("keeps the list and says why when the repo's jobs can't be read", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([]);
    const pull = vi
      .spyOn(api, "jlaunchRefreshSpecs")
      .mockRejectedValueOnce(new ApiError(422, "jobs.toml job 2: listed twice"))
      .mockRejectedValueOnce(new ApiError(502, "down"));
    render(<JlaunchScreen onClose={() => {}} />);
    const button = await screen.findByRole("button", {
      name: "Check the training repo for new jobs",
    });
    fireEvent.click(button);
    expect(
      await screen.findByText(
        "jobs.toml has a problem, so the list is unchanged: jobs.toml job 2: listed twice",
      ),
    ).toBeTruthy();
    fireEvent.click(button);
    expect(
      await screen.findByText(
        "Couldn't reach the training repo or the launcher. The list is unchanged.",
      ),
    ).toBeTruthy();
    expect(pull).toHaveBeenCalledTimes(2);
    expect(screen.getByText("GrowBot look")).toBeTruthy();
  });

  it("opens on Runs with the running run first, and back from a run returns to the list", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    const live = { ...job("z9", "running"), spec_name: "erdos", title: "Census" } as JlaunchJob;
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([job("b2", "succeeded"), live]);
    vi.spyOn(api, "jlaunchGetJob").mockResolvedValue({
      ...live,
      repo: "r",
      phase_times: [],
    } as unknown as JlaunchJob);
    vi.spyOn(api, "jlaunchLiveFiles").mockRejectedValue(new ApiError(404, "none"));
    const onClose = vi.fn();
    render(<JlaunchScreen onClose={onClose} />);
    const runs = await screen.findByRole("tab", { name: /Runs/ });
    await waitFor(() => expect(runs.getAttribute("aria-selected")).toBe("true"));
    const opens = screen.getAllByRole("button", { name: /^Open / });
    expect(opens[0]?.getAttribute("aria-label")).toBe("Open v2 of Census");
    fireEvent.click(opens[0] as HTMLElement);
    await screen.findByRole("tab", { name: "Overview" });
    expect(closeTopModalLayer()).toBe(true);
    await screen.findByRole("tab", { name: /Runs/ });
    expect(onClose).not.toHaveBeenCalled();
  });
});
