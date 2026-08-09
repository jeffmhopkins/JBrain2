import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type ReportGroup,
  type ReportListResponse,
  type VideoDetail,
  type VideoListResponse,
  api,
} from "../api/client";
import { ResearchScreen } from "./ResearchScreen";

const REPORTS: ReportListResponse = {
  items: [
    {
      id: "r1",
      question: "How was the 1918 flu toll estimated?",
      // Untitled → the card falls back to the question (what the existing assertions read).
      title: null,
      complexity: "deep",
      created_at: "2026-07-18T00:00:00Z",
      sub_agents: 6,
      rounds: 2,
      group_id: null,
      source_mode: "web",
      expires_at: null,
    },
    {
      id: "r2",
      question: "Best Eurorack modules for ambient",
      title: null,
      complexity: "comparative",
      created_at: "2026-07-15T00:00:00Z",
      sub_agents: 4,
      rounds: 1,
      group_id: null,
      source_mode: "web",
      expires_at: null,
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
      url: "https://youtu.be/v1",
      published_at: "2026-07-17T00:00:00Z",
      duration_s: 1694,
    },
    {
      video_id: "v2",
      provider: "youtube",
      title: "Starship Update",
      channel_name: "NASASpaceflight",
      url: "https://youtu.be/v2",
      published_at: "2026-07-18T00:00:00Z",
      duration_s: 896,
    },
  ],
  total: 2,
};

function stub() {
  vi.spyOn(api, "researchReports").mockResolvedValue(structuredClone(REPORTS));
  vi.spyOn(api, "researchVideos").mockResolvedValue(structuredClone(VIDEOS));
}

const noop = () => {};

