import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ReportListResponse, type VideoListResponse, api } from "../api/client";
import { ResearchScreen } from "./ResearchScreen";

const REPORTS: ReportListResponse = {
  items: [
    {
      id: "r1",
      question: "How was the 1918 flu toll estimated?",
      complexity: "deep",
      created_at: "2026-07-18T00:00:00Z",
      sub_agents: 6,
      rounds: 2,
    },
    {
      id: "r2",
      question: "Best Eurorack modules for ambient",
      complexity: "comparative",
      created_at: "2026-07-15T00:00:00Z",
      sub_agents: 4,
      rounds: 1,
    },
  ],
  total: 2,
};

const VIDEOS: VideoListResponse = {
  items: [
    {
      video_id: "v1",
      provider: "youtube",
      title: "Strix Halo deep research",
      channel_name: "Donato Capitella",
      url: "https://youtu.be/x",
      published_at: "2026-07-17T00:00:00Z",
      duration_s: 1694,
    },
  ],
  total: 1,
};

function stub() {
  vi.spyOn(api, "researchReports").mockResolvedValue(structuredClone(REPORTS));
  vi.spyOn(api, "researchVideos").mockResolvedValue(structuredClone(VIDEOS));
}

afterEach(() => vi.restoreAllMocks());

describe("ResearchScreen", () => {
  it("lists reports and switches to the videos tab", async () => {
    stub();
    render(<ResearchScreen onOpen={() => {}} />);
    expect(await screen.findByText("How was the 1918 flu toll estimated?")).toBeInTheDocument();
    // Report-only fields render (complexity + provenance).
    expect(screen.getByText("deep")).toBeInTheDocument();
    expect(screen.getByText("6 agents")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Videos/ }));
    expect(await screen.findByText("Strix Halo deep research")).toBeInTheDocument();
    expect(screen.getByText("Donato Capitella")).toBeInTheDocument();
  });

  it("filters the active tab as you type", async () => {
    stub();
    render(<ResearchScreen onOpen={() => {}} />);
    await screen.findByText("How was the 1918 flu toll estimated?");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "eurorack" } });
    expect(screen.queryByText("How was the 1918 flu toll estimated?")).not.toBeInTheDocument();
    expect(screen.getByText("Best Eurorack modules for ambient")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zzz" } });
    expect(screen.getByText(/Nothing matches/)).toBeInTheDocument();
  });

  it("opens the detail layer on a row tap", async () => {
    stub();
    const onOpen = vi.fn();
    render(<ResearchScreen onOpen={onOpen} />);
    fireEvent.click(await screen.findByText("How was the 1918 flu toll estimated?"));
    expect(onOpen).toHaveBeenCalledWith("report", "r1");
  });

  it("deletes with a tap-again confirm and undo, deferring the server commit", async () => {
    stub();
    const del = vi.spyOn(api, "deleteResearchReport").mockResolvedValue();
    render(<ResearchScreen onOpen={() => {}} undoMs={10_000} />);
    await screen.findByText("How was the 1918 flu toll estimated?");

    // Open the row's ⋯ menu → the action sheet.
    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    // First tap arms; second confirms.
    fireEvent.click(within(sheet).getByText("Delete"));
    fireEvent.click(within(sheet).getByText(/Tap again/));

    // The row is gone locally and an undo toast shows; the server DELETE has NOT fired.
    await waitFor(() =>
      expect(screen.queryByText("How was the 1918 flu toll estimated?")).not.toBeInTheDocument(),
    );
    expect(del).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByText("How was the 1918 flu toll estimated?")).toBeInTheDocument();
    expect(del).not.toHaveBeenCalled(); // undo cancelled the deferred commit
  });

  it("commits the delete once the undo window closes, then retires the snackbar", async () => {
    stub();
    const del = vi.spyOn(api, "deleteResearchReport").mockResolvedValue();
    render(<ResearchScreen onOpen={() => {}} undoMs={20} />);
    await screen.findByText("How was the 1918 flu toll estimated?");
    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText("Delete"));
    fireEvent.click(within(sheet).getByText(/Tap again/));
    await waitFor(() => expect(del).toHaveBeenCalledWith("r1"));
    // The snackbar's lifetime tracks the undo window — it dismisses when the delete commits.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument(),
    );
  });

  it("an Undo tap after the commit is inert (never resurrects a deleted row)", async () => {
    stub();
    const del = vi.spyOn(api, "deleteResearchReport").mockResolvedValue();
    render(<ResearchScreen onOpen={() => {}} undoMs={20} />);
    await screen.findByText("How was the 1918 flu toll estimated?");
    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText("Delete"));
    fireEvent.click(within(sheet).getByText(/Tap again/));
    // Grab the Undo button synchronously (before the 20ms commit) so the node survives, then
    // let the commit fire and click the now-stale button — it must NOT restore the row.
    const undo = await screen.findByRole("button", { name: "Undo" });
    await waitFor(() => expect(del).toHaveBeenCalled());
    fireEvent.click(undo);
    expect(screen.queryByText("How was the 1918 flu toll estimated?")).not.toBeInTheDocument();
  });

  it("shows an error state when the list fails to load", async () => {
    vi.spyOn(api, "researchReports").mockRejectedValue(new Error("boom"));
    vi.spyOn(api, "researchVideos").mockResolvedValue(structuredClone(VIDEOS));
    render(<ResearchScreen onOpen={() => {}} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
