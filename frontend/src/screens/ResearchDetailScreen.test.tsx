import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ReportDetail, type VideoDetail, api } from "../api/client";
import { confidenceColor } from "../components/AudioTranscript";
import { ResearchDetailScreen } from "./ResearchDetailScreen";

const REPORT: ReportDetail = {
  id: "r1",
  question: "How was the 1918 flu toll estimated?",
  report_md: "## Summary\n\nEstimates range 17M to 100M.[^1]",
  complexity: "deep",
  rounds: 2,
  sub_agents: 6,
  analyzed: true,
  revised: false,
  coverage_limited: true,
  truncated: false,
  sources: [{ url: "https://example.org", title: "A" }],
  created_at: "2026-07-18T00:00:00Z",
};

const VIDEO: VideoDetail = {
  source_id: "src1",
  video_id: "v1",
  provider: "youtube",
  title: "Strix Halo deep research",
  channel_name: "Donato Capitella",
  url: "https://youtu.be/x",
  transcript_source: "captions",
  summary: "A local deep-research agent on Strix Halo.",
  duration_s: 1694,
  duration_ms: 1694000,
  published_at: "2026-07-17T00:00:00Z",
  windows: [{ t_ms: 0, text: "intro line" }],
  frames: [
    { t_ms: 0, caption: "title", thumb_data_uri: "data:image/jpeg;base64,AAAA" },
    { t_ms: 4000, caption: "a diagram", thumb_data_uri: "data:image/jpeg;base64,BBBB" },
  ],
  cued_transcript: {
    text: "sure unsure",
    // Both start after 0 so neither is the active (steel-pill) word at the initial clock —
    // each keeps its own confidence tint for the assertion below.
    words: [
      { text: "sure", start_ms: 100, end_ms: 500, confidence: 0.95 },
      { text: "unsure", start_ms: 500, end_ms: 900, confidence: 0.25 },
    ],
  },
};

afterEach(() => vi.restoreAllMocks());

const noop = () => {};

// The per-item actions live on the list's consolidated ⋯ menu (ResearchScreen); this layer
// is pure reading, so its tests cover only the report/video render + error paths.
describe("ResearchDetailScreen", () => {
  it("renders a report's provenance strip and markdown body", async () => {
    vi.spyOn(api, "researchReport").mockResolvedValue(REPORT);
    render(<ResearchDetailScreen kind="report" id="r1" syncStatus="synced" onClose={noop} />);
    expect(await screen.findByText("How was the 1918 flu toll estimated?")).toBeInTheDocument();
    expect(screen.getByText("cross-checked")).toBeInTheDocument();
    expect(screen.getByText("coverage limited")).toBeInTheDocument();
    // The report_md renders through the shared Markdown path.
    expect(screen.getByText(/Estimates range 17M to 100M/)).toBeInTheDocument();
  });

  it("renders [^n] markers as favicon web citations, matching the tool-view", async () => {
    vi.spyOn(api, "researchReport").mockResolvedValue(REPORT);
    const { container } = render(
      <ResearchDetailScreen kind="report" id="r1" syncStatus="synced" onClose={noop} />,
    );
    await screen.findByText(/Estimates range 17M to 100M/);
    // The stored `sources` list is passed as positional cites, so [^1] resolves to a web
    // target — a tappable favicon link that opens the source — rather than a bare chip.
    const cite = container.querySelector<HTMLAnchorElement>(".md-webcite a");
    expect(cite).not.toBeNull();
    expect(cite?.getAttribute("href")).toBe("https://example.org");
    expect(container.querySelector(".md-cite button")).toBeNull();
  });

  it("renders a video via the VideoAnalysis card", async () => {
    vi.spyOn(api, "researchVideo").mockResolvedValue(VIDEO);
    render(<ResearchDetailScreen kind="video" id="v1" syncStatus="synced" onClose={noop} />);
    expect(await screen.findByText("Strix Halo deep research")).toBeInTheDocument();
    expect(screen.getByText(/A local deep-research agent/)).toBeInTheDocument();
  });

  it("renders the filmstrip stills from each frame's inline thumbnail", async () => {
    vi.spyOn(api, "researchVideo").mockResolvedValue(VIDEO);
    const { container } = render(
      <ResearchDetailScreen kind="video" id="v1" syncStatus="synced" onClose={noop} />,
    );
    await screen.findByText("Strix Halo deep research");
    const imgs = container.querySelectorAll<HTMLImageElement>(".tv-vid-frame-img");
    expect(imgs).toHaveLength(2); // thumbnails, not bare markers
    expect(imgs[0]?.getAttribute("src")).toBe("data:image/jpeg;base64,AAAA");
    expect(container.querySelector(".tv-vid-frame-ph")).toBeNull();
  });

  it("tints the transcript on real per-word confidence (not a flat color)", async () => {
    vi.spyOn(api, "researchVideo").mockResolvedValue(VIDEO);
    render(<ResearchDetailScreen kind="video" id="v1" syncStatus="synced" onClose={noop} />);
    await screen.findByText("Strix Halo deep research");
    fireEvent.click(screen.getByRole("tab", { name: "Transcript" }));
    // Each word carries its own confidence color — a high-confidence and a low-confidence
    // word tint differently (the old path hardcoded confidence to 1, coloring both green).
    expect(screen.getByRole("button", { name: "sure" }).style.color).toBe(confidenceColor(0.95));
    expect(screen.getByRole("button", { name: "unsure" }).style.color).toBe(confidenceColor(0.25));
  });

  it("shows an error state when the fetch fails", async () => {
    vi.spyOn(api, "researchReport").mockRejectedValue(new Error("boom"));
    render(<ResearchDetailScreen kind="report" id="r1" syncStatus="synced" onClose={noop} />);
    expect(await screen.findByText(/Couldn't load this/)).toBeInTheDocument();
  });
});