// Every render loads report folders; default to none so the Reports tab stays flat unless
// a test opts into folders. The device-local collapse set lives in localStorage — clear it
// between tests so a folded folder never leaks across cases.
beforeEach(() => {
  vi.spyOn(api, "researchReportGroups").mockResolvedValue([]);
});
afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("ResearchScreen", () => {
  it("lists reports and switches to the videos tab", async () => {
    stub();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    expect(await screen.findByText("How was the 1918 flu toll estimated?")).toBeInTheDocument();
    // Report-only fields render (complexity + provenance).
    expect(screen.getByText("deep")).toBeInTheDocument();
    expect(screen.getByText("6 agents")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Videos/ }));
    expect(await screen.findByText("Strix Halo deep research")).toBeInTheDocument();
    // The channel is the section header (grouped), not a per-row line.
    expect(screen.getByText("Donato Capitella")).toBeInTheDocument();
  });

  it("shows the short title when present, falling back to the question", async () => {
    vi.spyOn(api, "researchReports").mockResolvedValue({
      items: [
        {
          id: "rt",
          question: "Compare the current state of solid-state battery technologies for grid…",
          title: "Solid-State Batteries for Grid Storage",
          complexity: "deep",
          created_at: "2026-07-18T00:00:00Z",
          sub_agents: 5,
          rounds: 2,
          group_id: null,
          source_mode: "web",
          expires_at: null,
        },
      ],
      total: 1,
    });
    vi.spyOn(api, "researchVideos").mockResolvedValue({ items: [], total: 0 });
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    // The title heads the card; the long raw question is not shown.
    expect(await screen.findByText("Solid-State Batteries for Grid Storage")).toBeInTheDocument();
    expect(screen.queryByText(/Compare the current state/)).not.toBeInTheDocument();
  });

  it("groups videos by channel into collapsible sections with a thumbnail", async () => {
    stub();
    const { container } = render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    fireEvent.click(screen.getByRole("button", { name: /Videos/ }));
    await screen.findByText("Strix Halo deep research");

    // Two channel headers, alphabetical; each video renders a real thumbnail image.
    expect(screen.getByText("Donato Capitella")).toBeInTheDocument();
    expect(screen.getByText("NASASpaceflight")).toBeInTheDocument();
    const thumbs = container.querySelectorAll("img.rl-thumb-img");
    expect(thumbs.length).toBe(2);
    expect((thumbs[0] as HTMLImageElement).src).toContain("/vi/");

    // Collapsing a section hides its rows but keeps the other channel visible.
    fireEvent.click(screen.getByRole("button", { name: /NASASpaceflight/ }));
    expect(screen.queryByText("Starship Update")).not.toBeInTheDocument();
    expect(screen.getByText("Strix Halo deep research")).toBeInTheDocument();
  });

  it("orders a channel's videos newest-published first, regardless of analysis order", async () => {
    // Three videos in one channel, arriving in a jumbled (analysis) order; the section must
    // render them reverse-chronologically by publish date.
    vi.spyOn(api, "researchReports").mockResolvedValue({ items: [], total: 0 });
    vi.spyOn(api, "researchVideos").mockResolvedValue({
      items: [
        {
          video_id: "a",
          provider: "youtube",
          title: "Jul 3 video",
          channel_name: "NASASpaceflight",
          url: "https://youtu.be/a",
          published_at: "2026-07-03T00:00:00Z",
          duration_s: 100,
        },
        {
          video_id: "b",
          provider: "youtube",
          title: "Jul 18 video",
          channel_name: "NASASpaceflight",
          url: "https://youtu.be/b",
          published_at: "2026-07-18T00:00:00Z",
          duration_s: 100,
        },
        {
          video_id: "c",
          provider: "youtube",
          title: "Jul 10 video",
          channel_name: "NASASpaceflight",
          url: "https://youtu.be/c",
          published_at: "2026-07-10T00:00:00Z",
          duration_s: 100,
        },
      ],
      total: 3,
    });
    const { container } = render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    fireEvent.click(screen.getByRole("button", { name: /Videos/ }));
    await screen.findByText("Jul 18 video");
    const titles = [...container.querySelectorAll(".rl-title-video")].map((n) => n.textContent);
    expect(titles).toEqual(["Jul 18 video", "Jul 10 video", "Jul 3 video"]);
  });

  it("filters the active tab as you type", async () => {
    stub();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
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
    render(<ResearchScreen onOpen={onOpen} onOpenInJerv={noop} />);
    fireEvent.click(await screen.findByText("How was the 1918 flu toll estimated?"));
    expect(onOpen).toHaveBeenCalledWith("report", "r1");
  });

  it("seeds a jerv conversation from the consolidated ⋯ menu", async () => {
    stub();
    const onOpenInJerv = vi.fn();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={onOpenInJerv} />);
    await screen.findByText("How was the 1918 flu toll estimated?");
    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText("Open in jerv conversation"));
    expect(onOpenInJerv).toHaveBeenCalledWith(
      'Let\'s continue from my research report: "How was the 1918 flu toll estimated?".',
    );
  });

  it("shows a report's expiry and keeps it from the ⋯ menu", async () => {
    const keep = vi.spyOn(api, "keepResearchReport").mockResolvedValue();
    vi.spyOn(api, "researchReports").mockResolvedValue({
      items: [
        {
          id: "rex",
          question: "Daily news briefing for Friday",
          title: null,
          complexity: "brief",
          created_at: "2026-08-09T00:00:00Z",
          sub_agents: 5,
          rounds: 1,
          group_id: null,
          source_mode: "web",
          // ~3 days out → the subline reads "expires in 3 days".
          expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(),
        },
      ],
      total: 1,
    });
    vi.spyOn(api, "researchVideos").mockResolvedValue({ items: [], total: 0 });
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    await screen.findByText("Daily news briefing for Friday");
    expect(screen.getByText("expires in 3 days")).toBeInTheDocument();

    // Keep it → the API fires and the countdown clause disappears (optimistic clear).
    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText(/Keep this report/));
    await waitFor(() => expect(keep).toHaveBeenCalledWith("rex"));
    await waitFor(() => expect(screen.queryByText("expires in 3 days")).not.toBeInTheDocument());
  });

  it("copies a video summary from the ⋯ menu, fetching the full item on demand", async () => {
    stub();
    vi.spyOn(api, "researchVideo").mockResolvedValue({
      summary: "A local deep-research walkthrough.",
      windows: [],
      cued_transcript: null,
    } as unknown as VideoDetail);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    fireEvent.click(screen.getByRole("button", { name: /Videos/ }));
    await screen.findByText("Strix Halo deep research");
    fireEvent.click(screen.getAllByRole("button", { name: "Video actions" })[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText("Copy summary"));

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("A local deep-research walkthrough."),
    );
    expect(await screen.findByText("Summary copied.")).toBeInTheDocument();
  });

  it("deletes with a tap-again confirm and undo, deferring the server commit", async () => {
    stub();
    const del = vi.spyOn(api, "deleteResearchReport").mockResolvedValue();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} undoMs={10_000} />);
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
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} undoMs={20} />);
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
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} undoMs={20} />);
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
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  // --- report folders ------------------------------------------------------------------

  const GROUPS: ReportGroup[] = [{ id: "grp-med", name: "Medical", position: 0 }];

  function stubFoldered() {
    vi.spyOn(api, "researchReports").mockResolvedValue({
      items: [
        {
          id: "filed",
          question: "How does rituximab work?",
          title: null,
          complexity: "deep",
          created_at: "2026-07-18T00:00:00Z",
          sub_agents: 5,
          rounds: 2,
          group_id: "grp-med",
          source_mode: "web",
          expires_at: null,
        },
        {
          id: "loose",
          question: "Best Eurorack modules for ambient",
          title: null,
          complexity: "comparative",
          created_at: "2026-07-15T00:00:00Z",
          sub_agents: 4,
          rounds: 1,
          group_id: null,
          source_mode: "web",
          expires_at: null,
        },
      ],
      total: 2,
    });
    vi.spyOn(api, "researchVideos").mockResolvedValue({ items: [], total: 0 });
    vi.spyOn(api, "researchReportGroups").mockResolvedValue(structuredClone(GROUPS));
  }

  it("groups reports into folders + Ungrouped and folds a folder away", async () => {
    stubFoldered();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    await screen.findByText("How does rituximab work?");
    // The named folder + the trailing Ungrouped section both head their reports.
    expect(screen.getByText("Medical")).toBeInTheDocument();
    expect(screen.getByText("Ungrouped")).toBeInTheDocument();
    expect(screen.getByText("Best Eurorack modules for ambient")).toBeInTheDocument();

    // Collapsing Medical hides its report but keeps the Ungrouped one.
    fireEvent.click(screen.getByRole("button", { name: /Collapse Medical/ }));
    expect(screen.queryByText("How does rituximab work?")).not.toBeInTheDocument();
    expect(screen.getByText("Best Eurorack modules for ambient")).toBeInTheDocument();
  });

  it("files a report into a folder from the ⋯ menu", async () => {
    stubFoldered();
    const move = vi.spyOn(api, "moveReport").mockResolvedValue();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    await screen.findByText("Best Eurorack modules for ambient");

    // Open the Ungrouped report's ⋯ → the action sheet → Move to folder.
    const kebabs = screen.getAllByRole("button", { name: "Report actions" });
    fireEvent.click(kebabs[kebabs.length - 1] as HTMLElement); // the loose one is last
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText("Move to folder"));
    // The move sheet lists the folder; pick it.
    const moveSheet = await screen.findByRole("dialog");
    fireEvent.click(within(moveSheet).getByRole("button", { name: /Medical/ }));

    await waitFor(() => expect(move).toHaveBeenCalledWith("loose", "grp-med"));
    expect(await screen.findByText(/Moved to .*Medical/)).toBeInTheDocument();
  });

  it("renames a report from the ⋯ menu", async () => {
    stub();
    const rename = vi.spyOn(api, "renameResearchReport").mockResolvedValue();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    await screen.findByText("How was the 1918 flu toll estimated?");

    // Open the first report's ⋯ → the action sheet → Rename.
    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[0] as HTMLElement);
    fireEvent.click(within(await screen.findByRole("dialog")).getByText("Rename"));

    // The rename sheet pre-fills the current display title (the question, untitled here).
    const sheet = await screen.findByRole("dialog");
    const input = within(sheet).getByLabelText("Report title") as HTMLInputElement;
    expect(input.value).toBe("How was the 1918 flu toll estimated?");
    fireEvent.change(input, { target: { value: "1918 flu death toll" } });
    fireEvent.click(within(sheet).getByText("Save"));

    await waitFor(() => expect(rename).toHaveBeenCalledWith("r1", "1918 flu death toll"));
    // Optimistic update: the card now shows the new title.
    expect(await screen.findByText("1918 flu death toll")).toBeInTheDocument();
  });

  it("creates a folder and files the report via New folder…", async () => {
    // No folders yet → a flat list, but the ⋯ move action still creates the first folder.
    stub();
    vi.spyOn(api, "researchReportGroups").mockResolvedValue([]);
    const create = vi
      .spyOn(api, "createReportGroup")
      .mockResolvedValue({ id: "grp-new", name: "Synths", position: 0 });
    const move = vi.spyOn(api, "moveReport").mockResolvedValue();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    await screen.findByText("Best Eurorack modules for ambient");

    fireEvent.click(screen.getAllByRole("button", { name: "Report actions" })[1] as HTMLElement);
    fireEvent.click(within(await screen.findByRole("dialog")).getByText("Move to folder"));
    const moveSheet = await screen.findByRole("dialog");
    fireEvent.click(within(moveSheet).getByText("New folder…"));
    fireEvent.change(within(moveSheet).getByLabelText("New folder name"), {
      target: { value: "Synths" },
    });
    fireEvent.click(within(moveSheet).getByText("Create"));

    await waitFor(() => expect(create).toHaveBeenCalledWith("Synths"));
    await waitFor(() => expect(move).toHaveBeenCalledWith("r2", "grp-new"));
  });

  it("renames and deletes a folder in organize mode", async () => {
    stubFoldered();
    const rename = vi
      .spyOn(api, "renameReportGroup")
      .mockResolvedValue({ id: "grp-med", name: "Health", position: 0 });
    const del = vi.spyOn(api, "deleteReportGroup").mockResolvedValue();
    render(<ResearchScreen onOpen={noop} onOpenInJerv={noop} />);
    await screen.findByText("How does rituximab work?");

    fireEvent.click(screen.getByRole("button", { name: /Organize folders/ }));
    // Rename Medical → Health.
    fireEvent.click(screen.getByRole("button", { name: "Rename Medical" }));
    const input = screen.getByLabelText("Rename folder");
    fireEvent.change(input, { target: { value: "Health" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(rename).toHaveBeenCalledWith("grp-med", "Health"));
    expect(await screen.findByText("Health")).toBeInTheDocument();

    // Delete Health (tap-again confirm) → its report falls to Ungrouped.
    fireEvent.click(screen.getByRole("button", { name: "Delete Health" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete Health" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("grp-med"));
    expect(screen.queryByText("Health")).not.toBeInTheDocument();
  });
});
